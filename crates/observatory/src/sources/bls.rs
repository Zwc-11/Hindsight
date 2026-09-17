use super::*;
use std::io::BufReader;

fn reusable(store: &Store, job: &Job, url: Url, limit: u64) -> Result<Capture> {
    if let Some(v) = job.cursor.get("capture") {
        let c: Capture = serde_json::from_value(v.clone())?;
        if c.provider != job.provider || !store.root.join(&c.path).is_file() {
            return Err(invalid("Invalid stored source checkpoint"));
        }
        Ok(c)
    } else {
        fetch(store, job, url, HeaderMap::new(), None, limit)
    }
}
pub(super) fn bls(store: &Store, job: &Job) -> Result<(Capture, PageResult)> {
    let family = job.resource.split('/').next().unwrap_or_default();
    if !["wp", "pc", "cu", "ce"].contains(&family) {
        return Err(invalid("Unsupported BLS collection"));
    }
    let phase = job.cursor["phase"].as_str().unwrap_or("rows");
    if job.kind == "bls_catalog" && phase == "listing" {
        let url = Url::parse(&format!(
            "https://download.bls.gov/pub/time.series/{family}/"
        ))
        .map_err(|_| invalid("BLS URL"))?;
        let c = fetch(
            store,
            job,
            url.clone(),
            HeaderMap::new(),
            None,
            8 * 1024 * 1024,
        )?;
        let text = std::fs::read_to_string(store.root.join(&c.path))?;
        let doc = scraper::Html::parse_document(&text);
        let selector = scraper::Selector::parse("a[href]").map_err(|_| invalid("BLS selector"))?;
        let mut page = page_default();
        let mut names = BTreeSet::new();
        for a in doc.select(&selector) {
            let href = a.value().attr("href").unwrap_or_default();
            let absolute = url.join(href).map_err(|_| invalid("Invalid BLS link"))?;
            if absolute.host_str() != url.host_str() {
                continue;
            }
            let name = absolute.path().rsplit('/').next().unwrap_or_default();
            if name.starts_with(&format!("{family}.data.")) && names.insert(name.to_string()) {
                page.children.push(PlannedJob{provider:"bls".into(),kind:"bls_data".into(),resource:format!("{family}/{name}"),params:json!({"source_listing":url.as_str(),"scope":"all rows in named official bulk file","original_vintage":false}),priority:15});
            }
        }
        if names.is_empty() {
            return Err(invalid(
                "BLS directory contained no recognized data files; not complete",
            ));
        }
        page.note = format!(
            "Discovered {} official data objects; data histories remain queued",
            names.len()
        );
        page.received_rows = names.len() as i64;
        return Ok((c, page));
    }
    let filename = if job.kind == "bls_catalog" {
        format!("{family}.series")
    } else {
        job.resource
            .split('/')
            .nth(1)
            .ok_or_else(|| invalid("BLS data file missing"))?
            .into()
    };
    if filename.contains('/')
        || filename.contains("..")
        || !filename.starts_with(&format!("{family}."))
    {
        return Err(invalid("Unsafe BLS filename"));
    }
    let url = Url::parse(&format!(
        "https://download.bls.gov/pub/time.series/{family}/{filename}"
    ))
    .map_err(|_| invalid("BLS URL"))?;
    let capture = reusable(store, job, url.clone(), 256 * 1024 * 1024)?;
    let mut f = File::open(store.root.join(&capture.path))?;
    let offset = job.cursor["byte_offset"].as_u64().unwrap_or(0);
    let mut header = Vec::new();
    if offset > 0 {
        header = job.cursor["headers"]
            .as_array()
            .ok_or_else(|| invalid("BLS header checkpoint missing"))?
            .iter()
            .map(|v| v.as_str().unwrap_or_default().to_string())
            .collect();
        f.seek(SeekFrom::Start(offset))?;
    }
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(b'\t')
        .trim(csv::Trim::All)
        .has_headers(offset == 0)
        .flexible(true)
        .from_reader(BufReader::new(f));
    if offset == 0 {
        header = reader
            .headers()
            .map_err(|e| invalid(e.to_string()))?
            .iter()
            .map(String::from)
            .collect();
    }
    let idx = |field: &str| {
        header
            .iter()
            .position(|v| v == field)
            .ok_or_else(|| invalid(format!("BLS missing {field}")))
    };
    let id_idx = idx("series_id")?;
    let mut out = page_default();
    let mut seen = BTreeSet::new();
    let mut count = 0;
    let mut record = csv::StringRecord::new();
    let mut eof = false;
    while count < 10_000 {
        if !reader
            .read_record(&mut record)
            .map_err(|e| invalid(format!("Malformed BLS row: {e}")))?
        {
            eof = true;
            break;
        }
        let get = |i: usize| record.get(i).unwrap_or_default();
        let code = get(id_idx);
        if code.is_empty() {
            return Err(invalid("Empty BLS series identifier"));
        }
        let id = format!("BLS:{family}:{code}");
        if job.kind == "bls_catalog" {
            let title = header
                .iter()
                .position(|v| v == "series_title" || v == "series_name")
                .map(&get)
                .filter(|v| !v.is_empty())
                .unwrap_or(code);
            let meta = header
                .iter()
                .enumerate()
                .map(|(i, k)| (k.clone(), Value::String(get(i).into())))
                .collect::<serde_json::Map<_, _>>();
            out.series.push(series(
                id,
                job,
                code,
                title,
                "USA",
                "index or source-defined",
                "monthly",
                url.as_str(),
                Value::Object(meta),
            ));
        } else {
            let year = get(idx("year")?);
            let period = get(idx("period")?);
            let raw = get(idx("value")?);
            let y: i32 = year.parse().map_err(|_| invalid("BLS year invalid"))?;
            let (start, end, frequency) = if period == "M13" {
                let (a, b) = year_bounds(year)?;
                (a, b, "annual_average")
            } else {
                let m: u32 = period
                    .strip_prefix('M')
                    .ok_or_else(|| invalid("Unsupported BLS period; not coerced"))?
                    .parse()
                    .map_err(|_| invalid("BLS period invalid"))?;
                let first =
                    NaiveDate::from_ymd_opt(y, m, 1).ok_or_else(|| invalid("BLS month invalid"))?;
                let next = if m == 12 {
                    NaiveDate::from_ymd_opt(y + 1, 1, 1)
                } else {
                    NaiveDate::from_ymd_opt(y, m + 1, 1)
                }
                .ok_or_else(|| invalid("BLS calendar overflow"))?;
                (
                    first.to_string(),
                    next.pred_opt()
                        .ok_or_else(|| invalid("BLS month end"))?
                        .to_string(),
                    "monthly",
                )
            };
            let value = if ["-", "(S)", "", "(NA)"].contains(&raw) {
                None
            } else {
                Some(
                    raw.parse::<f64>()
                        .map_err(|_| invalid("BLS unexpected numeric/suppression token"))?,
                )
            };
            let sid = if frequency == "annual_average" {
                format!("{id}:M13")
            } else {
                id
            };
            if seen.insert(sid.clone()) {
                out.series.push(series(sid.clone(),job,code,code,"USA","index or source-defined",frequency,url.as_str(),json!({"collection":family,"needs_series_metadata_join":true,"original_vintage":false})));
            }
            let mut obs = observation(
                sid,
                start,
                end,
                value,
                "index or source-defined",
                json!({"footnotes":header.iter().position(|s|s=="footnote_codes").map(&get),"raw_period":period,"raw_value":raw,"original_vintage":false}),
            );
            obs.dimensions = json!({"period_type":frequency});
            out.observations.push(obs);
        }
        count += 1;
    }
    if !eof {
        out.next = Some(
            json!({"byte_offset":offset+reader.position().byte(),"headers":header,"capture":capture,"phase":"rows"}),
        );
    } else if job.kind == "bls_catalog" {
        out.next = Some(json!({"phase":"listing"}));
    }
    out.received_rows = count as i64;
    out.complete_scope = eof;
    out.note="Bounded BLS bulk rows; provider-current vintage, units require collection definitions; publication timestamps unknown".into();
    Ok((capture, out))
}
