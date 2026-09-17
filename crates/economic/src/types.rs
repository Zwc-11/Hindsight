//! Versioned research input. All timestamps are UTC milliseconds, money is USD micros.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub type Time = i64;
pub type Result<T> = std::result::Result<T, String>;
pub const MINUTE: i64 = 60_000;
pub const DAY: i64 = 86_400_000;
pub const MONEY: i64 = 1_000_000;

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EvidenceMode {
    Observed,
    Reconstructed,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Availability {
    pub published_at: Time,
    pub timestamp_precision: String,
    pub publication_timezone: String,
    pub first_seen_at: Option<Time>,
    pub extraction_completed_at: Option<Time>,
    pub modeled_available_at: Option<Time>,
}
impl Availability {
    pub fn ready(&self, mode: EvidenceMode) -> Option<Time> {
        match mode {
            EvidenceMode::Observed => Some(
                self.published_at
                    .max(self.first_seen_at?)
                    .max(self.extraction_completed_at?),
            ),
            EvidenceMode::Reconstructed => Some(self.published_at.max(self.modeled_available_at?)),
        }
    }
    pub fn validate(&self) -> Result<()> {
        if !["instant", "date_end_conservative"].contains(&self.timestamp_precision.as_str()) {
            return Err("timestamp precision must be instant or date_end_conservative".into());
        }
        self.publication_timezone
            .parse::<chrono_tz::Tz>()
            .map_err(|_| "unknown publication timezone")?;
        if let (Some(a), Some(b)) = (self.first_seen_at, self.extraction_completed_at) {
            if b < a {
                return Err("extraction precedes receipt".into());
            }
        }
        if self
            .modeled_available_at
            .is_some_and(|t| t < self.published_at)
        {
            return Err("modeled availability precedes publication".into());
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub id: String,
    pub url: String,
    pub title: String,
    pub permission: String,
    pub content_sha256: String,
    pub availability: Availability,
    pub excerpt: String,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(deny_unknown_fields)]
pub struct FactKey {
    pub entity: String,
    pub metric: String,
    pub source_series: String,
    pub unit: String,
    pub scale: i32,
    pub period_start: String,
    pub period_end: String,
    pub dimensions: BTreeMap<String, String>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Active,
    Retracted,
    Unresolved,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Fact {
    pub id: String,
    pub key: FactKey,
    pub value_micros: i64,
    pub revision: u32,
    pub status: Status,
    pub source_id: String,
    pub source_locator: String,
    pub availability: Availability,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Relationship {
    pub id: String,
    pub from: String,
    pub to: String,
    pub relation: String,
    pub product: Option<String>,
    pub valid_from: Time,
    pub valid_to: Option<Time>,
    pub revision: u32,
    pub status: Status,
    pub weight_ppm: i64,
    pub weight_method: String,
    pub source_id: String,
    pub source_locator: String,
    pub availability: Availability,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Bar {
    pub symbol: String,
    pub start: Time,
    pub end: Time,
    pub available_at: Time,
    pub open: i64,
    pub high: i64,
    pub low: i64,
    pub close: i64,
    pub volume: u64,
    pub source_id: String,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Session {
    pub date: String,
    pub open: Time,
    pub decision: Time,
    pub close: Time,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CorporateAction {
    pub id: String,
    pub symbol: String,
    pub effective_at: Time,
    pub split_numerator: u32,
    pub split_denominator: u32,
    pub dividend_per_share_micros: i64,
    pub dividend_pay_at: Option<Time>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Dataset {
    pub schema_version: u32,
    pub label: String,
    pub synthetic: bool,
    pub selection_disclosure: String,
    pub price_basis: String,
    pub calendar_version: String,
    pub calendar_source: String,
    pub sources: Vec<Source>,
    pub facts: Vec<Fact>,
    pub relationships: Vec<Relationship>,
    pub sessions: Vec<Session>,
    pub bars: Vec<Bar>,
    pub corporate_actions: Vec<CorporateAction>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub symbol: String,
    pub benchmark: String,
    pub issuer_metric: String,
    pub industry_entity: String,
    pub industry_metric: String,
    pub mode: EvidenceMode,
    pub horizon_sessions: usize,
    pub min_train: usize,
    pub train_window: usize,
    pub ridge: f64,
    pub signal_threshold: f64,
    pub order_latency_ms: i64,
    pub report_latency_ms: i64,
    pub observation_delay_ms: i64,
    pub initial_cash_micros: i64,
    pub max_shares: i64,
    pub spread_bps: u32,
    pub impact_bps: u32,
    pub fee_bps: u32,
    pub max_past_volume_ppm: u32,
    pub max_quote_age_ms: i64,
    pub bootstrap_seed: u64,
    pub bootstrap_repetitions: usize,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            symbol: "MU".into(),
            benchmark: "SPY".into(),
            issuer_metric: "revenue".into(),
            industry_entity: "MEMORY".into(),
            industry_metric: "revenue".into(),
            mode: EvidenceMode::Reconstructed,
            horizon_sessions: 5,
            min_train: 15,
            train_window: 60,
            ridge: 1.0,
            signal_threshold: 0.001,
            order_latency_ms: 100,
            report_latency_ms: 100,
            observation_delay_ms: 0,
            initial_cash_micros: 100_000 * MONEY,
            max_shares: 100,
            spread_bps: 2,
            impact_bps: 1,
            fee_bps: 1,
            max_past_volume_ppm: 10_000,
            max_quote_age_ms: 180_000,
            bootstrap_seed: 1729,
            bootstrap_repetitions: 500,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub dataset: Dataset,
    pub config: Config,
}
