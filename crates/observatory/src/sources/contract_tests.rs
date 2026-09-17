use super::*;
fn job(provider: &str, kind: &str) -> Job {
    Job {
        id: "fixture-only".into(),
        provider: provider.into(),
        kind: kind.into(),
        resource: "fixture".into(),
        params: json!({}),
        state: "running".into(),
        cutoff: time("2026-09-16T00:00:00Z").unwrap(),
        cursor: json!({}),
        attempts: 0,
        lease_token: Some("test".into()),
        lease_until: Some(now() + 100000),
        priority: 1,
        error: None,
        rows: 0,
        bytes: 0,
        updated_at: now(),
    }
}
fn wb() -> Value {
    json!([{"page":1,"pages":1,"total":2,"lastupdated":"2026-09-01"},[{"indicator":{"id":"FIXTURE","value":"Synthetic fixture"},"countryiso3code":"USA","country":{"value":"United States"},"date":"2024","value":12.5,"unit":"test units"},{"indicator":{"id":"FIXTURE","value":"Synthetic fixture"},"countryiso3code":"USA","country":{"value":"United States"},"date":"2023","value":null,"unit":"test units"}]])
}
#[test]
fn wb_preserves_null_and_does_not_invent_publication() {
    let r = normalize_wb(&job("worldbank", "wb_history"), &wb()).unwrap();
    assert_eq!(r.observations.len(), 2);
    assert_eq!(r.observations[1].value, None);
    assert_eq!(r.observations[0].published_at, None);
    assert_eq!(r.observations[0].reconstructed_at, None);
    assert!(r.complete_scope);
}
#[test]
fn wb_bad_numeric_value_is_not_silently_missing() {
    let mut v = wb();
    v[1][0]["value"] = "broken".into();
    assert!(normalize_wb(&job("worldbank", "wb_history"), &v).is_err());
}
#[test]
fn wb_changed_vintage_and_wrong_page_fail() {
    let mut j = job("worldbank", "wb_history");
    j.cursor = json!({"lastupdated":"2025-01-01"});
    assert!(normalize_wb(&j, &wb()).is_err());
    let mut j = job("worldbank", "wb_history");
    j.cursor = json!({"page":2});
    assert!(normalize_wb(&j, &wb()).is_err());
}
#[test]
fn wb_empty_response_is_not_proof_of_completeness() {
    assert!(normalize_wb(&job("worldbank", "wb_history"), &json!([])).is_err());
}
fn bars() -> Value {
    json!({"bars":{"MU":[{"t":"2026-09-15T14:00:00Z","o":100.0,"h":101.0,"l":99.0,"c":100.5,"v":1000,"n":20,"vw":100.25}]},"next_page_token":null})
}
#[test]
fn alpaca_volume_trade_count_vwap_and_left_edge_are_retained() {
    let mut j = job("alpaca", "alpaca_bars");
    j.params = json!({"timeframe":"1Min"});
    let p =
        alpaca::normalize_bars(&j, &bars(), "https://data.alpaca.markets/v2/stocks/bars").unwrap();
    let o = &p.observations[0];
    assert_eq!(o.extras["v"], 1000);
    assert_eq!(o.extras["n"], 20);
    assert_eq!(o.extras["vw"], 100.25);
    assert_eq!(
        o.extras["complete_not_before_ms"].as_i64().unwrap()
            - o.extras["start_ms"].as_i64().unwrap(),
        60000
    );
    assert!(!p.complete_scope);
    assert_eq!(o.reconstructed_at, None);
}
#[test]
fn unfinished_market_bar_is_not_accepted() {
    let mut j = job("alpaca", "alpaca_bars");
    j.params = json!({"timeframe":"1Min"});
    j.cutoff = time("2026-09-15T14:00:30Z").unwrap();
    assert!(
        alpaca::normalize_bars(&j, &bars(), "https://data.alpaca.markets/")
            .unwrap()
            .observations
            .is_empty()
    );
}
#[test]
fn malformed_ohlc_and_negative_trade_count_fail() {
    let mut j = job("alpaca", "alpaca_bars");
    j.params = json!({"timeframe":"1Min"});
    let mut v = bars();
    v["bars"]["MU"][0]["l"] = 200.into();
    assert!(alpaca::normalize_bars(&j, &v, "https://data.alpaca.markets/").is_err());
    let mut v = bars();
    v["bars"]["MU"][0]["n"] = (-1).into();
    assert!(alpaca::normalize_bars(&j, &v, "https://data.alpaca.markets/").is_err());
}
#[test]
fn unknown_market_interval_and_bad_token_fail() {
    let mut j = job("alpaca", "alpaca_bars");
    j.params = json!({"timeframe":"1Second"});
    assert!(alpaca::normalize_bars(&j, &bars(), "https://data.alpaca.markets/").is_err());
    j.params = json!({"timeframe":"1Min"});
    let mut v = bars();
    v["next_page_token"] = 7.into();
    assert!(alpaca::normalize_bars(&j, &v, "https://data.alpaca.markets/").is_err());
}
fn issuer_html() -> String {
    "<html><head><meta name='description' content='Nanya Technology June 2026 Revenue NT$ 29,388 Million'></head><body>2026/07/03 Monthly Revenue</body></html>".into()
}
#[test]
fn issuer_revenue_currency_scale_and_date_are_explicit() {
    let p = issuer::nanya_release(
        &job("issuer", "issuer_release"),
        &issuer_html(),
        "https://www.nanya.com/fixture-only",
    )
    .unwrap();
    let o = &p.observations[0];
    assert_eq!(o.value, Some(29388.));
    assert_eq!(o.unit, "TWD million");
    assert_eq!(o.period_start, "2026-06-01");
    assert_eq!(o.period_end, "2026-06-30");
    assert_eq!(o.published_at, Some(time("2026-07-03T16:00:00Z").unwrap()));
    assert_eq!(o.extras["not_production"], true);
}
#[test]
fn issuer_unexpected_unit_or_missing_publication_fails() {
    let j = job("issuer", "issuer_release");
    assert!(issuer::nanya_release(
        &j,
        &issuer_html().replace("NT$", "USD"),
        "https://www.nanya.com/"
    )
    .is_err());
    assert!(issuer::nanya_release(
        &j,
        &issuer_html().replace("2026/07/03", "undated"),
        "https://www.nanya.com/"
    )
    .is_err());
}
#[test]
fn issuer_published_after_cutoff_is_not_accepted() {
    let mut j = job("issuer", "issuer_release");
    j.cutoff = time("2026-07-01T00:00:00Z").unwrap();
    assert!(issuer::nanya_release(&j, &issuer_html(), "https://www.nanya.com/").is_err());
}
#[test]
fn sec_missing_schema_is_not_empty_success() {
    assert!(sec::companyfacts(
        &job("sec", "sec_companyfacts"),
        &json!({"not":"company facts"})
    )
    .is_err());
}
