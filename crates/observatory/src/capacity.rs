//! Reproducible synthetic storage/query capacity exercise, never merged into the real catalog.
use crate::{model::*, storage};
use arrow_array::{ArrayRef, Float64Array, Int64Array, RecordBatch, UInt32Array, UInt64Array};
use arrow_schema::{DataType, Field, Schema};
use parquet::{
    arrow::{arrow_reader::ParquetRecordBatchReaderBuilder, ArrowWriter, ProjectionMask},
    basic::Compression,
    file::properties::WriterProperties,
};
use serde_json::{json, Value};
use std::{
    fs::{self, File},
    path::Path,
    sync::Arc,
    time::Instant,
};

fn quantiles(mut values: Vec<f64>) -> Value {
    values.sort_by(f64::total_cmp);
    let q = |p: f64| values[((values.len() - 1) as f64 * p).round() as usize];
    json!({"samples":values.len(),"p50_ms":q(0.5),"p95_ms":q(0.95),"p99_ms":q(0.99)})
}
fn series(index: usize) -> Series {
    Series {
        id: format!("CAPACITY:{index:05}"),
        provider: "worldbank".into(),
        code: format!("CAPACITY:{index:05}"),
        name: format!("Synthetic capacity series {index:05}"),
        entity: "SYNTHETIC_CAPACITY_ONLY".into(),
        unit: "synthetic unit".into(),
        frequency: "daily".into(),
        description: "Synthetic performance fixture; not observed economic data.".into(),
        source_url: "https://api.worldbank.org/fixture-only-not-a-real-endpoint".into(),
        metadata: json!({"synthetic":true}),
    }
}
fn commit(
    store: &Store,
    series: Vec<Series>,
    observations: Vec<Observation>,
    part: usize,
) -> Result<()> {
    let request = json!({"synthetic_capacity_fixture":true,"partition":part});
    let id = store.enqueue(
        "worldbank",
        "capacity_fixture",
        &format!("part-{part}"),
        &request,
        now(),
        1,
    )?;
    let job = store
        .claim()?
        .ok_or_else(|| invalid("Fixture job claim failed"))?;
    if job.id != id {
        return Err(invalid("Capacity fixture unexpectedly contains other work"));
    }
    let bytes =
        serde_json::to_vec(&json!({"synthetic":true,"series":series,"observations":observations}))?;
    let sha = hash(&bytes);
    let path = format!("raw/{sha}.body");
    storage::publish(&store.root.join(&path), &bytes)?;
    let at = now();
    let capture = Capture {
        id: format!("capacity-{part}"),
        provider: "worldbank".into(),
        url: "https://api.worldbank.org/fixture-only-not-a-real-endpoint".into(),
        sha256: sha,
        path,
        bytes: bytes.len() as u64,
        request_started_at: at,
        response_headers_at: at,
        response_completed_at: at,
        etag: None,
        last_modified: None,
        representation: "synthetic_capacity_fixture".into(),
        original_sha256: None,
    };
    let page = PageResult {
        complete_scope: true,
        received_rows: observations.len() as i64,
        series,
        observations,
        next: None,
        advertised_total: None,
        note: "Synthetic capacity fixtures only; no actual source request".into(),
        children: vec![],
    };
    store.commit_page(&job, &capture, &page)?;
    Ok(())
}
fn facts(store: &Store) -> Result<Value> {
    let started = Instant::now();
    for batch in 0..10 {
        commit(
            store,
            (batch * 1000..(batch + 1) * 1000).map(series).collect(),
            vec![],
            batch,
        )?;
    }
    let day = chrono::NaiveDate::from_ymd_opt(2020, 1, 1).ok_or_else(|| invalid("Fixture date"))?;
    for batch in 0..20 {
        let mut observations = Vec::with_capacity(5000);
        for k in batch * 5000..(batch + 1) * 5000 {
            let natural = k % 50000;
            let entity = natural / 500;
            let period = natural % 500;
            let revision = k / 50000;
            let date = day
                .checked_add_days(chrono::Days::new(period as u64))
                .ok_or_else(|| invalid("Fixture date overflow"))?;
            let publication = date
                .and_hms_opt(12, 0, 0)
                .ok_or_else(|| invalid("Fixture clock"))?
                .and_utc()
                .timestamp_millis()
                + revision as i64 * 86_400_000;
            observations.push(Observation {
                series_id: format!("CAPACITY:{entity:05}"),
                period_start: date.to_string(),
                period_end: date.to_string(),
                value: Some((period as f64).sin() + revision as f64),
                unit: "synthetic unit".into(),
                dimensions: json!({"fixture":true}),
                published_at: Some(publication),
                reconstructed_at: Some(publication + 1000),
                source_order: Some(revision as i64),
                precision: "synthetic_instant".into(),
                quality: "synthetic_capacity_only".into(),
                extras: json!({"synthetic":true}),
            });
        }
        commit(store, vec![], observations, batch + 10)?;
    }
    Ok(
        json!({"versioned_facts":100000,"distinct_fact_keys":50000,"catalog_series":10000,"seconds":started.elapsed().as_secs_f64()}),
    )
}
fn bars(out: &Path, count: usize) -> Result<Value> {
    let started = Instant::now();
    let schema = Arc::new(Schema::new(vec![
        Field::new("instrument_id", DataType::UInt32, false),
        Field::new("start_ms", DataType::Int64, false),
        Field::new("end_ms", DataType::Int64, false),
        Field::new("open", DataType::Float64, false),
        Field::new("high", DataType::Float64, false),
        Field::new("low", DataType::Float64, false),
        Field::new("close", DataType::Float64, false),
        Field::new("volume", DataType::UInt64, false),
    ]));
    let file = File::create(out.join("synthetic-minute-bars.parquet"))?;
    let properties = WriterProperties::builder()
        .set_compression(Compression::ZSTD(Default::default()))
        .set_max_row_group_size(128000)
        .set_created_by("Hindsight synthetic capacity exercise; NOT market data".into())
        .build();
    let mut writer = ArrowWriter::try_new(file, schema.clone(), Some(properties))?;
    let mut state = 41u64;
    let mut minimum = f64::INFINITY;
    let mut maximum = f64::NEG_INFINITY;
    for start in (0..count).step_by(64000) {
        let n = (count - start).min(64000);
        let mut instrument = Vec::with_capacity(n);
        let mut times = Vec::with_capacity(n);
        let mut ends = Vec::with_capacity(n);
        let mut open = Vec::with_capacity(n);
        let mut high = Vec::with_capacity(n);
        let mut low = Vec::with_capacity(n);
        let mut close = Vec::with_capacity(n);
        let mut volume = Vec::with_capacity(n);
        for index in start..start + n {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            let p = 50.0 + (state >> 32) as f64 / u32::MAX as f64 * 100.0;
            minimum = minimum.min(p);
            maximum = maximum.max(p);
            let time = 1_577_836_800_000 + (index / 100) as i64 * 60000;
            instrument.push((index % 100) as u32);
            times.push(time);
            ends.push(time + 60000);
            open.push(p - 0.01);
            high.push(p + 0.03);
            low.push(p - 0.04);
            close.push(p);
            volume.push((state % 50000) + 1);
        }
        let arrays: Vec<ArrayRef> = vec![
            Arc::new(UInt32Array::from(instrument)),
            Arc::new(Int64Array::from(times)),
            Arc::new(Int64Array::from(ends)),
            Arc::new(Float64Array::from(open)),
            Arc::new(Float64Array::from(high)),
            Arc::new(Float64Array::from(low)),
            Arc::new(Float64Array::from(close)),
            Arc::new(UInt64Array::from(volume)),
        ];
        writer.write(&RecordBatch::try_new(schema.clone(), arrays)?)?;
    }
    writer.close()?;
    File::open(out.join("synthetic-minute-bars.parquet"))?.sync_all()?;
    let size = fs::metadata(out.join("synthetic-minute-bars.parquet"))?.len();
    let seconds = started.elapsed().as_secs_f64();
    let scan = Instant::now();
    let builder = ParquetRecordBatchReaderBuilder::try_new(File::open(
        out.join("synthetic-minute-bars.parquet"),
    )?)?;
    let rowgroups = builder.metadata().num_row_groups();
    let projection = ProjectionMask::roots(builder.parquet_schema(), [6]);
    let reader = builder
        .with_projection(projection)
        .with_batch_size(8192)
        .build()?;
    let (mut rows, mut scanned_min, mut scanned_max) = (0usize, f64::INFINITY, f64::NEG_INFINITY);
    for batch in reader {
        let batch = batch?;
        let values = batch
            .column(0)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or_else(|| invalid("Unexpected capacity scan schema"))?;
        rows += batch.num_rows();
        for v in values.values() {
            scanned_min = scanned_min.min(*v);
            scanned_max = scanned_max.max(*v);
        }
    }
    if rows != count || scanned_min != minimum || scanned_max != maximum {
        return Err(invalid(
            "Capacity full scan did not reproduce written rows/extrema",
        ));
    }
    Ok(
        json!({"synthetic_minute_rows":count,"instruments":100,"parquet_bytes":size,"row_groups":rowgroups,"build_seconds":seconds,"column_projected_full_scan_seconds":scan.elapsed().as_secs_f64(),"verified_rows":rows,"minimum":scanned_min,"maximum":scanned_max,"batch_cap":64000,"projection":"close column, all row groups","not_claimed":"This storage scan is not a 10M-row HTTP/PIT serving benchmark or a realistic exchange calendar."}),
    )
}
pub fn run(out: &Path, count: usize) -> Result<Value> {
    if out.exists() {
        return Err(invalid("Capacity output directory must be new"));
    }
    if !(10000..=10_000_000).contains(&count) {
        return Err(invalid(
            "Capacity row budget must be between 10000 and 10000000",
        ));
    }
    let parent = out.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let reserve = (fs2::total_space(parent)? * 15 / 100).max(20 * 1024 * 1024 * 1024);
    if fs2::available_space(parent)? < reserve + 12 * 1024 * 1024 * 1024 {
        return Err(Error::Resource(
            "Capacity test needs 12GiB of headroom above the reserve".into(),
        ));
    }
    fs::create_dir(out)?;
    storage::publish(&out.join("SYNTHETIC_ONLY.txt"),b"This directory contains artificial capacity tests, never real source observations. Do not import into a production catalog.")?;
    let store = Store::open(out.join("fixture-store"))?;
    let fact_result = facts(&store)?;
    let bar_result = bars(out, count)?;
    let scope = store.scope(None, None, "observed", "research")?;
    let mut workloads = Vec::new();
    for clients in [1usize, 8] {
        for kind in ["catalog", "history", "reconstructed"] {
            let mut handles = Vec::new();
            for client in 0..clients {
                let store = store.clone();
                let scope = scope.clone();
                handles.push(std::thread::spawn(move || -> Result<Vec<f64>> {
                    let mut elapsed = Vec::new();
                    for i in 0..60 {
                        let start = Instant::now();
                        let series = format!("CAPACITY:{:05}", (client * 13 + i) % 100);
                        match kind {
                            "catalog" => {
                                store.list_series(&scope, "capacity", "worldbank", "", 50)?;
                            }
                            "history" => {
                                store.points(&series, &scope, "2020", "2023", 600)?;
                            }
                            _ => {
                                let prior = Scope {
                                    as_of: 1_610_668_800_000,
                                    mode: "reconstructed".into(),
                                    ..scope.clone()
                                };
                                store.points(&series, &prior, "2020", "2023", 600)?;
                            }
                        }
                        elapsed.push(start.elapsed().as_secs_f64() * 1000.);
                    }
                    Ok(elapsed)
                }));
            }
            let mut values = Vec::new();
            for h in handles {
                values.extend(
                    h.join()
                        .map_err(|_| invalid("Capacity worker panicked"))??,
                );
            }
            workloads.push(
                json!({"workload":kind,"concurrent_readers":clients,"timing":quantiles(values)}),
            );
        }
    }
    let integrity = store.verify_objects()?;
    let report = json!({"synthetic_only":true,"facts":fact_result,"bars":bar_result,"native_query_workloads":workloads,"integrity":integrity,"cache":"OS cache not purged; actual native SQLite snapshot queries without a result cache","platform":{"architecture":std::env::consts::ARCH,"os":std::env::consts::OS},"scope_limits":["100k versioned-fact native query timings are separate from the 10M-row Parquet full scan.","No API, broker, latency-to-market, profitability or distributed-scaling claim.","No production catalog was modified by this capacity fixture."]});
    storage::publish(
        &out.join("capacity-report.json"),
        &serde_json::to_vec_pretty(&report)?,
    )?;
    Ok(report)
}
