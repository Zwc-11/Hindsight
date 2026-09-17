use crate::types::*;
use std::collections::BTreeMap;
pub struct Market<'a> {
    by_symbol: BTreeMap<&'a str, Vec<&'a Bar>>,
    sessions: &'a [Session],
}
impl<'a> Market<'a> {
    pub fn new(d: &'a Dataset) -> Self {
        let mut by_symbol: BTreeMap<&str, Vec<&Bar>> = BTreeMap::new();
        for b in &d.bars {
            by_symbol.entry(&b.symbol).or_default().push(b);
        }
        for bars in by_symbol.values_mut() {
            bars.sort_by_key(|b| b.start);
            bars.dedup_by_key(|b| b.start);
        }
        Self {
            by_symbol,
            sessions: &d.sessions,
        }
    }
    pub fn observed(&self, symbol: &str, at: Time, delay: i64) -> Vec<&'a Bar> {
        self.by_symbol
            .get(symbol)
            .into_iter()
            .flat_map(|v| v.iter().copied())
            .filter(|b| {
                b.end <= at
                    && b.available_at
                        .checked_add(delay)
                        .is_some_and(|ready| ready <= at)
            })
            .collect()
    }
    pub fn exact(&self, symbol: &str, at: Time) -> Option<&'a Bar> {
        let bars = self.by_symbol.get(symbol)?;
        let idx = bars.binary_search_by_key(&at, |b| b.start).ok()?;
        Some(bars[idx])
    }
    pub fn eligible_boundary(&self, arrival: Time) -> Option<Time> {
        let next = arrival
            .div_euclid(MINUTE)
            .checked_add(1)?
            .checked_mul(MINUTE)?;
        for s in self.sessions {
            if next < s.close {
                return Some(next.max(s.open));
            }
        }
        None
    }
    pub fn fill_reference(&self, symbol: &str, arrival: Time) -> Option<&'a Bar> {
        self.exact(symbol, self.eligible_boundary(arrival)?)
    }
}

/// Ex-post, non-reinvested cash dividends plus splits. Never supplied as a feature.
pub fn total_return(d: &Dataset, symbol: &str, entry: &Bar, exit: &Bar) -> Result<f64> {
    let mut shares = 1.0;
    let mut cash = 0.0;
    let mut actions: Vec<_> = d
        .corporate_actions
        .iter()
        .filter(|a| {
            a.symbol == symbol && a.effective_at > entry.start && a.effective_at <= exit.start
        })
        .collect();
    actions.sort_by_key(|a| (a.effective_at, &a.id));
    for a in actions {
        cash += shares * a.dividend_per_share_micros as f64;
        shares *= a.split_numerator as f64 / a.split_denominator as f64;
    }
    let value = (shares * exit.open as f64 + cash) / entry.open as f64 - 1.0;
    if !value.is_finite() {
        return Err("nonfinite total return".into());
    }
    Ok(value)
}
