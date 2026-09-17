mod support;
use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use hindsight_observatory::{
    analysis::AnalysisRequest,
    model::*,
    service::{self, App},
};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use std::sync::Arc;
use support::*;
use tower::ServiceExt;
fn app(store: &Store) -> axum::Router {
    service::router(
        App {
            store: store.clone(),
            token: Arc::new("test-local-session".into()),
            port: 8780,
            queries: Arc::new(tokio::sync::Semaphore::new(8)),
        },
        store.root.join("ui-not-present"),
    )
}
async fn get(store: &Store, path: &str) -> (StatusCode, Value) {
    let r = app(store)
        .oneshot(
            Request::builder()
                .uri(path)
                .header("host", "127.0.0.1:8780")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = r.status();
    let bytes = r.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
async fn post(store: &Store, path: &str, payload: Value, token: &str) -> (StatusCode, Value) {
    let r = app(store)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("host", "127.0.0.1:8780")
                .header("origin", "http://127.0.0.1:8780")
                .header("content-type", "application/json")
                .header("x-hindsight-token", token)
                .body(Body::from(payload.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = r.status();
    let bytes = r.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
#[tokio::test]
async fn host_and_cross_site_protection() {
    let (_d, s) = setup();
    for (host, site) in [
        ("attacker.invalid", "same-origin"),
        ("127.0.0.1:8780", "cross-site"),
    ] {
        let r = app(&s)
            .oneshot(
                Request::builder()
                    .uri("/api/bootstrap")
                    .header("host", host)
                    .header("sec-fetch-site", site)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::FORBIDDEN);
    }
}
#[tokio::test]
async fn mutation_needs_correct_local_session() {
    let (_d, s) = setup();
    let (status, _) = post(
        &s,
        "/api/workspaces",
        json!({"name":"test","payload":{}}),
        "wrong",
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(
        s.connect()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM saved_workspaces", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
}
#[tokio::test]
async fn startup_does_not_serve_fake_market_results() {
    let (_d, s) = setup();
    let (status, v) = get(&s, "/api/bootstrap").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["data_kind"], "real");
    let (_, v) = get(&s, "/api/overview").await;
    assert_eq!(v["changes"], json!([]));
}
#[tokio::test]
async fn source_text_cannot_become_executable_html() {
    let (_d, s) = setup();
    let mut p = page(vec![observation(10., 1)]);
    p.series[0].name = "<script>globalThis.stolen=true</script>".into();
    commit(&s, &p);
    let (status, v) = get(&s, "/api/catalog").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        v["rows"][0]["name"],
        "<script>globalThis.stolen=true</script>"
    );
    let r = app(&s)
        .oneshot(
            Request::builder()
                .uri("/api/catalog")
                .header("host", "127.0.0.1:8780")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert!(r.headers()["content-security-policy"]
        .to_str()
        .unwrap()
        .contains("script-src 'self'"));
    assert_eq!(r.headers()["x-content-type-options"], "nosniff");
}
#[tokio::test]
async fn future_cutoff_and_snapshot_are_rejected() {
    let (_d, s) = setup();
    assert_eq!(
        get(&s, "/api/catalog?snapshot=99").await.0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        get(&s, "/api/catalog?as_of=2100-01-01T00:00:00Z").await.0,
        StatusCode::BAD_REQUEST
    );
}
#[tokio::test]
async fn browser_payload_obeys_reconstructed_visibility() {
    let (_d, s) = setup();
    let a = observation(10., 1);
    commit(&s, &page(vec![a.clone()]));
    commit(&s, &page(vec![observation(888., 100)]));
    let at = chrono::DateTime::from_timestamp_millis(a.reconstructed_at.unwrap())
        .unwrap()
        .to_rfc3339()
        .replace('+', "%2B");
    let (_, v) = get(
        &s,
        &format!("/api/series/TEST:SERIES?mode=reconstructed&as_of={at}"),
    )
    .await;
    assert_eq!(v["points"].as_array().unwrap().len(), 1);
    assert_eq!(v["points"][0]["value"], 10.);
    assert!(v["points"][0].get("observed_at").is_none());
    assert!(!v.to_string().contains("888"));
}
#[tokio::test]
async fn workspace_save_really_persists() {
    let (_d, s) = setup();
    let payload = json!({"name":"Operating costs","payload":{"page":"Explore","selected":"TEST:SERIES","mode":"observed"}});
    assert_eq!(
        post(&s, "/api/workspaces", payload, "test-local-session")
            .await
            .0,
        StatusCode::OK
    );
    let (_, v) = get(&s, "/api/workspaces").await;
    assert_eq!(v["rows"][0]["name"], "Operating costs");
    assert_eq!(v["rows"][0]["payload"]["selected"], "TEST:SERIES");
}
#[tokio::test]
async fn job_actions_are_stateful_and_filtered_on_server() {
    let (_d, s) = setup();
    let j = job(&s);
    let (status, _) = post(
        &s,
        &format!("/api/jobs/{}/action", j.id),
        json!({"action":"pause"}),
        "test-local-session",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let (_, v) = get(&s, "/api/jobs?provider=worldbank&search=paused").await;
    assert_eq!(v["jobs"].as_array().unwrap().len(), 1);
    let (_, empty) = get(&s, "/api/jobs?provider=sec").await;
    assert_eq!(empty["jobs"], json!([]));
}
#[tokio::test]
async fn research_pipeline_retains_all_pairs_and_no_fake_significance() {
    let (_d, s) = setup();
    let mut obs = Vec::new();
    for year in 2000..2040 {
        for (series, scale) in [("X", 1.), ("Y", 2.)] {
            let mut o = observation(((year as f64) * 0.7).sin() * scale, year);
            o.series_id = series.into();
            o.period_start = format!("{year}-01-01");
            o.period_end = format!("{year}-12-31");
            o.published_at = None;
            o.reconstructed_at = None;
            obs.push(o);
        }
    }
    commit(&s, &page(obs));
    let scope = scope(&s);
    let req = AnalysisRequest {
        scope: scope.clone(),
        series_ids: vec!["X".into(), "Y".into()],
        start: "1990".into(),
        end: "2050".into(),
        transform: "change".into(),
        kind: "dependence".into(),
        max_lag: 4,
        window: 12,
        controls: vec![],
    };
    let id = s.submit_analysis(&req).unwrap();
    assert!(s.analysis_step().unwrap());
    let v = s.analysis_result(&id, &scope).unwrap();
    assert_eq!(v["state"], "completed");
    assert_eq!(v["result"]["pairs"].as_array().unwrap().len(), 1);
    assert!((v["result"]["pairs"][0]["pearson"].as_f64().unwrap() - 1.).abs() < 1e-10);
    assert_eq!(v["result"]["pairs"][0]["p_value"], Value::Null);
    assert!(
        v["result"]["provenance"]["kernel_sha256"]
            .as_str()
            .unwrap()
            .len()
            == 64
    );
    let mut wrong = scope;
    wrong.as_of -= 1;
    assert!(s.analysis_result(&id, &wrong).is_err());
}
#[tokio::test]
async fn replay_notes_do_not_preload_outcomes() {
    let (_d, s) = setup();
    commit(&s, &page(vec![observation(10., 1)]));
    let mut scope = scope(&s);
    scope.purpose = "display".into();
    let(status,v)=post(&s,"/api/replay/notes",json!({"scope":scope,"series_id":"TEST:SERIES","probability":0.6,"reason":"Synthetic API test"}),"test-local-session").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["outcomes_preloaded"], false);
    assert!(v.get("outcome").is_none());
}
