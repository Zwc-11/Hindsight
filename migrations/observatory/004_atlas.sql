CREATE TABLE IF NOT EXISTS atlas_seeds(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL,
    base_url TEXT NOT NULL, enabled INTEGER NOT NULL, rights_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS atlas_missions(
    id TEXT PRIMARY KEY, query TEXT NOT NULL, state TEXT NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    config_json TEXT NOT NULL, requests_used INTEGER NOT NULL DEFAULT 0,
    bytes_used INTEGER NOT NULL DEFAULT 0, datasets_found INTEGER NOT NULL DEFAULT 0,
    error TEXT);
CREATE TABLE IF NOT EXISTS atlas_sources(
    id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, host TEXT NOT NULL,
    protocol TEXT NOT NULL, rights_state TEXT NOT NULL, discovered_at INTEGER NOT NULL,
    parent_url TEXT, depth INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS atlas_datasets(
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES atlas_sources(id),
    external_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
    landing_url TEXT, license TEXT, spatial_json TEXT NOT NULL,
    temporal_json TEXT NOT NULL, keywords_json TEXT NOT NULL,
    discovered_at INTEGER NOT NULL, payload TEXT NOT NULL,
    UNIQUE(source_id, external_id));CREATE TABLE IF NOT EXISTS atlas_distributions(
    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES atlas_datasets(id),
    url TEXT NOT NULL, media_type TEXT, format TEXT, rights_state TEXT NOT NULL,
    payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS atlas_entities(
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, canonical_name TEXT NOT NULL,
    known_from INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS atlas_external_ids(
    entity_id TEXT NOT NULL REFERENCES atlas_entities(id), namespace TEXT NOT NULL,
    value TEXT NOT NULL, valid_from INTEGER, valid_to INTEGER,
    known_from INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(entity_id, namespace, value, known_from));
CREATE TABLE IF NOT EXISTS atlas_edges(
    id TEXT PRIMARY KEY, graph_layer TEXT NOT NULL, from_id TEXT NOT NULL,
    to_id TEXT NOT NULL, kind TEXT NOT NULL, valid_from INTEGER,
    valid_to INTEGER, known_from INTEGER NOT NULL, known_to INTEGER,
    evidence_id TEXT, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_atlas_edges_from ON atlas_edges(from_id,known_from);
CREATE INDEX IF NOT EXISTS idx_atlas_edges_to ON atlas_edges(to_id,known_from);
CREATE TABLE IF NOT EXISTS atlas_discovery_edges(
    id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES atlas_missions(id),
    from_url TEXT, to_url TEXT NOT NULL, relation TEXT NOT NULL,
    depth INTEGER NOT NULL, discovered_at INTEGER NOT NULL);CREATE TABLE IF NOT EXISTS atlas_fetches(
    id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES atlas_missions(id),
    url TEXT NOT NULL, started_at INTEGER NOT NULL, completed_at INTEGER,
    bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT, media_type TEXT,
    status TEXT NOT NULL, object_path TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS atlas_candidates(
    id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES atlas_missions(id),
    dataset_id TEXT REFERENCES atlas_datasets(id), score REAL NOT NULL,
    score_json TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
    created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_atlas_dataset_title ON atlas_datasets(title);
CREATE INDEX IF NOT EXISTS idx_atlas_mission_state ON atlas_missions(state,created_at);
CREATE INDEX IF NOT EXISTS idx_atlas_fetch_mission ON atlas_fetches(mission_id,status);
