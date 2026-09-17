use super::*;

pub fn nanya_release(job: &Job, text: &str, url: &str) -> Result<PageResult> {
    let html = scraper::Html::parse_document(text);
    let selector = scraper::Selector::parse("meta[name='description']")
        .map_err(|_| invalid("Meta selector"))?;
    let description = html
        .select(&selector)
        .next()
        .and_then(|e| e.value().attr("content"))
        .ok_or_else(|| invalid("Issuer description missing"))?;
    let words: Vec<_> = description.split_whitespace().collect();
    let revenue = words
        .iter()
        .position(|w| *w == "Revenue")
        .ok_or_else(|| invalid("Not a supported monthly revenue disclosure"))?;
    if revenue < 2 || words.get(revenue + 1) != Some(&"NT$") {
        return Err(invalid("Unexpected Nanya units/title grammar"));
    }
    let period = NaiveDate::parse_from_str(
        &format!("{} {} 01", words[revenue - 2], words[revenue - 1]),
        "%B %Y %d",
    )
    .map_err(|_| invalid("Issuer reporting month invalid"))?;
    let value = words
        .get(revenue + 2)
        .ok_or_else(|| invalid("Issuer revenue missing"))?
        .replace(',', "")
        .parse::<f64>()
        .map_err(|_| invalid("Issuer revenue malformed"))?;
    let unit = words.get(revenue + 3).copied().unwrap_or_default();
    if !unit.eq_ignore_ascii_case("Million") {
        return Err(invalid("Unsupported issuer revenue scale"));
    }
    let body_selector = scraper::Selector::parse("body").map_err(|_| invalid("Body selector"))?;
    let body = html
        .select(&body_selector)
        .next()
        .map(|e| e.text().collect::<Vec<_>>().join(" "))
        .ok_or_else(|| invalid("Issuer body missing"))?;
    let dates: Vec<_> = body
        .split_whitespace()
        .filter_map(|w| NaiveDate::parse_from_str(w.trim(), "%Y/%m/%d").ok())
        .collect();
    let publication = *dates
        .iter()
        .find(|d| **d >= period)
        .ok_or_else(|| invalid("Exact issuer publication date could not be established"))?;
    let published = hindsight_economic::evidence::conservative_date_end(
        &publication.to_string(),
        "Asia/Taipei",
        0,
    )
    .map_err(invalid)?;
    if published > job.cutoff {
        return Err(invalid("Issuer disclosure is after frozen cutoff"));
    }
    let end = if period.month() == 12 {
        NaiveDate::from_ymd_opt(period.year() + 1, 1, 1)
    } else {
        NaiveDate::from_ymd_opt(period.year(), period.month() + 1, 1)
    }
    .unwrap()
    .pred_opt()
    .unwrap();
    let id = "ISSUER:NANYA:MONTHLY_REVENUE:TWD_MILLION".to_string();
    let mut out = page_default();
    out.series.push(series(id.clone(),job,"NANYA_MONTHLY_REVENUE","Nanya · monthly revenue","NANYA","TWD million","monthly","https://www.nanya.com/en/IR/16/",json!({"description":"Unaudited consolidated monthly revenue; not units produced","product":"memory manufacturer revenue, not DRAM spot price","currency":"TWD","scale":6,"source":"original issuer disclosure"})));
    let mut o = observation(
        id,
        period.to_string(),
        end.to_string(),
        Some(value),
        "TWD million",
        json!({"source_url":url,"publication_date":publication.to_string(),"source_title":description,"extraction_method":"validated issuer title grammar + dated body","not_production":true}),
    );
    o.published_at = Some(published);
    o.reconstructed_at = Some(published + 60000);
    o.source_order = Some(published);
    o.precision = "publisher_date_end_conservative".into();
    o.quality = "original_issuer_release".into();
    out.observations.push(o);
    out.received_rows = 1;
    out.complete_scope = true;
    out.note="One original dated issuer disclosure; revenue units validated; not a complete archive or production series".into();
    Ok(out)
}
pub(super) fn issuer(store: &Store, job: &Job) -> Result<(Capture, PageResult)> {
    let base = match job.resource.as_str() {
        "nanya" => "https://www.nanya.com/en/IR/16/",
        "micron" => "https://investors.micron.com/",
        _ => return Err(invalid("Issuer not approved")),
    };
    let url = if job.kind == "issuer_release" {
        Url::parse(s(&job.params, "url")?).map_err(|_| invalid("Issuer source URL invalid"))?
    } else {
        Url::parse(base).map_err(|_| invalid("Issuer catalog URL"))?
    };
    let expected = Url::parse(base).map_err(|_| invalid("Issuer URL"))?;
    if url.host_str() != expected.host_str() {
        return Err(invalid("Issuer source URL host mismatch"));
    }
    let c = fetch(
        store,
        job,
        url.clone(),
        HeaderMap::new(),
        None,
        16 * 1024 * 1024,
    )?;
    let text = std::fs::read_to_string(store.root.join(&c.path))?;
    if job.kind == "issuer_release" && job.resource == "nanya" {
        return Ok((c, nanya_release(job, &text, url.as_str())?));
    }
    let html = scraper::Html::parse_document(&text);
    let selector =
        scraper::Selector::parse("a[href]").map_err(|_| invalid("Issuer link selector"))?;
    let mut seen = BTreeSet::new();
    let mut out = page_default();
    for node in html.select(&selector) {
        let href = node.value().attr("href").unwrap_or_default();
        let Ok(link) = url.join(href) else { continue };
        if link.host_str() != expected.host_str() || link.scheme() != "https" {
            continue;
        }
        let relevant = if job.resource == "nanya" {
            link.query_pairs().any(|(k, _)| k == "IRId")
        } else {
            link.path().contains("press-release") || link.path().contains("news-releases")
        };
        if relevant && seen.insert(link.to_string()) {
            let title = node.text().collect::<Vec<_>>().join(" ").trim().to_string();
            out.series.push(series(format!("ISSUER:DOC:{}",&hash(link.as_str().as_bytes())[..24]),job,&job.resource,&title,&job.resource,"document metadata","metadata",link.as_str(),json!({"role":"source_document","description":title,"historical_publication":"unverified until capture/parse"})));
            if job.kind == "issuer_catalog" {
                out.children.push(PlannedJob {
                    provider: "issuer".into(),
                    kind: "issuer_release".into(),
                    resource: job.resource.clone(),
                    params: json!({"url":link.as_str(),"scope":"one official disclosure"}),
                    priority: 18,
                });
            }
        }
    }
    if job.resource == "nanya" && job.kind == "issuer_catalog" {
        out.children.push(PlannedJob{provider:"issuer".into(),kind:"issuer_release".into(),resource:"nanya".into(),params:json!({"url":"https://www.nanya.com/en/IR/16/?IRId=13148","scope":"previously identified original source; not substituted for archive discovery"}),priority:55});
    }
    out.received_rows = seen.len() as i64;
    out.note="Official visible archive links catalogued; dynamic/archive pagination completeness remains unresolved. Unsupported issuer numeric formats are not inferred.".into();
    Ok((c, out))
}
