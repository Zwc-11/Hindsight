use crate::{analysis::AnalysisRequest, atlas::MissionRequest, model::*, sources};
use axum::{
    extract::{Path, Query, Request, State},
    http::{Method, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};
use tower_http::services::ServeDir;

#[derive(Clone)]
pub struct App {
    pub store: Store,
    pub token: Arc<String>,
    pub port: u16,
    pub queries: Arc<tokio::sync::Semaphore>,
}
pub struct ApiError(pub Error);
impl From<Error> for ApiError {
    fn from(e: Error) -> Self {
        Self(e)
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = match self.0 {
            Error::Blocked(_) => StatusCode::FORBIDDEN,
            Error::Conflict(_) => StatusCode::CONFLICT,
            Error::Invalid(_) => StatusCode::BAD_REQUEST,
            Error::Resource(_) => StatusCode::SERVICE_UNAVAILABLE,
            _ => StatusCode::INTERNAL_SERVER_ERROR,
        };
        if status == StatusCode::INTERNAL_SERVER_ERROR {
            eprintln!("Local API error: {}", self.0);
        }
        let text = if status == StatusCode::INTERNAL_SERVER_ERROR {
            "Internal query failed; consult local server log".into()
        } else {
            self.0.to_string()
        };
        (status, Json(json!({"error":text,"status":status.as_u16()}))).into_response()
    }
}
type Api<T> = std::result::Result<Json<T>, ApiError>;
pub async fn blocking<T: Send + 'static>(
    app: &App,
    f: impl FnOnce(Store) -> Result<T> + Send + 'static,
) -> Result<T> {
    let permit = app
        .queries
        .clone()
        .try_acquire_owned()
        .map_err(|_| Error::Resource("Interactive query budget busy; retry shortly".into()))?;
    let store = app.store.clone();
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        f(store)
    })
    .await
    .map_err(|_| invalid("Query task failed"))?
}
#[derive(Clone, Debug, Deserialize, Default)]
pub struct Params {
    pub snapshot: Option<i64>,
    pub as_of: Option<String>,
    pub mode: Option<String>,
    pub purpose: Option<String>,
    pub search: Option<String>,
    pub provider: Option<String>,
    pub after: Option<String>,
    pub limit: Option<usize>,
    pub start: Option<String>,
    pub end: Option<String>,
    pub series: Option<String>,
}
impl Params {
    pub fn scope(&self, store: &Store) -> Result<Scope> {
        store.scope(
            self.snapshot,
            self.as_of.as_deref().map(time).transpose()?,
            self.mode.as_deref().unwrap_or("observed"),
            self.purpose.as_deref().unwrap_or("display"),
        )
    }
}
pub fn router(app: App, ui: PathBuf) -> Router {
    Router::new()
        .route("/api/bootstrap", get(bootstrap))
        .route("/api/health", get(health))
        .route("/api/status", get(status))
        .route("/api/catalog", get(catalog))
        .route("/api/overview", get(overview))
        .route("/api/network", get(network))
        .route("/api/snapshots", get(snapshots))
        .route("/api/events", get(events))
        .route("/api/revisions/{id}", get(revisions))
        .route("/api/analyses", get(analyses).post(submit_analysis))
        .route("/api/analyses/{id}", get(analysis_result))
        .route("/api/analyses/{id}/cancel", post(cancel_analysis))
        .route("/api/replay/reveal", post(reveal))
        .route("/api/series/{id}", get(series))
        .route("/api/evidence/{id}", get(evidence))
        .route("/api/jobs", get(jobs))
        .route("/api/jobs/{id}/action", post(action))
        .route("/api/plan", post(plan))
        .route("/api/workspaces", get(workspaces).post(save_workspace))
        .route("/api/replay/notes", get(notes).post(save_note))
        .route("/api/atlas/overview", get(atlas_overview))
        .route("/api/atlas/datasets", get(atlas_datasets))
        .route("/api/atlas/datasets/{id}", get(atlas_dataset_detail))
        .route(
            "/api/atlas/datasets/{id}/observations",
            get(atlas_observations),
        )
        .route(
            "/api/atlas/distributions/{id}/sample",
            post(sample_atlas_distribution),
        )
        .route("/api/atlas/graph", get(atlas_graph))
        .route(
            "/api/atlas/missions",
            get(atlas_missions).post(create_atlas_mission),
        )
        .route("/api/atlas/missions/{id}/run", post(run_atlas_mission))
        .fallback_service(ServeDir::new(ui).append_index_html_on_directories(true))
        .layer(axum::extract::DefaultBodyLimit::max(256 * 1024))
        .layer(middleware::from_fn_with_state(app.clone(), protect))
        .with_state(app)
}
async fn protect(State(app): State<App>, request: Request, next: Next) -> Response {
    let host = request
        .headers()
        .get("host")
        .and_then(|h| h.to_str().ok())
        .unwrap_or_default();
    let expected = [
        format!("127.0.0.1:{}", app.port),
        format!("localhost:{}", app.port),
    ];
    if !expected.iter().any(|h| h == host) {
        return (StatusCode::FORBIDDEN, "Untrusted Host").into_response();
    }
    if request
        .headers()
        .get("sec-fetch-site")
        .and_then(|h| h.to_str().ok())
        == Some("cross-site")
    {
        return (StatusCode::FORBIDDEN, "Cross-site access denied").into_response();
    }
    if ![Method::GET, Method::HEAD].contains(request.method()) {
        let token = request
            .headers()
            .get("x-hindsight-token")
            .and_then(|v| v.to_str().ok());
        if token != Some(app.token.as_str()) {
            return (StatusCode::FORBIDDEN, "Missing local session token").into_response();
        }
        if let Some(origin) = request
            .headers()
            .get("origin")
            .and_then(|v| v.to_str().ok())
        {
            if !expected
                .iter()
                .any(|host| origin == format!("http://{host}"))
            {
                return (StatusCode::FORBIDDEN, "Invalid origin").into_response();
            }
        }
    }
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert("x-content-type-options", "nosniff".parse().unwrap());
    headers.insert("referrer-policy", "no-referrer".parse().unwrap());
    headers.insert("cache-control", "no-store".parse().unwrap());
    headers.insert("content-security-policy","default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'".parse().unwrap());
    response
}
async fn bootstrap(State(app): State<App>) -> Api<Value> {
    let token = app.token.clone();
    Ok(Json(blocking(&app,move|s|Ok(json!({"token":token.as_str(),"snapshot":s.snapshot_id()?,"now":chrono::Utc::now().to_rfc3339(),"name":"Hindsight Observatory","data_kind":"real","api_version":1}))).await?))
}
async fn health() -> Json<Value> {
    Json(
        json!({"state":"running","clock_utc":chrono::Utc::now().to_rfc3339(),"live_trading":false}),
    )
}
async fn status(State(app): State<App>) -> Api<Value> {
    Ok(Json(blocking(&app, |s| s.status()).await?))
}
async fn catalog(State(app): State<App>, Query(q): Query<Params>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            let scope = q.scope(&s)?;
            let limit = q.limit.unwrap_or(50).clamp(1, 200);
            let rows = s.list_series(
                &scope,
                q.search.as_deref().unwrap_or(""),
                q.provider.as_deref().unwrap_or(""),
                q.after.as_deref().unwrap_or(""),
                limit,
            )?;
            let next = if rows.len() == limit {
                rows.last().and_then(|v| v["id"].as_str()).map(String::from)
            } else {
                None
            };
            Ok(json!({"scope":scope,"rows":rows,"next":next,"limit":limit}))
        })
        .await?,
    ))
}
async fn series(
    State(app): State<App>,
    Path(id): Path<String>,
    Query(q): Query<Params>,
) -> Api<Value> {
    Ok(Json(blocking(&app,move|s|{let scope=q.scope(&s)?;let meta=s.series(&id,&scope)?;let points=s.points(&id,&scope,q.start.as_deref().unwrap_or("1800"),q.end.as_deref().unwrap_or("2300"),q.limit.unwrap_or(2000).min(5000))?;
        Ok(json!({"scope":scope,"series":meta,"points":points.iter().map(|p|crate::query::public_point(p,&scope)).collect::<Result<Vec<_>>>()?,"truncated":points.len()==q.limit.unwrap_or(2000).min(5000),"downsampled":false,"warning":"Latest-vintage observations are not an original historical release archive."}))}).await?))
}
async fn evidence(
    State(app): State<App>,
    Path(id): Path<String>,
    Query(q): Query<Params>,
) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            let scope = q.scope(&s)?;
            s.evidence(&id, &scope)
        })
        .await?,
    ))
}
async fn jobs(State(app): State<App>, Query(q): Query<Params>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            Ok(json!({"jobs":s.jobs_filtered(q.limit.unwrap_or(100),q.after.as_deref().unwrap_or(""),q.provider.as_deref().unwrap_or(""),q.search.as_deref().unwrap_or(""))?}))
        })
        .await?,
    ))
}
#[derive(Deserialize)]
struct Action {
    action: String,
}
async fn action(
    State(app): State<App>,
    Path(id): Path<String>,
    Json(a): Json<Action>,
) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            s.action(&id, &a.action)?;
            Ok(json!({"job":s.job(&id)?}))
        })
        .await?,
    ))
}
async fn plan(State(app): State<App>) -> Api<Value> {
    Ok(Json(blocking(&app, |s| sources::plan(&s)).await?))
}
async fn workspaces(State(app): State<App>) -> Api<Value> {
    Ok(Json(blocking(&app,|s|{
    let c=s.connect()?;let mut q=c.prepare("SELECT id,name,payload,updated_at FROM saved_workspaces ORDER BY name LIMIT 100")?;
    let rows=q.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,i64>(3)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;
    Ok(json!({"rows":rows.into_iter().map(|(id,name,payload,at)|json!({"id":id,"name":name,"payload":serde_json::from_str::<Value>(&payload).unwrap_or(Value::Null),"updated_at":at})).collect::<Vec<_>>()}))
}).await?))
}
#[derive(Deserialize)]
struct Workspace {
    name: String,
    payload: Value,
}
async fn save_workspace(State(app): State<App>, Json(w): Json<Workspace>) -> Api<Value> {
    Ok(Json(blocking(&app,move|s|{
    if w.name.trim().is_empty()||w.name.len()>100||serde_json::to_vec(&w.payload)?.len()>16384{return Err(invalid("Workspace name or state exceeds limits"));}
    let id=hash(w.name.trim().as_bytes());s.connect()?.execute("INSERT INTO saved_workspaces VALUES(?1,?2,?3,?4) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",rusqlite::params![id,w.name,serde_json::to_string(&w.payload)?,now()])?;Ok(json!({"id":id,"saved":true}))
}).await?))
}
async fn notes(State(app): State<App>, Query(q): Query<Params>) -> Api<Value> {
    Ok(Json(blocking(&app,move|s|{
    let scope=q.scope(&s)?;let encoded=serde_json::to_string(&scope)?;
    let c=s.connect()?;let mut query=c.prepare("SELECT id,at,series_id,probability,reason,revealed_at FROM replay_notes WHERE scope=?1 ORDER BY at DESC LIMIT 100")?;
    let rows=query.query_map([encoded],|r|Ok(json!({"id":r.get::<_,String>(0)?,"at":r.get::<_,i64>(1)?,"series":r.get::<_,String>(2)?,"probability":r.get::<_,f64>(3)?,"reason":r.get::<_,String>(4)?,"revealed_at":r.get::<_,Option<i64>>(5)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
    Ok(json!({"scope":scope,"notes":rows}))
}).await?))
}
#[derive(Deserialize)]
struct Note {
    scope: Scope,
    series_id: String,
    probability: f64,
    reason: String,
}
async fn save_note(State(app): State<App>, Json(n): Json<Note>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            n.scope.validate()?;
            s.series(&n.series_id, &n.scope)?;
            if !n.probability.is_finite()
                || !(0.0..=1.0).contains(&n.probability)
                || n.reason.len() > 4000
            {
                return Err(invalid("Invalid probability/reason"));
            }
            let id = uuid::Uuid::new_v4().to_string();
            s.connect()?.execute(
                "INSERT INTO replay_notes VALUES(?1,?2,?3,?4,?5,?6,NULL)",
                rusqlite::params![
                    id,
                    now(),
                    serde_json::to_string(&n.scope)?,
                    n.series_id,
                    n.probability,
                    n.reason
                ],
            )?;
            Ok(json!({"id":id,"saved":true,"outcomes_preloaded":false}))
        })
        .await?,
    ))
}

