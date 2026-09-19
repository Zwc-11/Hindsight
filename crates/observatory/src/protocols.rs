use crate::model::{invalid, Result};
use reqwest::Url;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct DistributionRecord {
    pub url: String,
    pub media_type: Option<String>,
    pub format: Option<String>,
    pub payload: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct DatasetRecord {
    pub external_id: String,
    pub title: String,
    pub description: String,
    pub landing_url: Option<String>,
    pub license: Option<String>,
    pub spatial: Value,
    pub temporal: Value,
    pub keywords: Vec<String>,
    pub distributions: Vec<DistributionRecord>,
    pub payload: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct DiscoveryPage {
    pub datasets: Vec<DatasetRecord>,
    pub follow: Vec<String>,
    pub provider_total: Option<u64>,
}

fn text(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(a)) => a
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(", "),
        _ => String::new(),
    }
}

fn absolute(base: &Url, value: &str) -> Option<String> {
    base.join(value).ok().map(|u| u.to_string())
}

pub fn parse(protocol: &str, base: &Url, bytes: &[u8]) -> Result<DiscoveryPage> {
    match protocol {
        "ckan" => parse_ckan(base, bytes),
        "stac" => parse_stac(base, bytes),
        "openapi" => parse_openapi(base, bytes),
        "ogc" => parse_ogc(base, bytes),
        "dcat" | "jsonld" => parse_jsonld(base, bytes),
        "sdmx" => parse_sdmx(base, bytes),
        _ => Err(invalid(format!(
            "Unsupported Atlas discovery protocol: {protocol}"
        ))),
    }
}

fn parse_ckan(base: &Url, bytes: &[u8]) -> Result<DiscoveryPage> {
    let root: Value = serde_json::from_slice(bytes)?;
    if root["success"] != true {
        return Err(invalid("CKAN response did not report success"));
    }
    let result = root
        .get("result")
        .ok_or_else(|| invalid("CKAN result missing"))?;
    let rows = result["results"]
        .as_array()
        .ok_or_else(|| invalid("CKAN results missing"))?;
    let mut datasets = Vec::with_capacity(rows.len());
    for row in rows {
        let id = row["id"]
            .as_str()
            .ok_or_else(|| invalid("CKAN package id missing"))?;
        let title = row["title"].as_str().unwrap_or(id).to_string();
        let mut distributions = Vec::new();
        for resource in row["resources"].as_array().into_iter().flatten() {
            let Some(url) = resource["url"].as_str() else {
                continue;
            };
            distributions.push(DistributionRecord {
                url: absolute(base, url).unwrap_or_else(|| url.to_string()),
                media_type: resource["mimetype"].as_str().map(str::to_string),
                format: resource["format"].as_str().map(str::to_string),
                payload: resource.clone(),
            });
        }
        let keywords = row["tags"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|v| v["display_name"].as_str().or_else(|| v["name"].as_str()))
            .map(str::to_string)
            .collect();
        datasets.push(DatasetRecord {
            external_id: id.to_string(), title,
            description: text(row.get("notes")),
            landing_url: row["url"].as_str().and_then(|u| absolute(base, u)).or_else(|| row["url"].as_str().map(str::to_string)),
            license: row["license_title"].as_str().map(str::to_string),
            spatial: row.get("spatial").cloned().unwrap_or(Value::Null),
            temporal: json!({"metadata_created":row["metadata_created"],"metadata_modified":row["metadata_modified"]}),
            keywords, distributions, payload: row.clone(),
        });
    }
    Ok(DiscoveryPage {
        datasets,
        follow: Vec::new(),
        provider_total: result["count"].as_u64(),
    })
}

