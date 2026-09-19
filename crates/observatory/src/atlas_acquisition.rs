use crate::{
    atlas::{resource_host_allowed, resource_policies, ResourcePolicy},
    model::*,
};
use chrono::{Datelike, NaiveDate};
use reqwest::{blocking::Client, redirect::Policy, Url};
use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{Cursor, Read},
    time::Duration,
};

const MAX_ROWS: usize = 100_000;
const MAX_COLUMNS: usize = 256;
const MAX_ZIP_MEMBERS: usize = 128;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct SchemaProfile {
    pub rows_seen: usize,
    pub columns: Vec<String>,
    pub numeric_columns: Vec<String>,
    pub missing_by_column: BTreeMap<String, usize>,
    pub chosen_member: Option<String>,
    pub normalized_observations: usize,
    pub normalized_series: usize,
    pub normalization: String,
}

#[derive(Clone, Debug)]
struct Distribution {
    id: String,
    dataset_id: String,
    url: String,
    media_type: Option<String>,
    format: Option<String>,
}

type NormalizedObservationRow = (String, String, String, f64, String, Value, Value);
type DistributionDbRow = (String, String, String, Option<String>, Option<String>);
type DatasetDetailDbRow = (
    String,
    String,
    String,
    Option<String>,
    Option<String>,
    String,
    String,
    String,
);

fn policy_for(url: &Url) -> Result<ResourcePolicy> {
    resource_policies()?
        .into_iter()
        .find(|p| resource_host_allowed(p, url) && p.sample_allowed && p.archive && p.research)
        .ok_or_else(|| {
            Error::Blocked(
                "blocked_permission: no reviewed Atlas resource policy permits this distribution"
                    .into(),
            )
        })
}

fn fetch_bounded(
    url: &Url,
    policy: &ResourcePolicy,
    requested_max: u64,
) -> Result<(Url, Vec<u8>, Option<String>)> {
    let limit = requested_max.min(policy.max_sample_bytes);
    if limit < 1024 {
        return Err(Error::Resource(
            "Atlas acquisition byte budget is too small".into(),
        ));
    }
    let client = Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(Policy::none())
        .user_agent("Hindsight-Atlas/0.1 bounded-resource-acquisition")
        .build()
        .map_err(|e| Error::Network(e.to_string()))?;
    let mut current = url.clone();
    for _ in 0..4 {
        if !resource_host_allowed(policy, &current) {
            return Err(Error::Blocked(
                "Atlas resource URL escaped approved host scope".into(),
            ));
        }
        let response = client
            .get(current.clone())
            .send()
            .map_err(|e| Error::Network(e.to_string()))?;
        if response.status().is_redirection() {
            let location = response
                .headers()
                .get(reqwest::header::LOCATION)
                .and_then(|v| v.to_str().ok())
                .ok_or_else(|| invalid("Atlas resource redirect omitted Location"))?;
            let next = current
                .join(location)
                .map_err(|_| invalid("Atlas resource redirect is invalid"))?;
            if !resource_host_allowed(policy, &next) {
                return Err(Error::Blocked(
                    "Atlas resource redirect escaped approved host scope".into(),
                ));
            }
            current = next;
            continue;
        }
        if !response.status().is_success() {
            return Err(Error::Http {
                status: response.status().as_u16(),
                retry_after_ms: 0,
            });
        }
        if response.content_length().is_some_and(|n| n > limit) {
            return Err(Error::Resource(
                "Atlas resource exceeds reviewed sample byte budget".into(),
            ));
        }
        let media = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let mut body =
            Vec::with_capacity(response.content_length().unwrap_or(0).min(limit) as usize);
        response.take(limit + 1).read_to_end(&mut body)?;
        if body.len() as u64 > limit {
            return Err(Error::Resource(
                "Atlas resource exceeded bounded read".into(),
            ));
        }
        return Ok((current, body, media));
    }
    Err(Error::Resource(
        "Atlas resource redirect limit exceeded".into(),
    ))
}

