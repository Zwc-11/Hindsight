//! Read-only public-data capture. Credentials stay in environment/header memory.
use crate::types::*;
use chrono::{DateTime, Utc};
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::{self, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};
const MAX_BYTES: u64 = 32 * 1024 * 1024;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Receipt {
    pub url: String,
    pub sha256: String,
    pub first_seen_at: Time,
    pub bytes: usize,
    pub source_permission_class: String,
    pub body_path: String,
}
pub fn immutable(path: &Path, bytes: &[u8]) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    match OpenOptions::new().write(true).create_new(true).open(path) {
        Ok(mut f) => {
            f.write_all(bytes).map_err(|e| e.to_string())?;
            f.sync_all().map_err(|e| e.to_string())?;
            Ok(())
        }
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            if fs::read(path).map_err(|e| e.to_string())? == bytes {
                Ok(())
            } else {
                Err(format!("refusing to overwrite {}", path.display()))
            }
        }
        Err(e) => Err(e.to_string()),
    }
}
fn client() -> Result<Client> {
    Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|e| e.to_string())
}
fn save(url: &str, bytes: &[u8], out: &Path, permission: &str) -> Result<Receipt> {
    let hash = format!("{:x}", Sha256::digest(bytes));
    let body_path = out.join("raw").join(format!("{hash}.body"));
    immutable(&body_path, bytes)?;
    let receipt = Receipt {
        url: url.into(),
        sha256: hash.clone(),
        first_seen_at: Utc::now().timestamp_millis(),
        bytes: bytes.len(),
        source_permission_class: permission.into(),
        body_path: body_path.to_string_lossy().into(),
    };
    let name = format!("{}-{}.json", receipt.first_seen_at, hash);
    immutable(
        &out.join("receipts").join(name),
        &serde_json::to_vec_pretty(&receipt).map_err(|e| e.to_string())?,
    )?;
    Ok(receipt)
}
pub fn capture_public(url: &str, out: &Path) -> Result<Receipt> {
    let parsed = reqwest::Url::parse(url).map_err(|e| e.to_string())?;
    let host = parsed.host_str().ok_or("missing host")?;
    if parsed.scheme() != "https"
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || ![
            "data.sec.gov",
            "www.sec.gov",
            "www.nanya.com",
            "investors.micron.com",
        ]
        .contains(&host)
    {
        return Err("public capture supports only allowlisted HTTPS sources".into());
    }
    let agent = if host.ends_with("sec.gov") {
        let a = std::env::var("HINDSIGHT_SEC_USER_AGENT").map_err(|_| {
            "Set HINDSIGHT_SEC_USER_AGENT to your application and real contact email"
        })?;
        if !a.contains('@') {
            return Err("SEC user agent must include a real contact email".into());
        }
        a
    } else {
        "Hindsight research source capture/0.1".into()
    };
    std::thread::sleep(Duration::from_millis(250));
    let permitted_host = host.to_string();
    let public_client = Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::custom(move |attempt| {
            if attempt.previous().len() >= 5 {
                attempt.error("public source redirect limit")
            } else if attempt.url().scheme() == "https"
                && attempt.url().host_str() == Some(permitted_host.as_str())
                && attempt.url().username().is_empty()
                && attempt.url().password().is_none()
            {
                attempt.follow()
            } else {
                attempt.stop()
            }
        }))
        .build()
        .map_err(|e| e.to_string())?;
    let response = public_client
        .get(url)
        .header("User-Agent", agent)
        .send()
        .map_err(|e| e.to_string())?;
    if !response.status().is_success() {
        return Err(format!(
            "source HTTP {}; no data fabricated",
            response.status()
        ));
    }
    let mut body = vec![];
    response
        .take(MAX_BYTES + 1)
        .read_to_end(&mut body)
        .map_err(|e| e.to_string())?;
    if body.len() as u64 > MAX_BYTES {
        return Err("source exceeds bounded capture size".into());
    }
    save(
        url,
        &body,
        out,
        "public-readable; redistribution not established",
    )
}

