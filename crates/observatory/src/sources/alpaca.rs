use super::*;

pub fn normalize_bars(job: &Job, v: &Value, url: &str) -> Result<PageResult> {
    let rows = v["bars"]
        .as_object()
        .ok_or_else(|| invalid("Alpaca bars object missing"))?;
    let timeframe = job.params["timeframe"]
        .as_str()
        .ok_or_else(|| invalid("Alpaca timeframe missing"))?;
    if !["1Day", "1Min"].contains(&timeframe) {
        return Err(invalid("Unsupported market interval"));
    }
    let mut out = page_default();
    for (symbol, values) in rows {
        let id = format!("ALPACA:{symbol}:{timeframe}");
        out.series.push(series(id.clone(),job,symbol,symbol,symbol,"USD",timeframe,url,json!({"feed":"sip","adjustment":"raw","original_vintage":false,"instrument_identity":"provider symbol; listing-history validation required","bar_label":"left edge","execution_note":"bar opens are research references, not guaranteed fills"})));
        for r in values
            .as_array()
            .ok_or_else(|| invalid("Alpaca bar rows must be an array"))?
        {
            let start = time(s(r, "t")?)?;
            let end = if timeframe == "1Min" {
                start
                    .checked_add(60000)
                    .ok_or_else(|| invalid("Bar timestamp overflow"))?
            } else {
                // Conservative daily eligibility, not an invented exchange close.
                start
                    .checked_add(2 * 86_400_000)
                    .ok_or_else(|| invalid("Daily timestamp overflow"))?
            };
            if start >= job.cutoff {
                continue;
            }
            if timeframe == "1Min" && end > job.cutoff {
                continue;
            }
            if timeframe == "1Day" && end > job.cutoff {
                continue;
            }
            for k in ["o", "h", "l", "c"] {
                hindsight_economic::ingest::decimal_micros(&r[k]).map_err(invalid)?;
            }
            let o = num(&r["o"]).ok_or_else(|| invalid("Invalid open"))?;
            let h = num(&r["h"]).ok_or_else(|| invalid("Invalid high"))?;
            let l = num(&r["l"]).ok_or_else(|| invalid("Invalid low"))?;
            let close = num(&r["c"]).ok_or_else(|| invalid("Invalid close"))?;
            if l > h
                || o < l
                || o > h
                || close < l
                || close > h
                || l <= 0.0
                || r["v"].as_u64().is_none()
            {
                return Err(invalid("Invalid OHLC/volume"));
            }
            if r.get("n")
                .is_some_and(|n| !n.is_null() && n.as_u64().is_none())
            {
                return Err(invalid("Invalid supplied trade count"));
            }
            if r.get("vw")
                .is_some_and(|n| !n.is_null() && num(n).is_none())
            {
                return Err(invalid("Invalid supplied VWAP"));
            }
            let finish = Utc
                .timestamp_millis_opt(end)
                .single()
                .ok_or_else(|| invalid("Bar end"))?
                .to_rfc3339();
            let mut point = observation(
                id.clone(),
                s(r, "t")?.into(),
                finish,
                Some(close),
                "USD",
                json!({"o":r["o"],"h":r["h"],"l":r["l"],"c":r["c"],"v":r["v"],"n":r["n"],"vw":r["vw"],"start_ms":start,"complete_not_before_ms":end,"feed":"sip","original_vintage":false,"availability":"current-vintage receipt only; historical model not approved"}),
            );
            point.dimensions = json!({"interval":timeframe,"feed":"sip","adjustment":"raw"});
            out.observations.push(point);
        }
        out.received_rows += values.as_array().unwrap().len() as i64;
    }
    match v.get("next_page_token") {
        None | Some(Value::Null) => {}
        Some(Value::String(t)) if !t.is_empty() => out.next = Some(json!({"page_token":t})),
        _ => return Err(invalid("Invalid Alpaca page token")),
    }
    out.note="All supplied bar fields retained; no original-vintage or historical-universe claim. Empty history requires coverage review.".into();
    Ok(out)
}
pub(super) fn alpaca(store: &Store, job: &Job) -> Result<(Capture, PageResult)> {
    let mut headers = HeaderMap::new();
    for (header, key) in [
        ("APCA-API-KEY-ID", "APCA_API_KEY_ID"),
        ("APCA-API-SECRET-KEY", "APCA_API_SECRET_KEY"),
    ] {
        let value = std::env::var(key)
            .map_err(|_| Error::Blocked(format!("blocked_auth: missing {key}")))?;
        headers.insert(
            reqwest::header::HeaderName::from_bytes(header.as_bytes())
                .map_err(|_| invalid("Internal header name"))?,
            HeaderValue::from_str(&value)
                .map_err(|_| invalid("Invalid credential header; value suppressed"))?,
        );
    }
    if job.kind == "alpaca_assets" {
        if !["active", "inactive"].contains(&job.resource.as_str()) {
            return Err(invalid("Unknown asset status"));
        }
        let mut url = Url::parse("https://paper-api.alpaca.markets/v2/assets")
            .map_err(|_| invalid("Asset URL"))?;
        url.query_pairs_mut()
            .append_pair("status", &job.resource)
            .append_pair("asset_class", "us_equity");
        let c = fetch(store, job, url.clone(), headers, None, 32 * 1024 * 1024)?;
        let v: Value = serde_json::from_reader(File::open(store.root.join(&c.path))?)?;
        let rows = v
            .as_array()
            .ok_or_else(|| invalid("Alpaca asset inventory must be an array"))?;
        let mut out = page_default();
        for r in rows {
            let symbol = s(r, "symbol")?;
            let asset = s(r, "id")?;
            out.series.push(series(format!("ALPACA:ASSET:{asset}"),job,symbol,r["name"].as_str().unwrap_or(symbol),asset,"listing metadata","metadata",url.as_str(),json!({"asset_id":asset,"symbol":symbol,"name":r["name"],"exchange":r["exchange"],"status":r["status"],"tradable":r["tradable"],"role":"instrument_catalog","historical_membership":"not established"})));
        }
        out.received_rows = rows.len() as i64;
        out.advertised_total = Some(rows.len() as i64);
        out.complete_scope = true;
        out.note="Current provider asset inventory only; inactive availability does not establish a survivorship-free historical universe".into();
        return Ok((c, out));
    }
    if job.resource.is_empty()
        || !job
            .resource
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".-".contains(&b))
    {
        return Err(invalid("Invalid market symbol"));
    }
    let start = s(&job.params, "start")?;
    let end = s(&job.params, "end")?;
    if time(start)? >= time(end)? || time(end)? > job.cutoff || time(end)? > now() - 15 * 60000 {
        return Err(invalid("Historical SIP end must respect the frozen cutoff and fifteen-minute access restriction"));
    }
    let mut url = Url::parse("https://data.alpaca.markets/v2/stocks/bars")
        .map_err(|_| invalid("Bars URL"))?;
    url.query_pairs_mut().extend_pairs([
        ("symbols", job.resource.as_str()),
        ("start", start),
        ("end", end),
        ("timeframe", s(&job.params, "timeframe")?),
        ("feed", "sip"),
        ("adjustment", "raw"),
        ("sort", "asc"),
        ("limit", "10000"),
    ]);
    if let Some(t) = job.cursor["page_token"].as_str() {
        url.query_pairs_mut().append_pair("page_token", t);
    }
    let c = fetch(store, job, url.clone(), headers, None, 32 * 1024 * 1024)?;
    let v: Value = serde_json::from_reader(File::open(store.root.join(&c.path))?)?;
    let out = normalize_bars(job, &v, url.as_str())?;
    Ok((c, out))
}
