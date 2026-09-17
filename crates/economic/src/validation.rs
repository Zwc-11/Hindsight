use crate::types::*;
use chrono::{DateTime, Datelike, Timelike};
use std::collections::{BTreeMap, BTreeSet};
pub fn validate(input: &Input) -> Result<()> {
    let d = &input.dataset;
    let c = &input.config;
    if d.schema_version != 1 || d.price_basis != "raw_with_explicit_actions" {
        return Err("unsupported schema or price basis".into());
    }
    if d.selection_disclosure.is_empty()
        || d.calendar_version.is_empty()
        || d.calendar_source.is_empty()
    {
        return Err("missing selection/calendar provenance".into());
    }
    if c.symbol == c.benchmark
        || c.horizon_sessions == 0
        || c.min_train < 3
        || c.train_window < c.min_train
        || c.max_shares < 0
        || c.initial_cash_micros <= 0
    {
        return Err("invalid research configuration".into());
    }
    if !c.ridge.is_finite()
        || c.ridge <= 0.0
        || !c.signal_threshold.is_finite()
        || c.signal_threshold < 0.0
    {
        return Err("invalid numerical configuration".into());
    }
    if c.order_latency_ms <= 0
        || c.order_latency_ms >= MINUTE
        || c.report_latency_ms < 0
        || c.report_latency_ms >= MINUTE
        || c.observation_delay_ms < 0
        || c.observation_delay_ms > 7 * DAY
        || c.max_quote_age_ms <= 0
    {
        return Err(
            "invalid delay; initial engine requires sub-minute order/report latency".into(),
        );
    }
    if c.max_past_volume_ppm > 1_000_000
        || c.fee_bps > 1000
        || c.impact_bps > 1000
        || c.spread_bps > 1000
        || c.bootstrap_repetitions < 10
    {
        return Err("invalid cost/capacity/bootstrap configuration".into());
    }
    let mut sources = BTreeSet::new();
    for s in &d.sources {
        if s.id.is_empty() || !sources.insert(&s.id) {
            return Err("duplicate/empty source id".into());
        }
        if s.content_sha256.len() != 64 || !s.content_sha256.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err("invalid source SHA256".into());
        }
        s.availability.validate()?;
    }
    let mut ids = BTreeMap::new();
    let mut revisions = BTreeMap::new();
    for f in &d.facts {
        f.availability.validate()?;
        if !sources.contains(&f.source_id) || f.source_locator.is_empty() {
            return Err("fact missing source/locator".into());
        }
        for date in [&f.key.period_start, &f.key.period_end] {
            chrono::NaiveDate::parse_from_str(date, "%Y-%m-%d")
                .map_err(|_| "invalid reporting period")?;
        }
        if f.key.period_start > f.key.period_end
            || !(-12..=12).contains(&f.key.scale)
            || f.key.unit.is_empty()
            || f.key.source_series.is_empty()
        {
            return Err("invalid fact identity".into());
        }
        if ids.insert(&f.id, f).is_some_and(|old| old != f)
            || revisions
                .insert((&f.key, f.revision), f)
                .is_some_and(|old| old != f)
        {
            return Err("conflicting fact id/revision".into());
        }
    }
    let mut edge_versions = BTreeMap::new();
    for e in &d.relationships {
        e.availability.validate()?;
        if !sources.contains(&e.source_id)
            || e.source_locator.is_empty()
            || e.weight_method.is_empty()
            || e.weight_ppm < 0
            || e.weight_ppm > 1_000_000
            || e.valid_to.is_some_and(|t| t <= e.valid_from)
        {
            return Err("invalid relationship evidence".into());
        }
        if ![
            "supplies",
            "purchases",
            "competes",
            "manufactures",
            "exposed_to",
        ]
        .contains(&e.relation.as_str())
        {
            return Err("unsupported relationship type".into());
        }
        if edge_versions
            .insert((&e.id, e.revision), e)
            .is_some_and(|old| old != e)
        {
            return Err("conflicting edge revision".into());
        }
    }
    validate_sessions(&d.sessions)?;
    if d.sessions.len() < c.min_train + c.horizon_sessions + 3 {
        return Err("insufficient sessions".into());
    }
    let mut bars = BTreeMap::new();
    for b in &d.bars {
        if !sources.contains(&b.source_id)
            || b.end.checked_sub(b.start) != Some(MINUTE)
            || b.start % MINUTE != 0
            || b.available_at < b.end
            || b.low <= 0
            || b.high < b.low
            || b.open < b.low
            || b.open > b.high
            || b.close < b.low
            || b.close > b.high
        {
            return Err("invalid minute bar".into());
        }
        let idx = d.sessions.partition_point(|s| s.open <= b.start);
        if idx == 0 || b.end > d.sessions[idx - 1].close {
            return Err("bar outside supplied regular session calendar".into());
        }
        if bars
            .insert((&b.symbol, b.start), b)
            .is_some_and(|old| old != b)
        {
            return Err("conflicting duplicate bar".into());
        }
    }
    for symbol in [&c.symbol, &c.benchmark] {
        if !d.bars.iter().any(|b| &b.symbol == symbol) {
            return Err(format!("no bars for {symbol}"));
        }
    }
    let mut action_ids = BTreeSet::new();
    let mut action_times = BTreeSet::new();
    for a in &d.corporate_actions {
        if !d.sessions.iter().any(|s| s.open == a.effective_at)
            || !action_times.insert((&a.symbol, a.effective_at))
        {
            return Err(
                "actions must be consolidated per symbol at a supplied session open".into(),
            );
        }
        if a.dividend_per_share_micros > 0 && a.dividend_pay_at.is_none_or(|t| t < a.effective_at) {
            return Err("dividend requires a valid payable date".into());
        }
        if !action_ids.insert(&a.id)
            || a.split_numerator == 0
            || a.split_denominator == 0
            || a.dividend_per_share_micros < 0
            || a.effective_at % MINUTE != 0
        {
            return Err("invalid/duplicate corporate action".into());
        }
    }
    Ok(())
}
fn validate_sessions(sessions: &[Session]) -> Result<()> {
    let mut previous = None;
    for s in sessions {
        let open = DateTime::from_timestamp_millis(s.open)
            .ok_or("bad session timestamp")?
            .with_timezone(&chrono_tz::America::New_York);
        let decision = DateTime::from_timestamp_millis(s.decision)
            .ok_or("bad decision timestamp")?
            .with_timezone(&chrono_tz::America::New_York);
        let close = DateTime::from_timestamp_millis(s.close)
            .ok_or("bad close timestamp")?
            .with_timezone(&chrono_tz::America::New_York);
        if previous.is_some_and(|t| t >= s.open)
            || open.date_naive().to_string() != s.date
            || decision.date_naive() != open.date_naive()
            || close.date_naive() != open.date_naive()
            || open.weekday().number_from_monday() > 5
            || (open.hour(), open.minute(), open.second()) != (9, 30, 0)
            || (decision.hour(), decision.minute(), decision.second()) != (10, 0, 0)
            || ![13, 16].contains(&close.hour())
            || close.minute() != 0
            || close.second() != 0
            || s.open % MINUTE != 0
            || s.close % MINUTE != 0
            || s.decision % MINUTE != 0
        {
            return Err("invalid pinned New York session schedule".into());
        }
        previous = Some(s.close);
    }
    Ok(())
}