fn safe_csv_bytes(
    body: &[u8],
    media: &str,
    format: &str,
    max_bytes: u64,
) -> Result<(Vec<u8>, Option<String>)> {
    let is_zip = media.to_ascii_lowercase().contains("zip")
        || format.to_ascii_lowercase().contains("zip")
        || body.starts_with(b"PK\x03\x04");
    if !is_zip {
        return Ok((body.to_vec(), None));
    }
    let mut archive = zip::ZipArchive::new(Cursor::new(body))
        .map_err(|e| invalid(format!("Invalid ZIP resource: {e}")))?;
    if archive.len() > MAX_ZIP_MEMBERS {
        return Err(Error::Resource("ZIP contains too many members".into()));
    }
    let expanded_cap = max_bytes.saturating_mul(8).min(128 * 1024 * 1024);
    let mut total = 0u64;
    let mut candidates = Vec::new();
    for i in 0..archive.len() {
        let file = archive
            .by_index(i)
            .map_err(|e| invalid(format!("Unreadable ZIP member: {e}")))?;
        if file.enclosed_name().is_none() {
            return Err(invalid("ZIP path traversal member rejected"));
        }
        if file.is_dir() {
            continue;
        }
        total = total
            .checked_add(file.size())
            .ok_or_else(|| Error::Resource("ZIP expanded-size overflow".into()))?;
        if total > expanded_cap {
            return Err(Error::Resource(
                "ZIP expanded data exceeds bounded acquisition budget".into(),
            ));
        }
        let name = file.name().to_string();
        if name.to_ascii_lowercase().ends_with(".csv") {
            candidates.push((i, name, file.size()));
        }
    }
    let (index, name, _) = candidates
        .into_iter()
        .filter(|(_, n, _)| !n.to_ascii_lowercase().contains("metadata"))
        .max_by_key(|(_, _, size)| *size)
        .ok_or_else(|| invalid("ZIP contains no bounded data CSV"))?;
    let mut file = archive
        .by_index(index)
        .map_err(|e| invalid(format!("Unreadable ZIP CSV: {e}")))?;
    if file.size() > expanded_cap {
        return Err(Error::Resource(
            "ZIP CSV exceeds expanded sample budget".into(),
        ));
    }
    let mut csv = Vec::with_capacity(file.size().min(16 * 1024 * 1024) as usize);
    file.read_to_end(&mut csv)?;
    Ok((csv, Some(name)))
}

fn period_bounds(value: &str) -> Result<(String, String)> {
    if let Ok(d) = NaiveDate::parse_from_str(value, "%Y-%m-%d") {
        return Ok((d.to_string(), d.to_string()));
    }
    if value.len() == 7 {
        let start = NaiveDate::parse_from_str(&format!("{value}-01"), "%Y-%m-%d")
            .map_err(|_| invalid("Invalid monthly period"))?;
        let next = if start.month() == 12 {
            NaiveDate::from_ymd_opt(start.year() + 1, 1, 1)
        } else {
            NaiveDate::from_ymd_opt(start.year(), start.month() + 1, 1)
        }
        .ok_or_else(|| invalid("Monthly period overflow"))?;
        return Ok((
            start.to_string(),
            next.pred_opt()
                .ok_or_else(|| invalid("Monthly period end"))?
                .to_string(),
        ));
    }
    if value.len() == 4 && value.bytes().all(|b| b.is_ascii_digit()) {
        let y: i32 = value
            .parse()
            .map_err(|_| invalid("Invalid annual period"))?;
        return Ok((format!("{y}-01-01"), format!("{y}-12-31")));
    }
    Err(invalid("Unsupported period format; no date invented"))
}

fn find_header(headers: &csv::StringRecord, names: &[&str]) -> Option<usize> {
    headers
        .iter()
        .position(|h| names.iter().any(|n| h.eq_ignore_ascii_case(n)))
}

