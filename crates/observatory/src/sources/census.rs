use super::*;

pub(super) fn census(store: &Store, job: &Job) -> Result<(Capture, PageResult)> {
    if !["imports", "exports"].contains(&job.resource.as_str()) {
        return Err(invalid("Unsupported Census trade direction"));
    }
    let prefix = if job.resource == "imports" { "I" } else { "E" };
    let base = format!(
        "https://api.census.gov/data/timeseries/intltrade/{}/hs",
        job.resource
    );
    if job.kind == "census_catalog" {
        let url =
            Url::parse(&(base.clone() + "/variables.json")).map_err(|_| invalid("Census URL"))?;
        let c = fetch(store, job, url, HeaderMap::new(), None, 8 * 1024 * 1024)?;
        let v: Value = serde_json::from_reader(File::open(store.root.join(&c.path))?)?;
        let fields = v["variables"]
            .as_object()
            .ok_or_else(|| invalid("Census variables object absent"))?;
        let mut out = page_default();
        for (code, meta) in fields {
            out.series.push(series(
                format!("CENSUS:FIELD:{}:{code}", job.resource),
                job,
                code,
                meta["label"].as_str().unwrap_or(code),
                "USA",
                "schema metadata",
                "metadata",
                &base,
                json!({"description":meta["concept"],"schema":meta,"role":"field_catalog"}),
            ));
        }
        let cutoff = Utc
            .timestamp_millis_opt(job.cutoff)
            .single()
            .ok_or_else(|| invalid("Census cutoff"))?;
        for y in 2010..=cutoff.year() {
            for m in 1..=12 {
                if y == cutoff.year() && m >= cutoff.month() {
                    break;
                }
                let month = format!("{y}-{m:02}");
                out.children.push(PlannedJob{provider:"census".into(),kind:"census_totals".into(),resource:job.resource.clone(),params:json!({"month":month,"start":format!("{month}-01"),"end":format!("{month}-31"),"scope":"reported all-country total-commodity table; then all HS codes per discovered geography"}),priority:if y>=cutoff.year()-2{16}else{10}});
            }
        }
        out.received_rows = fields.len() as i64;
        out.note="Trade variable metadata enumerated; all months queued, geography/commodity histories expand from actual records".into();
        return Ok((c, out));
    }
    let month = job.params["month"]
        .as_str()
        .ok_or_else(|| invalid("Census month missing"))?;
    let first = NaiveDate::parse_from_str(&format!("{month}-01"), "%Y-%m-%d")
        .map_err(|_| invalid("Census month invalid"))?;
    let next = if first.month() == 12 {
        NaiveDate::from_ymd_opt(first.year() + 1, 1, 1)
    } else {
        NaiveDate::from_ymd_opt(first.year(), first.month() + 1, 1)
    }
    .ok_or_else(|| invalid("Census month overflow"))?;
    let end = next
        .pred_opt()
        .ok_or_else(|| invalid("Census month end"))?
        .to_string();
    let code_field = format!("{prefix}_COMMODITY");
    let name_field = format!("{prefix}_COMMODITY_SDESC");
    let value_field = if prefix == "I" {
        "GEN_VAL_MO"
    } else {
        "ALL_VAL_MO"
    };
    let mut url = Url::parse(&base).map_err(|_| invalid("Census URL"))?;
    let get = format!("{value_field},{code_field},{name_field},CTY_CODE,CTY_NAME");
    url.query_pairs_mut()
        .append_pair("get", &get)
        .append_pair("time", month);
    if job.kind == "census_totals" {
        url.query_pairs_mut()
            .append_pair(&code_field, "TOTAL")
            .append_pair("CTY_CODE", "*");
    } else {
        let geo = job.params["geo"]
            .as_str()
            .ok_or_else(|| invalid("Census geography missing"))?;
        if !geo.bytes().all(|b| b.is_ascii_digit()) {
            return Err(invalid("Census geography invalid"));
        }
        url.query_pairs_mut()
            .append_pair(&code_field, "*")
            .append_pair("CTY_CODE", geo);
    }
    if let Ok(key) = std::env::var("CENSUS_API_KEY") {
        url.query_pairs_mut().append_pair("key", &key);
    }
    let c = fetch(store, job, url, HeaderMap::new(), None, 32 * 1024 * 1024)?;
    let v: Value = serde_json::from_reader(File::open(store.root.join(&c.path))?)?;
    let table = v
        .as_array()
        .ok_or_else(|| invalid("Census table missing"))?;
    if table.len() < 2 {
        return Err(Error::Blocked(
            "not_yet_published_or_unresolved: Census returned no records; not marked complete"
                .into(),
        ));
    }
    let headers = table[0]
        .as_array()
        .ok_or_else(|| invalid("Census header missing"))?;
    let column = |name: &str| {
        headers
            .iter()
            .position(|v| v.as_str() == Some(name))
            .ok_or_else(|| invalid(format!("Census missing {name}")))
    };
    let ci = column(&code_field)?;
    let ni = column(&name_field)?;
    let gi = column("CTY_CODE")?;
    let gn = column("CTY_NAME")?;
    let vi = column(value_field)?;
    let mut out = page_default();
    let mut seen = BTreeSet::new();
    for row in &table[1..] {
        let r = row
            .as_array()
            .ok_or_else(|| invalid("Census row malformed"))?;
        if r.len() != headers.len() {
            return Err(invalid("Census row width mismatch"));
        }
        let code = r[ci]
            .as_str()
            .ok_or_else(|| invalid("Commodity code missing"))?;
        let geo = r[gi]
            .as_str()
            .ok_or_else(|| invalid("Country code missing"))?;
        let title = r[ni].as_str().unwrap_or(code);
        let id = format!("CENSUS:{}:{geo}:{code}:{value_field}", job.resource);
        if seen.insert(id.clone()) {
            out.series.push(series(id.clone(),job,code,title,geo,"USD","monthly",&base,json!({"country":r[gn],"direction":job.resource,"classification":"HS","classification_year":first.year(),"description":format!("{value_field}: {title}"),"measure":"reported trade value, not unit price or production","original_vintage":false})));
        }
        let raw = r[vi].as_str().unwrap_or_default();
        let value = if raw.is_empty() || ["null", "N", "S", "-", "(S)"].contains(&raw) {
            None
        } else {
            Some(
                raw.parse::<f64>()
                    .map_err(|_| invalid("Unexpected Census numeric/suppression value"))?,
            )
        };
        let mut o = observation(
            id,
            first.to_string(),
            end.clone(),
            value,
            "USD",
            json!({"commodity":code,"geo":geo,"raw_value":raw,"classification_year":first.year(),"original_vintage":false}),
        );
        o.dimensions = json!({"direction":job.resource,"hs_code":code,"reporter":"USA","partner":geo,"measure":value_field});
        out.observations.push(o);
        if job.kind == "census_totals" && geo != "0000" {
            out.children.push(PlannedJob {
                provider: "census".into(),
                kind: "census_data".into(),
                resource: job.resource.clone(),
                params: json!({"month":month,"geo":geo,"start":first.to_string(),"end":end}),
                priority: 0,
            });
        }
    }
    out.received_rows = (table.len() - 1) as i64;
    out.note="Source-returned trade records for explicit month/geography scope; empty cells remain null; provider row-total completeness unavailable".into();
    Ok((c, out))
}
