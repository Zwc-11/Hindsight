use crate::{model::*, protocols};
use reqwest::{blocking::Client, redirect::Policy, Url};
use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    fs,
    io::Read,
    time::Duration,
};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SeedRights {
    pub state: String,
    pub display: bool,
    pub archive: bool,
    pub research: bool,
    pub training: bool,
    pub license: String,
    pub host_scope: Vec<String>,
    pub reviewed_at: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AtlasSeed {
    pub id: String,
    pub name: String,
    pub protocol: String,
    pub base_url: String,
    pub enabled: bool,
    pub rights: SeedRights,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ResourcePolicy {
    pub id: String,
    pub name: String,
    pub host_scope: Vec<String>,
    pub state: String,
    pub sample_allowed: bool,
    pub archive: bool,
    pub display: bool,
    pub research: bool,
    pub training: bool,
    pub commercial: bool,
    pub redistribution: bool,
    pub max_sample_bytes: u64,
    pub license: String,
    pub terms_url: String,
    pub reviewed_at: String,
    pub warning: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MissionRequest {
    pub query: String,
    #[serde(default = "default_requests")]
    pub max_requests: usize,
    #[serde(default = "default_bytes")]
    pub max_bytes: u64,
    #[serde(default = "default_depth")]
    pub max_depth: usize,
    #[serde(default = "default_sources")]
    pub max_sources: usize,
}
fn default_requests() -> usize {
    8
}
fn default_bytes() -> u64 {
    8 * 1024 * 1024
}
fn default_depth() -> usize {
    2
}
fn default_sources() -> usize {
    20
}

impl MissionRequest {
    pub fn validate(&self) -> Result<()> {
        if self.query.trim().len() < 2
            || self.query.len() > 200
            || self.max_requests == 0
            || self.max_requests > 32
            || self.max_bytes < 4096
            || self.max_bytes > 64 * 1024 * 1024
            || self.max_depth > 4
            || self.max_sources == 0
            || self.max_sources > 100
        {
            return Err(invalid(
                "Atlas mission exceeds bounded query/request/byte/depth/source limits",
            ));
        }
        Ok(())
    }
}

fn seeds() -> Result<Vec<AtlasSeed>> {
    serde_json::from_str(include_str!("../../../config/atlas/seeds.json")).map_err(Error::from)
}

pub(crate) fn resource_policies() -> Result<Vec<ResourcePolicy>> {
    serde_json::from_str(include_str!("../../../config/atlas/resource_policies.json"))
        .map_err(Error::from)
}

pub(crate) fn resource_host_allowed(policy: &ResourcePolicy, url: &Url) -> bool {
    url.scheme() == "https"
        && url.host_str().is_some_and(|host| {
            policy
                .host_scope
                .iter()
                .any(|allowed| host == allowed || host.ends_with(&format!(".{allowed}")))
        })
}

fn host_allowed(seed: &AtlasSeed, url: &Url) -> bool {
    url.scheme() == "https"
        && url.host_str().is_some_and(|host| {
            seed.rights
                .host_scope
                .iter()
                .any(|allowed| host == allowed || host.ends_with(&format!(".{allowed}")))
        })
}

fn source_id(url: &str) -> String {
    format!("atlas-source:{}", hash(url.as_bytes()))
}
fn dataset_id(source: &str, external: &str) -> String {
    format!(
        "atlas-dataset:{}",
        hash(format!("{source}\0{external}").as_bytes())
    )
}
fn distribution_id(dataset: &str, url: &str) -> String {
    format!(
        "atlas-dist:{}",
        hash(format!("{dataset}\0{url}").as_bytes())
    )
}
fn edge_id(layer: &str, from: &str, to: &str, kind: &str, known: i64) -> String {
    format!(
        "atlas-edge:{}",
        hash(format!("{layer}\0{from}\0{to}\0{kind}\0{known}").as_bytes())
    )
}

impl Store {
    pub fn bootstrap_atlas(&self) -> Result<()> {
        let c = self.connect()?;
        for seed in seeds()? {
            let payload = serde_json::to_string(&seed.rights)?;
            c.execute("INSERT INTO atlas_seeds(id,name,protocol,base_url,enabled,rights_json) VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(id) DO UPDATE SET name=excluded.name,protocol=excluded.protocol,base_url=excluded.base_url,enabled=excluded.enabled,rights_json=excluded.rights_json", params![seed.id, seed.name, seed.protocol, seed.base_url, seed.enabled as i64, payload])?;
        }
        for policy in resource_policies()? {
            c.execute(
                "INSERT INTO atlas_resource_policies(id,name,rights_json) VALUES(?1,?2,?3) ON CONFLICT(id) DO UPDATE SET name=excluded.name,rights_json=excluded.rights_json",
                params![policy.id, policy.name, serde_json::to_string(&policy)?],
            )?;
        }
        Ok(())
    }

    pub fn atlas_seeds(&self) -> Result<Vec<AtlasSeed>> {
        let c = self.connect()?;
        let mut q = c.prepare(
            "SELECT id,name,protocol,base_url,enabled,rights_json FROM atlas_seeds ORDER BY id",
        )?;
        let rows = q
            .query_map([], |r| {
                let rights: String = r.get(5)?;
                let rights = serde_json::from_str(&rights).map_err(|e| {
                    rusqlite::Error::FromSqlConversionFailure(
                        5,
                        rusqlite::types::Type::Text,
                        Box::new(e),
                    )
                })?;
                Ok(AtlasSeed {
                    id: r.get(0)?,
                    name: r.get(1)?,
                    protocol: r.get(2)?,
                    base_url: r.get(3)?,
                    enabled: r.get::<_, i64>(4)? != 0,
                    rights,
                })
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn create_atlas_mission(&self, request: &MissionRequest) -> Result<String> {
        request.validate()?;
        let id = uuid::Uuid::new_v4().to_string();
        let t = now();
        self.connect()?.execute("INSERT INTO atlas_missions(id,query,state,created_at,updated_at,config_json) VALUES(?1,?2,'queued',?3,?3,?4)", params![id,request.query.trim(),t,serde_json::to_string(request)?])?;
        Ok(id)
    }

    pub fn atlas_missions(&self, limit: usize) -> Result<Vec<Value>> {
        let c = self.connect()?;
        let mut q=c.prepare("SELECT id,query,state,created_at,updated_at,config_json,requests_used,bytes_used,datasets_found,error FROM atlas_missions ORDER BY created_at DESC LIMIT ?1")?;
        let rows=q.query_map([limit.min(100) as i64],|r|Ok(json!({"id":r.get::<_,String>(0)?,"query":r.get::<_,String>(1)?,"state":r.get::<_,String>(2)?,"created_at":r.get::<_,i64>(3)?,"updated_at":r.get::<_,i64>(4)?,"config":serde_json::from_str::<Value>(&r.get::<_,String>(5)?).unwrap_or(Value::Null),"requests_used":r.get::<_,i64>(6)?,"bytes_used":r.get::<_,i64>(7)?,"datasets_found":r.get::<_,i64>(8)?,"error":r.get::<_,Option<String>>(9)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(rows)
    }

    pub fn atlas_datasets(&self, search: &str, limit: usize) -> Result<Vec<Value>> {
        let c = self.connect()?;
        let pattern = format!("%{}%", search.to_lowercase());
        let mut q=c.prepare("SELECT d.id,d.external_id,d.title,d.description,d.landing_url,d.license,d.spatial_json,d.temporal_json,d.keywords_json,d.discovered_at,s.url,s.protocol,s.rights_state,(SELECT COUNT(*) FROM atlas_distributions x WHERE x.dataset_id=d.id) FROM atlas_datasets d JOIN atlas_sources s ON s.id=d.source_id WHERE ?1='' OR lower(d.title||' '||d.description||' '||d.keywords_json) LIKE ?2 ORDER BY d.discovered_at DESC,d.title LIMIT ?3")?;
        let rows=q.query_map(params![search,pattern,limit.min(500) as i64],|r|Ok(json!({"id":r.get::<_,String>(0)?,"external_id":r.get::<_,String>(1)?,"title":r.get::<_,String>(2)?,"description":r.get::<_,String>(3)?,"landing_url":r.get::<_,Option<String>>(4)?,"license":r.get::<_,Option<String>>(5)?,"spatial":serde_json::from_str::<Value>(&r.get::<_,String>(6)?).unwrap_or(Value::Null),"temporal":serde_json::from_str::<Value>(&r.get::<_,String>(7)?).unwrap_or(Value::Null),"keywords":serde_json::from_str::<Value>(&r.get::<_,String>(8)?).unwrap_or(Value::Null),"discovered_at":r.get::<_,i64>(9)?,"source":{"url":r.get::<_,String>(10)?,"protocol":r.get::<_,String>(11)?,"rights_state":r.get::<_,String>(12)?},"distributions":r.get::<_,i64>(13)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(rows)
    }

    pub fn atlas_overview(&self) -> Result<Value> {
        let c = self.connect()?;
        let count = |table: &str| -> Result<i64> {
            Ok(c.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))?)
        };
        let mut rights = BTreeMap::new();
        let mut q =
            c.prepare("SELECT rights_state,COUNT(*) FROM atlas_sources GROUP BY rights_state")?;
        for row in q.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))? {
            let (k, v) = row?;
            rights.insert(k, v);
        }
        Ok(
            json!({"scope":"WORLD","seeds":count("atlas_seeds")?,"sources":count("atlas_sources")?,"datasets":count("atlas_datasets")?,"distributions":count("atlas_distributions")?,"entities":count("atlas_entities")?,"edges":count("atlas_edges")?,"missions":count("atlas_missions")?,"rights":rights,"claim":"catalogued metadata only; no global completeness claim"}),
        )
    }

    pub fn atlas_graph(&self, search: &str, as_of: i64, limit: usize) -> Result<Value> {
        if as_of > now() + 1000 {
            return Err(invalid("Atlas graph as-of cannot be in the future"));
        }
        let c = self.connect()?;
        let pattern = format!("%{}%", search.to_lowercase());
        let mut q=c.prepare("SELECT id,kind,canonical_name,known_from,payload FROM atlas_entities WHERE known_from<=?1 AND (?2='' OR lower(canonical_name||' '||kind) LIKE ?3) ORDER BY canonical_name LIMIT ?4")?;
        let nodes=q.query_map(params![as_of,search,pattern,limit.min(300) as i64],|r|Ok(json!({"id":r.get::<_,String>(0)?,"kind":r.get::<_,String>(1)?,"name":r.get::<_,String>(2)?,"known_from":r.get::<_,i64>(3)?,"payload":serde_json::from_str::<Value>(&r.get::<_,String>(4)?).unwrap_or(Value::Null)})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        let ids: BTreeSet<String> = nodes
            .iter()
            .filter_map(|v| v["id"].as_str().map(str::to_string))
            .collect();
        let mut eq=c.prepare("SELECT id,graph_layer,from_id,to_id,kind,valid_from,valid_to,known_from,known_to,evidence_id,payload FROM atlas_edges WHERE known_from<=?1 AND (known_to IS NULL OR known_to>?1) ORDER BY known_from DESC LIMIT 2000")?;
        let edges=eq.query_map([as_of],|r|Ok(json!({"id":r.get::<_,String>(0)?,"layer":r.get::<_,String>(1)?,"from":r.get::<_,String>(2)?,"to":r.get::<_,String>(3)?,"kind":r.get::<_,String>(4)?,"valid_from":r.get::<_,Option<i64>>(5)?,"valid_to":r.get::<_,Option<i64>>(6)?,"known_from":r.get::<_,i64>(7)?,"known_to":r.get::<_,Option<i64>>(8)?,"evidence_id":r.get::<_,Option<String>>(9)?,"payload":serde_json::from_str::<Value>(&r.get::<_,String>(10)?).unwrap_or(Value::Null)})))?.filter_map(|r|match r {Ok(v) if ids.contains(v["from"].as_str().unwrap_or_default())||ids.contains(v["to"].as_str().unwrap_or_default())=>Some(Ok(v)),Ok(_)=>None,Err(e)=>Some(Err(e))}).collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(
            json!({"as_of":as_of,"nodes":nodes,"edges":edges,"semantics":"source/knowledge/statistical/predictive layers are distinct; an edge is not automatically causal"}),
        )
    }

    pub fn run_atlas_mission(&self, id: &str) -> Result<Value> {
        let c = self.connect()?;
        let row: Option<(String, String, String)> = c
            .query_row(
                "SELECT query,state,config_json FROM atlas_missions WHERE id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        let (query, state, config) = row.ok_or_else(|| invalid("Unknown Atlas mission"))?;
        if !["queued", "failed"].contains(&state.as_str()) {
            return Err(invalid(
                "Atlas mission is not runnable in its current state",
            ));
        }
        let request: MissionRequest = serde_json::from_str(&config)?;
        request.validate()?;
        c.execute(
            "UPDATE atlas_missions SET state='running',updated_at=?2,error=NULL WHERE id=?1",
            params![id, now()],
        )?;
        let result = self.run_atlas_mission_inner(id, &query, &request);
        match &result {
            Ok(v) => {
                c.execute("UPDATE atlas_missions SET state='completed',updated_at=?2,requests_used=?3,bytes_used=?4,datasets_found=?5,error=NULL WHERE id=?1",params![id,now(),v["requests_used"].as_i64().unwrap_or(0),v["bytes_used"].as_i64().unwrap_or(0),v["datasets_found"].as_i64().unwrap_or(0)])?;
            }
            Err(e) => {
                c.execute(
                    "UPDATE atlas_missions SET state='failed',updated_at=?2,error=?3 WHERE id=?1",
                    params![id, now(), e.to_string()],
                )?;
            }
        }
        result
    }

    fn run_atlas_mission_inner(
        &self,
        id: &str,
        query: &str,
        request: &MissionRequest,
    ) -> Result<Value> {
        let mut queue = VecDeque::new();
        for seed in self.atlas_seeds()?.into_iter().filter(|s| s.enabled) {
            if seed.rights.archive && seed.rights.display {
                queue.push_back((seed, 0usize, None::<String>));
            }
        }
        let client = Client::builder()
            .timeout(Duration::from_secs(15))
            .redirect(Policy::none())
            .user_agent("Hindsight-Atlas/0.1 metadata-discovery")
            .build()
            .map_err(|e| Error::Network(e.to_string()))?;
        let mut requests = 0usize;
        let mut bytes_used = 0u64;
        let mut found = BTreeSet::new();
        let mut visited = BTreeSet::new();
        let mut sources = 0usize;
        let mut source_errors = Vec::new();
        while let Some((seed, depth, parent)) = queue.pop_front() {
            if requests >= request.max_requests
                || bytes_used >= request.max_bytes
                || sources >= request.max_sources
            {
                break;
            }
            let mut requested =
                Url::parse(&seed.base_url).map_err(|_| invalid("Invalid Atlas seed URL"))?;
            if depth == 0 && seed.protocol == "ckan" {
                requested
                    .query_pairs_mut()
                    .append_pair("q", query)
                    .append_pair("rows", "12");
            }
            if !host_allowed(&seed, &requested) || !visited.insert(requested.to_string()) {
                continue;
            }
            let remaining = (request.max_bytes - bytes_used).min(2 * 1024 * 1024) as usize;
            let calls_left = request.max_requests - requests;
            let fetched = match atlas_fetch(&client, &seed, &requested, remaining, calls_left) {
                Ok(v) => v,
                Err((error, calls)) => {
                    requests += calls;
                    source_errors
                        .push(json!({"seed":seed.id,"url":requested.to_string(),"error":error.to_string()}));
                    continue;
                }
            };
            requests += fetched.calls;
            bytes_used += fetched.body.len() as u64;
            let url = fetched.url;
            visited.insert(url.to_string());
            let sha = hash(&fetched.body);
            let path = format!("raw/atlas/{}/{}.bin", &sha[..2], sha);
            let full = self.root.join(&path);
            if !full.exists() {
                fs::create_dir_all(
                    full.parent()
                        .ok_or_else(|| invalid("Atlas raw path has no parent"))?,
                )?;
                fs::write(&full, &fetched.body)?;
            }
            let sid = source_id(url.as_str());
            let t = now();
            let fetch_id = hash(format!("{id}\0{}\0{sha}", url).as_bytes());
            let parsed = protocols::parse(&seed.protocol, &url, &fetched.body);
            let mut db = self.connect()?;
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            tx.execute("INSERT OR IGNORE INTO atlas_sources(id,url,host,protocol,rights_state,discovered_at,parent_url,depth,payload) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",params![sid,url.as_str(),url.host_str().unwrap_or_default(),seed.protocol,seed.rights.state,t,parent,depth as i64,serde_json::to_string(&seed)?])?;
            match parsed {
                Err(error) => {
                    tx.execute("INSERT OR REPLACE INTO atlas_fetches(id,mission_id,url,started_at,completed_at,bytes,sha256,media_type,status,object_path,error) VALUES(?1,?2,?3,?4,?4,?5,?6,?7,'quarantined',?8,?9)",params![fetch_id,id,url.as_str(),t,fetched.body.len() as i64,sha,fetched.media,path,error.to_string()])?;
                    tx.commit()?;
                    source_errors.push(
                        json!({"seed":seed.id,"url":url.to_string(),"error":error.to_string(),"captured":true}),
                    );
                    continue;
                }
                Ok(page) => {
                    sources += 1;
                    tx.execute("INSERT OR REPLACE INTO atlas_fetches(id,mission_id,url,started_at,completed_at,bytes,sha256,media_type,status,object_path,error) VALUES(?1,?2,?3,?4,?4,?5,?6,?7,'captured',?8,NULL)",params![fetch_id,id,url.as_str(),t,fetched.body.len() as i64,sha,fetched.media,path])?;
                    let source_node = format!("source:{sid}");
                    tx.execute("INSERT OR IGNORE INTO atlas_entities(id,kind,canonical_name,known_from,payload) VALUES(?1,'Source',?2,?3,?4)",params![source_node,seed.name,t,serde_json::to_string(&seed)?])?;
                    for d in page.datasets {
                        let did = dataset_id(&sid, &d.external_id);
                        found.insert(did.clone());
                        tx.execute("INSERT INTO atlas_datasets(id,source_id,external_id,title,description,landing_url,license,spatial_json,temporal_json,keywords_json,discovered_at,payload) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12) ON CONFLICT(id) DO UPDATE SET title=excluded.title,description=excluded.description,landing_url=excluded.landing_url,license=excluded.license,spatial_json=excluded.spatial_json,temporal_json=excluded.temporal_json,keywords_json=excluded.keywords_json,payload=excluded.payload",params![did,sid,d.external_id,d.title,d.description,d.landing_url,d.license,serde_json::to_string(&d.spatial)?,serde_json::to_string(&d.temporal)?,serde_json::to_string(&d.keywords)?,t,serde_json::to_string(&d.payload)?])?;
                        for dist in &d.distributions {
                            let xid = distribution_id(&did, &dist.url);
                            tx.execute("INSERT OR REPLACE INTO atlas_distributions(id,dataset_id,url,media_type,format,rights_state,payload) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![xid,did,dist.url,dist.media_type,dist.format,seed.rights.state,serde_json::to_string(&dist.payload)?])?;
                        }
                        let dataset_node = format!("dataset:{did}");
                        tx.execute("INSERT OR IGNORE INTO atlas_entities(id,kind,canonical_name,known_from,payload) VALUES(?1,'Dataset',?2,?3,?4)",params![dataset_node,d.title,t,serde_json::to_string(&d)?])?;
                        let eid = edge_id("source", &source_node, &dataset_node, "publishes", t);
                        tx.execute("INSERT OR IGNORE INTO atlas_edges(id,graph_layer,from_id,to_id,kind,valid_from,valid_to,known_from,known_to,evidence_id,payload) VALUES(?1,'source',?2,?3,'publishes',NULL,NULL,?4,NULL,?5,'{}')",params![eid,source_node,dataset_node,t,fetch_id])?;
                        let score = score_dataset(query, &d);
                        let cid = hash(format!("{id}\0{did}").as_bytes());
                        tx.execute("INSERT OR REPLACE INTO atlas_candidates(id,mission_id,dataset_id,score,score_json,state,reason,created_at) VALUES(?1,?2,?3,?4,?5,'catalogued',?6,?7)",params![cid,id,did,score.total,serde_json::to_string(&score)?,score.reason,t])?;
                    }
                    for next in page.follow {
                        if depth < request.max_depth {
                            if let Ok(next_url) = Url::parse(&next) {
                                if host_allowed(&seed, &next_url) {
                                    let deid =
                                        hash(format!("{id}\0{}\0{}", url, next_url).as_bytes());
                                    tx.execute("INSERT OR IGNORE INTO atlas_discovery_edges(id,mission_id,from_url,to_url,relation,depth,discovered_at) VALUES(?1,?2,?3,?4,'typed_catalog_link',?5,?6)",params![deid,id,url.as_str(),next_url.as_str(),(depth+1) as i64,t])?;
                                    queue.push_back((
                                        AtlasSeed {
                                            base_url: next_url.to_string(),
                                            ..seed.clone()
                                        },
                                        depth + 1,
                                        Some(url.to_string()),
                                    ));
                                }
                            }
                        }
                    }
                    tx.commit()?;
                }
            }
        }
        if sources == 0 && !source_errors.is_empty() {
            return Err(Error::Network(format!(
                "All Atlas seed requests failed or were quarantined: {} failures",
                source_errors.len()
            )));
        }
        Ok(json!({
            "mission_id":id,"query":query,"requests_used":requests,"bytes_used":bytes_used,
            "datasets_found":found.len(),"sources_visited":sources,"source_errors":source_errors,
            "budget":{"max_requests":request.max_requests,"max_bytes":request.max_bytes,"max_depth":request.max_depth,"max_sources":request.max_sources},
            "claim":"real catalog metadata retrieved from reviewed seed catalogs; distribution acquisition remains separately gated"
        }))
    }
}

#[derive(Serialize)]
struct DatasetScore {
    total: f64,
    relevance: f64,
    provenance_quality: f64,
    cost_penalty: f64,
    reason: String,
}
fn score_dataset(query: &str, d: &protocols::DatasetRecord) -> DatasetScore {
    let tokens = query
        .to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| s.len() > 1)
        .map(str::to_string)
        .collect::<BTreeSet<_>>();
    let hay = format!("{} {} {}", d.title, d.description, d.keywords.join(" ")).to_lowercase();
    let hits = tokens.iter().filter(|t| hay.contains(t.as_str())).count();
    let relevance = if tokens.is_empty() {
        0.0
    } else {
        hits as f64 / tokens.len() as f64
    };
    let provenance = 1.0;
    let cost = if d.distributions.is_empty() {
        0.1
    } else {
        0.25
    };
    DatasetScore {
        total: (relevance * 0.65 + provenance * 0.35 - cost * 0.1).clamp(0.0, 1.0),
        relevance,
        provenance_quality: provenance,
        cost_penalty: cost,
        reason: format!(
            "{hits}/{} mission terms matched; catalog metadata only",
            tokens.len()
        ),
    }
}

struct FetchResult {
    url: Url,
    body: Vec<u8>,
    media: Option<String>,
    calls: usize,
}

fn atlas_fetch(
    client: &Client,
    seed: &AtlasSeed,
    start: &Url,
    limit: usize,
    max_calls: usize,
) -> std::result::Result<FetchResult, (Error, usize)> {
    if limit < 1024 || max_calls == 0 {
        return Err((
            Error::Resource("Atlas request/byte budget exhausted".into()),
            0,
        ));
    }
    let mut current = start.clone();
    let mut calls = 0usize;
    loop {
        if !host_allowed(seed, &current) {
            return Err((
                Error::Blocked("Atlas redirect escaped approved host scope".into()),
                calls,
            ));
        }
        if calls >= max_calls || calls >= 4 {
            return Err((
                Error::Resource("Atlas redirect/request budget exhausted".into()),
                calls,
            ));
        }
        calls += 1;
        let response = match client.get(current.clone()).send() {
            Ok(v) => v,
            Err(e) => return Err((Error::Network(e.to_string()), calls)),
        };
        if response.status().is_redirection() {
            let Some(location) = response
                .headers()
                .get(reqwest::header::LOCATION)
                .and_then(|v| v.to_str().ok())
            else {
                return Err((invalid("Atlas redirect omitted Location"), calls));
            };
            let next = match current.join(location) {
                Ok(v) => v,
                Err(_) => return Err((invalid("Atlas redirect Location is invalid"), calls)),
            };
            if !host_allowed(seed, &next) {
                return Err((
                    Error::Blocked("Atlas redirect escaped approved host scope".into()),
                    calls,
                ));
            }
            current = next;
            continue;
        }
        if !response.status().is_success() {
            return Err((
                Error::Http {
                    status: response.status().as_u16(),
                    retry_after_ms: 0,
                },
                calls,
            ));
        }
        if response.content_length().is_some_and(|n| n > limit as u64) {
            return Err((
                Error::Resource("Atlas response exceeds mission byte budget".into()),
                calls,
            ));
        }
        let media = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let mut body =
            Vec::with_capacity(response.content_length().unwrap_or(0).min(limit as u64) as usize);
        let mut take = response.take((limit + 1) as u64);
        if let Err(e) = take.read_to_end(&mut body) {
            return Err((Error::Io(e), calls));
        }
        if body.len() > limit {
            return Err((
                Error::Resource("Atlas response exceeded bounded read".into()),
                calls,
            ));
        }
        return Ok(FetchResult {
            url: current,
            body,
            media,
            calls,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn seed() -> AtlasSeed {
        AtlasSeed {
            id: "x".into(),
            name: "X".into(),
            protocol: "ckan".into(),
            base_url: "https://data.example/api".into(),
            enabled: true,
            rights: SeedRights {
                state: "reviewed_open_metadata".into(),
                display: true,
                archive: true,
                research: true,
                training: false,
                license: "fixture".into(),
                host_scope: vec!["data.example".into()],
                reviewed_at: "2026-09-17".into(),
            },
        }
    }
    #[test]
    fn host_scope_allows_https_subdomains_but_not_suffix_tricks() {
        let s = seed();
        assert!(host_allowed(
            &s,
            &Url::parse("https://www.data.example/a").unwrap()
        ));
        assert!(host_allowed(
            &s,
            &Url::parse("https://data.example/a").unwrap()
        ));
        assert!(!host_allowed(
            &s,
            &Url::parse("http://data.example/a").unwrap()
        ));
        assert!(!host_allowed(
            &s,
            &Url::parse("https://data.example.evil.test/a").unwrap()
        ));
        assert!(!host_allowed(
            &s,
            &Url::parse("https://evil-data.example.test/a").unwrap()
        ));
    }
    #[test]
    fn acquisition_score_is_bounded_and_rewards_relevance() {
        let d = protocols::DatasetRecord {
            external_id: "1".into(),
            title: "Global copper production".into(),
            description: "mine output".into(),
            landing_url: None,
            license: None,
            spatial: Value::Null,
            temporal: Value::Null,
            keywords: vec!["copper".into()],
            distributions: vec![],
            payload: json!({}),
        };
        let relevant = score_dataset("copper production", &d);
        let unrelated = score_dataset("rainfall wheat", &d);
        assert!((0.0..=1.0).contains(&relevant.total));
        assert!(relevant.total > unrelated.total);
    }
}