async fn atlas_overview(State(app): State<App>) -> Api<Value> {
    Ok(Json(blocking(&app, |s| s.atlas_overview()).await?))
}
async fn atlas_datasets(State(app): State<App>, Query(q): Query<Params>) -> Api<Value> {
    Ok(Json(blocking(&app, move |s| {
        Ok(json!({"rows":s.atlas_datasets(q.search.as_deref().unwrap_or(""),q.limit.unwrap_or(100))?}))
    }).await?))
}

async fn atlas_dataset_detail(State(app): State<App>, Path(id): Path<String>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| s.atlas_dataset_detail(&id)).await?,
    ))
}
async fn atlas_observations(
    State(app): State<App>,
    Path(id): Path<String>,
    Query(q): Query<Params>,
) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            let as_of = q
                .as_of
                .as_deref()
                .map(time)
                .transpose()?
                .unwrap_or_else(now);
            s.atlas_observations(
                &id,
                q.mode.as_deref().unwrap_or("observed"),
                as_of,
                q.limit.unwrap_or(1000),
            )
        })
        .await?,
    ))
}
#[derive(Deserialize)]
struct AtlasSample {
    max_bytes: Option<u64>,
}
async fn sample_atlas_distribution(
    State(app): State<App>,
    Path(id): Path<String>,
    Json(r): Json<AtlasSample>,
) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            s.sample_atlas_distribution(&id, r.max_bytes.unwrap_or(8 * 1024 * 1024))
        })
        .await?,
    ))
}

