//! Bounded research calculations; descriptive association is not causality.
use crate::model::*;
use serde::{Deserialize, Serialize};

pub fn pearson(x: &[f64], y: &[f64]) -> Result<f64> {
    if x.len() != y.len() || x.len() < 3 || x.iter().chain(y).any(|v| !v.is_finite()) {
        return Err(invalid(
            "Correlation requires at least three finite paired observations",
        ));
    }
    let sx = x.iter().fold(0.0f64, |m, v| m.max(v.abs()));
    let sy = y.iter().fold(0.0f64, |m, v| m.max(v.abs()));
    if sx == 0.0 || sy == 0.0 {
        return Err(invalid("Correlation undefined for constant data"));
    }
    let n = x.len() as f64;
    let mx = x.iter().map(|v| v / sx / n).sum::<f64>();
    let my = y.iter().map(|v| v / sy / n).sum::<f64>();
    let (mut xx, mut yy, mut xy) = (0.0, 0.0, 0.0);
    for (&a, &b) in x.iter().zip(y) {
        let ax = a / sx - mx;
        let by = b / sy - my;
        xx += ax * ax;
        yy += by * by;
        xy += ax * by;
    }
    if xx <= 0.0 || yy <= 0.0 {
        return Err(invalid("Correlation undefined for constant data"));
    }
    let correlation = xy / xx.sqrt() / yy.sqrt();
    if !correlation.is_finite() {
        return Err(invalid("Correlation scale is numerically unsupported"));
    }
    Ok(correlation.clamp(-1.0, 1.0))
}
pub fn ranks(x: &[f64]) -> Result<Vec<f64>> {
    if x.iter().any(|v| !v.is_finite()) {
        return Err(invalid("Ranks require finite values"));
    }
    let mut order: Vec<usize> = (0..x.len()).collect();
    order.sort_by(|&a, &b| x[a].total_cmp(&x[b]));
    let mut out = vec![0.0; x.len()];
    let mut i = 0;
    while i < order.len() {
        let mut j = i + 1;
        while j < order.len() && x[order[j]] == x[order[i]] {
            j += 1;
        }
        let rank = ((i + 1 + j) as f64) / 2.0;
        for k in i..j {
            out[order[k]] = rank;
        }
        i = j;
    }
    Ok(out)
}
pub fn spearman(x: &[f64], y: &[f64]) -> Result<f64> {
    pearson(&ranks(x)?, &ranks(y)?)
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PairInput {
    pub x: Vec<f64>,
    pub y: Vec<f64>,
}

pub mod covariance;
pub mod dependence;
pub mod state_space;
