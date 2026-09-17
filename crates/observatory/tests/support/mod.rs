use hindsight_observatory::{model::*, storage};
use serde_json::json;
use tempfile::TempDir;
pub fn setup() -> (TempDir, Store) {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path()).unwrap();
    (dir, store)
}
pub fn observation(value: f64, order: i64) -> Observation {
    Observation {
        series_id: "TEST:SERIES".into(),
        period_start: "2024-01-01".into(),
        period_end: "2024-01-31".into(),
        value: Some(value),
        unit: "USD million".into(),
        dimensions: json!({"entity":"fixture-only"}),
        published_at: Some(1_706_745_600_000 + order * 1000),
        reconstructed_at: Some(1_706_745_601_000 + order * 1000),
        source_order: Some(order),
        precision: "synthetic_instant".into(),
        quality: "test_fixture_only".into(),
        extras: json!({"synthetic":true}),
    }
}
pub fn page(observations: Vec<Observation>) -> PageResult {
    let mut seen = std::collections::BTreeSet::new();
    let series = observations
        .iter()
        .filter(|o| seen.insert(o.series_id.clone()))
        .map(|o| Series {
            id: o.series_id.clone(),
            provider: "worldbank".into(),
            code: o.series_id.clone(),
            name: "Synthetic fixture measurement".into(),
            entity: "FIXTURE".into(),
            unit: o.unit.clone(),
            frequency: "monthly".into(),
            description: "Not a real market observation".into(),
            source_url: "https://api.worldbank.org/fixture-only".into(),
            metadata: json!({"synthetic":true}),
        })
        .collect();
    PageResult {
        complete_scope: true,
        series,
        received_rows: observations.len() as i64,
        observations,
        next: None,
        advertised_total: None,
        note: "synthetic test fixture only".into(),
        children: vec![],
    }
}
pub fn capture(store: &Store) -> Capture {
    let body =
        serde_json::to_vec(&json!({"fixture":true,"nonce":uuid::Uuid::new_v4().to_string()}))
            .unwrap();
    let sha = hash(&body);
    let path = format!("raw/{sha}.body");
    storage::publish(&store.root.join(&path), &body).unwrap();
    Capture {
        id: uuid::Uuid::new_v4().to_string(),
        provider: "worldbank".into(),
        url: "https://api.worldbank.org/fixture-only".into(),
        sha256: sha,
        path,
        bytes: body.len() as u64,
        request_started_at: now() - 3,
        response_headers_at: now() - 2,
        response_completed_at: now() - 1,
        etag: None,
        last_modified: None,
        representation: "synthetic_test_fixture".into(),
        original_sha256: None,
    }
}
pub fn job(store: &Store) -> Job {
    let nonce = uuid::Uuid::new_v4().to_string();
    store
        .enqueue(
            "worldbank",
            "test_fixture",
            &nonce,
            &json!({"fixture":true}),
            now(),
            100,
        )
        .unwrap();
    store.claim().unwrap().unwrap()
}
pub fn commit(store: &Store, page: &PageResult) -> i64 {
    let job = job(store);
    let capture = capture(store);
    store.commit_page(&job, &capture, page).unwrap()
}
#[allow(dead_code)]
pub fn scope(store: &Store) -> Scope {
    store.scope(None, None, "observed", "research").unwrap()
}