async fn atlas_graph(State(app): State<App>, Query(q): Query<Params>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            let as_of = q
                .as_of
                .as_deref()
                .map(time)
                .transpose()?
                .unwrap_or_else(now);
            s.atlas_graph(
                q.search.as_deref().unwrap_or(""),
                as_of,
                q.limit.unwrap_or(100),
            )
        })
        .await?,
    ))
}
async fn atlas_missions(State(app): State<App>, Query(q): Query<Params>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            Ok(json!({"missions":s.atlas_missions(q.limit.unwrap_or(50))?}))
        })
        .await?,
    ))
}
async fn create_atlas_mission(State(app): State<App>, Json(r): Json<MissionRequest>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            let id = s.create_atlas_mission(&r)?;
            Ok(json!({"id":id,"state":"queued","query":r.query}))
        })
        .await?,
    ))
}
async fn run_atlas_mission(State(app): State<App>, Path(id): Path<String>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| s.run_atlas_mission(&id)).await?,
    ))
}

pub async fn serve(store: Store, port: u16, ui: PathBuf, worker: bool) -> Result<()> {
    let token = Arc::new(uuid::Uuid::new_v4().to_string());
    let app = App {
        store: store.clone(),
        token,
        port,
        queries: Arc::new(tokio::sync::Semaphore::new(8)),
    };
    let stop = Arc::new(AtomicBool::new(false));
    let worker_task = if worker {
        let flag = stop.clone();
        Some(tokio::spawn(crate::worker::supervised(store, flag)))
    } else {
        None
    };
    let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port)).await?;
    println!("Hindsight Observatory: http://127.0.0.1:{port} | worker={worker} | PID {} | stop with Ctrl-C",std::process::id());
    axum::serve(listener, router(app, ui))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    stop.store(true, Ordering::Relaxed);
    if let Some(t) = worker_task {
        let _ = t.await;
    }
    Ok(())
}

