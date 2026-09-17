//! Narrow consolidated companyfacts importer. Original segment/custom filing
//! contexts are NOT invented; frames are not used as a historical feature store.
use crate::{evidence::conservative_date_end, ingest::decimal_micros, types::*};
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
#[derive(Serialize)]
pub struct SecImport {
    pub sources: Vec<Source>,
    pub facts: Vec<Fact>,
    pub limitations: Vec<String>,
}
pub fn companyfacts(
    bytes: &[u8],
    taxonomy: &str,
    tag: &str,
    entity: &str,
    metric: &str,
    first_seen: Time,
) -> Result<SecImport> {
    let value: Value = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
    let cik = value["cik"].as_u64().ok_or("companyfacts CIK missing")?;
    let units = value["facts"][taxonomy][tag]["units"]
        .as_object()
        .ok_or("requested companyfacts tag not found")?;
    let raw_hash = format!("{:x}", Sha256::digest(bytes));
    let mut sources = BTreeMap::new();
    let mut candidates: BTreeMap<FactKey, Vec<(String, String, i64)>> = BTreeMap::new();
    for (unit, rows) in units {
        for row in rows.as_array().ok_or("invalid companyfacts units")? {
            let Some(start) = row["start"].as_str() else {
                continue;
            }; // instant facts require a separate explicit metric contract
            let end = row["end"].as_str().ok_or("missing reporting end")?;
            let filed = row["filed"].as_str().ok_or("missing filing date")?;
            let accn = row["accn"].as_str().ok_or("missing accession")?;
            if !accn.bytes().all(|b| b.is_ascii_digit() || b == b'-') {
                return Err("invalid accession".into());
            }
            let amount = decimal_micros(&row["val"])?;
            let published = conservative_date_end(filed, "America/New_York", 0)?;
            let id = format!("sec-{cik}-{accn}");
            sources.entry(id.clone()).or_insert(Source{id:id.clone(),url:format!("https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010}.json"),title:format!("SEC companyfacts record for accession {accn}; aggregate snapshot, not original document context"),permission:"public SEC financial facts; original-source context must be checked".into(),content_sha256:raw_hash.clone(),availability:Availability{published_at:published,timestamp_precision:"date_end_conservative".into(),publication_timezone:"America/New_York".into(),first_seen_at:Some(first_seen),extraction_completed_at:Some(first_seen),modeled_available_at:Some(published+MINUTE)},excerpt:format!("Accession {accn}, filed {filed}; consolidated {taxonomy}:{tag} values from archived JSON.")});
            let key = FactKey {
                entity: entity.into(),
                metric: metric.into(),
                source_series: format!("sec-{cik}-{taxonomy}:{tag}"),
                unit: unit.clone(),
                scale: 0,
                period_start: start.into(),
                period_end: end.into(),
                dimensions: BTreeMap::new(),
            };
            candidates
                .entry(key)
                .or_default()
                .push((filed.into(), id, amount));
        }
    }
    let mut facts = vec![];
    for (key, mut rows) in candidates {
        rows.sort();
        rows.dedup();
        for pair in rows.windows(2) {
            if pair[0].0 == pair[1].0 && pair[0].2 != pair[1].2 {
                return Err(
                    "same-date conflicting SEC revisions require original acceptance times/context"
                        .into(),
                );
            }
        }
        for (revision, (_, source_id, amount)) in rows.into_iter().enumerate() {
            let source = sources.get(&source_id).ok_or("source mapping")?;
            let id = format!(
                "{}-{}-{}-{}-{}-{}",
                source_id, tag, key.unit, key.period_start, key.period_end, revision
            );
            facts.push(Fact {
                id,
                key: key.clone(),
                value_micros: amount,
                revision: revision as u32 + 1,
                status: Status::Active,
                source_id,
                source_locator: format!(
                    "facts/{taxonomy}/{tag}/units/{}; reporting {}..{}",
                    key.unit, key.period_start, key.period_end
                ),
                availability: source.availability.clone(),
            });
        }
    }
    if facts.is_empty() {
        return Err("no duration facts found; instant facts are not silently coerced".into());
    }
    Ok(SecImport{sources:sources.into_values().collect(),facts,limitations:vec!["Consolidated companyfacts only; no inferred segment dimensions or original-document passage verification.".into(),"Filed date has day precision; reconstructed availability is after publisher-local date plus one minute, not acceptance time.".into(),"Latest downloaded aggregate may lack historical vendor vintages; archived raw hash does not recover missing versions.".into(),"Quarterly and year-to-date periods remain separate identities; select a consistent duration series before model training.".into()]})
}