fn profile_and_normalize(
    csv_bytes: &[u8],
    member: Option<String>,
    dataset: &str,
    _distribution: &str,
    observed_at: i64,
) -> Result<(SchemaProfile, Vec<NormalizedObservationRow>)> {
    let mut reader = csv::ReaderBuilder::new()
        .flexible(false)
        .from_reader(csv_bytes);
    let headers = reader
        .headers()
        .map_err(|e| invalid(format!("CSV headers invalid: {e}")))?
        .clone();
    if headers.is_empty() || headers.len() > MAX_COLUMNS {
        return Err(invalid("CSV column count outside bounded schema limits"));
    }
    let columns = headers.iter().map(str::to_string).collect::<Vec<_>>();
    let period_i = find_header(&headers, &["REF_DATE", "date", "period", "time"]);
    let value_i = find_header(&headers, &["VALUE", "value"]);
    let unit_i = find_header(&headers, &["UOM", "unit"]);
    let mut missing = columns
        .iter()
        .map(|c| (c.clone(), 0usize))
        .collect::<BTreeMap<_, _>>();
    let mut numeric_counts = vec![0usize; headers.len()];
    let mut nonmissing = vec![0usize; headers.len()];
    let mut normalized = Vec::new();
    let mut series = BTreeSet::new();
    let mut rows = 0usize;
    for result in reader.records() {
        if rows >= MAX_ROWS {
            return Err(Error::Resource(
                "CSV row count exceeds bounded sample profile".into(),
            ));
        }
        let row = result.map_err(|e| invalid(format!("Malformed CSV record: {e}")))?;
        rows += 1;
        for (i, v) in row.iter().enumerate() {
            if v.trim().is_empty() {
                *missing.get_mut(&columns[i]).unwrap() += 1;
            } else {
                nonmissing[i] += 1;
                if v.parse::<f64>().ok().is_some_and(f64::is_finite) {
                    numeric_counts[i] += 1;
                }
            }
        }
        let (Some(pi), Some(vi), Some(ui)) = (period_i, value_i, unit_i) else {
            continue;
        };
        let raw_value = row.get(vi).unwrap_or_default().trim();
        if raw_value.is_empty() {
            continue;
        }
        let value: f64 = raw_value
            .parse()
            .map_err(|_| invalid("Recognized VALUE column contains nonnumeric data"))?;
        if !value.is_finite() {
            return Err(invalid("Recognized VALUE column contains nonfinite data"));
        }
        let unit = row.get(ui).unwrap_or_default().trim();
        if unit.is_empty() {
            return Err(invalid("Recognized UOM column is empty"));
        }
        let (start, end) = period_bounds(row.get(pi).unwrap_or_default().trim())?;
        let mut dims = Map::new();
        for (i, h) in headers.iter().enumerate() {
            if [pi, vi, ui].contains(&i)
                || matches!(h, "STATUS" | "SYMBOL" | "TERMINATED" | "DECIMALS")
            {
                continue;
            }
            let v = row.get(i).unwrap_or_default();
            if !v.is_empty() {
                dims.insert(h.into(), Value::String(v.into()));
            }
        }
        let dim_value = Value::Object(dims);
        let skey = format!(
            "atlas-series:{}",
            hash(format!("{dataset}\0{unit}\0{}", serde_json::to_string(&dim_value)?).as_bytes())
        );
        series.insert(skey.clone());
        let payload = json!({"source_row":rows,"current_vintage":true,"observed_at":observed_at,"historical_publication_unknown":true});
        normalized.push((
            skey,
            start,
            end,
            value,
            unit.to_string(),
            dim_value,
            payload,
        ));
    }
    let numeric_columns = columns
        .iter()
        .enumerate()
        .filter(|(i, _)| nonmissing[*i] > 0 && numeric_counts[*i] == nonmissing[*i])
        .map(|(_, c)| c.clone())
        .collect();
    let normalization = if period_i.is_some() && value_i.is_some() && unit_i.is_some() {
        "recognized period/value/unit columns; current-vintage observations only"
    } else {
        "schema profiled only; observation columns not recognized"
    }
    .to_string();
    Ok((
        SchemaProfile {
            rows_seen: rows,
            columns,
            numeric_columns,
            missing_by_column: missing,
            chosen_member: member,
            normalized_observations: normalized.len(),
            normalized_series: series.len(),
            normalization,
        },
        normalized,
    ))
}

