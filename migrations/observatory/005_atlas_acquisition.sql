CREATE TABLE IF NOT EXISTS atlas_resource_policies(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rights_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atlas_distribution_profiles(
    distribution_id TEXT PRIMARY KEY REFERENCES atlas_distributions(id),
    profiled_at INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    media_type TEXT,
    format TEXT,
    schema_json TEXT NOT NULL,
    sample_object_path TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS atlas_observations(
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES atlas_datasets(id),
    distribution_id TEXT NOT NULL REFERENCES atlas_distributions(id),
    series_key TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    published_at INTEGER,
    observed_at INTEGER NOT NULL,
    quality TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_atlas_observations_dataset
    ON atlas_observations(dataset_id, period_end, observed_at);
CREATE INDEX IF NOT EXISTS idx_atlas_observations_series
    ON atlas_observations(series_key, period_end, observed_at);
