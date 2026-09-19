use hindsight_observatory::{atlas::MissionRequest, model::Store, protocols};
use reqwest::Url;
use serde_json::json;
fn setup() -> (tempfile::TempDir, Store) {
    let d = tempfile::tempdir().unwrap();
    let s = Store::open(d.path()).unwrap();
    (d, s)
}

#[test]
fn atlas_bootstraps_world_seeds_without_claiming_completeness() {
    let (_d, store) = setup();
    let seeds = store.atlas_seeds().unwrap();
    assert!(seeds.iter().any(|s| s.id == "canada-ckan" && s.enabled));
    assert!(seeds.iter().any(|s| s.id == "uk-ckan" && s.enabled));
    assert!(seeds.iter().any(|s| s.id == "nasa-cmr-stac" && s.enabled));
    let overview = store.atlas_overview().unwrap();
    assert_eq!(overview["scope"], "WORLD");
    assert_eq!(overview["datasets"], 0);
    assert!(overview["claim"]
        .as_str()
        .unwrap()
        .contains("no global completeness"));
}

#[test]
fn mission_budgets_fail_closed() {
    let (_d, store) = setup();
    for request in [
        MissionRequest {
            query: "".into(),
            max_requests: 8,
            max_bytes: 1 << 20,
            max_depth: 2,
            max_sources: 20,
        },
        MissionRequest {
            query: "copper".into(),
            max_requests: 33,
            max_bytes: 1 << 20,
            max_depth: 2,
            max_sources: 20,
        },
        MissionRequest {
            query: "copper".into(),
            max_requests: 8,
            max_bytes: 128 << 20,
            max_depth: 2,
            max_sources: 20,
        },
        MissionRequest {
            query: "copper".into(),
            max_requests: 8,
            max_bytes: 1 << 20,
            max_depth: 5,
            max_sources: 20,
        },
    ] {
        assert!(store.create_atlas_mission(&request).is_err());
    }
}

#[test]
fn future_graph_edge_is_not_visible_before_known_from() {
    let (_d, store) = setup();
    let c = store.connect().unwrap();
    c.execute(
        "INSERT INTO atlas_entities VALUES('a','Company','Alpha',1000,'{}')",
        [],
    )
    .unwrap();
    c.execute(
        "INSERT INTO atlas_entities VALUES('b','Product','Beta',1000,'{}')",
        [],
    )
    .unwrap();
    c.execute("INSERT INTO atlas_edges VALUES('e','knowledge','a','b','manufactures',500,NULL,2000,NULL,NULL,'{}')", []).unwrap();
    let early = store.atlas_graph("", 1500, 10).unwrap();
    assert!(early["edges"].as_array().unwrap().is_empty());
    let late = store.atlas_graph("", 2500, 10).unwrap();
    assert_eq!(late["edges"].as_array().unwrap().len(), 1);
    assert_eq!(late["edges"][0]["kind"], "manufactures");
}

#[test]
fn ckan_parser_retains_resources_rights_and_total() {
    let base = Url::parse("https://catalog.example/api/3/action/package_search").unwrap();
    let body = json!({"success":true,"result":{"count":42,"results":[{
        "id":"d1","title":"Copper trade","notes":"monthly flows","license_title":"Open",
        "metadata_created":"2020-01-01","metadata_modified":"2026-01-01",
        "tags":[{"name":"copper"}],"resources":[{"url":"https://files.example/copper.csv","mimetype":"text/csv","format":"CSV"}]
    }]}});
    let page = protocols::parse("ckan", &base, &serde_json::to_vec(&body).unwrap()).unwrap();
    assert_eq!(page.provider_total, Some(42));
    assert_eq!(page.datasets[0].external_id, "d1");
    assert_eq!(page.datasets[0].license.as_deref(), Some("Open"));
    assert_eq!(
        page.datasets[0].distributions[0].format.as_deref(),
        Some("CSV")
    );
}

