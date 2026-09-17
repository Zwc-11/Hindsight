use crate::{
    model::*,
    storage::{check_disk, file_hash, publish},
};
use reqwest::{blocking::Client, header::HeaderMap, Url};
use serde_json::Value;
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    time::Duration,
};

pub fn validate_url(url: &Url) -> Result<()> {
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some_and(|p| p != 443)
    {
        return Err(invalid("Only approved HTTPS source URLs are allowed"));
    }
    let allowed = [
        "api.worldbank.org",
        "download.bls.gov",
        "api.bls.gov",
        "www.bls.gov",
        "api.census.gov",
        "data.sec.gov",
        "www.sec.gov",
        "data.alpaca.markets",
        "paper-api.alpaca.markets",
        "api.eia.gov",
        "api.bea.gov",
        "apps.bea.gov",
        "www.nanya.com",
        "investors.micron.com",
    ];
    if !url.host_str().is_some_and(|h| allowed.contains(&h)) {
        return Err(invalid("Source host is not allowlisted"));
    }
    Ok(())
}
pub fn redact_url(url: &Url) -> String {
    let mut clean = url.clone();
    let pairs: Vec<_> = url
        .query_pairs()
        .map(|(k, v)| {
            let sensitive = ["key", "api_key", "apikey", "userid", "token", "secret"]
                .contains(&k.to_lowercase().as_str());
            (
                k.to_string(),
                if sensitive {
                    "[REDACTED]".into()
                } else {
                    v.to_string()
                },
            )
        })
        .collect();
    clean.set_query(None);
    if !pairs.is_empty() {
        clean.query_pairs_mut().extend_pairs(pairs);
    }
    clean.to_string()
}
pub fn scrub(value: &mut Value, secrets: &[String]) {
    match value {
        Value::Object(map) => {
            for (k, v) in map.iter_mut() {
                if [
                    "api_key",
                    "apikey",
                    "key",
                    "userid",
                    "authorization",
                    "secret",
                    "token",
                ]
                .contains(&k.to_lowercase().as_str())
                {
                    *v = Value::String("[REDACTED]".into());
                } else {
                    scrub(v, secrets);
                }
            }
        }
        Value::Array(a) => {
            for v in a {
                scrub(v, secrets);
            }
        }
        Value::String(s) => {
            for secret in secrets {
                if !secret.is_empty() {
                    *s = s.replace(secret, "[REDACTED]");
                }
            }
        }
        _ => {}
    }
}
pub fn fetch(
    store: &Store,
    job: &Job,
    url: Url,
    headers: HeaderMap,
    body: Option<Value>,
    limit: u64,
) -> Result<Capture> {
    validate_url(&url)?;
    store.alive(job)?;
    let policy = store.access_check(&job.provider)?;
    check_disk(&store.root, limit)?;
    let wait = store.reserve_request(&job.provider)?;
    std::thread::sleep(Duration::from_millis(wait as u64));
    store.alive(job)?;
    let allowed_host = url.host_str().unwrap_or_default().to_string();
    let client = Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(if limit > 32 * 1024 * 1024 {
            1800
        } else {
            50
        }))
        .redirect(reqwest::redirect::Policy::custom(move |a| {
            if a.previous().len() < 5
                && a.url().scheme() == "https"
                && a.url().host_str() == Some(&allowed_host)
                && a.url().username().is_empty()
                && a.url().password().is_none()
            {
                a.follow()
            } else {
                a.stop()
            }
        }))
        .build()
        .map_err(|_| Error::Network("client initialization".into()))?;
    let started = now();
    let mut request = if let Some(body) = body {
        client.post(url.clone()).json(&body)
    } else {
        client.get(url.clone())
    };
    request = request.headers(headers).header(
        "User-Agent",
        if job.provider == "sec" {
            std::env::var("HINDSIGHT_SEC_USER_AGENT").unwrap_or_default()
        } else {
            "Hindsight local economic observatory/0.2".into()
        },
    );
    let mut response = request.send().map_err(|e| {
        Error::Network(
            if e.is_timeout() {
                "timeout"
            } else {
                "connection"
            }
            .into(),
        )
    })?;
    let response_headers = now();
    let status = response.status();
    if !status.is_success() {
        let retry = response
            .headers()
            .get("retry-after")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse::<i64>().ok())
            .unwrap_or(0)
            .saturating_mul(1000)
            .clamp(0, 86_400_000);
        return Err(Error::Http {
            status: status.as_u16(),
            retry_after_ms: retry,
        });
    }
    if response.content_length().is_some_and(|n| n > limit) {
        return Err(Error::Resource(
            "Source object exceeds explicit size budget".into(),
        ));
    }
    let etag = response
        .headers()
        .get("etag")
        .and_then(|v| v.to_str().ok())
        .map(String::from);
    let last_modified = response
        .headers()
        .get("last-modified")
        .and_then(|v| v.to_str().ok())
        .map(String::from);
    let temp = store
        .root
        .join("staging")
        .join(format!("{}.download", uuid::Uuid::new_v4()));
    let mut opts = OpenOptions::new();
    opts.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(&temp)?;
    let mut buf = [0u8; 65536];
    let mut bytes = 0u64;
    let mut heartbeat = now();
    loop {
        let n = response
            .read(&mut buf)
            .map_err(|_| Error::Network("response interrupted".into()))?;
        if n == 0 {
            break;
        }
        bytes += n as u64;
        if bytes > limit {
            return Err(Error::Resource(
                "Source exceeded streaming byte limit".into(),
            ));
        }
        f.write_all(&buf[..n])?;
        if now() - heartbeat > 2000 {
            store.alive(job)?;
            check_disk(&store.root, 1_048_576)?;
            store.connect()?.execute(
                "UPDATE jobs SET lease_until=?2 WHERE id=?1 AND lease_token=?3 AND state='running'",
                rusqlite::params![job.id, now() + 120_000, job.lease_token],
            )?;
            heartbeat = now();
        }
    }
    let completed = now();
    f.sync_all()?;
    drop(f);
    store.alive(job)?;
    let mut secrets: Vec<String> = policy
        .credential_env
        .iter()
        .filter(|k| !k.contains("USER_AGENT"))
        .filter_map(|k| std::env::var(k).ok())
        .filter(|v| !v.is_empty())
        .collect();
    if job.provider == "census" {
        if let Ok(key) = std::env::var("CENSUS_API_KEY") {
            if !key.is_empty() {
                secrets.push(key);
            }
        }
    }
    let original = file_hash(&temp)?;
    let mut representation = "original_bytes".to_string();
    let mut original_sha = None;
    if ["eia", "bea", "census"].contains(&job.provider.as_str()) && !secrets.is_empty() {
        if bytes > 32 * 1024 * 1024 {
            return Err(Error::Resource(
                "Secret-bearing response exceeds bounded sanitization budget".into(),
            ));
        }
        let mut v: Value = serde_json::from_reader(File::open(&temp)?)?;
        scrub(&mut v, &secrets);
        let sanitized = serde_json::to_vec(&v)?;
        let mut f = OpenOptions::new().write(true).truncate(true).open(&temp)?;
        f.write_all(&sanitized)?;
        f.sync_all()?;
        bytes = sanitized.len() as u64;
        representation = "sanitized_json; query credentials removed".into();
        original_sha = Some(original);
    }
    let sha = file_hash(&temp)?;
    let relative = format!("raw/{}/{}.body", &sha[..2], sha);
    let target = store.root.join(&relative);
    fs::create_dir_all(target.parent().expect("raw parent"))?;
    if !target.exists() {
        fs::hard_link(&temp, &target)?;
    } else if file_hash(&target)? != sha {
        return Err(invalid("Existing raw object is corrupt"));
    }
    fs::remove_file(&temp)?;
    File::open(target.parent().expect("raw parent"))?.sync_all()?;
    let capture = Capture {
        id: uuid::Uuid::new_v4().to_string(),
        provider: job.provider.clone(),
        url: redact_url(&url),
        sha256: sha,
        path: relative,
        bytes,
        request_started_at: started,
        response_headers_at: response_headers,
        response_completed_at: completed,
        etag,
        last_modified,
        representation,
        original_sha256: original_sha,
    };
    publish(
        &store
            .root
            .join("staging")
            .join(format!("{}.receipt.json", capture.id)),
        &serde_json::to_vec_pretty(&capture)?,
    )?;
    Ok(capture)
}
