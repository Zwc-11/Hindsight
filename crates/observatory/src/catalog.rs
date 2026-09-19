use crate::model::*;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use std::{collections::BTreeMap, time::Duration};

impl Store {
    pub fn open(root: impl Into<std::path::PathBuf>) -> Result<Self> {
        let store = Self { root: root.into() };
        std::fs::create_dir_all(&store.root)?;
        for dir in ["raw", "staging", "canonical", "snapshots"] {
            std::fs::create_dir_all(store.root.join(dir))?;
        }
        let c = store.connect()?;
        c.execute_batch(include_str!(
            "../../../migrations/observatory/001_catalog.sql"
        ))?;
        c.execute(
            "INSERT OR IGNORE INTO schema_migrations VALUES(1,?1)",
            [now()],
        )?;
        c.execute_batch(include_str!(
            "../../../migrations/observatory/002_readiness.sql"
        ))?;
        c.execute(
            "INSERT OR IGNORE INTO schema_migrations VALUES(2,?1)",
            [now()],
        )?;
        let applied: bool = c.query_row(
            "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version=3)",
            [],
            |r| r.get(0),
        )?;
        if !applied {
            c.execute_batch(include_str!(
                "../../../migrations/observatory/003_completion.sql"
            ))?;
            c.execute("INSERT INTO schema_migrations VALUES(3,?1)", [now()])?;
        }
        let atlas_applied: bool = c.query_row(
            "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version=4)",
            [],
            |r| r.get(0),
        )?;
        if !atlas_applied {
            c.execute_batch(include_str!(
                "../../../migrations/observatory/004_atlas.sql"
            ))?;
            c.execute("INSERT INTO schema_migrations VALUES(4,?1)", [now()])?;
        }
        let acquisition_applied: bool = c.query_row(
            "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version=5)",
            [],
            |r| r.get(0),
        )?;
        if !acquisition_applied {
            c.execute_batch(include_str!(
                "../../../migrations/observatory/005_atlas_acquisition.sql"
            ))?;
            c.execute("INSERT INTO schema_migrations VALUES(5,?1)", [now()])?;
        }
        let policies: Vec<Policy> =
            serde_json::from_str(include_str!("../../../config/sources/observatory.json"))?;
        for p in policies {
            let payload = serde_json::to_string(&p)?;
            let old: Option<String> = c
                .query_row(
                    "SELECT payload FROM source_policies WHERE id=?1 AND version=?2",
                    params![p.id, p.version],
                    |r| r.get(0),
                )
                .optional()?;
            if old.is_some_and(|old| old != payload) {
                return Err(Error::Conflict(
                    "Policy changed without version increment".into(),
                ));
            }
            c.execute(
                "INSERT OR IGNORE INTO source_policies VALUES(?1,?2,?3)",
                params![p.id, p.version, payload],
            )?;
            c.execute("INSERT INTO providers(id,name,policy_version) VALUES(?1,?2,?3) ON CONFLICT(id) DO UPDATE SET name=excluded.name,policy_version=excluded.policy_version",params![p.id,p.name,p.version])?;
        }
        store.bootstrap_atlas()?;
        Ok(store)
    }
    pub fn connect(&self) -> Result<Connection> {
        let c = Connection::open(self.root.join("catalog.sqlite"))?;
        c.busy_timeout(Duration::from_secs(10))?;
        c.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;",
        )?;
        Ok(c)
    }
    pub fn policies(&self) -> Result<Vec<Policy>> {
        let c = self.connect()?;
        let mut q=c.prepare("SELECT s.payload FROM providers p JOIN source_policies s ON s.id=p.id AND s.version=p.policy_version ORDER BY p.id")?;
        let rows = q
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        rows.into_iter()
            .map(|v| serde_json::from_str(&v).map_err(Error::from))
            .collect()
    }
    pub fn policy(&self, id: &str) -> Result<Policy> {
        self.policies()?
            .into_iter()
            .find(|p| p.id == id)
            .ok_or_else(|| invalid("Unknown source provider"))
    }
    pub fn policy_check(&self, id: &str, purpose: &str) -> Result<Policy> {
        let p = self.policy(id)?;
        let today = chrono::Utc::now().format("%Y-%m-%d").to_string();
        p.authorize(purpose, &today)?;
        Ok(p)
    }
    pub fn access_check(&self, id: &str) -> Result<Policy> {
        let p = self.policy_check(id, "archive")?;
        let missing: Vec<_> = p
            .credential_env
            .iter()
            .filter(|v| std::env::var(v).ok().is_none_or(|s| s.trim().is_empty()))
            .cloned()
            .collect();
        if !missing.is_empty() {
            return Err(Error::Blocked(format!(
                "blocked_auth: set {} in server environment",
                missing.join(", ")
            )));
        }
        if id == "sec"
            && !std::env::var("HINDSIGHT_SEC_USER_AGENT")
                .unwrap_or_default()
                .contains('@')
        {
            return Err(Error::Blocked(
                "blocked_auth: SEC requires your real contact user-agent".into(),
            ));
        }
        Ok(p)
    }
    pub fn snapshot_id(&self) -> Result<i64> {
        Ok(self.connect()?.query_row(
            "SELECT COALESCE(MAX(id),0) FROM dataset_snapshots",
            [],
            |r| r.get(0),
        )?)
    }
    pub fn scope(
        &self,
        snapshot: Option<i64>,
        as_of: Option<i64>,
        mode: &str,
        purpose: &str,
    ) -> Result<Scope> {
        let latest = self.snapshot_id()?;
        let s = Scope {
            snapshot: snapshot.unwrap_or(latest),
            as_of: as_of.unwrap_or_else(now),
            mode: mode.into(),
            purpose: purpose.into(),
        };
        s.validate()?;
        if s.snapshot > latest || s.as_of > now() + 1000 {
            return Err(invalid("Snapshot or as-of is in the future"));
        }
        Ok(s)
    }
    pub fn enqueue(
        &self,
        provider: &str,
        kind: &str,
        resource: &str,
        params_value: &Value,
        cutoff: i64,
        priority: i64,
    ) -> Result<String> {
        let p = self.policy(provider)?;
        let key = json!([provider, kind, resource, params_value, cutoff, p.version]);
        let id = hash(&serde_json::to_vec(&key)?);
        let (state, error) = match self.access_check(provider) {
            Ok(_) => ("queued", None),
            Err(e) => (
                if e.to_string().contains("blocked_auth") {
                    "blocked_auth"
                } else {
                    "blocked_permission"
                },
                Some(e.to_string()),
            ),
        };
        self.connect()?.execute("INSERT OR IGNORE INTO jobs(id,provider,kind,resource,params,state,cutoff,priority,error,updated_at,requested_start,requested_end) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",params![id,provider,kind,resource,serde_json::to_string(params_value)?,state,cutoff,priority,error,now(),params_value["start"].as_str(),params_value["end"].as_str()])?;
        Ok(id)
    }
    pub fn job(&self, id: &str) -> Result<Job> {
        let c = self.connect()?;
        Ok(c.query_row(&(JOB_SELECT.to_owned() + " WHERE id=?1"), [id], job_row)?)
    }
    pub fn jobs(&self, limit: usize, after: &str) -> Result<Vec<Job>> {
        let c = self.connect()?;
        let mut q = c.prepare(&(JOB_SELECT.to_owned() + " WHERE id>?1 ORDER BY id LIMIT ?2"))?;
        let rows = q
            .query_map(params![after, limit.min(500) as i64], job_row)?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }
    pub fn jobs_filtered(
        &self,
        limit: usize,
        after: &str,
        provider: &str,
        state: &str,
    ) -> Result<Vec<Job>> {
        let c = self.connect()?;
        let mut q=c.prepare(&(JOB_SELECT.to_owned()+" WHERE id>?1 AND (?2='' OR provider=?2) AND (?3='' OR state=?3) ORDER BY id LIMIT ?4"))?;
        let rows = q
            .query_map(
                params![after, provider, state, limit.min(500) as i64],
                job_row,
            )?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }
    pub fn claim(&self) -> Result<Option<Job>> {
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let t = now();
        tx.execute("UPDATE jobs SET state='queued',lease_token=NULL,lease_until=NULL,error='Expired worker lease; checkpoint retained' WHERE state='running' AND lease_until<?1",[t])?;
        let id:Option<String>=tx.query_row("SELECT id FROM jobs WHERE state IN ('queued','retry_wait') AND not_before<=?1 ORDER BY priority DESC,updated_at,id LIMIT 1",[t],|r|r.get(0)).optional()?;
        let Some(id) = id else { return Ok(None) };
        let token = uuid::Uuid::new_v4().to_string();
        tx.execute("UPDATE jobs SET state='running',attempts=attempts+1,lease_token=?2,lease_until=?3,updated_at=?4 WHERE id=?1",params![id,token,t+120_000,t])?;
        tx.execute("INSERT INTO job_events(job_id,at,state,detail) VALUES(?1,?2,'running','Lease acquired')",params![id,t])?;
        tx.commit()?;
        Ok(Some(self.job(&id)?))
    }
    pub fn alive(&self, job: &Job) -> Result<()> {
        let j = self.job(&job.id)?;
        if j.state != "running"
            || j.lease_token != job.lease_token
            || j.lease_until.is_none_or(|t| t < now())
        {
            return Err(Error::Cancelled(
                "Worker lease lost or job paused/cancelled".into(),
            ));
        }
        Ok(())
    }
    pub fn action(&self, id: &str, action: &str) -> Result<()> {
        let job = self.job(id)?;
        let state = match action {
            "pause" if ["queued", "retry_wait", "running"].contains(&job.state.as_str()) => {
                "paused"
            }
            "cancel"
                if !["complete_for_declared_scope", "cancelled"].contains(&job.state.as_str()) =>
            {
                "cancelled"
            }
            "resume"
                if [
                    "paused",
                    "cancelled",
                    "failed",
                    "partial",
                    "rate_limited",
                    "blocked_auth",
                    "blocked_permission",
                    "blocked_resources",
                    "quarantined",
                ]
                .contains(&job.state.as_str()) =>
            {
                self.access_check(&job.provider)?;
                "queued"
            }
            _ => return Err(invalid("Action is not valid in this job state")),
        };
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute("UPDATE jobs SET state=?2,lease_token=NULL,lease_until=NULL,error=NULL,attempts=0,not_before=0,updated_at=?3 WHERE id=?1",params![id,state,now()])?;
        tx.execute(
            "INSERT INTO job_events(job_id,at,state,detail) VALUES(?1,?2,?3,?4)",
            params![id, now(), state, action],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn fail(&self, job: &Job, error: &Error) -> Result<()> {
        let text = error.to_string();
        let retry = matches!(
            error,
            Error::Network(_)
                | Error::Http {
                    status: 429 | 500..=599,
                    ..
                }
        );
        let state = match error {
            Error::Blocked(_) if text.contains("blocked_auth") => "blocked_auth",
            Error::Blocked(_) => "blocked_permission",
            Error::Http { status: 401, .. } => "blocked_auth",
            Error::Http { status: 403, .. } => "blocked_permission",
            Error::Resource(_) => "blocked_resources",
            Error::Cancelled(_) => return Ok(()),
            Error::Conflict(_) | Error::Invalid(_) => "quarantined",
            _ if retry && job.attempts < 6 => "retry_wait",
            _ => "failed",
        };
        let backoff = match error {
            Error::Http { retry_after_ms, .. } => *retry_after_ms,
            _ => 0,
        }
        .max((1i64 << job.attempts.min(10)) * 1000)
            + (now() % 997);
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let n=tx.execute("UPDATE jobs SET state=?2,error=?3,lease_token=NULL,lease_until=NULL,not_before=?4,updated_at=?5 WHERE id=?1 AND state='running' AND lease_token=?6",params![job.id,state,text,now()+backoff,now(),job.lease_token])?;
        if n > 0 {
            tx.execute(
                "INSERT INTO job_events(job_id,at,state,detail) VALUES(?1,?2,?3,?4)",
                params![job.id, now(), state, text],
            )?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn reserve_request(&self, provider: &str) -> Result<i64> {
        let p = self.access_check(provider)?;
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let t = now();
        let next: i64 = tx.query_row(
            "SELECT next_allowed_at FROM providers WHERE id=?1",
            [provider],
            |r| r.get(0),
        )?;
        let slot = next.max(t);
        tx.execute(
            "UPDATE providers SET next_allowed_at=?2 WHERE id=?1",
            params![provider, slot + p.interval_ms],
        )?;
        let day = chrono::Utc::now().format("%Y-%m-%d").to_string();
        tx.execute("INSERT INTO request_counts VALUES(?1,?2,1) ON CONFLICT(provider,day) DO UPDATE SET count=count+1",params![provider,day])?;
        tx.commit()?;
        Ok((slot - t).max(0))
    }
    pub fn list_series(
        &self,
        scope: &Scope,
        search: &str,
        provider: &str,
        after: &str,
        limit: usize,
    ) -> Result<Vec<Value>> {
        scope.validate()?;
        if scope.mode == "reconstructed" {
            let c = self.connect()?;
            let mut q=c.prepare("SELECT s.id FROM series s WHERE s.id>?1 AND (?2='' OR s.provider=?2) AND (?3='' OR instr(lower(s.id),lower(?3))>0) AND (EXISTS(SELECT 1 FROM series_versions v WHERE v.series_id=s.id AND v.generation<=?4 AND v.known_at<=?5) OR EXISTS(SELECT 1 FROM observation_versions o WHERE o.series_id=s.id AND o.generation<=?4 AND o.reconstructed_at<=?5 AND o.published_at<=?5)) ORDER BY s.id LIMIT ?6")?;
            let ids = q
                .query_map(
                    params![
                        after,
                        provider,
                        search,
                        scope.snapshot,
                        scope.as_of,
                        limit.min(500) as i64
                    ],
                    |r| r.get::<_, String>(0),
                )?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            return ids
                .into_iter()
                .filter_map(|id| match self.series(&id, scope) {
                    Ok(s) => Some(serde_json::to_value(s).map_err(Error::from)),
                    Err(Error::Blocked(_)) => None,
                    Err(e) => Some(Err(e)),
                })
                .collect();
        }
        let allowed = self
            .policies()?
            .into_iter()
            .filter(|p| {
                p.authorize(
                    &scope.purpose,
                    &chrono::Utc::now().format("%Y-%m-%d").to_string(),
                )
                .is_ok()
            })
            .map(|p| p.id)
            .collect::<Vec<_>>();
        let c = self.connect()?;
        let mut q=c.prepare("SELECT v.payload,(SELECT COUNT(*) FROM observation_versions o WHERE o.series_id=s.id AND o.generation<=?1 AND ((?7='observed' AND o.observed_at<=?2 AND (o.published_at IS NULL OR o.published_at<=?2)) OR (?7='reconstructed' AND o.reconstructed_at<=?2))) FROM series s JOIN series_versions v ON v.series_id=s.id WHERE v.generation=(SELECT MAX(w.generation) FROM series_versions w WHERE w.series_id=s.id AND w.generation<=?1 AND w.known_at<=?2) AND s.id>?3 AND (?4='' OR s.provider=?4) AND (?5='' OR instr(lower(v.name || ' ' || v.code || ' ' || v.entity),lower(?5))>0) ORDER BY s.id LIMIT ?6")?;
        let rows = q
            .query_map(
                params![
                    scope.snapshot,
                    scope.as_of,
                    after,
                    provider,
                    search,
                    limit.min(500) as i64,
                    scope.mode
                ],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
            )?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let mut out = vec![];
        for (s, count) in rows {
            let mut v: Value = serde_json::from_str(&s)?;
            if allowed.contains(&v["provider"].as_str().unwrap_or_default().to_string()) {
                v["captured_version_count"] = count.into();
                out.push(v);
            }
        }
        Ok(out)
    }
    pub fn series(&self, id: &str, scope: &Scope) -> Result<Series> {
        scope.validate()?;
        if scope.snapshot > self.snapshot_id()? {
            return Err(invalid("Snapshot is not committed"));
        }
        let c = self.connect()?;
        let payload:Option<String>=c.query_row("SELECT payload FROM series_versions WHERE series_id=?1 AND generation<=?2 AND known_at<=?3 ORDER BY generation DESC LIMIT 1",params![id,scope.snapshot,scope.as_of],|r|r.get(0)).optional()?;
        let s: Series = if let Some(payload) = payload {
            serde_json::from_str(&payload)?
        } else if scope.mode == "reconstructed" {
            let row:Option<(String,String)>=c.query_row("SELECT s.provider,o.unit FROM series s JOIN observation_versions o ON o.series_id=s.id WHERE s.id=?1 AND o.generation<=?2 AND o.reconstructed_at IS NOT NULL AND o.reconstructed_at<=?3 AND o.published_at<=?3 ORDER BY o.reconstructed_at DESC LIMIT 1",params![id,scope.snapshot,scope.as_of],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
            let (provider, unit) =
                row.ok_or_else(|| invalid("Series is unavailable in this historical snapshot"))?;
            Series{id:id.into(),provider,code:id.into(),name:id.into(),entity:id.into(),unit,frequency:"historical metadata withheld".into(),description:"An eligible historical observation exists; present-day descriptive metadata is intentionally withheld.".into(),source_url:String::new(),metadata:json!({"historical_metadata_withheld":true})}
        } else {
            return Err(invalid("Series is unavailable in this historical snapshot"));
        };
        self.policy_check(&s.provider, &scope.purpose)?;
        Ok(s)
    }
    pub fn points(
        &self,
        id: &str,
        scope: &Scope,
        start: &str,
        end: &str,
        limit: usize,
    ) -> Result<Vec<Point>> {
        self.series(id, scope)?;
        if start > end || limit > 100_000 {
            return Err(invalid(
                "Invalid range or query budget exceeds 100000 records",
            ));
        }
        let c = self.connect()?;
        // Financial readiness and storage snapshot are separate predicates. Filter before choosing a revision.
        let mut q=c.prepare("WITH visible AS (SELECT *, ROW_NUMBER() OVER(PARTITION BY natural_key ORDER BY COALESCE(source_order,observed_at) DESC,observed_at DESC,id DESC) AS rn FROM observation_versions WHERE series_id=?1 AND generation<=?2 AND period_end>=?3 AND period_end<=?4 AND ((?5='observed' AND observed_at<=?6 AND (published_at IS NULL OR published_at<=?6)) OR (?5='reconstructed' AND reconstructed_at IS NOT NULL AND reconstructed_at<=?6))) SELECT id,natural_key,series_id,period_start,period_end,value,unit,published_at,observed_at,reconstructed_at,source_order,capture_id,precision,quality,generation,extras FROM visible WHERE rn=1 ORDER BY period_end,natural_key LIMIT ?7")?;
        let rows = q
            .query_map(
                params![
                    id,
                    scope.snapshot,
                    start,
                    end,
                    scope.mode,
                    scope.as_of,
                    limit as i64
                ],
                point_row,
            )?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }
    pub fn evidence(&self, id: &str, scope: &Scope) -> Result<Value> {
        let c = self.connect()?;
        let row:Option<(String,String,String)>=c.query_row("SELECT o.series_id,o.semantic_json,c.payload FROM observation_versions o JOIN captures c ON c.id=o.capture_id WHERE o.id=?1 AND o.generation<=?2 AND ((?3='observed' AND o.observed_at<=?4 AND (o.published_at IS NULL OR o.published_at<=?4)) OR (?3='reconstructed' AND o.reconstructed_at IS NOT NULL AND o.reconstructed_at<=?4))",params![id,scope.snapshot,scope.mode,scope.as_of],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
        let (sid, observation, capture) =
            row.ok_or_else(|| invalid("Evidence is unavailable in selected scope"))?;
        let series = self.series(&sid, scope)?;
        let mut receipt: Value = serde_json::from_str(&capture)?;
        // A reconstructed historical response must not smuggle future collector metadata in tooltips.
        if scope.mode == "reconstructed" {
            receipt = json!({"url":receipt["url"],"provenance_note":"Historical reconstruction; acquisition hash and receipt require current evidence view"});
        }
        Ok(
            json!({"id":id,"scope":scope,"series":series,"observation":serde_json::from_str::<Value>(&observation)?,"capture":receipt,"policy":self.policy(&series.provider)?}),
        )
    }
    pub fn status(&self) -> Result<Value> {
        let c = self.connect()?;
        let mut counts = BTreeMap::new();
        let mut q = c.prepare("SELECT state,COUNT(*) FROM jobs GROUP BY state")?;
        for row in q.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))? {
            let (k, v) = row?;
            counts.insert(k, v);
        }
        let mut providers = vec![];
        for policy in self.policies()? {
            let (jobs,complete,rows,bytes):(i64,i64,i64,i64)=c.query_row("SELECT COUNT(*),COALESCE(SUM(state='complete_for_declared_scope'),0),COALESCE(SUM(rows),0),COALESCE(SUM(bytes),0) FROM jobs WHERE provider=?1",[&policy.id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
            let series: i64 = c.query_row(
                "SELECT COUNT(*) FROM series WHERE provider=?1",
                [&policy.id],
                |r| r.get(0),
            )?;
            providers.push(json!({"policy":policy,"access":self.access_check(&policy.id).err().map(|e|e.to_string()),"jobs":jobs,"completed_jobs":complete,"committed_rows":rows,"raw_bytes":bytes,"catalog_series":series,"coverage":"collection-scoped; global completeness unresolved"}));
        }
        let (rows,captures,raw_bytes):(i64,i64,i64)=c.query_row("SELECT (SELECT COUNT(*) FROM observation_versions),(SELECT COUNT(*) FROM captures),(SELECT COALESCE(SUM(bytes),0) FROM raw_objects)",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        Ok(
            json!({"clock_utc":chrono::Utc::now().to_rfc3339(),"snapshot":self.snapshot_id()?,"jobs":counts,"providers":providers,"observation_versions":rows,"captures":captures,"raw_bytes":raw_bytes,"free_bytes":fs2::available_space(&self.root)?,"disk_reserve_bytes":(fs2::total_space(&self.root)?*15/100).max(20*1024*1024*1024),"complete_all":false,"default_data_kind":"real","live_trading":false}),
        )
    }
}
const JOB_SELECT:&str="SELECT id,provider,kind,resource,params,state,cutoff,cursor,attempts,lease_token,lease_until,priority,error,rows,bytes,updated_at FROM jobs";
fn json_col(r: &rusqlite::Row<'_>, i: usize) -> rusqlite::Result<Value> {
    let s: String = r.get(i)?;
    serde_json::from_str(&s).map_err(|e| {
        rusqlite::Error::FromSqlConversionFailure(i, rusqlite::types::Type::Text, Box::new(e))
    })
}
fn job_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Job> {
    Ok(Job {
        id: r.get(0)?,
        provider: r.get(1)?,
        kind: r.get(2)?,
        resource: r.get(3)?,
        params: json_col(r, 4)?,
        state: r.get(5)?,
        cutoff: r.get(6)?,
        cursor: json_col(r, 7)?,
        attempts: r.get(8)?,
        lease_token: r.get(9)?,
        lease_until: r.get(10)?,
        priority: r.get(11)?,
        error: r.get(12)?,
        rows: r.get(13)?,
        bytes: r.get(14)?,
        updated_at: r.get(15)?,
    })
}
fn point_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Point> {
    Ok(Point {
        id: r.get(0)?,
        key: r.get(1)?,
        series_id: r.get(2)?,
        period_start: r.get(3)?,
        period_end: r.get(4)?,
        value: r.get(5)?,
        unit: r.get(6)?,
        published_at: r.get(7)?,
        observed_at: r.get(8)?,
        reconstructed_at: r.get(9)?,
        source_order: r.get(10)?,
        capture_id: r.get(11)?,
        precision: r.get(12)?,
        quality: r.get(13)?,
        generation: r.get(14)?,
        extras: json_col(r, 15)?,
    })
}
