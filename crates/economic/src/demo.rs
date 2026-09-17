//! Entirely synthetic prices, facts and relationships on a pinned real calendar.
use crate::types::*;
use std::collections::BTreeMap;
pub fn input() -> Result<Input> {
    let sessions: Vec<Session> = serde_json::from_str(include_str!(
        "../../../fixtures/economic/xnys_2026_sessions.json"
    ))
    .map_err(|e| e.to_string())?;
    let first = sessions[0].open;
    let availability = |at| Availability {
        published_at: at,
        timestamp_precision: "instant".into(),
        publication_timezone: "UTC".into(),
        first_seen_at: Some(at + 1000),
        extraction_completed_at: Some(at + 2000),
        modeled_available_at: Some(at + 2000),
    };
    let mut sources = vec![Source {
        id: "synthetic-market".into(),
        url: "fixture://deterministic-market".into(),
        title: "Synthetic minute prices, not vendor data".into(),
        permission: "synthetic-redistributable".into(),
        content_sha256: "0".repeat(64),
        availability: availability(first - DAY),
        excerpt: "Generated exclusively for software verification.".into(),
    }];
    let mut facts = vec![];
    let mut relationships = vec![];
    let mut bars = vec![];
    for (i, s) in sessions.iter().enumerate() {
        for (symbol, base, drift) in [("MU", 100 * MONEY, 55_000), ("SPY", 500 * MONEY, 35_000)] {
            for minute in 0..(s.close - s.open) / MINUTE {
                let wave = (((i as i64 + minute / 17) % 19) - 9) * 8_000;
                let open = base + i as i64 * drift + minute * 170 + wave;
                let close = open + ((minute % 7) - 3) * 600;
                bars.push(Bar {
                    symbol: symbol.into(),
                    start: s.open + minute * MINUTE,
                    end: s.open + (minute + 1) * MINUTE,
                    available_at: s.open + (minute + 1) * MINUTE,
                    open,
                    high: open.max(close) + 800,
                    low: open.min(close) - 800,
                    close,
                    volume: 20_000 + (i as u64 * 97 + minute as u64 * 31) % 8000,
                    source_id: "synthetic-market".into(),
                });
            }
        }
        if i % 7 == 0 {
            for (entity, value) in [
                ("MU", 1000 + i as i64 * 5),
                ("MEMORY", 700 + i as i64 * 3),
                ("SUPPLIER", 300 + i as i64 * 2),
            ] {
                let id = format!("{entity}-release-{i}");
                let at = s.open - 5 * MINUTE;
                sources.push(Source {
                    id: id.clone(),
                    url: format!("fixture://{id}"),
                    title: format!("SYNTHETIC {entity} release"),
                    permission: "synthetic-redistributable".into(),
                    content_sha256: format!("{:064x}", i + 1),
                    availability: availability(at),
                    excerpt: format!(
                        "Synthetic reported revenue {value}; no real business observation."
                    ),
                });
                facts.push(Fact {
                    id: format!("{id}-v1"),
                    key: FactKey {
                        entity: entity.into(),
                        metric: "revenue".into(),
                        source_series: format!("synthetic-{entity}"),
                        unit: "USD".into(),
                        scale: 0,
                        period_start: s.date.clone(),
                        period_end: s.date.clone(),
                        dimensions: BTreeMap::new(),
                    },
                    value_micros: value * MONEY,
                    revision: 1,
                    status: Status::Active,
                    source_id: id,
                    source_locator: "fixture:value".into(),
                    availability: availability(at),
                });
            }
        }
    }
    let correction_time = sessions[12].decision + MINUTE;
    let mut corrected = facts[0].clone();
    corrected.id = "MU-first-correction".into();
    corrected.revision = 2;
    corrected.value_micros += 17 * MONEY;
    corrected.availability = availability(correction_time);
    let correction_source = "MU-correction-source".to_string();
    corrected.source_id = correction_source.clone();
    sources.push(Source {
        id: correction_source,
        url: "fixture://correction".into(),
        title: "SYNTHETIC correction".into(),
        permission: "synthetic-redistributable".into(),
        content_sha256: "c".repeat(64),
        availability: availability(correction_time),
        excerpt: "A later correction must not change earlier views.".into(),
    });
    facts.push(corrected);
    relationships.push(Relationship {
        id: "supplier-to-mu".into(),
        from: "SUPPLIER".into(),
        to: "MU".into(),
        relation: "supplies".into(),
        product: Some("synthetic-memory-component".into()),
        valid_from: first - DAY,
        valid_to: None,
        revision: 1,
        status: Status::Active,
        weight_ppm: 1_000_000,
        weight_method: "synthetic equal weight; not an empirical relationship".into(),
        source_id: "SUPPLIER-release-0".into(),
        source_locator: "fixture:relationship".into(),
        availability: availability(sessions[6].open),
    });
    Ok(Input {dataset:Dataset {schema_version:1,label:"Synthetic memory-industry research demonstration".into(),synthetic:true,selection_disclosure:"MU/SPY are retrospectively selected engineering identifiers; all market/economic values and the supplier edge are synthetic.".into(),price_basis:"raw_with_explicit_actions".into(),calendar_version:"exchange_calendars pinned fixture; see calendar_provenance.json".into(),calendar_source:"XNYS session export with America/New_York decisions".into(),sources,facts,relationships,sessions,bars,corporate_actions:vec![]},config:Config::default()})
}
