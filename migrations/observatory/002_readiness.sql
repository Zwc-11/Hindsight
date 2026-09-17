CREATE TABLE IF NOT EXISTS refresh_schedule(provider TEXT PRIMARY KEY REFERENCES providers(id),interval_ms INTEGER NOT NULL,next_due INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS readiness(requirement_id TEXT PRIMARY KEY,state TEXT NOT NULL,detail TEXT NOT NULL,evidence TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_experiments_state ON experiments(state,created_at);
CREATE INDEX IF NOT EXISTS idx_series_generation ON series_versions(series_id,generation,known_at);
CREATE INDEX IF NOT EXISTS idx_revisions_span ON observation_versions(series_id,period_start,period_end,source_order,observed_at);