impl Store {
    fn atlas_distribution(&self, id: &str) -> Result<Distribution> {
        let c = self.connect()?;
        let row: Option<DistributionDbRow> = c
            .query_row(
                "SELECT id,dataset_id,url,media_type,format FROM atlas_distributions WHERE id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .optional()?;
        row.map(|(id, dataset_id, url, media_type, format)| Distribution {
            id,
            dataset_id,
            url,
            media_type,
            format,
        })
        .ok_or_else(|| invalid("Unknown Atlas distribution"))
    }
    pub fn atlas_dataset_detail(&self, id: &str) -> Result<Value> {
        let c = self.connect()?;
        let base:Option<DatasetDetailDbRow>=c.query_row("SELECT external_id,title,description,landing_url,license,spatial_json,temporal_json,keywords_json FROM atlas_datasets WHERE id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?))).optional()?;
        let Some((
            external_id,
            title,
            description,
            landing_url,
            license,
            spatial,
            temporal,
            keywords,
        )) = base
        else {
            return Err(invalid("Unknown Atlas dataset"));
        };
        let mut q=c.prepare("SELECT x.id,x.url,x.media_type,x.format,x.rights_state,p.status,p.profiled_at,p.schema_json FROM atlas_distributions x LEFT JOIN atlas_distribution_profiles p ON p.distribution_id=x.id WHERE x.dataset_id=?1 ORDER BY x.id")?;
        let raw = q
            .query_map([id], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, Option<String>>(2)?,
                    r.get::<_, Option<String>>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, Option<String>>(5)?,
                    r.get::<_, Option<i64>>(6)?,
                    r.get::<_, Option<String>>(7)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let distributions=raw.into_iter().map(|(dist_id,url,media_type,format,catalog_rights,profile_status,profiled_at,schema)| {
            let acquisition=Url::parse(&url).ok().and_then(|u|policy_for(&u).ok()).map(|p|json!({"state":"allowed","policy_id":p.id,"license":p.license,"warning":p.warning})).unwrap_or_else(||json!({"state":"blocked_permission","reason":"No reviewed resource policy permits acquisition from this host."}));
            json!({"id":dist_id,"url":url,"media_type":media_type,"format":format,"catalog_rights":catalog_rights,"profile_status":profile_status,"profiled_at":profiled_at,"schema":schema.and_then(|s|serde_json::from_str::<Value>(&s).ok()),"acquisition":acquisition})
        }).collect::<Vec<_>>();
        let observation_count: i64 = c.query_row(
            "SELECT COUNT(*) FROM atlas_observations WHERE dataset_id=?1",
            [id],
            |r| r.get(0),
        )?;
        Ok(
            json!({"id":id,"external_id":external_id,"title":title,"description":description,"landing_url":landing_url,"license":license,"spatial":serde_json::from_str::<Value>(&spatial).unwrap_or(Value::Null),"temporal":serde_json::from_str::<Value>(&temporal).unwrap_or(Value::Null),"keywords":serde_json::from_str::<Value>(&keywords).unwrap_or(json!([])),"observation_count":observation_count,"distributions":distributions}),
        )
    }
    pub fn atlas_observations(
        &self,
        dataset: &str,
        mode: &str,
        as_of: i64,
        limit: usize,
    ) -> Result<Value> {
        if !["observed", "reconstructed"].contains(&mode) {
            return Err(invalid(
                "Atlas evidence mode must be observed or reconstructed",
            ));
        }
        if mode == "reconstructed" {
            return Ok(
                json!({"dataset_id":dataset,"mode":mode,"as_of":as_of,"rows":[],"reason":"Current-vintage resource samples have no verified historical publication clock and are withheld from reconstructed history."}),
            );
        }
        let c = self.connect()?;
        let mut q=c.prepare("SELECT id,series_key,period_start,period_end,value,unit,dimensions_json,observed_at,quality,payload FROM atlas_observations WHERE dataset_id=?1 AND observed_at<=?2 ORDER BY period_end,series_key LIMIT ?3")?;
        let rows=q.query_map(params![dataset,as_of,limit.min(5000) as i64],|r|Ok(json!({"id":r.get::<_,String>(0)?,"series_key":r.get::<_,String>(1)?,"period_start":r.get::<_,String>(2)?,"period_end":r.get::<_,String>(3)?,"value":r.get::<_,f64>(4)?,"unit":r.get::<_,String>(5)?,"dimensions":serde_json::from_str::<Value>(&r.get::<_,String>(6)?).unwrap_or(Value::Null),"observed_at":r.get::<_,i64>(7)?,"quality":r.get::<_,String>(8)?,"payload":serde_json::from_str::<Value>(&r.get::<_,String>(9)?).unwrap_or(Value::Null)})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(
            json!({"dataset_id":dataset,"mode":mode,"as_of":as_of,"rows":rows,"truncated":rows.len()==limit.min(5000),"warning":"Observed current-vintage sample; not an original historical release archive."}),
        )
    }
    pub fn sample_atlas_distribution(&self, id: &str, max_bytes: u64) -> Result<Value> {
        let d = self.atlas_distribution(id)?;
        let url = Url::parse(&d.url).map_err(|_| invalid("Invalid Atlas distribution URL"))?;
        let policy = policy_for(&url)?;
        let (final_url, body, media) = fetch_bounded(&url, &policy, max_bytes)?;
        let format = d.format.clone().unwrap_or_default();
        let (csv_bytes, member) = safe_csv_bytes(
            &body,
            media.as_deref().or(d.media_type.as_deref()).unwrap_or(""),
            &format,
            policy.max_sample_bytes,
        )?;
        let observed = now();
        let (profile, rows) =
            profile_and_normalize(&csv_bytes, member, &d.dataset_id, &d.id, observed)?;
        let sha = hash(&body);
        let path = format!("raw/atlas-resource/{}/{}.bin", &sha[..2], sha);
        let full = self.root.join(&path);
        if !full.exists() {
            fs::create_dir_all(
                full.parent()
                    .ok_or_else(|| invalid("Atlas resource path has no parent"))?,
            )?;
            fs::write(&full, &body)?;
        }
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing: Option<String> = tx
            .query_row(
                "SELECT sha256 FROM atlas_distribution_profiles WHERE distribution_id=?1",
                [id],
                |r| r.get(0),
            )
            .optional()?;
        if existing.as_deref() == Some(&sha) {
            let count: i64 = tx.query_row(
                "SELECT COUNT(*) FROM atlas_observations WHERE distribution_id=?1",
                [id],
                |r| r.get(0),
            )?;
            return Ok(
                json!({"distribution_id":id,"status":"already_profiled","sha256":sha,"observations":count,"schema":profile,"policy":policy}),
            );
        }
        if existing.is_some() {
            return Err(Error::Conflict("Atlas distribution bytes changed; a new versioning/reconciliation step is required before replacing the prior sample".into()));
        }
        tx.execute("INSERT INTO atlas_distribution_profiles VALUES(?1,?2,?3,?4,?5,?6,?7,?8,'validated',NULL)",params![id,observed,body.len() as i64,sha,media,d.format,serde_json::to_string(&profile)?,path])?;
        for (series_key, start, end, value, unit, dimensions, payload) in rows {
            let oid = format!(
                "atlas-obs:{}",
                hash(
                    format!(
                        "{}\0{}\0{}\0{}\0{}",
                        d.dataset_id, series_key, start, end, unit
                    )
                    .as_bytes()
                )
            );
            tx.execute("INSERT OR IGNORE INTO atlas_observations(id,dataset_id,distribution_id,series_key,period_start,period_end,value,unit,dimensions_json,published_at,observed_at,quality,payload) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,NULL,?10,'current_vintage_observed_only',?11)",params![oid,d.dataset_id,d.id,series_key,start,end,value,unit,serde_json::to_string(&dimensions)?,observed,serde_json::to_string(&payload)?])?;
        }
        tx.commit()?;
        Ok(
            json!({"distribution_id":id,"dataset_id":d.dataset_id,"final_url":final_url.to_string(),"status":"validated","bytes":body.len(),"sha256":sha,"schema":profile,"policy":policy,"historical_reconstruction":false}),
        )
    }
}

#[cfg(test)]
mod acquisition_tests {
    use super::*;
    use std::io::Write;
    #[test]
    fn monthly_period_uses_real_month_end() {
        assert_eq!(
            period_bounds("2024-02").unwrap(),
            ("2024-02-01".into(), "2024-02-29".into())
        );
        assert_eq!(period_bounds("2023-02").unwrap().1, "2023-02-28");
    }
    #[test]
    fn unknown_period_is_rejected_not_invented() {
        assert!(period_bounds("Q1-2024").is_err());
    }
    #[test]
    fn resource_scope_rejects_lookalike_hosts() {
        let p = resource_policies().unwrap().remove(0);
        assert!(resource_host_allowed(
            &p,
            &Url::parse("https://www150.statcan.gc.ca/a").unwrap()
        ));
        assert!(!resource_host_allowed(
            &p,
            &Url::parse("https://www150.statcan.gc.ca.evil.test/a").unwrap()
        ));
    }
    #[test]
    fn zip_path_traversal_is_rejected() {
        let mut cur = Cursor::new(Vec::new());
        {
            let mut w = zip::ZipWriter::new(&mut cur);
            let opt = zip::write::SimpleFileOptions::default();
            w.start_file("../evil.csv", opt).unwrap();
            w.write_all(b"REF_DATE,UOM,VALUE\n2024-01,x,1\n").unwrap();
            w.finish().unwrap();
        }
        assert!(safe_csv_bytes(&cur.into_inner(), "application/zip", "ZIP", 1 << 20).is_err());
    }
    #[test]
    fn recognized_csv_normalizes_current_vintage() {
        let csv=b"REF_DATE,GEO,UOM,VALUE,STATUS\n2024-01,Canada,Terajoules,10,\n2024-02,Canada,Terajoules,11,\n";
        let (p, rows) = profile_and_normalize(csv, None, "d", "x", 1234).unwrap();
        assert_eq!(p.rows_seen, 2);
        assert_eq!(p.normalized_observations, 2);
        assert_eq!(p.normalized_series, 1);
        assert_eq!(rows[1].2, "2024-02-29");
    }
    #[test]
    fn nonnumeric_value_in_recognized_schema_fails_closed() {
        let csv = b"REF_DATE,UOM,VALUE\n2024-01,USD,not-a-number\n";
        assert!(profile_and_normalize(csv, None, "d", "x", 1).is_err());
    }
}
