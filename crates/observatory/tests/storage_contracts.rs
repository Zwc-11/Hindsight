mod support;
use hindsight_observatory::{analysis, fetch, model::*, storage};
use serde_json::json;
use std::{fs, io::Write};
use support::*;

#[test]
fn empty_real_catalog_never_falls_back_to_fixture() {
    let (_d, s) = setup();
    let v = s.status().unwrap();
    assert_eq!(v["observation_versions"], 0);
    assert_eq!(v["default_data_kind"], "real");
    assert_eq!(v["complete_all"], false);
}
#[test]
fn migrations_can_run_twice_without_losing_data() {
    let (d, s) = setup();
    commit(&s, &page(vec![observation(10.0, 1)]));
    let reopened = Store::open(d.path()).unwrap();
    assert_eq!(
        reopened
            .points("TEST:SERIES", &scope(&s), "2024", "2025", 10)
            .unwrap()
            .len(),
        1
    );
}
#[test]
fn commit_publishes_parquet_and_verifiable_manifest() {
    let (_d, s) = setup();
    commit(&s, &page(vec![observation(10.0, 1)]));
    assert_eq!(s.verify_objects().unwrap()["passed"], true);
    assert_eq!(
        s.connect()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM partitions", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
}
#[test]
fn different_capture_of_same_semantic_fact_is_not_revision() {
    let (_d, s) = setup();
    let p = page(vec![observation(10.0, 1)]);
    commit(&s, &p);
    commit(&s, &p);
    assert_eq!(s.status().unwrap()["observation_versions"], 1);
    assert_eq!(s.status().unwrap()["captures"], 2);
}
#[test]
fn correction_is_visible_only_after_publication() {
    let (_d, s) = setup();
    let a = observation(10.0, 1);
    let b = observation(25.0, 10);
    commit(&s, &page(vec![a.clone()]));
    commit(&s, &page(vec![b.clone()]));
    let old = s
        .scope(None, a.reconstructed_at, "reconstructed", "research")
        .unwrap();
    let current = s
        .scope(None, b.reconstructed_at, "reconstructed", "research")
        .unwrap();
    assert_eq!(
        s.points("TEST:SERIES", &old, "2024", "2025", 10).unwrap()[0].value,
        Some(10.0)
    );
    assert_eq!(
        s.points("TEST:SERIES", &current, "2024", "2025", 10)
            .unwrap()[0]
            .value,
        Some(25.0)
    );
}
#[test]
fn historical_snapshot_excludes_later_storage_commit() {
    let (_d, s) = setup();
    let first = commit(&s, &page(vec![observation(10.0, 1)]));
    commit(&s, &page(vec![observation(25.0, 10)]));
    let old = s.scope(Some(first), None, "observed", "research").unwrap();
    assert_eq!(
        s.points("TEST:SERIES", &old, "2024", "2025", 10).unwrap()[0].value,
        Some(10.0)
    );
}
#[test]
fn source_order_not_reingestion_order() {
    let (_d, s) = setup();
    commit(&s, &page(vec![observation(25.0, 10)]));
    commit(&s, &page(vec![observation(10.0, 1)]));
    assert_eq!(
        s.points("TEST:SERIES", &scope(&s), "2024", "2025", 10)
            .unwrap()[0]
            .value,
        Some(25.0)
    );
}
#[test]
fn same_revision_conflict_rolls_back_catalog() {
    let (_d, s) = setup();
    commit(&s, &page(vec![observation(10.0, 1)]));
    let j = job(&s);
    let c = capture(&s);
    assert!(s
        .commit_page(&j, &c, &page(vec![observation(25.0, 1)]))
        .is_err());
    assert_eq!(s.status().unwrap()["observation_versions"], 1);
}
#[test]
fn future_metadata_is_withheld_from_reconstructed_view() {
    let (_d, s) = setup();
    let p = observation(10.0, 1);
    commit(&s, &page(vec![p.clone()]));
    let view = s
        .scope(None, p.reconstructed_at, "reconstructed", "display")
        .unwrap();
    let m = s.series("TEST:SERIES", &view).unwrap();
    assert_eq!(m.metadata["historical_metadata_withheld"], true);
    assert!(!m.name.contains("Synthetic fixture measurement"));
    let points = s.points("TEST:SERIES", &view, "2024", "2025", 10).unwrap();
    let public = hindsight_observatory::query::public_point(&points[0], &view).unwrap();
    assert!(public.get("observed_at").is_none());
    assert!(public.get("capture_id").is_none());
}
#[test]
fn policies_block_fred_and_redistribution() {
    let (_d, s) = setup();
    assert!(s.policy_check("fred", "archive").is_err());
    assert!(s.policy_check("worldbank", "redistribution").is_err());
    let id = s
        .enqueue("fred", "test", "all", &json!({}), now(), 0)
        .unwrap();
    assert_eq!(s.job(&id).unwrap().state, "blocked_permission");
    assert!(s.claim().unwrap().is_none());
}
#[test]
fn paused_worker_cannot_commit() {
    let (_d, s) = setup();
    let j = job(&s);
    s.action(&j.id, "pause").unwrap();
    assert!(s
        .commit_page(&j, &capture(&s), &page(vec![observation(10.0, 1)]))
        .is_err());
    assert_eq!(s.snapshot_id().unwrap(), 0);
}
#[test]
fn expired_lease_is_fenced_after_reclaim() {
    let (_d, s) = setup();
    let j = job(&s);
    s.connect()
        .unwrap()
        .execute("UPDATE jobs SET lease_until=0 WHERE id=?1", [&j.id])
        .unwrap();
    let next = s.claim().unwrap().unwrap();
    assert_ne!(j.lease_token, next.lease_token);
    assert!(s.alive(&j).is_err());
    assert!(s.alive(&next).is_ok());
}
#[test]
fn cyclic_page_does_not_publish_a_snapshot() {
    let (_d, s) = setup();
    let j = job(&s);
    let mut p = page(vec![observation(1.0, 1)]);
    p.next = Some(json!({}));
    assert!(s.commit_page(&j, &capture(&s), &p).is_err());
    assert_eq!(s.snapshot_id().unwrap(), 0);
}
#[test]
fn mismatched_provider_total_does_not_claim_completion() {
    let (_d, s) = setup();
    let j = job(&s);
    let mut p = page(vec![observation(1.0, 1)]);
    p.advertised_total = Some(10);
    assert!(s.commit_page(&j, &capture(&s), &p).is_err());
    assert_eq!(s.snapshot_id().unwrap(), 0);
}
#[test]
fn shared_rate_limit_reserves_distinct_slots() {
    let (_d, s) = setup();
    let first = s.reserve_request("worldbank").unwrap();
    let next = s.reserve_request("worldbank").unwrap();
    assert!(next >= first + 900);
}
#[test]
fn transient_failures_are_retryable_and_auth_is_not() {
    let (_d, s) = setup();
    let j = job(&s);
    s.fail(
        &j,
        &Error::Http {
            status: 429,
            retry_after_ms: 2000,
        },
    )
    .unwrap();
    assert_eq!(s.job(&j.id).unwrap().state, "retry_wait");
    let j = job(&s);
    s.fail(
        &j,
        &Error::Http {
            status: 401,
            retry_after_ms: 0,
        },
    )
    .unwrap();
    assert_eq!(s.job(&j.id).unwrap().state, "blocked_auth");
}
#[test]
fn bytes_with_wrong_hash_are_never_published() {
    let (_d, s) = setup();
    let j = job(&s);
    let c = capture(&s);
    fs::write(s.root.join(&c.path), b"corrupt").unwrap();
    assert!(s
        .commit_page(&j, &c, &page(vec![observation(1.0, 1)]))
        .is_err());
    assert_eq!(s.snapshot_id().unwrap(), 0);
}
#[test]
fn verification_detects_corruption_after_commit() {
    let (_d, s) = setup();
    commit(&s, &page(vec![observation(1.0, 1)]));
    let path: String = s
        .connect()
        .unwrap()
        .query_row("SELECT path FROM partitions LIMIT 1", [], |r| r.get(0))
        .unwrap();
    fs::write(s.root.join(path), b"corrupt").unwrap();
    assert_eq!(s.verify_objects().unwrap()["passed"], false);
}
#[test]
fn backup_can_reopen_and_preserve_historical_queries() {
    let (d, s) = setup();
    commit(&s, &page(vec![observation(1.0, 1)]));
    let path = d.path().join("backup");
    assert_eq!(s.backup(&path).unwrap()["passed"], true);
    let restored = Store::open(path).unwrap();
    assert_eq!(
        restored
            .points("TEST:SERIES", &scope(&restored), "2024", "2025", 10)
            .unwrap()[0]
            .value,
        Some(1.0)
    );
}
#[test]
fn archive_traversal_and_truncation_rejected() {
    for p in ["../x", "/tmp/x", "a/../../x", "C:\\x", "a\\b"] {
        assert!(storage::safe_member(p).is_err());
    }
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("bad.zip");
    fs::write(&p, b"PK\x03\x04truncated").unwrap();
    assert!(storage::inspect_zip(&p, 1024, 1024).is_err());
}
#[test]
fn valid_archive_is_bounded_and_crc_checked() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("ok.zip");
    let mut z = zip::ZipWriter::new(fs::File::create(&p).unwrap());
    z.start_file("data.json", zip::write::SimpleFileOptions::default())
        .unwrap();
    z.write_all(b"{\"test\":true}").unwrap();
    z.finish().unwrap();
    assert_eq!(storage::inspect_zip(&p, 1000, 1000).unwrap().len(), 1);
    assert!(storage::inspect_zip(&p, 2, 2).is_err());
}
#[test]
fn urls_and_echoed_credentials_are_sanitized() {
    let u = reqwest::Url::parse("https://api.eia.gov/v2/?api_key=secret-value&offset=2").unwrap();
    assert!(!fetch::redact_url(&u).contains("secret-value"));
    let mut v = json!({"api_key":"secret-value","nested":{"url":"key=secret-value","data":[1,2]}});
    fetch::scrub(&mut v, &["secret-value".into()]);
    assert!(!v.to_string().contains("secret-value"));
    for u in [
        "http://api.worldbank.org/v2/",
        "https://127.0.0.1/",
        "https://api.worldbank.org.evil.invalid/",
        "https://user:pass@api.worldbank.org/",
    ] {
        assert!(fetch::validate_url(&reqwest::Url::parse(u).unwrap()).is_err());
    }
}
#[test]
fn source_contexts_are_not_overwritten() {
    let (_d, s) = setup();
    let a = observation(10.0, 1);
    let mut b = a.clone();
    b.dimensions = json!({"segment":"different"});
    b.value = Some(20.0);
    commit(&s, &page(vec![a, b]));
    assert_eq!(
        s.points("TEST:SERIES", &scope(&s), "2024", "2025", 10)
            .unwrap()
            .len(),
        2
    );
}
#[test]
fn research_refuses_cross_purpose_and_duplicate_inputs() {
    let req = analysis::AnalysisRequest {
        scope: Scope {
            snapshot: 0,
            as_of: 0,
            mode: "observed".into(),
            purpose: "display".into(),
        },
        series_ids: vec!["x".into(), "x".into()],
        start: "2020".into(),
        end: "2024".into(),
        transform: "change".into(),
        kind: "dependence".into(),
        max_lag: 4,
        window: 12,
        controls: vec![],
    };
    assert!(req.validate().is_err());
}

