use super::*;
pub(super) fn bea(store: &Store, job: &Job) -> Result<(Capture, PageResult)> {
    let key = std::env::var("BEA_API_KEY")
        .map_err(|_| Error::Blocked("blocked_auth: BEA_API_KEY is missing".into()))?;
    let mut url = Url::parse("https://apps.bea.gov/api/data/").map_err(|_| invalid("BEA URL"))?;
    url.query_pairs_mut()
        .append_pair("UserID", &key)
        .append_pair("ResultFormat", "JSON")
        .append_pair("DatasetName", "InputOutput");
    if job.kind == "bea_catalog" {
        url.query_pairs_mut()
            .append_pair("method", "GetParameterValues")
            .append_pair("ParameterName", "TableID");
    } else {
        if !job.resource.bytes().all(|b| b.is_ascii_digit()) {
            return Err(invalid("BEA table ID must be numeric"));
        }
        url.query_pairs_mut()
            .append_pair("method", "GetData")
            .append_pair("TableID", &job.resource)
            .append_pair("Year", "ALL");
    }
    let c = fetch(store, job, url, HeaderMap::new(), None, 32 * 1024 * 1024)?;
    let v: Value = serde_json::from_reader(File::open(store.root.join(&c.path))?)?;
    let r = &v["BEAAPI"]["Results"];
    if r.is_null() || r.get("Error").is_some() {
        return Err(invalid(
            "BEA error/missing Results; source response archived",
        ));
    }
    let mut out = page_default();
    if job.kind == "bea_catalog" {
        let rows = r["ParamValue"]
            .as_array()
            .ok_or_else(|| invalid("BEA table catalog missing"))?;
        for row in rows {
            let code = s(row, "Key")?;
            let name = s(row, "Desc")?;
            out.series.push(series(
                format!("BEA:TABLE:{code}"),
                job,
                code,
                name,
                "USA",
                "table metadata",
                "metadata",
                "https://www.bea.gov/itable/input-output",
                json!({"description":name,"role":"input_output_table"}),
            ));
            out.children.push(PlannedJob{provider:"bea".into(),kind:"bea_data".into(),resource:code.into(),params:json!({"title":name,"scope":"ALL years supplied for this table","classification_vintage":"retain source record"}),priority:12});
        }
        out.received_rows = rows.len() as i64;
        out.note = "All discovered input-output table IDs; histories separately queued".into();
    } else {
        let rows = r["Data"]
            .as_array()
            .ok_or_else(|| invalid("BEA data missing"))?;
        let mut seen = BTreeSet::new();
        for row in rows {
            let year = row["Year"]
                .as_str()
                .map(String::from)
                .or_else(|| row["Year"].as_i64().map(|y| y.to_string()))
                .ok_or_else(|| invalid("BEA Year missing"))?;
            let (a, b) = year_bounds(&year)?;
            let from = row["RowCode"].as_str().unwrap_or("unspecified");
            let to = row["ColCode"].as_str().unwrap_or("unspecified");
            let unit = row["CL_UNIT"].as_str().unwrap_or(
                "table-defined; coefficients versus monetary units require metadata review",
            );
            let code = format!("{}:{from}:{to}", job.resource);
            let id = format!("BEA:{code}");
            if seen.insert(id.clone()) {
                out.series.push(series(id.clone(),job,&code,job.params["title"].as_str().unwrap_or(&job.resource),"USA",unit,"annual","https://www.bea.gov/itable/input-output",json!({"row_code":from,"column_code":to,"description":job.params["title"],"unit_multiplier":row["UNIT_MULT"],"interpretation":"sector accounting; not named corporate links"})));
            }
            let mut o = observation(
                id,
                a,
                b,
                num(&row["DataValue"]),
                unit,
                json!({"source_record":row,"original_vintage":false}),
            );
            o.dimensions = json!({"row":from,"column":to,"table_id":job.resource,"unit_mult":row["UNIT_MULT"]});
            out.observations.push(o);
        }
        out.received_rows = rows.len() as i64;
        out.note="Source-returned InputOutput rows; stable sector mapping and matrix orientation must be validated before propagation".into();
    }
    Ok((c, out))
}
