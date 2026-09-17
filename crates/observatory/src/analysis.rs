//! Durable, bounded analytical jobs over a pinned, permissioned historical view.
use crate::{math, model::*};
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AnalysisRequest {
    pub scope: Scope,
    pub series_ids: Vec<String>,
    pub start: String,
    pub end: String,
    pub transform: String,
    pub kind: String,
    #[serde(default = "default_lags")]
    pub max_lag: usize,
    #[serde(default = "default_window")]
    pub window: usize,
    #[serde(default)]
    pub controls: Vec<String>,
}
fn default_lags() -> usize {
    4
}
fn default_window() -> usize {
    12
}
impl AnalysisRequest {
    pub fn validate(&self) -> Result<()> {
        self.scope.validate()?;
        if self.series_ids.len() < 2
            || self.series_ids.len() > 8
            || self.series_ids.iter().collect::<BTreeSet<_>>().len() != self.series_ids.len()
            || self.controls.len() > 4
            || self.max_lag > 12
            || self.window < 3
            || self.window > 250
            || !["change", "log_return", "level"].contains(&self.transform.as_str())
            || !["dependence", "forecast", "nowcast"].contains(&self.kind.as_str())
            || self.start > self.end
            || self.start.len() > 40
            || self.end.len() > 40
        {
            return Err(invalid(
                "Invalid analytical scope, method, range, or bounded workload",
            ));
        }
        if self.scope.purpose != "research" {
            return Err(invalid("Analysis requires research-purpose scope"));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize)]
pub struct AlignedRow {
    pub period_start: String,
    pub period_end: String,
    pub values: Vec<f64>,
    pub observation_ids: Vec<String>,
}

/// No interpolation: intersect identical reporting spans. Transform consecutive native observations,
/// then intersect; this does not count forward-filled monthly values as independent minute samples.
pub fn align(series: &[Vec<Point>], transform: &str) -> Result<Vec<AlignedRow>> {
    if series.len() < 2 || !["change", "log_return", "level"].contains(&transform) {
        return Err(invalid("Invalid alignment input"));
    }
    let mut maps = Vec::new();
    for points in series {
        let mut sorted = points.clone();
        sorted.sort_by(|a, b| {
            (&a.period_end, &a.period_start, &a.id).cmp(&(&b.period_end, &b.period_start, &b.id))
        });
        let mut map: BTreeMap<(String, String), (f64, String)> = BTreeMap::new();
        let mut previous: Option<&Point> = None;
        for p in &sorted {
            if let Some(v) = p.value {
                let value = match transform {
                    "level" => Some(v),
                    _ => previous
                        .and_then(|old| old.value)
                        .and_then(|old| match transform {
                            "change" => Some(v - old),
                            "log_return" if old > 0.0 && v > 0.0 => Some(v.ln() - old.ln()),
                            _ => None,
                        }),
                };
                if let Some(v) = value.filter(|v| v.is_finite()) {
                    let key = (p.period_start.clone(), p.period_end.clone());
                    if map.insert(key, (v, p.id.clone())).is_some() {
                        return Err(invalid("Ambiguous reporting contexts; choose a normalized single-context series"));
                    }
                }
            }
            // A missing observation breaks changes rather than bridging a missing release.
            previous = Some(p);
        }
        maps.push(map);
    }
    let mut out = Vec::new();
    for (key, (first, id)) in &maps[0] {
        let mut values = vec![*first];
        let mut ids = vec![id.clone()];
        for m in &maps[1..] {
            if let Some((v, id)) = m.get(key) {
                values.push(*v);
                ids.push(id.clone());
            }
        }
        if values.len() == maps.len() {
            out.push(AlignedRow {
                period_start: key.0.clone(),
                period_end: key.1.clone(),
                values,
                observation_ids: ids,
            });
        }
    }
    Ok(out)
}

pub fn compute(
    store: &Store,
    request: &AnalysisRequest,
    cancelled: impl Fn() -> bool,
) -> Result<Value> {
    request.validate()?;
    let mut ids = request.series_ids.clone();
    for id in &request.controls {
        if !ids.contains(id) {
            ids.push(id.clone());
        }
    }
    let mut data = Vec::new();
    let mut meta = Vec::new();
    let mut counts = Vec::new();
    for id in &ids {
        if cancelled() {
            return Err(Error::Cancelled("Analysis cancelled".into()));
        }
        let series = store.series(id, &request.scope)?;
        if request.kind != "dependence" || !request.controls.is_empty() {
            store.policy_check(&series.provider, "training")?;
        }
        meta.push(series);
        let points = store.points(id, &request.scope, &request.start, &request.end, 5001)?;
        if points.len() > 5000 {
            return Err(Error::Resource(
                "Choose a narrower analysis interval (at most 5000 native observations per series)"
                    .into(),
            ));
        }
        let dates: BTreeSet<_> = points
            .iter()
            .filter_map(|p| p.published_at.map(|v| v / 86_400_000))
            .collect();
        counts.push(json!({"series":id,"native_versions":points.len(),"nonmissing":points.iter().filter(|p|p.value.is_some()).count(),"distinct_verified_publication_dates":dates.len(),"publication_unknown":points.iter().filter(|p|p.published_at.is_none()).count()}));
        data.push(points);
    }
    if meta.windows(2).any(|p| p[0].frequency != p[1].frequency) && request.kind != "nowcast" {
        return Ok(
            json!({"state":"insufficient_data","reason":"Native cadences differ. Use an explicitly specified aggregation/mixed-frequency model, not silent forward filling.","series":meta,"counts":counts}),
        );
    }
    if request.kind == "nowcast" {
        return crate::forecast::nowcast_from_sources(&data, &meta, request);
    }
    if request.kind == "forecast" {
        return crate::forecast::forecast_from_sources(store, request, &data, &meta);
    }
    let aligned = align(&data, &request.transform)?;
    if aligned.len() < 6 {
        return Ok(
            json!({"state":"insufficient_data","reason":"At least six actual matched reporting spans are required for descriptive analysis.","matched_samples":aligned.len(),"counts":counts,"series":meta}),
        );
    }
    let mut pairs = Vec::new();
    for i in 0..request.series_ids.len() {
        for j in i + 1..request.series_ids.len() {
            if cancelled() {
                return Err(Error::Cancelled("Analysis cancelled".into()));
            }
            let x = aligned.iter().map(|r| r.values[i]).collect::<Vec<_>>();
            let y = aligned.iter().map(|r| r.values[j]).collect::<Vec<_>>();
            let mut lags = Vec::new();
            for lag in -(request.max_lag as i32)..=request.max_lag as i32 {
                let d = lag.unsigned_abs() as usize;
                if x.len() <= d + 5 {
                    continue;
                }
                let (a, b) = if lag >= 0 {
                    (&x[..x.len() - d], &y[d..])
                } else {
                    (&x[d..], &y[..y.len() - d])
                };
                lags.push(json!({"lag":lag,"samples":a.len(),"pearson":math::pearson(a,b).ok()}));
            }
            let dcor = if x.len() <= 2000 {
                math::dependence::distance_correlation(&x, &y).ok()
            } else {
                None
            };
            let window = request.window.min(x.len());
            let rolling = math::dependence::rolling(&x, &y, window)?;
            let controls = if ids.len() > request.series_ids.len() {
                let control_rows: Vec<Vec<f64>> = aligned
                    .iter()
                    .map(|r| r.values[request.series_ids.len()..].to_vec())
                    .collect();
                Some(crate::forecast::crossfit_residual_correlation(
                    &x,
                    &y,
                    &control_rows,
                )?)
            } else {
                None
            };
            pairs.push(json!({"x":ids[i],"y":ids[j],"samples":x.len(),"pearson":math::pearson(&x,&y).ok(),"spearman":math::spearman(&x,&y).ok(),"distance_correlation":dcor,"distance_note":if x.len()>2000{"disabled above quadratic computation budget"}else{"nonnegative nonlinear dependence; no sign or direction"},"ew_correlation":math::dependence::ew_correlation(&x,&y,0.1).ok(),"rolling":rolling,"lag_response":lags,"residualized":controls,"p_value":null,"q_value":null,"inference":"descriptive only; dependence-aware null assumptions not established for these sources"}));
        }
    }
    let rows: Vec<Vec<f64>> = aligned.iter().map(|r| r.values.clone()).collect();
    let covariance = math::covariance::ledoit_wolf(&rows)
        .map(|v| serde_json::to_value(v).unwrap_or(Value::Null))
        .unwrap_or_else(|e| json!({"state":"undefined","reason":e.to_string()}));
    Ok(
        json!({"state":"completed","kind":"descriptive_dependence","scope":request.scope,"series":meta,"transform":request.transform,"counts":counts,"matched_samples":aligned.len(),"matched_rows":aligned,"pairs":pairs,"covariance":covariance,"testing_family":{"pairs":request.series_ids.len()*(request.series_ids.len()-1)/2,"lag_range":[-(request.max_lag as i32),request.max_lag as i32],"transform":request.transform,"selection":"all requested pairs; none discarded for an attractive result","q_values":"not computed because marginal p-values are not justified"},"limitations":["Association is not a causal claim or an executable strategy.","Measurement-period lags are not proof of information arriving before price changes.","Latest-vintage observations support present descriptive research, not original-vintage backtest claims.","Level correlations can be spurious for trending/nonstationary series."]}),
    )
}

impl Store {
    pub fn submit_analysis(&self, request: &AnalysisRequest) -> Result<String> {
        request.validate()?;
        for id in request.series_ids.iter().chain(&request.controls) {
            let s = self.series(id, &request.scope)?;
            self.policy_check(&s.provider, "research")?;
            if request.kind != "dependence" || !request.controls.is_empty() {
                self.policy_check(&s.provider, "training")?;
            }
        }
        let c = self.connect()?;
        let pending: i64 = c.query_row(
            "SELECT COUNT(*) FROM experiments WHERE state IN ('queued','running')",
            [],
            |r| r.get(0),
        )?;
        if pending >= 16 {
            return Err(Error::Resource("Analysis queue is full".into()));
        }
        let id = uuid::Uuid::new_v4().to_string();
        c.execute("INSERT INTO experiments(id,state,created_at,scope,request) VALUES(?1,'queued',?2,?3,?4)",params![id,now(),serde_json::to_string(&request.scope)?,serde_json::to_string(request)?])?;
        Ok(id)
    }
    pub fn analysis_result(&self, id: &str, scope: &Scope) -> Result<Value> {
        scope.validate()?;
        let c = self.connect()?;
        type StoredRun = (String, i64, String, String, Option<String>, Option<String>);
        let row: Option<StoredRun> = c
            .query_row(
                "SELECT state,created_at,scope,request,result,error FROM experiments WHERE id=?1",
                [id],
                |r| {
                    Ok((
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                    ))
                },
            )
            .optional()?;
        let (state, created, stored_scope, request, result, error) =
            row.ok_or_else(|| invalid("Unknown research job"))?;
        let own: Scope = serde_json::from_str(&stored_scope)?;
        if own.snapshot != scope.snapshot
            || own.as_of != scope.as_of
            || own.mode != scope.mode
            || own.purpose != scope.purpose
        {
            return Err(Error::Blocked(
                "Result belongs to a different historical/purpose scope".into(),
            ));
        }
        let req: AnalysisRequest = serde_json::from_str(&request)?;
        for sid in req.series_ids.iter().chain(&req.controls) {
            self.series(sid, scope)?;
        }
        Ok(
            json!({"id":id,"state":state,"created_at":created,"scope":own,"request":req,"result":result.map(|v|serde_json::from_str::<Value>(&v)).transpose()?,"error":error}),
        )
    }
    pub fn analysis_step(&self) -> Result<bool> {
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row:Option<(String,String)>=tx.query_row("SELECT id,request FROM experiments WHERE state='queued' AND cancel_requested=0 ORDER BY created_at LIMIT 1",[],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        let Some((id, payload)) = row else {
            return Ok(false);
        };
        tx.execute("UPDATE experiments SET state='running' WHERE id=?1", [&id])?;
        tx.commit()?;
        let req: AnalysisRequest = serde_json::from_str(&payload)?;
        let cancelled = || {
            self.connect()
                .and_then(|c| {
                    c.query_row(
                        "SELECT cancel_requested!=0 FROM experiments WHERE id=?1",
                        [&id],
                        |r| r.get::<_, bool>(0),
                    )
                    .map_err(Error::from)
                })
                .unwrap_or(true)
        };
        let result = compute(self, &req, cancelled);
        match result {
            Ok(mut v) => {
                let policies: Vec<_> = req
                    .series_ids
                    .iter()
                    .map(|id| {
                        self.series(id, &req.scope)
                            .and_then(|s| self.policy(&s.provider))
                            .map(|p| json!({"id":p.id,"version":p.version}))
                    })
                    .collect::<Result<Vec<_>>>()?;
                let kernel = concat!(
                    include_str!("math.rs"),
                    include_str!("math/covariance.rs"),
                    include_str!("math/dependence.rs"),
                    include_str!("math/state_space.rs"),
                    include_str!("forecast.rs"),
                    include_str!("../../economic/src/numerics.rs"),
                    include_str!("../../../cpp/numerics/ridge.cpp")
                );
                v["provenance"] = json!({"request_sha256":hash(&serde_json::to_vec(&req)?),"kernel_sha256":hash(kernel.as_bytes()),"policies":policies,"package_version":env!("CARGO_PKG_VERSION"),"data_snapshot":req.scope.snapshot,"evidence_mode":req.scope.mode,"architecture":std::env::consts::ARCH,"result_class":"research result or readiness report; not a causal/profitability claim"});
                let status = if v["state"] == "insufficient_data" {
                    "insufficient_data"
                } else {
                    "completed"
                };
                c.execute("UPDATE experiments SET state=?2,result=?3,error=NULL WHERE id=?1 AND cancel_requested=0",params![id,status,serde_json::to_string(&v)?])?;
            }
            Err(Error::Cancelled(_)) => {
                c.execute(
                    "UPDATE experiments SET state='cancelled' WHERE id=?1",
                    [&id],
                )?;
            }
            Err(e) => {
                c.execute("UPDATE experiments SET state='failed',error=?2 WHERE id=?1 AND cancel_requested=0",params![id,e.to_string()])?;
            }
        }
        c.execute(
            "UPDATE experiments SET state='cancelled' WHERE id=?1 AND cancel_requested=1",
            [&id],
        )?;
        Ok(true)
    }
}
