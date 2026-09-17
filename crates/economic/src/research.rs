use crate::{
    evidence::{latest_metric, snapshot, Snapshot},
    market::{total_return, Market},
    types::*,
};
use serde::Serialize;

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
pub enum Variant {
    A,
    B,
    C,
    D,
}
impl Variant {
    pub fn columns(self) -> usize {
        match self {
            Self::A => 3,
            Self::B => 6,
            Self::C => 9,
            Self::D => 11,
        }
    }
}
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Features {
    pub at: Time,
    pub session_index: usize,
    pub values: Vec<f64>,
    pub observation_ids: Vec<String>,
    pub relationship_ids: Vec<String>,
    pub last_market_event: Time,
    pub past_volume: u64,
    pub snapshot: Snapshot,
}
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Label {
    pub entry: Time,
    pub exit: Time,
    pub available_at: Time,
    pub relative_total_return: f64,
}
fn price_features(bars: &[&Bar]) -> Option<(f64, f64)> {
    if bars.len() < 2 {
        return None;
    }
    let window = &bars[bars.len().saturating_sub(31)..];
    let returns: Vec<_> = window
        .windows(2)
        .map(|p| p[1].close as f64 / p[0].close as f64 - 1.0)
        .collect();
    let mean = returns.iter().sum::<f64>() / returns.len() as f64;
    let vol =
        (returns.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / returns.len() as f64).sqrt();
    Some((
        window.last()?.close as f64 / window.first()?.close as f64 - 1.0,
        vol,
    ))
}
fn metric_values(
    view: &Snapshot,
    entity: &str,
    metric: &str,
    mode: EvidenceMode,
) -> Result<(Vec<f64>, Option<String>)> {
    let Some(f) = latest_metric(view, entity, metric)? else {
        return Ok((vec![0.0, 0.0, 1.0], None));
    };
    let value = f.value_micros as f64 / MONEY as f64 * 10f64.powi(f.key.scale);
    let ready = f
        .availability
        .ready(mode)
        .ok_or("selected unavailable observation")?;
    Ok((
        vec![value, (view.as_of - ready).max(0) as f64 / DAY as f64, 0.0],
        Some(f.id.clone()),
    ))
}
pub fn features(
    data: &Dataset,
    c: &Config,
    market: &Market<'_>,
    index: usize,
) -> Result<Option<Features>> {
    let at = data.sessions[index].decision;
    let visible = market.observed(&c.symbol, at, c.observation_delay_ms);
    let bench = market.observed(&c.benchmark, at, c.observation_delay_ms);
    let Some(last) = visible.last() else {
        return Ok(None);
    };
    if at - last.end > c.max_quote_age_ms + c.observation_delay_ms {
        return Ok(None);
    }
    // A raw-price window spanning a split is not silently treated as a return signal.
    let oldest = visible[visible.len().saturating_sub(31)].start;
    if data.corporate_actions.iter().any(|a| {
        a.symbol == c.symbol
            && a.effective_at > oldest
            && a.effective_at <= at
            && a.split_numerator != a.split_denominator
    }) {
        return Ok(None);
    }
    let (Some((momentum, vol)), Some((benchmark_momentum, _))) =
        (price_features(&visible), price_features(&bench))
    else {
        return Ok(None);
    };
    let view = snapshot(data, at, c.mode)?;
    let mut values = vec![momentum, vol, benchmark_momentum];
    let mut ids = vec![];
    for (entity, metric) in [
        (&c.symbol, &c.issuer_metric),
        (&c.industry_entity, &c.industry_metric),
    ] {
        let (v, id) = metric_values(&view, entity, metric, c.mode)?;
        values.extend(v);
        if let Some(id) = id {
            ids.push(id);
        }
    }
    let mut network = 0.0;
    let mut weight = 0.0;
    let mut edges = vec![];
    for e in &view.relationships {
        if e.to != c.symbol || e.relation != "supplies" {
            continue;
        }
        if let Some(f) = latest_metric(&view, &e.from, &c.industry_metric)? {
            network += (f.value_micros as f64 / MONEY as f64 * 10f64.powi(f.key.scale))
                * e.weight_ppm as f64;
            weight += e.weight_ppm as f64;
            ids.push(f.id.clone());
            edges.push(e.id.clone());
        }
    }
    values.extend([
        if weight > 0.0 { network / weight } else { 0.0 },
        if weight > 0.0 { 0.0 } else { 1.0 },
    ]);
    ids.sort();
    ids.dedup();
    edges.sort();
    edges.dedup();
    Ok(Some(Features {
        at,
        session_index: index,
        values,
        observation_ids: ids,
        relationship_ids: edges,
        last_market_event: last.end,
        past_volume: last.volume,
        snapshot: view,
    }))
}
pub fn label(
    data: &Dataset,
    c: &Config,
    market: &Market<'_>,
    index: usize,
) -> Result<Option<Label>> {
    let Some(exit_session) = data.sessions.get(index + c.horizon_sessions) else {
        return Ok(None);
    };
    let enter_time = data.sessions[index].decision + c.order_latency_ms;
    let exit_time = exit_session.decision + c.order_latency_ms;
    let (Some(a), Some(b), Some(ba), Some(bb)) = (
        market.fill_reference(&c.symbol, enter_time),
        market.fill_reference(&c.symbol, exit_time),
        market.fill_reference(&c.benchmark, enter_time),
        market.fill_reference(&c.benchmark, exit_time),
    ) else {
        return Ok(None);
    };
    if a.start != ba.start || b.start != bb.start {
        return Err("unsynchronized target references".into());
    }
    let value = total_return(data, &c.symbol, a, b)? - total_return(data, &c.benchmark, ba, bb)?;
    let available_at = b
        .available_at
        .max(bb.available_at)
        .checked_add(c.observation_delay_ms)
        .ok_or("label availability overflow")?;
    Ok(Some(Label {
        entry: a.start,
        exit: b.start,
        available_at,
        relative_total_return: value,
    }))
}
pub fn training_indices(
    rows: &[Option<Features>],
    labels: &[Option<Label>],
    index: usize,
    c: &Config,
    at: Time,
) -> Vec<usize> {
    let mut eligible: Vec<_> = (0..index)
        .filter(|&j| {
            rows[j].is_some()
                && labels[j]
                    .as_ref()
                    .is_some_and(|l| l.available_at <= at && l.exit < at)
        })
        .collect();
    if eligible.len() > c.train_window {
        eligible.drain(..eligible.len() - c.train_window);
    }
    eligible
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Interval {
    pub mean: f64,
    pub low: f64,
    pub high: f64,
    pub blocks: usize,
    pub observations: usize,
}
pub fn paired_block_interval(
    gains: &[f64],
    block: usize,
    repeats: usize,
    seed: u64,
) -> Option<Interval> {
    if block == 0 || gains.len() < block * 2 || repeats < 10 || gains.iter().any(|x| !x.is_finite())
    {
        return None;
    }
    let mut state = seed.max(1);
    let mut samples = Vec::with_capacity(repeats);
    for _ in 0..repeats {
        let mut sum = 0.0;
        let mut count = 0;
        while count < gains.len() {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            let start = state as usize % (gains.len() - block + 1);
            for x in &gains[start..start + block] {
                if count == gains.len() {
                    break;
                }
                sum += x;
                count += 1;
            }
        }
        samples.push(sum / gains.len() as f64);
    }
    samples.sort_by(f64::total_cmp);
    Some(Interval {
        mean: gains.iter().sum::<f64>() / gains.len() as f64,
        low: samples[repeats * 25 / 1000],
        high: samples[(repeats * 975 / 1000).min(repeats - 1)],
        blocks: gains.len() / block,
        observations: gains.len(),
    })
}