async fn overview(State(app): State<App>, Query(q): Query<Params>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| s.overview(&q.scope(&s)?)).await?,
    ))
}
async fn network(State(app): State<App>, Query(q): Query<Params>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            s.networks(&q.scope(&s)?, q.search.as_deref().unwrap_or(""))
        })
        .await?,
    ))
}
async fn revisions(
    State(app): State<App>,
    Path(id): Path<String>,
    Query(q): Query<Params>,
) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| s.revisions(&id, &q.scope(&s)?)).await?,
    ))
}
async fn snapshots(State(app): State<App>) -> Api<Value> {
    Ok(Json(blocking(&app,|s|{
    let c=s.connect()?;let mut q=c.prepare("SELECT id,committed_at,description FROM dataset_snapshots ORDER BY id DESC LIMIT 100")?;
    let rows=q.query_map([],|r|Ok(json!({"id":r.get::<_,i64>(0)?,"committed_at":r.get::<_,i64>(1)?,"description":r.get::<_,String>(2)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
    Ok(json!({"snapshots":rows,"semantics":"Storage commit times do not establish historical market knowledge."}))
}).await?))
}
async fn submit_analysis(State(app): State<App>, Json(r): Json<AnalysisRequest>) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| {
            s.scope(
                Some(r.scope.snapshot),
                Some(r.scope.as_of),
                &r.scope.mode,
                &r.scope.purpose,
            )?;
            let id = s.submit_analysis(&r)?;
            Ok(json!({"id":id,"state":"queued","scope":r.scope}))
        })
        .await?,
    ))
}
async fn analysis_result(
    State(app): State<App>,
    Path(id): Path<String>,
    Query(q): Query<Params>,
) -> Api<Value> {
    Ok(Json(
        blocking(&app, move |s| s.analysis_result(&id, &q.scope(&s)?)).await?,
    ))
}
async fn analyses(State(app): State<App>, Query(q): Query<Params>) -> Api<Value> {
    Ok(Json(blocking(&app,move|s|{
    let scope=q.scope(&s)?;let encoded=serde_json::to_string(&scope)?;let c=s.connect()?;
    let mut q=c.prepare("SELECT id,state,created_at,request,error FROM experiments WHERE scope=?1 ORDER BY created_at DESC LIMIT 100")?;
    let rows=q.query_map([encoded],|r|Ok(json!({"id":r.get::<_,String>(0)?,"state":r.get::<_,String>(1)?,"created_at":r.get::<_,i64>(2)?,"request":r.get::<_,String>(3)?,"error":r.get::<_,Option<String>>(4)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
    Ok(json!({"scope":scope,"rows":rows}))
}).await?))
}
async fn cancel_analysis(State(app): State<App>, Path(id): Path<String>) -> Api<Value> {
    Ok(Json(blocking(&app,move|s|{
    let n=s.connect()?.execute("UPDATE experiments SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END WHERE id=?1 AND state IN ('queued','running')",[&id])?;
    if n==0{return Err(invalid("Analysis is not pending"));}Ok(json!({"id":id,"cancel_requested":true}))
}).await?))
}
#[derive(Deserialize)]
struct Reveal {
    id: String,
    scope: Scope,
}
async fn reveal(State(app): State<App>, Json(r): Json<Reveal>) -> Api<Value> {
    Ok(Json(blocking(&app,move|s|{
    r.scope.validate()?;let c=s.connect()?;
    let encoded=serde_json::to_string(&r.scope)?;
    let series:String=c.query_row("SELECT series_id FROM replay_notes WHERE id=?1 AND scope=?2",rusqlite::params![r.id,encoded],|r|r.get(0))?;
    let prior=s.points(&series,&r.scope,"1800","2300",5000)?;
    let last=prior.last().ok_or_else(||invalid("No eligible baseline observation to reveal against"))?;
    let current=s.scope(None,None,"observed","display")?;
    let later=s.points(&series,&current,&last.period_end,"2300",5000)?;
    let next=later.iter().find(|p|p.period_start>last.period_end&&p.value.is_some());
    c.execute("UPDATE replay_notes SET revealed_at=?2 WHERE id=?1",rusqlite::params![r.id,now()])?;
    c.execute("INSERT INTO access_audit(at,action,scope,detail) VALUES(?1,'explicit_outcome_reveal',?2,?3)",rusqlite::params![now(),encoded,r.id])?;
    Ok(json!({"id":r.id,"explicit_reveal":true,"baseline":crate::query::public_point(last,&r.scope)?,"outcome":next,"state":if next.is_some(){"revealed"}else{"not_yet_observed"},"warning":"Outcome uses the currently captured vintage and is deliberately separated from the replay information set."}))
}).await?))
}
async fn events(
    State(app): State<App>,
) -> axum::response::Sse<
    impl futures_util::Stream<
        Item = std::result::Result<axum::response::sse::Event, std::convert::Infallible>,
    >,
> {
    let stream = futures_util::stream::unfold(app, |app| async move {
        tokio::time::sleep(std::time::Duration::from_secs(3)).await;
        let status = blocking(&app, |s| {
            Ok(json!({"snapshot":s.snapshot_id()?,"clock":now()}))
        })
        .await;
        let value = match status {
            Ok(v) => v,
            Err(e) => json!({"error":e.to_string()}),
        };
        Some((
            Ok(axum::response::sse::Event::default()
                .event("watermark")
                .data(value.to_string())),
            app,
        ))
    });
    axum::response::Sse::new(stream).keep_alive(axum::response::sse::KeepAlive::default())
}
