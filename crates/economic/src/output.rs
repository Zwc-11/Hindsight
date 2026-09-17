use crate::{
    engine::{digest, Run},
    ingest::immutable,
    types::*,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, path::Path, process::Command};
const DASHBOARD: &str = include_str!("../../../ui/economic/dashboard.html");
pub fn safe_embedded_json(value: &impl serde::Serialize) -> Result<String> {
    Ok(serde_json::to_string(value)
        .map_err(|e| e.to_string())?
        .replace('<', "\\u003c")
        .replace('>', "\\u003e")
        .replace('&', "\\u0026")
        .replace('\u{2028}', "\\u2028")
        .replace('\u{2029}', "\\u2029"))
}
pub fn write_run(input: &Input, run: &Run, out: &Path) -> Result<()> {
    if out.exists() {
        return Err(format!(
            "output directory already exists: {}; choose a new immutable run directory",
            out.display()
        ));
    }
    std::fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let git = |args: &[&str]| {
        Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args(args)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| o.stdout)
            .unwrap_or_default()
    };
    let sha = String::from_utf8_lossy(&git(&["rev-parse", "HEAD"]))
        .trim()
        .to_string();
    let patch = git(&["diff", "--no-ext-diff", "--binary", "HEAD"]);
    let untracked = git(&["ls-files", "--others", "--exclude-standard", "-z"]);
    let mut hashes = BTreeMap::new();
    for name in untracked.split(|b| *b == 0).filter(|n| !n.is_empty()) {
        let name = std::str::from_utf8(name).map_err(|e| e.to_string())?;
        let path = repo.join(name);
        if path.is_file() {
            let bytes = std::fs::read(path).map_err(|e| e.to_string())?;
            hashes.insert(name.to_string(), format!("{:x}", Sha256::digest(bytes)));
        }
    }
    let compiler = option_env!("HINDSIGHT_RUSTC_VERSION")
        .unwrap_or("recorded in Cargo toolchain; see validation log");
    let manifest = json!({"schema_version":1,"git_sha":sha,"uncommitted_patch_sha256":format!("{:x}",Sha256::digest(&patch)),"untracked_source_hashes":hashes,"input_sha256":run.input_hash,"config_sha256":digest(&input.config)?,"report_sha256":digest(run)?,"evidence_mode":input.config.mode,"calendar_version":input.dataset.calendar_version,"calendar_sha256":digest(&input.dataset.sessions)?,"source_policies":input.dataset.sources.iter().map(|s|(&s.id,&s.permission,&s.content_sha256)).collect::<Vec<_>>(),"bootstrap_seed":input.config.bootstrap_seed,"compiler":compiler,"architecture":std::env::consts::ARCH,"os":std::env::consts::OS,"build_profile":if cfg!(debug_assertions){"debug"}else{"release"},"execution_reference":"strictly next minute open, modeled half-spread+impact+fee once","cash_scale":"USD integer micro-units; whole shares","result_claim":if input.dataset.synthetic{"synthetic software demonstration only"}else{"research result; not prospective validation"}});
    immutable(
        &out.join("report.json"),
        &serde_json::to_vec_pretty(run).map_err(|e| e.to_string())?,
    )?;
    immutable(
        &out.join("manifest.json"),
        &serde_json::to_vec_pretty(&manifest).map_err(|e| e.to_string())?,
    )?;
    immutable(
        &out.join("coverage.json"),
        &serde_json::to_vec_pretty(&run.coverage).map_err(|e| e.to_string())?,
    )?;
    let page = DASHBOARD.replace("__REPORT__", &safe_embedded_json(run)?);
    immutable(&out.join("index.html"), page.as_bytes())?;
    let mut events = String::new();
    for trial in &run.trials {
        for event in &trial.account.events {
            events.push_str(
                &serde_json::to_string(&json!({"variant":trial.variant,"event":event}))
                    .map_err(|e| e.to_string())?,
            );
            events.push('\n');
        }
    }
    immutable(&out.join("ledger.jsonl"), events.as_bytes())?;
    let note=format!("# Hindsight economic research run\n\nDataset: {}\n\nSynthetic: {}\n\nEvidence mode: {:?}\n\nInput hash: `{}`\n\nOpen `index.html` for the four-view dashboard. Read `report.json`, `manifest.json`, `coverage.json` and `ledger.jsonl` for auditable records.\n\nNo broker was contacted. No leverage or options are implemented.\n",input.dataset.label,input.dataset.synthetic,input.config.mode,run.input_hash);
    immutable(&out.join("README.md"), note.as_bytes())?;
    Ok(())
}
