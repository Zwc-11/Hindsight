//! Process-death drills operate only on marked temporary fixture stores.
mod support;
use hindsight_observatory::{model::*, storage};
use serde_json::json;
use std::{
    fs,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use support::*;
fn recovery_page() -> PageResult {
    let observations = (0..50000)
        .map(|i| {
            let mut o = observation(i as f64, 2);
            o.dimensions = json!({"recovery_fixture_index":i});
            o
        })
        .collect();
    page(observations)
}
#[test]
#[ignore = "subprocess driver invoked only by the three process-death tests"]
fn crash_writer_child() {
    let Some(root) = std::env::var_os("HINDSIGHT_RECOVERY_FIXTURE") else {
        return;
    };
    let root = std::path::PathBuf::from(root);
    assert!(root.join("RECOVERY_FIXTURE_ONLY").is_file());
    let phase = std::env::var("HINDSIGHT_RECOVERY_PHASE").unwrap();
    let store = Store::open(&root).unwrap();
    if phase == "commit" {
        let j = job(&store);
        let c = capture(&store);
        let p = recovery_page();
        store.commit_page(&j, &c, &p).unwrap();
        fs::write(
            root.join("unexpected-completion"),
            b"commit finished before kill",
        )
        .unwrap();
    } else {
        let bytes = if phase == "archive" {
            b"PK\x03\x04truncated source archive".as_slice()
        } else {
            b"{\"incomplete_page\":[1,2,".as_slice()
        };
        fs::write(root.join("staging/interrupted.part"), bytes).unwrap();
        fs::write(root.join("child-ready"), b"ready").unwrap();
    }
    loop {
        std::thread::park_timeout(Duration::from_secs(60));
    }
}
fn drill(phase: &str) {
    let (_dir, store) = setup();
    fs::write(
        store.root.join("RECOVERY_FIXTURE_ONLY"),
        b"synthetic isolated fault injection",
    )
    .unwrap();
    commit(&store, &page(vec![observation(10., 1)]));
    let snapshot = store.snapshot_id().unwrap();
    let initial = fs::read_dir(store.root.join("canonical")).unwrap().count();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args(["--ignored", "--exact", "crash_writer_child", "--nocapture"])
        .env("HINDSIGHT_RECOVERY_FIXTURE", &store.root)
        .env("HINDSIGHT_RECOVERY_PHASE", phase)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let started = Instant::now();
    loop {
        let reached = if phase == "commit" {
            fs::read_dir(store.root.join("canonical"))
                .unwrap()
                .filter_map(|e| e.ok())
                .filter(|e| e.path().extension().is_some_and(|s| s == "parquet"))
                .count()
                > initial
        } else {
            store.root.join("child-ready").exists()
        };
        if reached {
            break;
        }
        if let Some(exit) = child.try_wait().unwrap() {
            panic!("Recovery child exited before fault point: {exit}");
        }
        if started.elapsed() > Duration::from_secs(45) {
            let _ = child.kill();
            let _ = child.wait();
            panic!("Recovery fault point timed out");
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    child.kill().unwrap();
    let exit = child.wait().unwrap();
    assert!(!exit.success());
    assert!(
        !store.root.join("unexpected-completion").exists(),
        "Kill missed the intended pre-commit boundary"
    );
    assert_eq!(
        store.snapshot_id().unwrap(),
        snapshot,
        "Uncommitted metadata leaked into the reader snapshot"
    );
    assert_eq!(store.status().unwrap()["observation_versions"], 1);
    store
        .connect()
        .unwrap()
        .execute("UPDATE jobs SET lease_until=0 WHERE state='running'", [])
        .unwrap();
    let reconciliation = store.reconcile().unwrap();
    assert_eq!(reconciliation["deleted_objects"], 0);
    if phase == "archive" {
        assert!(storage::inspect_zip(
            &store.root.join("staging/interrupted.part"),
            1000000,
            1000000
        )
        .is_err());
    }
    let p = if phase == "commit" {
        recovery_page()
    } else {
        page(vec![observation(20., 2)])
    };
    let pending = store.claim().unwrap();
    if let Some(j) = pending {
        store.commit_page(&j, &capture(&store), &p).unwrap();
    } else {
        commit(&store, &p);
    }
    commit(&store, &p);
    let expected = if phase == "commit" { 50001 } else { 2 };
    assert_eq!(store.status().unwrap()["observation_versions"], expected);
    assert_eq!(store.verify_objects().unwrap()["passed"], true);
    println!(
        "{}",
        json!({"phase":phase,"child_forcibly_terminated":true,"accepted_before":1,"accepted_after_resume":expected,"duplicate_replay_added_versions":0,"retained_orphans":reconciliation["retained_unreferenced_objects"],"production_store_touched":false})
    );
}
#[test]
fn interrupted_page_keeps_accepted_history() {
    drill("page");
}
#[test]
fn interrupted_archive_is_not_published_as_success() {
    drill("archive");
}
#[test]
fn death_between_file_publication_and_catalog_commit_is_recoverable() {
    drill("commit");
}
