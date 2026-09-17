//! Provider-specific schemas and coverage semantics. No synthetic fallback.
use crate::{fetch::fetch, model::*};
use chrono::{Datelike, NaiveDate, TimeZone, Utc};
use reqwest::{
    header::{HeaderMap, HeaderValue},
    Url,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs::File,
    io::{Read, Seek, SeekFrom},
};

fn s<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v[k].as_str()
        .ok_or_else(|| invalid(format!("Source missing string field {k}")))
}
fn num(v: &Value) -> Option<f64> {
    v.as_f64()
        .or_else(|| {
            v.as_str()
                .and_then(|s| s.replace(',', "").parse::<f64>().ok())
        })
        .filter(|n| n.is_finite())
}
fn page_default() -> PageResult {
    PageResult {
        complete_scope: false,
        series: vec![],
        observations: vec![],
        next: None,
        advertised_total: None,
        received_rows: 0,
        note: String::new(),
        children: vec![],
    }
}
#[allow(clippy::too_many_arguments)]
fn series(
    id: String,
    job: &Job,
    code: &str,
    name: &str,
    entity: &str,
    unit: &str,
    frequency: &str,
    url: &str,
    metadata: Value,
) -> Series {
    Series {
        id,
        provider: job.provider.clone(),
        code: code.into(),
        name: name.into(),
        entity: entity.into(),
        unit: if unit.is_empty() {
            "unspecified; inspect source metadata".into()
        } else {
            unit.into()
        },
        frequency: frequency.into(),
        description: metadata["description"].as_str().unwrap_or_default().into(),
        source_url: url.into(),
        metadata,
    }
}
fn observation(
    id: String,
    start: String,
    end: String,
    value: Option<f64>,
    unit: &str,
    extras: Value,
) -> Observation {
    Observation {
        series_id: id,
        period_start: start,
        period_end: end,
        value,
        unit: unit.into(),
        dimensions: json!({}),
        published_at: None,
        reconstructed_at: None,
        source_order: None,
        precision: "unknown_publication".into(),
        quality: if value.is_some() {
            "latest_vintage"
        } else {
            "source_missing"
        }
        .into(),
        extras,
    }
}
fn year_bounds(year: &str) -> Result<(String, String)> {
    let y: i32 = year.parse().map_err(|_| invalid("Invalid annual period"))?;
    if !(1800..=2300).contains(&y) {
        return Err(invalid("Annual period outside supported range"));
    }
    Ok((format!("{y}-01-01"), format!("{y}-12-31")))
}
fn wb_parts(v: &Value) -> Result<(&Value, &Vec<Value>)> {
    let a = v
        .as_array()
        .ok_or_else(|| invalid("World Bank response is not an array"))?;
    if a.len() != 2 {
        return Err(invalid("World Bank metadata/data response missing"));
    }
    let rows = a[1]
        .as_array()
        .ok_or_else(|| invalid("World Bank rows missing; empty is not completeness"))?;
    Ok((&a[0], rows))
}
pub fn normalize_wb(job: &Job, v: &Value) -> Result<PageResult> {
    let (h, rows) = wb_parts(v)?;
    let page = h["page"]
        .as_i64()
        .or_else(|| h["page"].as_str().and_then(|s| s.parse().ok()))
        .ok_or_else(|| invalid("World Bank page missing"))?;
    let pages = h["pages"]
        .as_i64()
        .ok_or_else(|| invalid("World Bank pages missing"))?;
    let total = h["total"]
        .as_i64()
        .or_else(|| h["total"].as_str().and_then(|s| s.parse().ok()))
        .ok_or_else(|| invalid("World Bank total missing"))?;
    if page < 1 || pages < page || page != job.cursor["page"].as_i64().unwrap_or(1) {
        return Err(invalid("Unexpected World Bank page ordering"));
    }
    if job.cursor.get("lastupdated").is_some() && job.cursor["lastupdated"] != h["lastupdated"] {
        return Err(Error::Conflict(
            "World Bank vintage changed during pagination".into(),
        ));
    }
    let mut out = page_default();
    out.received_rows = rows.len() as i64;
    out.advertised_total = Some(total);
    out.complete_scope = true;
    if page < pages {
        out.next = Some(json!({"page":page+1,"lastupdated":h["lastupdated"]}));
    }
    if job.kind == "wb_catalog" {
        for r in rows {
            let code = s(r, "id")?;
            let name = s(r, "name")?;
            let id = format!("WB:{code}");
            out.series.push(series(id,job,code,name,"WORLD",r["unit"].as_str().unwrap_or("source-defined"),"annual",&format!("https://data.worldbank.org/indicator/{code}"),json!({"source_id":"2","description":r["sourceNote"],"organization":r["sourceOrganization"],"topics":r["topics"],"role":"indicator_catalog","original_vintage":false})));
        }
        out.note = "Full WDI indicator catalog pagination; not downloaded histories".into();
    } else {
        let mut seen = BTreeSet::new();
        for r in rows {
            let code = s(&r["indicator"], "id")?;
            let geo = s(r, "countryiso3code")?;
            if geo.is_empty() {
                continue;
            }
            let id = format!("WB:{code}:{geo}");
            let (start, end) = year_bounds(s(r, "date")?)?;
            let name = s(&r["indicator"], "value")?;
            let country = s(&r["country"], "value")?;
            let unit = r["unit"]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or("source-defined; see indicator name");
            if seen.insert(id.clone()) {
                out.series.push(series(id.clone(),job,code,name,geo,unit,"annual",&format!("https://data.worldbank.org/indicator/{code}?locations={geo}"),json!({"country":country,"source_id":"2","description":name,"original_vintage":false,"provider_lastupdated":h["lastupdated"]})));
            }
            let value = match r.get("value") {
                Some(Value::Null) => None,
                Some(v) => Some(
                    num(v)
                        .ok_or_else(|| invalid("World Bank supplied an invalid numeric token"))?,
                ),
                None => return Err(invalid("World Bank observation is missing value field")),
            };
            out.observations.push(observation(id,start,end,value,unit,json!({"country":country,"decimal":r["decimal"],"obs_status":r["obs_status"],"provider_lastupdated":h["lastupdated"],"original_vintage":false})));
        }
        out.note="All API rows for the declared indicator/country/year range, including source nulls; current vintage only".into();
    }
    Ok(out)
}
pub fn request(store: &Store, job: &Job) -> Result<(Capture, PageResult)> {
    match job.kind.as_str() {
        "wb_catalog" | "wb_history" => {
            let path = if job.kind == "wb_catalog" {
                "https://api.worldbank.org/v2/sources/2/indicators".into()
            } else {
                format!(
                    "https://api.worldbank.org/v2/country/all/indicator/{}",
                    job.resource
                )
            };
            let mut url = Url::parse(&path).map_err(|_| invalid("Invalid catalog route"))?;
            url.query_pairs_mut()
                .append_pair("format", "json")
                .append_pair("per_page", "1000")
                .append_pair(
                    "page",
                    &job.cursor["page"].as_i64().unwrap_or(1).to_string(),
                );
            if job.kind == "wb_history" {
                url.query_pairs_mut()
                    .append_pair("source", "2")
                    .append_pair(
                        "date",
                        &format!(
                            "1960:{}",
                            Utc.timestamp_millis_opt(job.cutoff)
                                .single()
                                .ok_or_else(|| invalid("cutoff"))?
                                .year()
                        ),
                    );
            }
            let c = fetch(store, job, url, HeaderMap::new(), None, 32 * 1024 * 1024)?;
            let v: Value = serde_json::from_reader(File::open(store.root.join(&c.path))?)?;
            Ok((c, normalize_wb(job, &v)?))
        }
        "bls_catalog" | "bls_data" => bls(store, job),
        "census_catalog" | "census_totals" | "census_data" => census(store, job),
        "alpaca_assets" | "alpaca_bars" => alpaca(store, job),
        "eia_catalog" | "eia_data" => eia(store, job),
        "bea_catalog" | "bea_data" => bea(store, job),
        "sec_bulk" | "sec_index" | "sec_document" => sec(store, job),
        "issuer_catalog" | "issuer_release" => issuer(store, job),
        _ => Err(invalid(
            "Unknown provider job kind; no simulated data returned",
        )),
    }
}
pub fn plan(store: &Store) -> Result<Value> {
    let cutoff = now();
    let mut jobs = vec![];
    jobs.push(store.enqueue("worldbank","wb_catalog","2",&json!({"start":"1960-01-01","end":Utc::now().format("%Y-%m-%d").to_string(),"scope":"all WDI indicators; full-country histories expanded after discovery"}),cutoff,100)?);
    for family in ["wp", "pc", "cu", "ce"] {
        jobs.push(store.enqueue(
            "bls",
            "bls_catalog",
            family,
            &json!({"scope":"all series and listed data files in collection"}),
            cutoff,
            70,
        )?);
    }
    for direction in ["imports", "exports"] {
        jobs.push(store.enqueue("census","census_catalog",direction,&json!({"start":"2010-01","end":Utc::now().format("%Y-%m").to_string(),"scope":"HS commodity and geography catalogs before data expansion"}),cutoff,60)?);
    }
    for status in ["active", "inactive"] {
        jobs.push(store.enqueue("alpaca","alpaca_assets",status,&json!({"start":"2016-01-01","end":Utc::now().to_rfc3339(),"scope":"all entitled US equity/ETF listings; raw daily and minute"}),cutoff-15*60_000,90)?);
    }
    for resource in ["companyfacts", "submissions"] {
        jobs.push(store.enqueue(
            "sec",
            "sec_bulk",
            resource,
            &json!({"scope":"all members of provider nightly bulk archive"}),
            cutoff,
            80,
        )?);
    }
    jobs.push(store.enqueue(
        "eia",
        "eia_catalog",
        "",
        &json!({"scope":"recursive API v2 metadata; facet-aware histories"}),
        cutoff,
        60,
    )?);
    jobs.push(store.enqueue(
        "bea",
        "bea_catalog",
        "InputOutput",
        &json!({"scope":"all input-output table IDs and available years"}),
        cutoff,
        60,
    )?);
    for host in ["nanya", "micron"] {
        jobs.push(store.enqueue(
            "issuer",
            "issuer_catalog",
            host,
            &json!({"scope":"official archive links; provider completeness unresolved"}),
            cutoff,
            50,
        )?);
    }
    for p in ["fred", "dram"] {
        jobs.push(store.enqueue(
            p,
            "permission_gate",
            "all",
            &json!({"scope":"catalog family discovered; permission blocked"}),
            cutoff,
            0,
        )?);
    }
    Ok(
        json!({"cutoff_utc":Utc.timestamp_millis_opt(cutoff).single().unwrap().to_rfc3339(),"cutoff_ms":cutoff,"jobs":jobs,"universe":"all approved discovered partitions; no watchlist-only scope","market_entitlement_delay_ms":900000,"completed_download":false}),
    )
}
pub fn expand(store: &Store, job: &Job, page: &PageResult) -> Result<usize> {
    let mut n = 0;
    if job.kind == "wb_catalog" {
        let priority: [&str; 8] = [
            "NY.GDP.MKTP.CD",
            "NV.IND.MANF.ZS",
            "TX.VAL.ICTG.ZS.UN",
            "TM.VAL.ICTG.ZS.UN",
            "NE.EXP.GNFS.CD",
            "NE.IMP.GNFS.CD",
            "FP.CPI.TOTL.ZG",
            "EG.USE.ELEC.KH.PC",
        ];
        for s in &page.series {
            store.enqueue("worldbank","wb_history",&s.code,&json!({"start":"1960-01-01","end":Utc.timestamp_millis_opt(job.cutoff).single().unwrap().format("%Y-%m-%d").to_string(),"countries":"all","provider_source":"2"}),job.cutoff,if priority.contains(&s.code.as_str()){50}else{5})?;
            n += 1;
        }
    }
    if job.kind == "alpaca_assets" {
        for s in &page.series {
            let mut c = store.connect()?;
            let tx = c.transaction()?;
            tx.execute(
                "INSERT OR REPLACE INTO instruments VALUES(?1,?2,'alpaca',?3,?4,?5)",
                rusqlite::params![
                    s.metadata["asset_id"].as_str().unwrap_or(&s.id),
                    s.entity,
                    job.resource == "active",
                    now(),
                    serde_json::to_string(&s.metadata)?
                ],
            )?;
            tx.commit()?;
            let end = Utc.timestamp_millis_opt(job.cutoff).single().unwrap();
            for y in 2016..=end.year() {
                for timeframe in ["1Day", "1Min"] {
                    let start = format!("{y}-01-01T00:00:00Z");
                    let stop = if y == end.year() {
                        end.to_rfc3339()
                    } else {
                        format!("{}-01-01T00:00:00Z", y + 1)
                    };
                    store.enqueue("alpaca","alpaca_bars",&s.code,&json!({"timeframe":timeframe,"start":start,"end":stop,"listing_history":"unverified; provider availability only"}),job.cutoff,if timeframe=="1Day"{20}else{1})?;
                    n += 1;
                }
            }
        }
    }
    for child in &page.children {
        store.enqueue(
            &child.provider,
            &child.kind,
            &child.resource,
            &child.params,
            job.cutoff,
            child.priority,
        )?;
        n += 1;
    }
    Ok(n)
}

mod bls;
use bls::bls;
mod census;
use census::census;
mod alpaca;
use alpaca::alpaca;
mod eia;
use eia::eia;
mod bea;
use bea::bea;
mod sec;
use sec::sec;
mod issuer;
use issuer::issuer;

#[cfg(test)]
mod contract_tests;