pub fn decimal_micros(value: &Value) -> Result<i64> {
    let text = match value {
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        _ => return Err("expected decimal price".into()),
    };
    if text.contains(['e', 'E']) {
        return Err("exponent decimal requires explicit normalization".into());
    }
    let (whole, fraction) = text.split_once('.').unwrap_or((&text, ""));
    if fraction.len() > 6 || !fraction.bytes().all(|b| b.is_ascii_digit()) {
        return Err("price exceeds micro precision".into());
    }
    let integer: i128 = whole.parse().map_err(|_| "invalid decimal integer")?;
    let frac = if fraction.is_empty() {
        0
    } else {
        fraction
            .parse::<i128>()
            .map_err(|_| "invalid decimal fraction")?
            * 10i128.pow((6 - fraction.len()) as u32)
    };
    let sign = if text.starts_with('-') { -1 } else { 1 };
    i64::try_from(integer * 1_000_000 + sign * frac).map_err(|_| "decimal overflow".into())
}
pub fn alpaca_page(value: &Value, source_id: &str) -> Result<Vec<Bar>> {
    let object = value
        .get("bars")
        .and_then(Value::as_object)
        .ok_or("missing bars object")?;
    let mut bars = vec![];
    for (symbol, rows) in object {
        for row in rows.as_array().ok_or("bars must be an array")? {
            let start =
                DateTime::parse_from_rfc3339(row["t"].as_str().ok_or("bar missing timestamp")?)
                    .map_err(|e| e.to_string())?
                    .timestamp_millis();
            let mut prices = vec![];
            for k in ["o", "h", "l", "c"] {
                prices.push(decimal_micros(&row[k])?);
            }
            let end = start.checked_add(MINUTE).ok_or("bar timestamp overflow")?;
            bars.push(Bar {
                symbol: symbol.clone(),
                start,
                end,
                available_at: end,
                open: prices[0],
                high: prices[1],
                low: prices[2],
                close: prices[3],
                volume: row["v"].as_u64().ok_or("invalid volume")?,
                source_id: source_id.into(),
            });
        }
    }
    Ok(bars)
}
#[derive(Default)]
pub struct Pagination {
    seen: BTreeSet<String>,
    pages: usize,
}
impl Pagination {
    pub fn next(&mut self, value: &Value) -> Result<Option<String>> {
        self.pages += 1;
        if self.pages > 10000 {
            return Err("pagination page limit exceeded".into());
        }
        match value.get("next_page_token") {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(s)) if !s.is_empty() => {
                if !self.seen.insert(s.clone()) {
                    return Err("pagination cycle".into());
                }
                Ok(Some(s.clone()))
            }
            _ => Err("invalid pagination token".into()),
        }
    }
}
pub fn fetch_alpaca(symbols: &str, start: &str, end: &str, out: &Path) -> Result<PathBuf> {
    let end_time = DateTime::parse_from_rfc3339(end)
        .map_err(|e| e.to_string())?
        .timestamp_millis();
    let start_time = DateTime::parse_from_rfc3339(start)
        .map_err(|e| e.to_string())?
        .timestamp_millis();
    if start_time >= end_time || end_time > Utc::now().timestamp_millis() - 15 * MINUTE {
        return Err("historical SIP range must end at least fifteen minutes ago".into());
    }
    if symbols.is_empty()
        || symbols.len() > 300
        || !symbols.bytes().all(|b| {
            b.is_ascii_uppercase() || b.is_ascii_digit() || b == b',' || b == b'.' || b == b'-'
        })
    {
        return Err("invalid symbols".into());
    }
    let key = std::env::var("APCA_API_KEY_ID").map_err(|_| "APCA_API_KEY_ID is not set")?;
    let secret =
        std::env::var("APCA_API_SECRET_KEY").map_err(|_| "APCA_API_SECRET_KEY is not set")?;
    let http = client()?;
    let mut pagination = Pagination::default();
    let mut token: Option<String> = None;
    let mut bars = vec![];
    let mut receipts = vec![];
    loop {
        let mut url = reqwest::Url::parse("https://data.alpaca.markets/v2/stocks/bars")
            .map_err(|e| e.to_string())?;
        url.query_pairs_mut().extend_pairs([
            ("symbols", symbols),
            ("start", start),
            ("end", end),
            ("timeframe", "1Min"),
            ("feed", "sip"),
            ("adjustment", "raw"),
            ("limit", "10000"),
            ("sort", "asc"),
        ]);
        if let Some(t) = &token {
            url.query_pairs_mut().append_pair("page_token", t);
        }
        std::thread::sleep(Duration::from_millis(350));
        let response = http
            .get(url.clone())
            .header("APCA-API-KEY-ID", &key)
            .header("APCA-API-SECRET-KEY", &secret)
            .send()
            .map_err(|_| "Alpaca request failed; credentials were not logged")?;
        if !response.status().is_success() {
            return Err(format!(
                "Alpaca HTTP {}; partial captures retained; resume explicitly",
                response.status()
            ));
        }
        let mut bytes = vec![];
        response
            .take(MAX_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        if bytes.len() as u64 > MAX_BYTES {
            return Err("Alpaca page size limit".into());
        }
        let receipt = save(
            url.as_str(),
            &bytes,
            out,
            "Alpaca account license; no redistribution assumed",
        )?;
        let value: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
        bars.extend(alpaca_page(&value, &receipt.sha256)?);
        receipts.push(receipt);
        token = pagination.next(&value)?;
        if token.is_none() {
            break;
        }
    }
    bars.sort_by(|a, b| (&a.symbol, a.start).cmp(&(&b.symbol, b.start)));
    for pair in bars.windows(2) {
        if pair[0].symbol == pair[1].symbol && pair[0].start == pair[1].start && pair[0] != pair[1]
        {
            return Err("conflicting vendor bar versions; review raw captures".into());
        }
    }
    bars.dedup();
    let payload = serde_json::json!({"schema_version":1,"feed":"sip","adjustment":"raw","availability_policy":"reconstructed completed-bar timing; original publication vintage not verified","bars":bars,"receipts":receipts,"requires":"session filtering, corporate-action data, source registry and coverage review before research"});
    let path = out.join("normalized-bars.json");
    immutable(
        &path,
        &serde_json::to_vec_pretty(&payload).map_err(|e| e.to_string())?,
    )?;
    Ok(path)
}
