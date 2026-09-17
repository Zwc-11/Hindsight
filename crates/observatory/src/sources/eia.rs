use super::*;

pub(super) fn eia(store: &Store, job: &Job) -> Result<(Capture, PageResult)> {
    if job.resource.contains("..")
        || !job
            .resource
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/-_".contains(&b))
    {
        return Err(invalid("Invalid EIA route"));
    }
    let key = std::env::var("EIA_API_KEY")
        .map_err(|_| Error::Blocked("blocked_auth: EIA_API_KEY is missing".into()))?;
    let endpoint = if job.kind == "eia_data" {
        format!(
            "https://api.eia.gov/v2/{}/data/",
            job.resource.trim_matches('/')
        )
    } else {
        format!("https://api.eia.gov/v2/{}/", job.resource.trim_matches('/'))
    };
    let mut url = Url::parse(&endpoint).map_err(|_| invalid("EIA URL"))?;
    url.query_pairs_mut().append_pair("api_key", &key);
    if job.kind == "eia_data" {
        if job.params["approved_original_eia"].as_bool() != Some(true) {
            return Err(Error::Blocked(
                "blocked_permission: EIA source ownership must be reviewed for this route".into(),
            ));
        }
        url.query_pairs_mut()
            .append_pair("frequency", s(&job.params, "frequency")?)
            .append_pair("start", s(&job.params, "start")?)
            .append_pair("end", s(&job.params, "end")?)
            .append_pair(
                "offset",
                &job.cursor["offset"].as_u64().unwrap_or(0).to_string(),
            )
            .append_pair("length", "5000");
        for field in job.params["fields"]
            .as_array()
            .ok_or_else(|| invalid("EIA data fields missing"))?
        {
            url.query_pairs_mut().append_pair(
                "data[]",
                field.as_str().ok_or_else(|| invalid("EIA field invalid"))?,
            );
        }
        url.query_pairs_mut()
            .append_pair("sort[0][column]", "period")
            .append_pair("sort[0][direction]", "asc");
        if let Some(facets) = job.params["facets"].as_array() {
            for (i, facet) in facets.iter().enumerate() {
                url.query_pairs_mut()
                    .append_pair(
                        &format!("sort[{}][column]", i + 1),
                        facet.as_str().unwrap_or_default(),
                    )
                    .append_pair(&format!("sort[{}][direction]", i + 1), "asc");
            }
        }
    }
    let c = fetch(store, job, url, HeaderMap::new(), None, 32 * 1024 * 1024)?;
    let v: Value = serde_json::from_reader(File::open(store.root.join(&c.path))?)?;
    let response = v
        .get("response")
        .ok_or_else(|| invalid("EIA response missing"))?;
    let out = if job.kind == "eia_catalog" {
        catalog(job, response)?
    } else {
        data(job, response)?
    };
    Ok((c, out))
}
fn catalog(job: &Job, response: &Value) -> Result<PageResult> {
    let mut out = page_default();
    if let Some(routes) = response["routes"].as_array() {
        for route in routes {
            let code = s(route, "id")?;
            let next = if job.resource.is_empty() {
                code.to_string()
            } else {
                format!("{}/{code}", job.resource)
            };
            out.children.push(PlannedJob {
                provider: "eia".into(),
                kind: "eia_catalog".into(),
                resource: next.clone(),
                params: json!({"scope":"metadata tree"}),
                priority: 30,
            });
            out.series.push(series(
                format!("EIA:ROUTE:{next}"),
                job,
                &next,
                route["name"].as_str().unwrap_or(code),
                "USA",
                "metadata",
                "metadata",
                "https://www.eia.gov/opendata/",
                json!({"description":route["description"],"role":"route_catalog"}),
            ));
        }
        out.received_rows = routes.len() as i64;
    } else if let (Some(fields), Some(frequencies)) = (
        response["data"].as_object(),
        response["frequency"].as_array(),
    ) {
        let fields: Vec<_> = fields.keys().cloned().collect();
        let facets = response["facets"]
            .as_array()
            .map(|v| {
                v.iter()
                    .filter_map(|f| f["id"].as_str())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let approved = [
            "electricity/retail-sales",
            "electricity/electric-power-operational-data",
        ]
        .contains(&job.resource.as_str());
        for f in frequencies {
            let freq = s(f, "id")?;
            if !["annual", "monthly", "daily"].contains(&freq) {
                continue;
            }
            let start = response["startPeriod"].as_str().unwrap_or("1900");
            let cutoff = Utc
                .timestamp_millis_opt(job.cutoff)
                .single()
                .ok_or_else(|| invalid("EIA cutoff"))?;
            let end = match freq {
                "annual" => (cutoff.year() - 1).to_string(),
                "monthly" => {
                    let d = NaiveDate::from_ymd_opt(cutoff.year(), cutoff.month(), 1)
                        .unwrap()
                        .pred_opt()
                        .unwrap();
                    d.format("%Y-%m").to_string()
                }
                _ => cutoff.date_naive().pred_opt().unwrap().to_string(),
            };
            out.children.push(PlannedJob{provider:"eia".into(),kind:"eia_data".into(),resource:job.resource.clone(),params:json!({"frequency":freq,"start":start,"end":end,"fields":fields,"facets":facets,"approved_original_eia":approved,"scope":"all facet rows in frozen requested range"}),priority:10});
        }
        out.received_rows = fields.len() as i64;
    } else {
        return Err(invalid(
            "EIA metadata has neither routes nor supported data schema",
        ));
    }
    out.note =
        "EIA metadata discovered; only reviewed original-EIA routes may ingest values".into();
    Ok(out)
}

fn data(job: &Job, response: &Value) -> Result<PageResult> {
    let rows = response["data"]
        .as_array()
        .ok_or_else(|| invalid("EIA data rows missing"))?;
    let total = response["total"]
        .as_u64()
        .or_else(|| response["total"].as_str().and_then(|s| s.parse().ok()))
        .ok_or_else(|| invalid("EIA row total missing"))?;
    let offset = job.cursor["offset"].as_u64().unwrap_or(0);
    let mut seen = BTreeSet::new();
    let mut out = page_default();
    for row in rows {
        let period = s(row, "period")?;
        let (start, end) = period_bounds(period)?;
        let mut dimensions = serde_json::Map::new();
        for facet in job.params["facets"].as_array().into_iter().flatten() {
            let name = facet.as_str().unwrap_or_default();
            dimensions.insert(name.into(), row[name].clone());
        }
        for field in job.params["fields"].as_array().into_iter().flatten() {
            let name = field
                .as_str()
                .ok_or_else(|| invalid("EIA numeric field invalid"))?;
            let unit = row[format!("{name}-units")]
                .as_str()
                .unwrap_or("source-defined");
            let code = format!(
                "{}:{name}:{}",
                job.resource,
                &hash(serde_json::to_string(&dimensions)?.as_bytes())[..16]
            );
            let id = format!("EIA:{code}");
            if seen.insert(id.clone()) {
                out.series.push(series(id.clone(),job,&code,&format!("{} / {name}",job.resource),"USA",unit,s(&job.params,"frequency")?,"https://www.eia.gov/opendata/",json!({"facets":dimensions,"description":job.resource,"original_vintage":false})));
            }
            let mut o = observation(
                id,
                start.clone(),
                end.clone(),
                num(&row[name]),
                unit,
                json!({"source_row":row,"original_vintage":false}),
            );
            o.dimensions = Value::Object(dimensions.clone());
            out.observations.push(o);
        }
    }
    out.received_rows = rows.len() as i64;
    out.advertised_total = Some(total as i64);
    out.complete_scope = true;
    if offset + (rows.len() as u64) < total {
        if rows.is_empty() {
            return Err(invalid("EIA empty page before advertised total"));
        }
        out.next = Some(json!({"offset":offset+rows.len() as u64}));
    }
    out.note="Stable period/facet pagination; current provider vintage; annual/monthly totals retain aggregation semantics".into();
    Ok(out)
}
pub(super) fn period_bounds(period: &str) -> Result<(String, String)> {
    if period.len() == 4 {
        return year_bounds(period);
    }
    if period.len() == 7 {
        let start = NaiveDate::parse_from_str(&format!("{period}-01"), "%Y-%m-%d")
            .map_err(|_| invalid("Invalid monthly period"))?;
        let next = if start.month() == 12 {
            NaiveDate::from_ymd_opt(start.year() + 1, 1, 1)
        } else {
            NaiveDate::from_ymd_opt(start.year(), start.month() + 1, 1)
        }
        .ok_or_else(|| invalid("Monthly period overflow"))?;
        return Ok((start.to_string(), next.pred_opt().unwrap().to_string()));
    }
    let d = NaiveDate::parse_from_str(period, "%Y-%m-%d")
        .map_err(|_| invalid("Unsupported economic period"))?;
    Ok((d.to_string(), d.to_string()))
}
