//! Explicit lifecycle, reconciliation and source-cadence updates. No installed background daemon.
use crate::model::*;
use rusqlite::{params, OptionalExtension};
use serde_json::{json, Value};
use std::collections::BTreeSet;
impl Store {
    pub fn configure_updates(&self, enabled: bool) -> Result<Value> {
        let c = self.connect()?;
        for (provider, interval) in [
            ("worldbank", 7 * 86_400_000i64),
            ("issuer", 86_400_000),
            ("bls", 86_400_000),
            ("census", 7 * 86_400_000),
            ("sec", 86_400_000),
            ("alpaca", 3_600_000),
            ("eia", 86_400_000),
            ("bea", 7 * 86_400_000),
        ] {
            c.execute("INSERT INTO refresh_schedule(provider,interval_ms,next_due,enabled) VALUES(?1,?2,?3,?4) ON CONFLICT(provider) DO UPDATE SET enabled=excluded.enabled",params![provider,interval,now()+interval,enabled])?;
        }
        Ok(
            json!({"enabled":enabled,"lifecycle":"Only executes inside an active worker process. Host/server shutdown stops collection; checkpoints persist."}),
        )
    }
    pub fn refresh_due(&self) -> Result<bool> {
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row:Option<(String,i64)>=tx.query_row("SELECT provider,interval_ms FROM refresh_schedule WHERE enabled=1 AND next_due<=?1 ORDER BY next_due LIMIT 1",[now()],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        let Some((provider, interval)) = row else {
            return Ok(false);
        };
        tx.execute(
            "UPDATE refresh_schedule SET next_due=?2 WHERE provider=?1",
            params![provider, now() + interval],
        )?;
        tx.commit()?;
        if let Err(e) = self.access_check(&provider) {
            c.execute(
                "UPDATE providers SET last_error=?2 WHERE id=?1",
                params![provider, e.to_string()],
            )?;
            return Ok(true);
        }
        let cutoff = if provider == "alpaca" {
            now() - 900_000
        } else {
            now()
        };
        let mut q=c.prepare("SELECT DISTINCT kind,resource,params FROM jobs WHERE provider=?1 AND state='complete_for_declared_scope' AND kind NOT LIKE '%catalog%' AND kind NOT IN ('sec_document','permission_gate') LIMIT 10000")?;
        let rows = q
            .query_map([&provider], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let mut dedup = BTreeSet::new();
        for (kind, resource, encoded) in rows {
            let mut p: Value = serde_json::from_str(&encoded)?;
            if kind == "alpaca_bars" {
                let frame = p["timeframe"].as_str().unwrap_or("1Min");
                if !dedup.insert((kind.clone(), resource.clone(), frame.to_owned())) {
                    continue;
                }
                let overlap = if frame == "1Min" { 7 } else { 45 };
                p["start"] = chrono::DateTime::from_timestamp_millis(cutoff - overlap * 86_400_000)
                    .ok_or_else(|| invalid("Refresh timestamp"))?
                    .to_rfc3339()
                    .into();
                p["end"] = chrono::DateTime::from_timestamp_millis(cutoff)
                    .ok_or_else(|| invalid("Refresh timestamp"))?
                    .to_rfc3339()
                    .into();
                p["scope"]="bounded revision overlap; periodic full historical consistency review remains required".into();
            } else if !dedup.insert((kind.clone(), resource.clone(), encoded)) {
                continue;
            }
            p["refresh_of"] =
                "completed declared collection; updates do not overwrite old captures".into();
            self.enqueue(&provider, &kind, &resource, &p, cutoff, 30)?;
        }
        // Discover new releases/catalog members without fabricating a full-universe completion claim.
        if provider == "issuer" {
            for host in ["nanya", "micron"] {
                self.enqueue(
                    "issuer",
                    "issuer_catalog",
                    host,
                    &json!({"refresh":true,"scope":"official archive discovery"}),
                    cutoff,
                    40,
                )?;
            }
        }
        if provider == "worldbank" {
            self.enqueue("worldbank","wb_catalog","2",&json!({"refresh":true,"scope":"full WDI indicator catalog; new history jobs remain explicit"}),cutoff,40)?;
        }
        Ok(true)
    }
    pub fn reconcile(&self) -> Result<Value> {
        let c = self.connect()?;
        let expired=c.execute("UPDATE jobs SET state='queued',lease_token=NULL,lease_until=NULL,error='Expired lease recovered; durable cursor retained' WHERE state='running' AND lease_until<?1",[now()])?;
        let mut q=c.prepare("SELECT path FROM raw_objects UNION SELECT path FROM partitions UNION SELECT manifest_path FROM dataset_snapshots WHERE manifest_path!=''")?;
        let known = q
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<BTreeSet<_>, _>>()?;
        fn visit(
            base: &std::path::Path,
            at: &std::path::Path,
            known: &BTreeSet<String>,
            out: &mut Vec<String>,
        ) -> Result<()> {
            for entry in std::fs::read_dir(at)? {
                let e = entry?;
                let ty = e.file_type()?;
                if ty.is_symlink() {
                    return Err(invalid("Symlink found in managed data store"));
                }
                if ty.is_dir() {
                    visit(base, &e.path(), known, out)?;
                } else {
                    let rel = e
                        .path()
                        .strip_prefix(base)
                        .map_err(|_| invalid("Managed path"))?
                        .to_string_lossy()
                        .to_string();
                    if !known.contains(&rel) {
                        out.push(rel);
                    }
                }
            }
            Ok(())
        }
        let mut orphans = Vec::new();
        for dir in ["raw", "canonical", "snapshots", "staging"] {
            visit(&self.root, &self.root.join(dir), &known, &mut orphans)?;
        }
        let check = self.verify_objects()?;
        Ok(
            json!({"expired_jobs_requeued":expired,"retained_unreferenced_objects":orphans,"verification":check,"deleted_objects":0,"policy":"Orphans retained for source reconciliation; committed objects are never guessed or overwritten."}),
        )
    }
    pub fn restore_from(backup: &std::path::Path, out: &std::path::Path) -> Result<Value> {
        if out.exists() {
            return Err(invalid("Restore destination must be a new directory"));
        }
        let frozen = Store {
            root: backup.into(),
        };
        let verification = frozen.verify_objects()?;
        if verification["passed"] != true {
            return Err(invalid("Backup failed integrity verification"));
        }
        let result = frozen.backup(out)?;
        let restored = Store::open(out)?;
        let c = restored.connect()?;
        c.execute("UPDATE jobs SET state='queued',lease_token=NULL,lease_until=NULL WHERE state='running'",[])?;
        c.execute(
            "UPDATE experiments SET state='queued' WHERE state='running' AND cancel_requested=0",
            [],
        )?;
        c.execute("UPDATE refresh_schedule SET enabled=0", [])?;
        Ok(
            json!({"verification":result,"destination":out,"updates_enabled":false,"note":"Restored read-only research data. Workers and schedules are not auto-started."}),
        )
    }
}
pub fn math_request(v: Value) -> Result<Value> {
    let method = v["method"]
        .as_str()
        .ok_or_else(|| invalid("Math request needs method"))?;
    let out = match method {
        "pair" => {
            let p: crate::math::PairInput = serde_json::from_value(v["input"].clone())?;
            json!({"pearson":crate::math::pearson(&p.x,&p.y)?,"spearman":crate::math::spearman(&p.x,&p.y)?,"distance":crate::math::dependence::distance_correlation(&p.x,&p.y)?,"ew":crate::math::dependence::ew_correlation(&p.x,&p.y,0.1)?})
        }
        "covariance" => {
            let rows: Vec<Vec<f64>> = serde_json::from_value(v["input"].clone())?;
            serde_json::to_value(crate::math::covariance::ledoit_wolf(&rows)?)?
        }
        "adjust" => {
            let p: Vec<f64> = serde_json::from_value(v["input"].clone())?;
            json!({"BH":crate::math::dependence::adjust(&p,"BH")?,"BY":crate::math::dependence::adjust(&p,"BY")?})
        }
        "filter" => {
            let i: crate::math::state_space::FilterInput =
                serde_json::from_value(v["input"].clone())?;
            serde_json::to_value(crate::math::state_space::filter(&i)?)?
        }
        "shift_null" => {
            let p: crate::math::PairInput = serde_json::from_value(v["input"].clone())?;
            serde_json::to_value(crate::math::dependence::circular_shift_null(
                &p.x,
                &p.y,
                v["distance"].as_bool().unwrap_or(false),
            )?)?
        }
        _ => return Err(invalid("Unknown bounded numerical method")),
    };
    Ok(out)
}
