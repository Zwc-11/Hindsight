use super::*;
use crate::storage::safe_member;

pub fn companyfacts(job: &Job, v: &Value) -> Result<PageResult> {
    let cik = v["cik"]
        .as_u64()
        .ok_or_else(|| invalid("SEC CIK missing"))?;
    let entity = format!("CIK{cik:010}");
    let facts = v["facts"]
        .as_object()
        .ok_or_else(|| invalid("SEC facts object missing"))?;
    let mut out = page_default();
    for (taxonomy, tags) in facts {
        for (tag, info) in tags
            .as_object()
            .ok_or_else(|| invalid("SEC taxonomy malformed"))?
        {
            let units = info["units"]
                .as_object()
                .ok_or_else(|| invalid("SEC units missing"))?;
            for (unit, rows) in units {
                let id = format!("SEC:{entity}:{taxonomy}:{tag}:{unit}");
                let label = info["label"].as_str().unwrap_or(tag);
                out.series.push(series(id.clone(),job,tag,label,&entity,unit,"filing; reporting spans vary",&format!("https://www.sec.gov/edgar/browse/?CIK={cik}"),json!({"description":info["description"],"taxonomy":taxonomy,"company":v["entityName"],"context_scope":"consolidated standard-taxonomy facts","original_filing_context":false})));
                for row in rows
                    .as_array()
                    .ok_or_else(|| invalid("SEC unit rows invalid"))?
                {
                    let end = s(row, "end")?;
                    let start = row["start"].as_str().unwrap_or(end);
                    let filed = s(row, "filed")?;
                    let published = hindsight_economic::evidence::conservative_date_end(
                        filed,
                        "America/New_York",
                        0,
                    )
                    .map_err(invalid)?;
                    if published > job.cutoff {
                        continue;
                    }
                    let accn = s(row, "accn")?;
                    if !accn.bytes().all(|b| b.is_ascii_digit() || b == b'-') {
                        return Err(invalid("Invalid SEC accession"));
                    }
                    let mut o = observation(
                        id.clone(),
                        start.into(),
                        end.into(),
                        Some(num(&row["val"]).ok_or_else(|| invalid("Invalid SEC numeric fact"))?),
                        unit,
                        json!({"accession":accn,"filed":filed,"form":row["form"],"fiscal_year":row["fy"],"fiscal_period":row["fp"],"locator":format!("facts/{taxonomy}/{tag}/units/{unit}"),"original_document_verified":false}),
                    );
                    o.published_at = Some(published);
                    o.reconstructed_at = Some(published + 60000);
                    o.source_order = Some(published);
                    o.precision = "publisher_date_end_conservative".into();
                    o.quality =
                        "aggregate_filing_fact; original-context verification required".into();
                    o.dimensions = json!({"period_kind":if row.get("start").is_some(){"duration"}else{"instant"},"context":"entity-wide"});
                    out.observations.push(o);
                }
            }
        }
    }
    out.received_rows = out.observations.len() as i64;
    out.note="All supplied consolidated taxonomy/unit facts; filed date is conservative, same-date conflicting values are quarantined; historical aggregate revisions not authenticated".into();
    Ok(out)
}
fn submissions(job: &Job, v: &Value) -> Result<PageResult> {
    let cik = v["cik"]
        .as_str()
        .map(String::from)
        .or_else(|| v["cik"].as_u64().map(|v| v.to_string()))
        .or_else(|| job.params["cik"].as_str().map(String::from))
        .ok_or_else(|| invalid("SEC submissions CIK missing"))?;
    let mut out = page_default();
    let entity = format!("CIK{cik}");
    let recent = v.pointer("/filings/recent").unwrap_or(v);
    let accns = recent["accessionNumber"]
        .as_array()
        .ok_or_else(|| invalid("SEC submissions accession array missing"))?;
    for (i, accn) in accns.iter().enumerate() {
        let accession = accn
            .as_str()
            .ok_or_else(|| invalid("SEC accession malformed"))?;
        let document = recent["primaryDocument"][i].as_str().unwrap_or_default();
        let date = recent["filingDate"][i].as_str().unwrap_or_default();
        if !accession.bytes().all(|b| b.is_ascii_digit() || b == b'-')
            || document.contains("..")
            || document.contains('/')
        {
            return Err(invalid("Invalid SEC filing locator"));
        }
        let acceptance = recent["acceptanceDateTime"][i]
            .as_str()
            .and_then(|s| time(s).ok());
        let publication = acceptance.or_else(|| {
            hindsight_economic::evidence::conservative_date_end(date, "America/New_York", 0).ok()
        });
        if publication.is_some_and(|p| p > job.cutoff) {
            continue;
        }
        let path = format!(
            "edgar/data/{}/{}/{}",
            cik.trim_start_matches('0'),
            accession.replace('-', ""),
            document
        );
        out.series.push(series(format!("SEC:FILING:{accession}"),job,accession,document,&entity,"filing metadata","metadata",&format!("https://www.sec.gov/Archives/{path}"),json!({"form":recent["form"][i],"filed":date,"published_at":publication,"role":"filing_catalog","original_document_verified":false})));
        if !document.is_empty() {
            out.children.push(PlannedJob{provider:"sec".into(),kind:"sec_document".into(),resource:path,params:json!({"cik":cik,"accession":accession,"published_at":publication,"scope":"original filing capture and source-indexable text; custom numeric contexts require review"}),priority:0});
        }
    }
    if let Some(files) = v.pointer("/filings/files").and_then(Value::as_array) {
        for file in files {
            let name = s(file, "name")?;
            safe_member(name)?;
            out.children.push(PlannedJob {
                provider: "sec".into(),
                kind: "sec_index".into(),
                resource: name.into(),
                params: json!({"cik":cik,"scope":"historical filing shard"}),
                priority: 25,
            });
        }
    }
    out.received_rows = accns.len() as i64;
    out.note="Filing inventory and original-document jobs; historical shards preserved, not silently discarded".into();
    Ok(out)
}