fn parse_stac(base: &Url, bytes: &[u8]) -> Result<DiscoveryPage> {
    let root: Value = serde_json::from_slice(bytes)?;
    let mut datasets = Vec::new();
    let mut follow = Vec::new();
    let mut accept_collection = |row: &Value| -> Result<()> {
        let id = row["id"]
            .as_str()
            .ok_or_else(|| invalid("STAC collection id missing"))?;
        let title = row["title"].as_str().unwrap_or(id).to_string();
        let distributions = row["assets"]
            .as_object()
            .into_iter()
            .flat_map(|m| m.values())
            .filter_map(|asset| {
                let href = asset["href"].as_str()?;
                Some(DistributionRecord {
                    url: absolute(base, href).unwrap_or_else(|| href.to_string()),
                    media_type: asset["type"].as_str().map(str::to_string),
                    format: None,
                    payload: asset.clone(),
                })
            })
            .collect();
        datasets.push(DatasetRecord {
            external_id: id.to_string(),
            title,
            description: text(row.get("description")),
            landing_url: row["links"]
                .as_array()
                .into_iter()
                .flatten()
                .find(|l| l["rel"] == "self")
                .and_then(|l| l["href"].as_str())
                .and_then(|u| absolute(base, u)),
            license: row["license"].as_str().map(str::to_string),
            spatial: row
                .pointer("/extent/spatial")
                .cloned()
                .unwrap_or(Value::Null),
            temporal: row
                .pointer("/extent/temporal")
                .cloned()
                .unwrap_or(Value::Null),
            keywords: row["keywords"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect(),
            distributions,
            payload: row.clone(),
        });
        Ok(())
    };
    if root["type"] == "Collection" {
        accept_collection(&root)?;
    }
    if let Some(rows) = root["collections"].as_array() {
        for row in rows {
            accept_collection(row)?;
        }
    }
    for link in root["links"].as_array().into_iter().flatten() {
        if matches!(link["rel"].as_str(), Some("child" | "collection" | "data")) {
            if let Some(href) = link["href"].as_str().and_then(|u| absolute(base, u)) {
                follow.push(href);
            }
        }
    }
    follow.sort();
    follow.dedup();
    Ok(DiscoveryPage {
        datasets,
        follow,
        provider_total: None,
    })
}

fn parse_openapi(base: &Url, bytes: &[u8]) -> Result<DiscoveryPage> {
    let root: Value = serde_json::from_slice(bytes)?;
    if root.get("openapi").is_none() && root.get("swagger").is_none() {
        return Err(invalid("OpenAPI version missing"));
    }
    let mut datasets = Vec::new();
    let Some(paths) = root["paths"].as_object() else {
        return Err(invalid("OpenAPI paths missing"));
    };
    for (path, methods) in paths {
        let Some(get) = methods.get("get") else {
            continue;
        };
        datasets.push(DatasetRecord {
            external_id: format!("GET {path}"),
            title: get["summary"]
                .as_str()
                .or_else(|| get["operationId"].as_str())
                .unwrap_or(path)
                .to_string(),
            description: text(get.get("description")),
            landing_url: absolute(base, path),
            license: root
                .pointer("/info/license/name")
                .and_then(Value::as_str)
                .map(str::to_string),
            spatial: Value::Null,
            temporal: Value::Null,
            keywords: get["tags"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect(),
            distributions: Vec::new(),
            payload: get.clone(),
        });
    }
    Ok(DiscoveryPage {
        datasets,
        follow: Vec::new(),
        provider_total: None,
    })
}

fn parse_ogc(base: &Url, bytes: &[u8]) -> Result<DiscoveryPage> {
    let root: Value = serde_json::from_slice(bytes)?;
    let rows = root["collections"]
        .as_array()
        .ok_or_else(|| invalid("OGC collections missing"))?;
    let mut datasets = Vec::new();
    for row in rows {
        let id = row["id"]
            .as_str()
            .ok_or_else(|| invalid("OGC collection id missing"))?;
        datasets.push(DatasetRecord {
            external_id: id.to_string(),
            title: row["title"].as_str().unwrap_or(id).to_string(),
            description: text(row.get("description")),
            landing_url: row["links"]
                .as_array()
                .into_iter()
                .flatten()
                .find(|l| l["rel"] == "self")
                .and_then(|l| l["href"].as_str())
                .and_then(|u| absolute(base, u)),
            license: None,
            spatial: row
                .pointer("/extent/spatial")
                .cloned()
                .unwrap_or(Value::Null),
            temporal: row
                .pointer("/extent/temporal")
                .cloned()
                .unwrap_or(Value::Null),
            keywords: Vec::new(),
            distributions: Vec::new(),
            payload: row.clone(),
        });
    }
    Ok(DiscoveryPage {
        datasets,
        follow: Vec::new(),
        provider_total: Some(rows.len() as u64),
    })
}

fn parse_jsonld(base: &Url, bytes: &[u8]) -> Result<DiscoveryPage> {
    let root: Value = serde_json::from_slice(bytes)?;
    let rows: Vec<&Value> = root
        .get("@graph")
        .and_then(Value::as_array)
        .map(|a| a.iter().collect())
        .unwrap_or_else(|| vec![&root]);
    let mut datasets = Vec::new();
    for row in rows {
        let kind = row.get("@type");
        let is_dataset = kind
            .and_then(Value::as_str)
            .is_some_and(|s| s.ends_with("Dataset"))
            || kind.and_then(Value::as_array).is_some_and(|a| {
                a.iter()
                    .any(|v| v.as_str().is_some_and(|s| s.ends_with("Dataset")))
            });
        if !is_dataset {
            continue;
        }
        let id = row["@id"]
            .as_str()
            .or_else(|| row["identifier"].as_str())
            .unwrap_or_else(|| row["name"].as_str().unwrap_or("dataset"));
        let mut distributions = Vec::new();
        for d in row["distribution"].as_array().into_iter().flatten() {
            if let Some(url) = d["contentUrl"].as_str().or_else(|| d["accessURL"].as_str()) {
                distributions.push(DistributionRecord {
                    url: absolute(base, url).unwrap_or_else(|| url.into()),
                    media_type: d["encodingFormat"].as_str().map(str::to_string),
                    format: d["format"].as_str().map(str::to_string),
                    payload: d.clone(),
                });
            }
        }
        datasets.push(DatasetRecord {
            external_id: id.to_string(),
            title: row["name"].as_str().unwrap_or(id).to_string(),
            description: text(row.get("description")),
            landing_url: row["url"].as_str().and_then(|u| absolute(base, u)),
            license: row["license"].as_str().map(str::to_string),
            spatial: row.get("spatialCoverage").cloned().unwrap_or(Value::Null),
            temporal: row.get("temporalCoverage").cloned().unwrap_or(Value::Null),
            keywords: Vec::new(),
            distributions,
            payload: (*row).clone(),
        });
    }
    Ok(DiscoveryPage {
        datasets,
        follow: Vec::new(),
        provider_total: None,
    })
}

fn parse_sdmx(base: &Url, bytes: &[u8]) -> Result<DiscoveryPage> {
    let xml_text = std::str::from_utf8(bytes).map_err(|_| invalid("SDMX response is not UTF-8"))?;
    if xml_text.trim_start().starts_with('{') {
        let root: Value = serde_json::from_slice(bytes)?;
        let rows = root
            .pointer("/data/dataflows")
            .and_then(Value::as_array)
            .or_else(|| root.pointer("/dataflows").and_then(Value::as_array))
            .ok_or_else(|| invalid("SDMX JSON dataflows missing"))?;
        let datasets = rows
            .iter()
            .filter_map(|row| {
                let id = row["id"].as_str()?;
                Some(DatasetRecord {
                    external_id: id.into(),
                    title: row["name"].as_str().unwrap_or(id).into(),
                    description: text(row.get("description")),
                    landing_url: None,
                    license: None,
                    spatial: Value::Null,
                    temporal: Value::Null,
                    keywords: Vec::new(),
                    distributions: Vec::new(),
                    payload: row.clone(),
                })
            })
            .collect();
        return Ok(DiscoveryPage {
            datasets,
            follow: Vec::new(),
            provider_total: Some(rows.len() as u64),
        });
    }
    let doc = roxmltree::Document::parse(xml_text)
        .map_err(|e| invalid(format!("Invalid SDMX XML: {e}")))?;
    let mut datasets = Vec::new();
    for node in doc
        .descendants()
        .filter(|n| n.tag_name().name() == "Dataflow")
    {
        let Some(id) = node.attribute("id") else {
            continue;
        };
        let title = node
            .children()
            .find(|n| n.tag_name().name() == "Name")
            .and_then(|n| n.text())
            .unwrap_or(id);
        datasets.push(DatasetRecord { external_id:id.into(), title:title.into(), description:String::new(), landing_url:Some(base.to_string()), license:None, spatial:Value::Null, temporal:Value::Null, keywords:Vec::new(), distributions:Vec::new(), payload:json!({"agency":node.attribute("agencyID"),"version":node.attribute("version")}) });
    }
    if datasets.is_empty() {
        return Err(invalid("SDMX response contained no dataflows"));
    }
    Ok(DiscoveryPage {
        provider_total: Some(datasets.len() as u64),
        datasets,
        follow: Vec::new(),
    })
}
