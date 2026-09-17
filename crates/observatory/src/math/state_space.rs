//! Release-time filtering of a scalar dynamic factor with lagged aggregate observations.
//! This is a small explicit state-space model, not a reproduction of a general EM nowcaster.
use crate::model::*;
use serde::{Deserialize, Serialize};
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AggregateRelease {
    pub released_at_step: usize,
    pub period_start_step: usize,
    pub period_end_step: usize,
    pub value: f64,
    pub aggregation: String,
    pub noise_variance: f64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct FilterInput {
    pub phi: f64,
    pub process_variance: f64,
    pub initial_variance: f64,
    pub memory: usize,
    pub steps: usize,
    pub releases: Vec<AggregateRelease>,
}
#[derive(Debug, Deserialize, Serialize)]
pub struct FilterPoint {
    pub step: usize,
    pub mean: f64,
    pub variance: f64,
    pub observations_used: usize,
}
pub fn filter(input: &FilterInput) -> Result<Vec<FilterPoint>> {
    let d = input.memory;
    if d == 0
        || d > 256
        || input.steps == 0
        || input.steps > 10_000
        || !input.phi.is_finite()
        || input.phi.abs() >= 1.0
        || !input.process_variance.is_finite()
        || input.process_variance <= 0.0
        || !input.initial_variance.is_finite()
        || input.initial_variance <= 0.0
    {
        return Err(invalid("Invalid bounded stable state-space configuration"));
    }
    for r in &input.releases {
        if !r.value.is_finite()
            || !r.noise_variance.is_finite()
            || r.noise_variance <= 0.0
            || r.period_start_step > r.period_end_step
            || r.period_end_step > r.released_at_step
            || r.released_at_step >= input.steps
            || r.released_at_step - r.period_start_step >= d
            || !["sum", "mean", "last"].contains(&r.aggregation.as_str())
        {
            return Err(invalid(
                "Release timing, aggregation interval, noise or lag-memory is invalid",
            ));
        }
    }
    let mut schedule = input.releases.clone();
    schedule.sort_by_key(|r| (r.released_at_step, r.period_start_step, r.period_end_step));
    let mut m = vec![0.0; d];
    let mut p = vec![vec![0.0; d]; d];
    for (i, row) in p.iter_mut().enumerate() {
        for (j, v) in row.iter_mut().enumerate() {
            *v = input.initial_variance * input.phi.powi(i.abs_diff(j) as i32);
        }
    }
    let mut out = vec![];
    let mut release_index = 0;
    for step in 0..input.steps {
        if step > 0 {
            let mut prior = vec![vec![0.0; d]; d];
            prior[0][0] = input.phi * input.phi * p[0][0] + input.process_variance;
            for i in 1..d {
                prior[0][i] = input.phi * p[0][i - 1];
                prior[i][0] = prior[0][i];
                for j in 1..d {
                    prior[i][j] = p[i - 1][j - 1];
                }
            }
            for i in (1..d).rev() {
                m[i] = m[i - 1];
            }
            m[0] *= input.phi;
            p = prior;
        }
        let mut used = 0;
        while release_index < schedule.len() && schedule[release_index].released_at_step == step {
            let r = &schedule[release_index];
            let mut h = vec![0.0; d];
            if r.aggregation == "last" {
                h[step - r.period_end_step] = 1.0;
            } else {
                let scale = if r.aggregation == "mean" {
                    1.0 / (r.period_end_step - r.period_start_step + 1) as f64
                } else {
                    1.0
                };
                for period in r.period_start_step..=r.period_end_step {
                    h[step - period] = scale;
                }
            }
            let ph: Vec<f64> = p
                .iter()
                .map(|row| row.iter().zip(&h).map(|(x, h)| x * h).sum())
                .collect();
            let variance = h.iter().zip(&ph).map(|(h, v)| h * v).sum::<f64>() + r.noise_variance;
            if !variance.is_finite() || variance <= 0.0 {
                return Err(invalid("Invalid innovation covariance"));
            }
            let innovation = r.value - m.iter().zip(&h).map(|(v, h)| v * h).sum::<f64>();
            let gain: Vec<_> = ph.iter().map(|v| v / variance).collect();
            for i in 0..d {
                m[i] += gain[i] * innovation;
            }
            // Joseph-form rank update, including measurement noise; no future smoother.
            for i in 0..d {
                for j in 0..=i {
                    let updated =
                        p[i][j] - gain[i] * ph[j] - ph[i] * gain[j] + gain[i] * gain[j] * variance;
                    if !updated.is_finite() {
                        return Err(invalid("Filter covariance became nonfinite"));
                    }
                    p[i][j] = updated;
                    p[j][i] = updated;
                }
            }
            used += 1;
            release_index += 1;
        }
        if !m[0].is_finite() || p[0][0] < -1e-10 {
            return Err(invalid("Unstable filtered state"));
        }
        out.push(FilterPoint {
            step,
            mean: m[0],
            variance: p[0][0].max(0.0),
            observations_used: used,
        });
    }
    Ok(out)
}