pub(super) fn sec(store: &Store, job: &Job) -> Result<(Capture, PageResult)> {
    if job.kind == "sec_bulk" {
        if !["companyfacts", "submissions"].contains(&job.resource.as_str()) {
            return Err(invalid("Unknown SEC archive"));
        }
        let url = if job.resource == "companyfacts" {
            "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
        } else {
            "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
        };
        let capture = if let Some(c) = job.cursor.get("capture") {
            serde_json::from_value::<Capture>(c.clone())?
        } else {
            fetch(
                store,
                job,
                Url::parse(url).map_err(|_| invalid("SEC bulk URL"))?,
                HeaderMap::new(),
                None,
                4 * 1024 * 1024 * 1024,
            )?
        };
        let file = File::open(store.root.join(&capture.path))?;
        let mut archive =
            zip::ZipArchive::new(file).map_err(|_| invalid("SEC ZIP corrupt/truncated"))?;
        if archive.len() > 100_000 {
            return Err(Error::Resource("Archive exceeds entry-count budget".into()));
        }
        let member_index = job.cursor["member"].as_u64().unwrap_or(0) as usize;
        let offset = job.cursor["row_offset"].as_u64().unwrap_or(0) as usize;
        if member_index >= archive.len() {
            return Err(invalid("Archive checkpoint exceeds member count"));
        }
        let members = archive.len();
        let mut member = archive
            .by_index(member_index)
            .map_err(|_| invalid("SEC member invalid"))?;
        safe_member(member.name())?;
        if member.is_dir()
            || !member.name().ends_with(".json")
            || member.size() > 64 * 1024 * 1024
            || member.unix_mode().is_some_and(|m| m & 0o170000 == 0o120000)
        {
            return Err(invalid(
                "SEC archive member is not an allowed bounded JSON file",
            ));
        }
        let mut buf = Vec::new();
        std::io::Read::by_ref(&mut member)
            .take(64 * 1024 * 1024 + 1)
            .read_to_end(&mut buf)?;
        if buf.len() as u64 != member.size() {
            return Err(invalid("SEC member length/CRC mismatch"));
        }
        let v: Value = serde_json::from_slice(&buf)?;
        let mut page = if job.resource == "companyfacts" {
            companyfacts(job, &v)?
        } else {
            submissions(job, &v)?
        };
        page.complete_scope = true;
        let total = page.observations.len();
        if offset > total {
            return Err(invalid("SEC row checkpoint exceeds parsed member"));
        }
        if total > 20_000 {
            page.observations = page
                .observations
                .into_iter()
                .skip(offset)
                .take(20_000)
                .collect();
            page.received_rows = page.observations.len() as i64;
        }
        if offset + page.observations.len() < total {
            page.next = Some(
                json!({"capture":capture,"member":member_index,"row_offset":offset+page.observations.len()}),
            );
        } else if member_index + 1 < members {
            page.next = Some(json!({"capture":capture,"member":member_index+1,"row_offset":0}));
        }
        page.note = format!(
            "{}; archive member {}/{}: {}",
            page.note,
            member_index + 1,
            members,
            member.name()
        );
        return Ok((capture, page));
    }
    if job.kind == "sec_index" {
        safe_member(&job.resource)?;
        let url = Url::parse(&format!(
            "https://data.sec.gov/submissions/{}",
            job.resource
        ))
        .map_err(|_| invalid("SEC index URL"))?;
        let c = fetch(store, job, url, HeaderMap::new(), None, 32 * 1024 * 1024)?;
        let v: Value = serde_json::from_reader(File::open(store.root.join(&c.path))?)?;
        return Ok((c, submissions(job, &v)?));
    }
    safe_member(&job.resource)?;
    if !job.resource.starts_with("edgar/data/") {
        return Err(invalid("Original filing path outside Archives/edgar/data"));
    }
    let url = Url::parse(&format!("https://www.sec.gov/Archives/{}", job.resource))
        .map_err(|_| invalid("SEC filing URL"))?;
    let c = fetch(
        store,
        job,
        url.clone(),
        HeaderMap::new(),
        None,
        32 * 1024 * 1024,
    )?;
    let text = std::fs::read_to_string(store.root.join(&c.path))?;
    let doc = scraper::Html::parse_document(&text);
    let selector = scraper::Selector::parse("title").map_err(|_| invalid("Title selector"))?;
    let title = doc
        .select(&selector)
        .next()
        .map(|e| e.text().collect::<String>())
        .unwrap_or_else(|| job.resource.clone());
    let mut out = page_default();
    out.series.push(series(format!("SEC:DOCUMENT:{}",job.params["accession"].as_str().unwrap_or(&c.sha256)),job,&job.resource,&title,job.params["cik"].as_str().unwrap_or("unknown"),"document","filing",url.as_str(),json!({"description":"Original filing archived; custom/segment context extraction is not automatically approved","capture_id":c.id,"published_at":job.params["published_at"],"role":"source_document","bytes":c.bytes})));
    out.received_rows = 1;
    out.note="Original filing bytes archived. Numeric inline-XBRL contexts require parser/review; no invented segment facts".into();
    Ok((c, out))
}