#[test]
fn statistical_research_permission_is_not_model_training_permission() {
    let (_d, s) = setup();
    let mut second = observation(20.0, 1);
    second.series_id = "SECOND".into();
    commit(&s, &page(vec![observation(10.0, 1), second]));
    let mut policy = s.policy("worldbank").unwrap();
    policy.training = false;
    s.connect()
        .unwrap()
        .execute(
            "UPDATE source_policies SET payload=?1 WHERE id='worldbank'",
            [serde_json::to_string(&policy).unwrap()],
        )
        .unwrap();
    let req = analysis::AnalysisRequest {
        scope: scope(&s),
        series_ids: vec!["TEST:SERIES".into(), "SECOND".into()],
        start: "2020".into(),
        end: "2025".into(),
        transform: "change".into(),
        kind: "forecast".into(),
        max_lag: 4,
        window: 12,
        controls: vec![],
    };
    assert!(s.submit_analysis(&req).is_err());
    let mut descriptive = req;
    descriptive.kind = "dependence".into();
    assert!(s.submit_analysis(&descriptive).is_ok());
}
#[test]
fn policy_snapshot_checks_preserve_expiry_and_purpose() {
    let (_d, s) = setup();
    let policy = s.policy("worldbank").unwrap();
    assert!(policy.authorize("research", "2026-09-16").is_ok());
    assert!(policy.authorize("research", "2099-01-01").is_err());
    assert!(policy.authorize("redistribution", "2026-09-16").is_err());
}
