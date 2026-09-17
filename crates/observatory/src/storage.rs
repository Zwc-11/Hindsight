//! Canonical immutable Parquet partitions; SQLite is the catalog + rebuildable query index.
use crate::model::*;
use arrow_array::{ArrayRef, Float64Array, Int64Array, RecordBatch, StringArray};
use arrow_schema::{DataType, Field, Schema};
use parquet::{arrow::ArrowWriter, basic::Compression, file::properties::WriterProperties};
use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Component, Path},
    sync::Arc,
};

pub fn check_disk(root: &Path, additional: u64) -> Result<()> {
    let total = fs2::total_space(root)?;
    let free = fs2::available_space(root)?;
    let reserve = (total * 15 / 100).max(20 * 1024 * 1024 * 1024);
    if free < reserve.saturating_add(additional) {
        return Err(Error::Resource(format!(
            "blocked_resources: free {free} bytes; reserve {reserve}, next allocation {additional}"
        )));
    }
    Ok(())
}
pub fn publish(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| invalid("Missing output parent"))?;
    fs::create_dir_all(parent)?;
    if path.exists() {
        if fs::read(path)? == bytes {
            return Ok(());
        }
        return Err(Error::Conflict(
            "Refusing to overwrite content-addressed object".into(),
        ));
    }
    let temp = parent.join(format!(".{}.partial", uuid::Uuid::new_v4()));
    let mut f = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temp)?;
    f.write_all(bytes)?;
    f.sync_all()?;
    drop(f);
    match fs::hard_link(&temp, path) {
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            if fs::read(path)? != bytes {
                return Err(Error::Conflict(
                    "Publication race produced different bytes".into(),
                ));
            }
        }
        Err(e) => return Err(e.into()),
    }
    fs::remove_file(temp)?;
    File::open(parent)?.sync_all()?;
    Ok(())
}
pub fn file_hash(path: &Path) -> Result<String> {
    use sha2::{Digest, Sha256};
    let mut f = File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = [0; 65536];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(format!("{:x}", h.finalize()))
}
pub fn safe_member(name: &str) -> Result<std::path::PathBuf> {
    if name.contains('\\') || name.contains(':') || name.contains('\0') {
        return Err(invalid("Unsafe archive member name"));
    }
    let p = Path::new(name);
    if p.as_os_str().is_empty() || p.components().any(|c| !matches!(c, Component::Normal(_))) {
        return Err(invalid("Unsafe archive member path"));
    }
    Ok(p.to_path_buf())
}
/// Validate every member and CRC without extracting executable content. Bound expanded bytes.
pub fn inspect_zip(path: &Path, max_expanded: u64, max_member: u64) -> Result<Vec<(String, u64)>> {
    let f = File::open(path)?;
    let mut z = zip::ZipArchive::new(f).map_err(|_| invalid("Corrupt or truncated ZIP"))?;
    if z.len() > 100_000 {
        return Err(invalid("Archive entry budget exceeded"));
    }
    let mut out = vec![];
    let mut total = 0u64;
    for i in 0..z.len() {
        let mut member = z.by_index(i).map_err(|_| invalid("Invalid ZIP member"))?;
        if member.is_dir() {
            continue;
        }
        safe_member(member.name())?;
        if member.unix_mode().is_some_and(|m| m & 0o170000 == 0o120000) {
            return Err(invalid("Archive symlinks rejected"));
        }
        if member.size() > max_member {
            return Err(Error::Resource("Expanded member exceeds budget".into()));
        }
        total = total
            .checked_add(member.size())
            .ok_or_else(|| invalid("Archive size overflow"))?;
        if total > max_expanded {
            return Err(Error::Resource("Expanded archive exceeds budget".into()));
        }
        let mut sink = std::io::sink();
        let actual = std::io::copy(&mut member.by_ref().take(max_member + 1), &mut sink)?;
        if actual != member.size() {
            return Err(invalid("ZIP member length mismatch"));
        }
        out.push((member.name().into(), actual));
    }
    Ok(out)
}
fn write_parquet(
    path: &Path,
    observations: &[Observation],
    capture: &Capture,
    observed_at: i64,
    generation: i64,
) -> Result<()> {
    let schema = Arc::new(Schema::new(vec![
        Field::new("series_id", DataType::Utf8, false),
        Field::new("natural_key", DataType::Utf8, false),
        Field::new("period_start", DataType::Utf8, false),
        Field::new("period_end", DataType::Utf8, false),
        Field::new("value", DataType::Float64, true),
        Field::new("unit", DataType::Utf8, false),
        Field::new("published_at", DataType::Int64, true),
        Field::new("observed_at", DataType::Int64, false),
        Field::new("reconstructed_at", DataType::Int64, true),
        Field::new("generation", DataType::Int64, false),
        Field::new("capture_id", DataType::Utf8, false),
        Field::new("observation_json", DataType::Utf8, false),
    ]));
    let props = WriterProperties::builder()
        .set_compression(Compression::ZSTD(Default::default()))
        .set_max_row_group_size(8192)
        .build();
    let f = OpenOptions::new().write(true).create_new(true).open(path)?;
    let mut writer =
        ArrowWriter::try_new(f, schema.clone(), Some(props)).map_err(|e| invalid(e.to_string()))?;
    for chunk in observations.chunks(8192) {
        let keys: Vec<_> = chunk.iter().map(Observation::key).collect();
        let payload: Vec<_> = chunk
            .iter()
            .map(serde_json::to_string)
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let columns: Vec<ArrayRef> = vec![
            Arc::new(StringArray::from(
                chunk
                    .iter()
                    .map(|o| o.series_id.as_str())
                    .collect::<Vec<_>>(),
            )),
            Arc::new(StringArray::from(
                keys.iter().map(String::as_str).collect::<Vec<_>>(),
            )),
            Arc::new(StringArray::from(
                chunk
                    .iter()
                    .map(|o| o.period_start.as_str())
                    .collect::<Vec<_>>(),
            )),
            Arc::new(StringArray::from(
                chunk
                    .iter()
                    .map(|o| o.period_end.as_str())
                    .collect::<Vec<_>>(),
            )),
            Arc::new(Float64Array::from(
                chunk.iter().map(|o| o.value).collect::<Vec<_>>(),
            )),
            Arc::new(StringArray::from(
                chunk.iter().map(|o| o.unit.as_str()).collect::<Vec<_>>(),
            )),
            Arc::new(Int64Array::from(
                chunk.iter().map(|o| o.published_at).collect::<Vec<_>>(),
            )),
            Arc::new(Int64Array::from(vec![observed_at; chunk.len()])),
            Arc::new(Int64Array::from(
                chunk.iter().map(|o| o.reconstructed_at).collect::<Vec<_>>(),
            )),
            Arc::new(Int64Array::from(vec![generation; chunk.len()])),
            Arc::new(StringArray::from(vec![capture.id.as_str(); chunk.len()])),
            Arc::new(StringArray::from(
                payload.iter().map(String::as_str).collect::<Vec<_>>(),
            )),
        ];
        let batch =
            RecordBatch::try_new(schema.clone(), columns).map_err(|e| invalid(e.to_string()))?;
        writer.write(&batch).map_err(|e| invalid(e.to_string()))?;
    }
    writer.close().map_err(|e| invalid(e.to_string()))?;
    File::open(path)?.sync_all()?;
    Ok(())
}
impl Store {
    pub fn commit_page(&self, job: &Job, capture: &Capture, page: &PageResult) -> Result<i64> {
        self.alive(job)?;
        self.policy_check(&job.provider, "archive")?;
        if page.observations.len() > 100_000 || page.series.len() > 100_000 {
            return Err(Error::Resource(
                "Normalized page exceeds record budget".into(),
            ));
        }
        for o in &page.observations {
            o.validate()?;
        }
        if page.series.iter().any(|s| s.provider != job.provider) {
            return Err(invalid("Cross-provider normalization rejected"));
        }
        check_disk(&self.root, (page.observations.len() as u64) * 2048)?;
        if file_hash(&self.root.join(&capture.path))? != capture.sha256 {
            return Err(invalid("Raw object checksum mismatch before publication"));
        }
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let live: Option<(String, String, String)> = tx
            .query_row(
                "SELECT state,COALESCE(lease_token,''),cursor FROM jobs WHERE id=?1",
                [&job.id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        if live
            != Some((
                "running".into(),
                job.lease_token.clone().unwrap_or_default(),
                serde_json::to_string(&job.cursor)?,
            ))
        {
            return Err(Error::Cancelled(
                "Fenced worker: job state/checkpoint changed".into(),
            ));
        }
        let cursor_hash = hash(&serde_json::to_vec(&job.cursor)?);
        if let Some(next) = &page.next {
            let next_hash = hash(&serde_json::to_vec(next)?);
            let seen: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM job_checkpoints WHERE job_id=?1 AND cursor_hash=?2)",
                params![job.id, next_hash],
                |r| r.get(0),
            )?;
            if seen || next == &job.cursor {
                return Err(invalid("Cyclic pagination checkpoint"));
            }
        }
        let (previous_total, previous_received): (Option<i64>, i64) = tx.query_row(
            "SELECT advertised_total,received_rows FROM jobs WHERE id=?1",
            [&job.id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if previous_total.is_some()
            && page.advertised_total.is_some()
            && previous_total != page.advertised_total
        {
            return Err(Error::Conflict(
                "Provider total changed during frozen backfill; reconcile separately".into(),
            ));
        }
        let received = previous_received + page.received_rows;
        let total = previous_total.or(page.advertised_total);
        if page.next.is_none() && total.is_some_and(|n| n != received) {
            return Err(Error::Conflict(format!(
                "Provider total mismatch: received {received}, advertised {total:?}"
            )));
        }
        let at = now();
        tx.execute(
            "INSERT INTO dataset_snapshots(committed_at,description) VALUES(?1,?2)",
            params![
                at,
                format!("{} {} {}", job.provider, job.resource, cursor_hash)
            ],
        )?;
        let generation = tx.last_insert_rowid();
        tx.execute(
            "INSERT OR IGNORE INTO raw_objects VALUES(?1,?2,?3,?4)",
            params![
                capture.sha256,
                capture.path,
                capture.bytes as i64,
                capture.representation
            ],
        )?;
        let new_capture = tx.execute(
            "INSERT OR IGNORE INTO captures VALUES(?1,?2,?3,?4,?5)",
            params![
                capture.id,
                capture.provider,
                capture.sha256,
                capture.response_completed_at,
                serde_json::to_string(capture)?
            ],
        )?;
        for series in &page.series {
            tx.execute(
                "INSERT OR IGNORE INTO series VALUES(?1,?2,?3,?4)",
                params![series.id, series.provider, at, generation],
            )?;
            let payload = serde_json::to_string(series)?;
            let old:Option<String>=tx.query_row("SELECT payload FROM series_versions WHERE series_id=?1 ORDER BY generation DESC LIMIT 1",[&series.id],|r|r.get(0)).optional()?;
            if old.as_deref() != Some(&payload) {
                tx.execute(
                    "INSERT INTO series_versions VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                    params![
                        series.id,
                        generation,
                        at,
                        series.code,
                        series.name,
                        series.entity,
                        series.unit,
                        series.frequency,
                        payload
                    ],
                )?;
            }
            tx.execute(
                "INSERT OR IGNORE INTO entities VALUES(?1,'provider-defined',?1,?2,'{}')",
                params![series.entity, at],
            )?;
        }
        let mut inserted = Vec::new();
        for o in &page.observations {
            let semantic = serde_json::to_string(o)?;
            let id = o.semantic_id();
            let key = o.key();
            tx.execute(
                "INSERT OR IGNORE INTO observations VALUES(?1,?2,?3,?4,?5,?6)",
                params![
                    key,
                    o.series_id,
                    o.period_start,
                    o.period_end,
                    o.unit,
                    serde_json::to_string(&o.dimensions)?
                ],
            )?;
            if let Some(order) = o.source_order {
                let conflict:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM observation_versions WHERE natural_key=?1 AND source_order=?2 AND value IS NOT ?3)",params![key,order,o.value],|r|r.get(0))?;
                if conflict {
                    return Err(Error::Conflict(
                        "Conflicting values for the same source-resolved revision".into(),
                    ));
                }
            }
            let n=tx.execute("INSERT OR IGNORE INTO observation_versions VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17)",params![id,key,o.series_id,o.period_start,o.period_end,o.value,o.unit,o.published_at,at,o.reconstructed_at,o.source_order,capture.id,o.precision,o.quality,generation,serde_json::to_string(&o.extras)?,semantic])?;
            tx.execute(
                "INSERT OR IGNORE INTO observation_captures VALUES(?1,?2)",
                params![id, capture.id],
            )?;
            if n > 0 {
                inserted.push(o.clone());
            }
        }
        let mut partition_info = Value::Null;
        if !inserted.is_empty() {
            let staging = self
                .root
                .join("staging")
                .join(format!("{}.parquet", uuid::Uuid::new_v4()));
            write_parquet(&staging, &inserted, capture, at, generation)?;
            let sha = file_hash(&staging)?;
            let relative = format!("canonical/{}/{}/{}.parquet", job.provider, &sha[..2], sha);
            let dest = self.root.join(&relative);
            fs::create_dir_all(dest.parent().expect("canonical parent"))?;
            if !dest.exists() {
                fs::hard_link(&staging, &dest)?;
            }
            fs::remove_file(staging)?;
            File::open(dest.parent().expect("parent"))?.sync_all()?;
            let min = inserted.iter().map(|o| o.period_end.as_str()).min();
            let max = inserted.iter().map(|o| o.period_end.as_str()).max();
            tx.execute(
                "INSERT INTO partitions VALUES(?1,?2,?3,?4,?5,?6,?7,?8,'validated')",
                params![
                    sha,
                    job.id,
                    relative,
                    sha,
                    inserted.len() as i64,
                    min,
                    max,
                    generation
                ],
            )?;
            partition_info = json!({"path":relative,"sha256":sha,"rows":inserted.len(),"min_time":min,"max_time":max});
        }
        let manifest = json!({"schema_version":1,"snapshot":generation,"parent":generation-1,"capture":capture,"job_id":job.id,"cursor":job.cursor,"next_cursor":page.next,"partition":partition_info,"series":page.series,"observed_at":at,"cutoff_utc_ms":job.cutoff,"policy":self.policy(&job.provider)?,"note":page.note});
        let manifest_path = format!(
            "snapshots/{generation:012}-{}.json",
            hash(&serde_json::to_vec(&manifest)?)
        );
        publish(
            &self.root.join(&manifest_path),
            &serde_json::to_vec_pretty(&manifest)?,
        )?;
        tx.execute(
            "UPDATE dataset_snapshots SET manifest_path=?2 WHERE id=?1",
            params![generation, manifest_path],
        )?;
        tx.execute(
            "INSERT INTO job_checkpoints VALUES(?1,?2,?3,?4,?5)",
            params![
                job.id,
                cursor_hash,
                serde_json::to_string(&job.cursor)?,
                generation,
                at
            ],
        )?;
        let state = if page.next.is_some() {
            "queued"
        } else if page.complete_scope {
            "complete_for_declared_scope"
        } else {
            "validated"
        };
        tx.execute("UPDATE jobs SET state=?2,cursor=?3,lease_token=NULL,lease_until=NULL,rows=rows+?4,bytes=bytes+?5,updated_at=?6,advertised_total=?7,received_rows=?8,complete_through=CASE WHEN ?2='complete_for_declared_scope' THEN requested_end ELSE complete_through END,error=NULL,attempts=0 WHERE id=?1",params![job.id,state,serde_json::to_string(&page.next.clone().unwrap_or(job.cursor.clone()))?,inserted.len() as i64,if new_capture>0{capture.bytes as i64}else{0},at,total,received])?;
        tx.execute(
            "INSERT INTO job_events(job_id,at,state,detail) VALUES(?1,?2,?3,?4)",
            params![
                job.id,
                at,
                state,
                format!(
                    "{} normalized observations; {}; scope only",
                    inserted.len(),
                    page.note
                )
            ],
        )?;
        tx.execute(
            "INSERT OR IGNORE INTO discovery_outbox VALUES(?1,?2,?3,NULL)",
            params![
                format!("{}:{}", job.id, cursor_hash),
                serde_json::to_string(job)?,
                serde_json::to_string(page)?
            ],
        )?;
        tx.commit()?;
        Ok(generation)
    }
    pub fn verify_objects(&self) -> Result<Value> {
        let c = self.connect()?;
        let mut q = c.prepare(
            "SELECT path,sha256 FROM raw_objects UNION ALL SELECT path,sha256 FROM partitions",
        )?;
        let mut checked = 0;
        let mut failures = vec![];
        for row in q.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))? {
            let (path, sha) = row?;
            checked += 1;
            match file_hash(&self.root.join(&path)) {
                Ok(actual) if actual == sha => {}
                _ => failures.push(path),
            }
        }
        let integrity: String = c.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
        Ok(
            json!({"checked_objects":checked,"failures":failures,"sqlite_integrity":integrity,"passed":failures.is_empty()&&integrity=="ok"}),
        )
    }
    pub fn backup(&self, out: &Path) -> Result<Value> {
        if out.exists() {
            return Err(invalid("Backup destination must not already exist"));
        }
        fs::create_dir_all(out)?;
        let c = self.connect()?;
        c.backup("main", out.join("catalog.sqlite"), None)?;
        let frozen = Store { root: out.into() };
        let bc = frozen.connect()?;
        let mut q=bc.prepare("SELECT path FROM raw_objects UNION SELECT path FROM partitions UNION SELECT manifest_path FROM dataset_snapshots WHERE manifest_path!=''")?;
        let paths = q
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        for path in &paths {
            let dst = out.join(path);
            fs::create_dir_all(dst.parent().expect("backup parent"))?;
            fs::copy(self.root.join(path), dst)?;
        }
        let check = frozen.verify_objects()?;
        publish(
            &out.join("backup-manifest.json"),
            &serde_json::to_vec_pretty(
                &json!({"created_at":now(),"files":paths.len(),"verification":check,"same_disk_warning":"Copy to a separate failure domain for an actual backup"}),
            )?,
        )?;
        Ok(check)
    }
}
