use hindsight_economic::{
    demo, engine, evidence::*, ingest, market::Market, numerics::Model, output, portfolio::Account,
    research, sec, types::*, validation,
};
use serde_json::json;
use std::{collections::BTreeMap, sync::OnceLock};
fn fixture() -> Input {
    static INPUT: OnceLock<Input> = OnceLock::new();
    INPUT
        .get_or_init(|| {
            let mut i = demo::input().unwrap();
            i.dataset.sessions.truncate(48);
            let end = i.dataset.sessions.last().unwrap().close;
            i.dataset.bars.retain(|b| {
                b.start < end && {
                    let s = i
                        .dataset
                        .sessions
                        .iter()
                        .find(|s| s.open <= b.start && b.start < s.close)
                        .unwrap();
                    b.start < s.open + 35 * MINUTE || b.end == s.close
                }
            });
            i.config.min_train = 8;
            i.config.train_window = 20;
            i.config.horizon_sessions = 3;
            i.config.bootstrap_repetitions = 100;
            i
        })
        .clone()
}
fn timestamp(s: &str) -> Time {
    chrono::DateTime::parse_from_rfc3339(s)
        .unwrap()
        .timestamp_millis()
}
fn view(i: &Input, t: Time) -> Snapshot {
    snapshot(&i.dataset, t, i.config.mode).unwrap()
}
#[test]
fn fixture_validates() {
    validation::validate(&fixture()).unwrap();
}
#[test]
fn observed_requires_receipt_and_extraction() {
    let mut a = fixture().dataset.facts[0].availability.clone();
    a.first_seen_at = None;
    assert_eq!(a.ready(EvidenceMode::Observed), None);
    assert!(a.ready(EvidenceMode::Reconstructed).is_some());
    a.first_seen_at = Some(10);
    a.extraction_completed_at = None;
    assert_eq!(a.ready(EvidenceMode::Observed), None);
}
#[test]
fn publication_cannot_be_bypassed() {
    let mut a = fixture().dataset.facts[0].availability.clone();
    a.modeled_available_at = Some(a.published_at - 1);
    assert!(a.validate().is_err());
}
#[test]
fn extraction_cannot_precede_receipt() {
    let mut a = fixture().dataset.facts[0].availability.clone();
    a.extraction_completed_at = Some(a.first_seen_at.unwrap() - 1);
    assert!(a.validate().is_err());
}
#[test]
fn source_readiness_also_applies() {
    let mut i = fixture();
    let t = i.dataset.sessions[5].decision;
    let source = i.dataset.facts[0].source_id.clone();
    i.dataset
        .sources
        .iter_mut()
        .find(|s| s.id == source)
        .unwrap()
        .availability
        .modeled_available_at = Some(t + DAY);
    assert!(!view(&i, t).facts.iter().any(|f| f.source_id == source));
}
#[test]
fn correction_does_not_rewrite_history() {
    let i = fixture();
    let correction = i
        .dataset
        .facts
        .iter()
        .find(|f| f.id == "MU-first-correction")
        .unwrap();
    let before = view(
        &i,
        correction.availability.modeled_available_at.unwrap() - 1,
    );
    assert_eq!(
        before
            .facts
            .iter()
            .find(|f| f.key == correction.key)
            .unwrap()
            .revision,
        1
    );
    let after = view(&i, correction.availability.modeled_available_at.unwrap());
    assert_eq!(
        after
            .facts
            .iter()
            .find(|f| f.key == correction.key)
            .unwrap()
            .revision,
        2
    );
}
#[test]
fn reingestion_is_not_revision_order() {
    let mut i = fixture();
    let t = i.dataset.sessions[20].decision;
    let before = view(&i, t);
    i.dataset.facts.reverse();
    i.dataset.sources.reverse();
    assert_eq!(before, view(&i, t));
}
#[test]
fn conflicting_fact_revision_fails_closed() {
    let mut i = fixture();
    let mut f = i.dataset.facts[0].clone();
    f.value_micros += 1;
    i.dataset.facts.push(f);
    assert!(validation::validate(&i).is_err());
}
#[test]
fn distinct_reporting_periods_are_preserved() {
    let mut i = fixture();
    let mut f = i.dataset.facts[0].clone();
    f.id = "quarter-versus-ytd".into();
    f.key.period_start = "2025-01-01".into();
    i.dataset.facts.push(f);
    validation::validate(&i).unwrap();
    let v = view(&i, i.dataset.sessions[1].decision);
    assert_eq!(v.facts.iter().filter(|f| f.key.entity == "MU").count(), 2);
}
#[test]
fn segment_facts_do_not_replace_consolidated() {
    let mut i = fixture();
    let mut f = i.dataset.facts[0].clone();
    f.id = "segment".into();
    f.key.dimensions.insert("segment".into(), "memory".into());
    i.dataset.facts.push(f);
    let v = view(&i, i.dataset.sessions[1].decision);
    assert!(latest_metric(&v, "MU", "revenue")
        .unwrap()
        .unwrap()
        .key
        .dimensions
        .is_empty());
}
#[test]
fn unit_conflicts_are_not_averaged() {
    let mut i = fixture();
    let mut f = i.dataset.facts[0].clone();
    f.id = "eur".into();
    f.key.unit = "EUR".into();
    i.dataset.facts.push(f);
    let v = view(&i, i.dataset.sessions[1].decision);
    assert!(latest_metric(&v, "MU", "revenue").is_err());
}
#[test]
fn missing_source_is_an_error() {
    let mut i = fixture();
    i.dataset.facts[0].source_id = "does-not-exist".into();
    assert!(validation::validate(&i).is_err());
}
#[test]
fn historical_relationship_requires_knowledge() {
    let i = fixture();
    assert!(view(&i, i.dataset.sessions[3].decision)
        .relationships
        .is_empty());
    assert_eq!(
        view(&i, i.dataset.sessions[7].decision).relationships.len(),
        1
    );
}
#[test]
fn later_retraction_preserves_earlier_graph() {
    let mut i = fixture();
    let before = view(&i, i.dataset.sessions[10].decision);
    let mut e = i.dataset.relationships[0].clone();
    e.revision = 2;
    e.status = Status::Retracted;
    e.availability.modeled_available_at = Some(i.dataset.sessions[15].decision);
    i.dataset.relationships.push(e);
    assert_eq!(before, view(&i, i.dataset.sessions[10].decision));
    assert!(view(&i, i.dataset.sessions[16].decision)
        .relationships
        .is_empty());
}
#[test]
fn date_precision_respects_spring_dst() {
    assert_eq!(
        conservative_date_end("2026-03-08", "America/New_York", 0).unwrap(),
        timestamp("2026-03-09T04:00:00Z")
    );
}
#[test]
fn date_precision_respects_fall_dst() {
    assert_eq!(
        conservative_date_end("2026-11-01", "America/New_York", 0).unwrap(),
        timestamp("2026-11-02T05:00:00Z")
    );
}
#[test]
fn invalid_timezone_is_rejected() {
    assert!(conservative_date_end("2026-01-01", "unknown", 0).is_err());
}
#[test]
fn invalid_date_is_rejected() {
    assert!(conservative_date_end("2026-02-30", "UTC", 0).is_err());
}
#[test]
fn bar_is_unavailable_at_its_start() {
    let i = fixture();
    let m = Market::new(&i.dataset);
    let b = &i.dataset.bars[10];
    assert!(!m
        .observed(&b.symbol, b.start, 0)
        .iter()
        .any(|x| x.start == b.start));
    assert!(m
        .observed(&b.symbol, b.end, 0)
        .iter()
        .any(|x| x.start == b.start));
}
#[test]
fn delayed_feed_uses_old_observations() {
    let i = fixture();
    let m = Market::new(&i.dataset);
    let t = i.dataset.sessions[2].decision;
    assert!(m
        .observed("MU", t, 15 * MINUTE)
        .iter()
        .all(|b| b.end <= t - 15 * MINUTE));
}
#[test]
fn execution_uses_post_arrival_boundary() {
    let i = fixture();
    let m = Market::new(&i.dataset);
    let t = i.dataset.sessions[1].decision;
    assert_eq!(m.fill_reference("MU", t + 100).unwrap().start, t + MINUTE);
    assert_eq!(
        m.fill_reference("MU", t + MINUTE).unwrap().start,
        t + 2 * MINUTE
    );
}
#[test]
fn missing_next_minute_is_not_skipped() {
    let mut i = fixture();
    let t = i.dataset.sessions[1].decision;
    i.dataset
        .bars
        .retain(|b| b.symbol != "MU" || b.start != t + MINUTE);
    assert!(Market::new(&i.dataset)
        .fill_reference("MU", t + 100)
        .is_none());
}
#[test]
fn future_volume_cannot_change_order_size() {
    let i = fixture();
    let t = i.dataset.sessions[2].decision;
    let mut a = Account::new(i.config.initial_cash_micros);
    a.rebalance(10, t, 1, &i.config, &Market::new(&i.dataset))
        .unwrap();
    let mut changed = i.clone();
    for b in &mut changed.dataset.bars {
        if b.start >= t {
            b.volume = 0;
        }
    }
    let mut b = Account::new(i.config.initial_cash_micros);
    b.rebalance(10, t, 1, &i.config, &Market::new(&changed.dataset))
        .unwrap();
    assert_eq!(a, b);
}
#[test]
fn negative_cash_is_impossible() {
    let mut i = fixture();
    i.config.initial_cash_micros = MONEY;
    let t = i.dataset.sessions[2].decision;
    let mut a = Account::new(MONEY);
    a.rebalance(100, t, 1, &i.config, &Market::new(&i.dataset))
        .unwrap();
    assert_eq!(a.cash_micros, MONEY);
    assert_eq!(a.shares, 0);
}
#[test]
fn short_target_is_rejected() {
    let i = fixture();
    let mut a = Account::new(MONEY);
    assert!(a
        .rebalance(
            -1,
            i.dataset.sessions[2].decision,
            1,
            &i.config,
            &Market::new(&i.dataset)
        )
        .is_err());
}
#[test]
fn fee_is_counted_once() {
    let i = fixture();
    let t = i.dataset.sessions[2].decision;
    let mut a = Account::new(i.config.initial_cash_micros);
    a.rebalance(10, t, 1, &i.config, &Market::new(&i.dataset))
        .unwrap();
    let fill = a
        .events
        .iter()
        .find(|e| e.phase == "simulated_fill")
        .unwrap();
    assert_eq!(
        a.cash_micros,
        i.config.initial_cash_micros - 10 * fill.price_micros - fill.fee_micros
    );
    assert_eq!(a.fees_micros, fill.fee_micros);
}
#[test]
fn dividend_is_receivable_until_payment() {
    let mut i = fixture();
    let t = i.dataset.sessions[3].open;
    i.dataset.corporate_actions.push(CorporateAction {
        id: "div".into(),
        symbol: "MU".into(),
        effective_at: t,
        split_numerator: 1,
        split_denominator: 1,
        dividend_per_share_micros: MONEY,
        dividend_pay_at: Some(t + DAY),
    });
    let mut a = Account::new(100 * MONEY);
    a.shares = 10;
    a.apply_actions(&i.dataset, "MU", t).unwrap();
    assert_eq!(a.cash_micros, 100 * MONEY);
    assert_eq!(a.receivables[0].amount_micros, 10 * MONEY);
    a.apply_actions(&i.dataset, "MU", t + DAY).unwrap();
    assert_eq!(a.cash_micros, 110 * MONEY);
    a.apply_actions(&i.dataset, "MU", t + 2 * DAY).unwrap();
    assert_eq!(a.cash_micros, 110 * MONEY);
}
#[test]
fn fractional_split_is_not_invented() {
    let mut i = fixture();
    let t = i.dataset.sessions[3].open;
    i.dataset.corporate_actions.push(CorporateAction {
        id: "split".into(),
        symbol: "MU".into(),
        effective_at: t,
        split_numerator: 1,
        split_denominator: 2,
        dividend_per_share_micros: 0,
        dividend_pay_at: None,
    });
    let mut a = Account::new(MONEY);
    a.shares = 3;
    assert!(a.apply_actions(&i.dataset, "MU", t).is_err());
    assert_eq!(a.shares, 3);
}
#[test]
fn constant_target_predicts_constant() {
    let m = Model::fit(&[vec![0.0], vec![1.0], vec![2.0]], &[3.0, 3.0, 3.0], 1.0).unwrap();
    assert!((m.predict(&[20.0]).unwrap() - 3.0).abs() < 1e-12);
}
#[test]
fn cpp_ridge_matches_analytic_solution() {
    let m = Model::fit(&[vec![-1.0], vec![0.0], vec![1.0]], &[1.0, 3.0, 5.0], 1.0).unwrap();
    assert!((m.predict(&[1.0]).unwrap() - 4.5).abs() < 1e-12);
}
#[test]
fn cpp_rejects_nan() {
    assert!(Model::fit(&[vec![f64::NAN], vec![1.0]], &[1.0, 2.0], 1.0).is_err());
}
#[test]
fn cpp_rejects_bad_dimensions() {
    assert!(Model::fit(&[vec![1.0], vec![1.0, 2.0]], &[1.0, 2.0], 1.0).is_err());
}
#[test]
fn cpp_rejects_negative_penalty() {
    assert!(Model::fit(&[vec![1.0], vec![2.0]], &[1.0, 2.0], -1.0).is_err());
}
#[test]
fn prediction_dimension_is_checked() {
    let m = Model::fit(&[vec![0.0], vec![1.0]], &[1.0, 2.0], 1.0).unwrap();
    assert!(m.predict(&[1.0, 2.0]).is_err());
}
#[test]
fn decimal_prices_are_exact() {
    assert_eq!(
        ingest::decimal_micros(&json!("123.456789")).unwrap(),
        123456789
    );
    assert_eq!(ingest::decimal_micros(&json!("-0.5")).unwrap(), -500000);
    assert!(ingest::decimal_micros(&json!("1.0000001")).is_err());
}
#[test]
fn pagination_cycle_is_rejected() {
    let mut p = ingest::Pagination::default();
    assert_eq!(
        p.next(&json!({"next_page_token":"A"})).unwrap(),
        Some("A".into())
    );
    assert!(p.next(&json!({"next_page_token":"A"})).is_err());
}
#[test]
fn pagination_ends_explicitly() {
    assert_eq!(
        ingest::Pagination::default()
            .next(&json!({"next_page_token":null}))
            .unwrap(),
        None
    );
}
#[test]
fn alpaca_page_preserves_volume_and_left_edge() {
    let v = json!({"bars":{"MU":[{"t":"2026-01-05T15:00:00Z","o":100,"h":102,"l":99,"c":101,"v":12345}]}});
    let b = ingest::alpaca_page(&v, "page").unwrap();
    assert_eq!(b[0].volume, 12345);
    assert_eq!(b[0].end, b[0].start + MINUTE);
    assert_eq!(b[0].available_at, b[0].end);
}
#[test]
fn malformed_alpaca_is_not_empty_success() {
    assert!(ingest::alpaca_page(&json!({}), "p").is_err());
}
#[test]
fn sec_import_uses_conservative_publication() {
    let v = json!({"cik":723125,"facts":{"us-gaap":{"Revenues":{"units":{"USD":[{"start":"2025-01-01","end":"2025-03-31","filed":"2025-04-15","accn":"0001-25-001","val":123}]}}}}});
    let r = sec::companyfacts(
        &serde_json::to_vec(&v).unwrap(),
        "us-gaap",
        "Revenues",
        "MU",
        "revenue",
        timestamp("2026-01-01T00:00:00Z"),
    )
    .unwrap();
    assert_eq!(
        r.facts[0].availability.modeled_available_at.unwrap(),
        timestamp("2025-04-16T04:01:00Z")
    );
    assert_eq!(r.facts[0].value_micros, 123 * MONEY);
}
#[test]
fn embedded_html_cannot_close_json_script() {
    let s =
        output::safe_embedded_json(&json!({"text":"</script><script>alert(1)</script>"})).unwrap();
    assert!(!s.contains('<'));
    assert!(s.contains("\\u003c"));
}
#[test]
fn block_interval_is_seeded_and_paired() {
    let gains: Vec<_> = (0..50).map(|i| i as f64 / 100.0).collect();
    assert_eq!(
        research::paired_block_interval(&gains, 5, 100, 42),
        research::paired_block_interval(&gains, 5, 100, 42)
    );
    assert!(research::paired_block_interval(&gains[..4], 5, 100, 42).is_none());
}
#[test]
fn invalid_config_is_rejected() {
    let mut i = fixture();
    i.config.ridge = f64::INFINITY;
    assert!(validation::validate(&i).is_err());
}
#[test]
fn invalid_bar_is_rejected() {
    let mut i = fixture();
    i.dataset.bars[0].available_at = i.dataset.bars[0].start;
    assert!(validation::validate(&i).is_err());
}
#[test]
fn duplicate_bar_conflict_is_rejected() {
    let mut i = fixture();
    let mut b = i.dataset.bars[0].clone();
    b.volume += 1;
    i.dataset.bars.push(b);
    assert!(validation::validate(&i).is_err());
}
#[test]
fn no_required_marketimmune_dependency() {
    let manifest = include_str!("../Cargo.toml");
    assert!(!manifest.contains("marketimmune"));
}
#[test]
fn all_training_labels_have_matured() {
    let i = fixture();
    let r = engine::run(&i).unwrap();
    assert!(r.trials[0].decisions.iter().any(|d| d.prediction.is_some()));
    for t in r.trials {
        for d in t.decisions {
            assert!(d.trained_through.is_none_or(|ready| ready <= d.at));
        }
    }
}
#[test]
fn future_data_mutations_do_not_rewrite_snapshots() {
    let i = fixture();
    let cutoff = i.dataset.sessions[10].decision;
    let expected = view(&i, cutoff);
    for n in 0..1000 {
        let mut changed = i.clone();
        let mut f = changed.dataset.facts[0].clone();
        f.id = format!("future-{n}");
        f.revision = 100 + n;
        f.value_micros += n as i64;
        f.availability.modeled_available_at = Some(cutoff + DAY);
        changed.dataset.facts.push(f);
        assert_eq!(expected, view(&changed, cutoff));
    }
}
#[test]
fn data_coverage_reports_gaps() {
    let i = fixture();
    let c = engine::coverage(&i).unwrap();
    assert!(c.missing_minutes["MU"] > 0);
    assert!(c.independent_release_dates < c.bars_by_symbol["MU"]);
}
#[test]
fn dataframe_order_is_not_model_order() {
    let i = fixture();
    let cutoff = i.dataset.sessions[35].decision;
    let base = engine::run(&i).unwrap();
    let mut reversed = i.clone();
    reversed.dataset.facts.reverse();
    reversed.dataset.bars.reverse();
    let other = engine::run(&reversed).unwrap();
    assert_eq!(
        hindsight_economic::audit::signature(&base, cutoff),
        hindsight_economic::audit::signature(&other, cutoff)
    );
}
#[test]
fn empty_graph_omits_network_trial() {
    let mut i = fixture();
    i.dataset.relationships.clear();
    assert_eq!(engine::run(&i).unwrap().trials.len(), 3);
}
#[test]
fn fact_context_is_order_independent() {
    let a: BTreeMap<_, _> = [("a", "1"), ("b", "2")].into_iter().collect();
    let b: BTreeMap<_, _> = [("b", "2"), ("a", "1")].into_iter().collect();
    assert_eq!(engine::digest(&a).unwrap(), engine::digest(&b).unwrap());
}

#[test]
fn duplicate_order_id_cannot_double_account() {
    let i = fixture();
    let t = i.dataset.sessions[2].decision;
    let mut a = Account::new(i.config.initial_cash_micros);
    a.rebalance(10, t, 1, &i.config, &Market::new(&i.dataset))
        .unwrap();
    let before = a.clone();
    assert!(a
        .rebalance(20, t, 1, &i.config, &Market::new(&i.dataset))
        .is_err());
    assert_eq!(a, before);
}
#[test]
fn intraday_corporate_actions_fail_explicitly() {
    let mut i = fixture();
    let t = i.dataset.sessions[3].decision;
    i.dataset.corporate_actions.push(CorporateAction {
        id: "intraday".into(),
        symbol: "MU".into(),
        effective_at: t,
        split_numerator: 2,
        split_denominator: 1,
        dividend_per_share_micros: 0,
        dividend_pay_at: None,
    });
    assert!(validation::validate(&i).is_err());
}
