mod support;
use hindsight_observatory::{
    analysis::{self, AnalysisRequest},
    model::*,
};
use serde_json::{json, Value};
use support::*;
fn month(index: i32, series: &str) -> Observation {
    let year = 2020 + index / 12;
    let m = (index % 12 + 1) as u32;
    let start = chrono::NaiveDate::from_ymd_opt(year, m, 1).unwrap();
    let next = if m == 12 {
        chrono::NaiveDate::from_ymd_opt(year + 1, 1, 1)
    } else {
        chrono::NaiveDate::from_ymd_opt(year, m + 1, 1)
    }
    .unwrap();
    let end = next.pred_opt().unwrap();
    let at = next
        .and_hms_opt(12, 0, 0)
        .unwrap()
        .and_utc()
        .timestamp_millis();
    let mut o = observation(100. + index as f64 * 0.8 + (index as f64 * 0.7).sin(), 1);
    o.series_id = series.into();
    if series == "X" {
        o.value = o.value.map(|v| v * 0.3 + (index as f64 * 0.6).cos());
    }
    o.period_start = start.to_string();
    o.period_end = end.to_string();
    o.published_at = Some(at);
    o.reconstructed_at = Some(at + 1000);
    o
}
fn request(s: &Store) -> AnalysisRequest {
    let mut view = scope(s);
    view.mode = "reconstructed".into();
    AnalysisRequest {
        scope: view,
        series_ids: vec!["X".into(), "Y".into()],
        start: "2010".into(),
        end: "2030".into(),
        transform: "level".into(),
        kind: "forecast".into(),
        max_lag: 0,
        window: 12,
        controls: vec![],
    }
}
fn base(s: &Store) {
    let rows = (0..60)
        .flat_map(|i| [month(i, "X"), month(i, "Y")])
        .collect();
    commit(s, &page(rows));
}
fn result(s: &Store) -> Value {
    analysis::compute(s, &request(s), || false).unwrap()
}
fn semantic(v: &Value) -> Vec<Value> {
    v["scored"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            json!({
                "decision_at": r["decision_at"],
                "baseline": r["baseline"],
                "candidate": r["candidate"],
                "training_count": r["training_count"],
                "trained_through": r["trained_through"],
                "evidence": r["evidence"],
            })
        })
        .collect()
}
#[test]
fn future_corrections_do_not_rewrite_past_forecasts_or_training_labels() {
    let (_d, s) = setup();
    base(&s);
    let clean = result(&s);
    assert_eq!(clean["state"], "completed");
    let mut correction = month(10, "Y");
    correction.value = Some(999999.);
    correction.source_order = Some(2);
    correction.published_at = Some(time("2026-05-01T12:00:00Z").unwrap());
    correction.reconstructed_at = Some(time("2026-05-01T12:00:01Z").unwrap());
    commit(&s, &page(vec![correction]));
    let changed = result(&s);
    assert_eq!(clean["decisions"], changed["decisions"]);
}
#[test]
fn previously_unmatured_target_is_not_scored_early() {
    let (_d, s) = setup();
    base(&s);
    let clean = result(&s);
    let prior = semantic(&clean);
    commit(&s, &page(vec![month(60, "X"), month(60, "Y")]));
    let later = result(&s);
    let next = semantic(&later);
    assert!(next.len() >= prior.len());
    assert_eq!(prior, &next[..prior.len()]);
}
#[test]
fn every_forecast_uses_only_matured_initial_release_labels() {
    let (_d, s) = setup();
    base(&s);
    let r = result(&s);
    for row in r["scored"].as_array().unwrap() {
        let decision_at = row["decision_at"].as_i64().unwrap();
        assert!(row["trained_through"].as_i64().unwrap() < decision_at);
        assert!(row["evidence"]["target_available_at"].as_i64().unwrap() > decision_at);
    }
}
#[test]
fn unsupported_forecast_lag_and_transform_fail_explicitly() {
    let (_d, s) = setup();
    base(&s);
    let mut req = request(&s);
    req.max_lag = 4;
    req.transform = "log_return".into();
    let error = analysis::compute(&s, &req, || false).unwrap_err();
    assert!(error.to_string().contains("not implemented"));
}
