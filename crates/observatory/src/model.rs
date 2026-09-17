use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Invalid(String),
    #[error("{0}")]
    Blocked(String),
    #[error("{0}")]
    Conflict(String),
    #[error("{0}")]
    Resource(String),
    #[error("{0}")]
    Cancelled(String),
    #[error("HTTP {status}; retry_after_ms={retry_after_ms}")]
    Http { status: u16, retry_after_ms: i64 },
    #[error("network request failed ({0}); credentials suppressed")]
    Network(String),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Sql(#[from] rusqlite::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Parquet(#[from] parquet::errors::ParquetError),
    #[error(transparent)]
    Arrow(#[from] arrow_schema::ArrowError),
}
pub type Result<T> = std::result::Result<T, Error>;
pub fn invalid(s: impl Into<String>) -> Error {
    Error::Invalid(s.into())
}
pub fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}
pub fn hash(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(bytes))
}
pub fn time(s: &str) -> Result<i64> {
    chrono::DateTime::parse_from_rfc3339(s)
        .map(|t| t.timestamp_millis())
        .map_err(|_| invalid("Expected RFC3339 timestamp with timezone"))
}

#[derive(Clone, Debug)]
pub struct Store {
    pub root: PathBuf,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Policy {
    pub id: String,
    pub name: String,
    pub url: String,
    pub version: String,
    pub reviewed_at: String,
    pub review_due: String,
    pub license: String,
    pub display: bool,
    pub archive: bool,
    pub research: bool,
    pub training: bool,
    pub commercial: bool,
    pub redistribution: bool,
    pub gate: String,
    pub credential_env: Vec<String>,
    pub scope: String,
    pub warning: String,
    pub interval_ms: i64,
}
impl Policy {
    pub fn authorize(&self, purpose: &str, today: &str) -> Result<()> {
        if self.gate != "enabled" || !self.permits(purpose) || self.review_due.as_str() < today {
            return Err(Error::Blocked(format!(
                "blocked_permission: {}: {purpose}; policy review required",
                self.id
            )));
        }
        Ok(())
    }
    pub fn permits(&self, purpose: &str) -> bool {
        match purpose {
            "display" => self.display,
            "archive" => self.archive,
            "research" => self.research,
            "training" => self.training,
            "commercial" => self.commercial,
            "redistribution" => self.redistribution,
            _ => false,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Series {
    pub id: String,
    pub provider: String,
    pub code: String,
    pub name: String,
    pub entity: String,
    pub unit: String,
    pub frequency: String,
    pub description: String,
    pub source_url: String,
    pub metadata: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Observation {
    pub series_id: String,
    pub period_start: String,
    pub period_end: String,
    pub value: Option<f64>,
    pub unit: String,
    pub dimensions: Value,
    pub published_at: Option<i64>,
    pub reconstructed_at: Option<i64>,
    pub source_order: Option<i64>,
    pub precision: String,
    pub quality: String,
    pub extras: Value,
}
impl Observation {
    pub fn validate(&self) -> Result<()> {
        if self.series_id.is_empty()
            || self.period_start.is_empty()
            || self.period_start > self.period_end
            || self.unit.is_empty()
            || self.value.is_some_and(|v| !v.is_finite())
        {
            return Err(invalid("Invalid observation identity/value"));
        }
        if self.reconstructed_at.is_some() && self.published_at.is_none() {
            return Err(invalid("Reconstruction requires publication evidence"));
        }
        if let (Some(p), Some(r)) = (self.published_at, self.reconstructed_at) {
            if r < p {
                return Err(invalid("Reconstruction precedes publication"));
            }
        }
        Ok(())
    }
    pub fn semantic_id(&self) -> String {
        let mut v = serde_json::to_value(self).expect("serializable observation");
        if let Some(e) = v.get_mut("extras").and_then(Value::as_object_mut) {
            e.remove("provider_lastupdated");
        }
        hash(
            serde_json::to_string(&v)
                .expect("serializable observation")
                .as_bytes(),
        )
    }
    pub fn key(&self) -> String {
        hash(
            serde_json::to_string(&(
                &self.series_id,
                &self.period_start,
                &self.period_end,
                &self.unit,
                &self.dimensions,
            ))
            .expect("serializable identity")
            .as_bytes(),
        )
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Capture {
    pub id: String,
    pub provider: String,
    pub url: String,
    pub sha256: String,
    pub path: String,
    pub bytes: u64,
    pub request_started_at: i64,
    pub response_headers_at: i64,
    pub response_completed_at: i64,
    pub etag: Option<String>,
    pub last_modified: Option<String>,
    pub representation: String,
    pub original_sha256: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Job {
    pub id: String,
    pub provider: String,
    pub kind: String,
    pub resource: String,
    pub params: Value,
    pub state: String,
    pub cutoff: i64,
    pub cursor: Value,
    pub attempts: i64,
    pub lease_token: Option<String>,
    pub lease_until: Option<i64>,
    pub priority: i64,
    pub error: Option<String>,
    pub rows: i64,
    pub bytes: i64,
    pub updated_at: i64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Scope {
    pub snapshot: i64,
    pub as_of: i64,
    pub mode: String,
    pub purpose: String,
}
impl Scope {
    pub fn validate(&self) -> Result<()> {
        if self.snapshot < 0
            || self.as_of < 0
            || !["observed", "reconstructed"].contains(&self.mode.as_str())
            || ![
                "display",
                "research",
                "training",
                "commercial",
                "redistribution",
            ]
            .contains(&self.purpose.as_str())
        {
            return Err(invalid("Invalid historical query scope"));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Point {
    pub id: String,
    pub key: String,
    pub series_id: String,
    pub period_start: String,
    pub period_end: String,
    pub value: Option<f64>,
    pub unit: String,
    pub published_at: Option<i64>,
    pub observed_at: i64,
    pub reconstructed_at: Option<i64>,
    pub source_order: Option<i64>,
    pub capture_id: String,
    pub precision: String,
    pub quality: String,
    pub generation: i64,
    pub extras: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PlannedJob {
    pub provider: String,
    pub kind: String,
    pub resource: String,
    pub params: Value,
    pub priority: i64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PageResult {
    #[serde(default)]
    pub complete_scope: bool,
    pub series: Vec<Series>,
    pub observations: Vec<Observation>,
    pub next: Option<Value>,
    pub advertised_total: Option<i64>,
    pub received_rows: i64,
    pub note: String,
    #[serde(default)]
    pub children: Vec<PlannedJob>,
}