#[test]
fn stac_parser_preserves_extent_and_typed_follow_links() {
    let base = Url::parse("https://earth.example/stac/").unwrap();
    let body = json!({"type":"Catalog","stac_version":"1.0.0","links":[
        {"rel":"child","href":"provider"},{"rel":"self","href":"https://earth.example/stac/"}
    ],"collections":[{"type":"Collection","id":"temp","title":"Temperature","description":"surface temperature","license":"proprietary","extent":{"spatial":{"bbox":[[-180,-90,180,90]]},"temporal":{"interval":[["2000-01-01",null]]}},"links":[]}]});
    let page = protocols::parse("stac", &base, &serde_json::to_vec(&body).unwrap()).unwrap();
    assert_eq!(page.datasets.len(), 1);
    assert_eq!(page.datasets[0].spatial["bbox"][0][0], -180);
    assert_eq!(page.follow, vec!["https://earth.example/stac/provider"]);
}

#[test]
fn openapi_discovery_never_turns_post_into_a_read_dataset() {
    let base = Url::parse("https://api.example/v1/openapi.json").unwrap();
    let body = json!({"openapi":"3.1.0","info":{"title":"Example"},"paths":{
        "/series":{"get":{"operationId":"series","summary":"List series","tags":["data"]}},
        "/orders":{"post":{"operationId":"createOrder"}}
    }});
    let page = protocols::parse("openapi", &base, &serde_json::to_vec(&body).unwrap()).unwrap();
    assert_eq!(page.datasets.len(), 1);
    assert_eq!(page.datasets[0].external_id, "GET /series");
}

#[test]
fn ogc_collection_keeps_spatial_and_temporal_semantics() {
    let base = Url::parse("https://geo.example/").unwrap();
    let body = json!({"collections":[{"id":"ports","title":"Ports","description":"locations","extent":{"spatial":{"bbox":[[-10,40,10,60]]},"temporal":{"interval":[["2020-01-01","2026-01-01"]]}},"links":[]}]});
    let page = protocols::parse("ogc", &base, &serde_json::to_vec(&body).unwrap()).unwrap();
    assert_eq!(page.datasets[0].title, "Ports");
    assert_eq!(page.datasets[0].temporal["interval"][0][1], "2026-01-01");
}

#[test]
fn jsonld_dataset_is_discovered_without_executing_document_content() {
    let base = Url::parse("https://data.example/catalog.jsonld").unwrap();
    let body = json!({"@context":"https://schema.org","@graph":[{"@type":"Dataset","@id":"urn:x","name":"Wheat prices","description":"official series","distribution":[{"contentUrl":"/wheat.csv","encodingFormat":"text/csv"}]}]});
    let page = protocols::parse("jsonld", &base, &serde_json::to_vec(&body).unwrap()).unwrap();
    assert_eq!(page.datasets[0].title, "Wheat prices");
    assert_eq!(
        page.datasets[0].distributions[0].url,
        "https://data.example/wheat.csv"
    );
}

#[test]
fn sdmx_xml_dataflow_discovery_is_namespace_agnostic() {
    let base = Url::parse("https://stats.example/sdmx/dataflow").unwrap();
    let xml=br#"<?xml version='1.0'?><mes:Structure xmlns:mes='urn:sdmx:org.sdmx.infomodel.message:2.1' xmlns:str='urn:sdmx:org.sdmx.infomodel.structure:2.1' xmlns:com='urn:sdmx:org.sdmx.infomodel.common:2.1'><str:Dataflows><str:Dataflow id='DF_GDP' agencyID='TEST' version='1.0'><com:Name>Global GDP</com:Name></str:Dataflow></str:Dataflows></mes:Structure>"#;
    let page = protocols::parse("sdmx", &base, xml).unwrap();
    assert_eq!(page.datasets[0].external_id, "DF_GDP");
    assert_eq!(page.datasets[0].title, "Global GDP");
}

#[test]
fn malformed_protocol_payloads_do_not_become_empty_success() {
    let base = Url::parse("https://example.invalid/").unwrap();
    assert!(protocols::parse("ckan", &base, b"{}").is_err());
    assert!(protocols::parse("ogc", &base, b"{}").is_err());
    assert!(protocols::parse("openapi", &base, b"{}").is_err());
    assert!(protocols::parse("sdmx", &base, b"<root/>").is_err());
}
