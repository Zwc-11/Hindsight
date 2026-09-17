use crate::{model::*, sources};
use rusqlite::{params, OptionalExtension};
use serde_json::{json, Value};
use std::time::{Duration, Instant};

pub fn expand_pending(store: &Store) -> Result<usize> {
    let c = store.connect()?;
    let row:Option<(String,String,String)>=c.query_row("SELECT id,job_json,page_json FROM discovery_outbox WHERE expanded_at IS NULL ORDER BY id LIMIT 1",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    let Some((id, j, p)) = row else { return Ok(0) };
    let job: Job = serde_json::from_str(&j)?;
    let page: PageResult = serde_json::from_str(&p)?;
    let n = sources::expand(store, &job, &page)?;
    c.execute(
        "UPDATE discovery_outbox SET expanded_at=?2 WHERE id=?1",
        params![id, now()],
    )?;
    Ok(n + 1)
}
pub fn step(store: &Store) -> Result<bool> {
    if store.refresh_due()? {
        return Ok(true);
    }
    if store.analysis_step()? {
        return Ok(true);
    }
    if expand_pending(store)? > 0 {
        return Ok(true);
    }
    let Some(job) = store.claim()? else {
        return Ok(false);
    };
    println!(
        "{} {} {} {}",
        chrono::Utc::now().to_rfc3339(),
        job.provider,
        job.kind,
        job.resource
    );
    let result = sources::request(store, &job).and_then(|(capture, page)| {
        store.commit_page(&job, &capture, &page)?;
        println!(
            "committed {} received records; {} normalized; {} series; next={}",
            page.received_rows,
            page.observations.len(),
            page.series.len(),
            page.next.is_some()
        );
        Ok(())
    });
    if let Err(error) = result {
        eprintln!("job {}: {}", &job.id[..12], error);
        store.fail(&job, &error)?;
    }
    Ok(true)
}
fn own_worker(store: &Store) -> Result<std::fs::File> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(store.root.join("worker.lock"))?;
    fs2::FileExt::try_lock_exclusive(&file)
        .map_err(|_| Error::Conflict("Another worker owns this local data catalog".into()))?;
    store.connect()?.execute("UPDATE experiments SET state='queued',error='Interrupted worker; pinned request retained' WHERE state='running' AND cancel_requested=0",[])?;
    Ok(file)
}
pub fn run(store: &Store, max_steps: usize, max_seconds: u64) -> Result<Value> {
    let _owner = own_worker(store)?;
    let started = Instant::now();
    let mut steps = 0;
    while steps < max_steps && started.elapsed().as_secs() < max_seconds {
        if !step(store)? {
            break;
        }
        steps += 1;
    }
    Ok(
        json!({"steps":steps,"elapsed_seconds":started.elapsed().as_secs_f64(),"remaining":store.status()?,"lifecycle":"bounded worker exited; use serve --worker for supervised updates"}),
    )
}
pub async fn supervised(store: Store, stop: std::sync::Arc<std::sync::atomic::AtomicBool>) {
    let _owner = match own_worker(&store) {
        Ok(file) => file,
        Err(e) => {
            eprintln!("Worker start failed: {e}");
            return;
        }
    };
    while !stop.load(std::sync::atomic::Ordering::Relaxed) {
        let copy = store.clone();
        let result = tokio::task::spawn_blocking(move || step(&copy)).await;
        let wait = match result {
            Ok(Ok(true)) => 10,
            Ok(Ok(false)) => 1000,
            other => {
                eprintln!("worker error: {other:?}");
                3000
            }
        };
        tokio::time::sleep(Duration::from_millis(wait)).await;
    }
}
