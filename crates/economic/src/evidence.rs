use crate::types::*;
use serde::Serialize;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct Snapshot {
    pub as_of: Time,
    pub mode: EvidenceMode,
    pub sources: Vec<Source>,
    pub facts: Vec<Fact>,
    pub relationships: Vec<Relationship>,
}

/// Source and derived-object readiness both apply. No latest-vintage fallback.
pub fn snapshot(data: &Dataset, at: Time, mode: EvidenceMode) -> Result<Snapshot> {
    let sources: BTreeMap<_, _> = data
        .sources
        .iter()
        .filter(|s| s.availability.ready(mode).is_some_and(|t| t <= at))
        .map(|s| (s.id.as_str(), s))
        .collect();
    let mut facts: BTreeMap<&FactKey, &Fact> = BTreeMap::new();
    for fact in &data.facts {
        if !sources.contains_key(fact.source_id.as_str())
            || fact.availability.ready(mode).is_none_or(|t| t > at)
        {
            continue;
        }
        if let Some(old) = facts.get(&fact.key) {
            if fact.revision == old.revision && *old != fact {
                return Err(format!("conflicting revision: {}", fact.id));
            }
            if fact.revision <= old.revision {
                continue;
            }
        }
        facts.insert(&fact.key, fact);
    }
    let mut edges: BTreeMap<&str, &Relationship> = BTreeMap::new();
    for edge in &data.relationships {
        if !sources.contains_key(edge.source_id.as_str())
            || edge.availability.ready(mode).is_none_or(|t| t > at)
        {
            continue;
        }
        if let Some(old) = edges.get(edge.id.as_str()) {
            if edge.revision == old.revision && *old != edge {
                return Err(format!("conflicting relationship: {}", edge.id));
            }
            if edge.revision <= old.revision {
                continue;
            }
        }
        edges.insert(&edge.id, edge);
    }
    Ok(Snapshot {
        as_of: at,
        mode,
        sources: sources.values().map(|s| (*s).clone()).collect(),
        facts: facts
            .values()
            .filter(|f| f.status == Status::Active)
            .map(|f| (*f).clone())
            .collect(),
        relationships: edges
            .values()
            .filter(|e| {
                e.status == Status::Active
                    && e.valid_from <= at
                    && e.valid_to.is_none_or(|t| at < t)
            })
            .map(|e| (*e).clone())
            .collect(),
    })
}

/// Conflicting units, dimensions, or series are not silently combined.
pub fn latest_metric<'a>(
    view: &'a Snapshot,
    entity: &str,
    metric: &str,
) -> Result<Option<&'a Fact>> {
    let mut candidates: Vec<_> = view
        .facts
        .iter()
        .filter(|f| f.key.entity == entity && f.key.metric == metric && f.key.dimensions.is_empty())
        .collect();
    candidates.sort_by(|a, b| {
        (&a.key.period_end, &a.key.period_start).cmp(&(&b.key.period_end, &b.key.period_start))
    });
    let Some(last) = candidates.last().copied() else {
        return Ok(None);
    };
    for f in &candidates {
        if f.key.period_end == last.key.period_end
            && f.key.period_start == last.key.period_start
            && f.key != last.key
        {
            return Err(format!("ambiguous metric identity for {entity}/{metric}; normalize to a declared series first"));
        }
    }
    Ok(Some(last))
}

pub fn conservative_date_end(date: &str, timezone: &str, allowance_ms: i64) -> Result<Time> {
    use chrono::{NaiveDate, TimeZone};
    if allowance_ms < 0 {
        return Err("negative publication allowance".into());
    }
    let day = NaiveDate::parse_from_str(date, "%Y-%m-%d").map_err(|e| e.to_string())?;
    let tz: chrono_tz::Tz = timezone.parse().map_err(|_| "unknown timezone")?;
    let midnight = day
        .succ_opt()
        .ok_or("date overflow")?
        .and_hms_opt(0, 0, 0)
        .ok_or("midnight")?;
    let next = tz
        .from_local_datetime(&midnight)
        .single()
        .ok_or("ambiguous publisher midnight")?;
    next.timestamp_millis()
        .checked_add(allowance_ms)
        .ok_or_else(|| "timestamp overflow".into())
}
