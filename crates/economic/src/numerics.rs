use crate::types::Result;
use serde::{Deserialize, Serialize};
#[cxx::bridge(namespace = "hindsight")]
mod ffi {
    unsafe extern "C++" {
        include!("ridge.hpp");
        fn ridge_solve(x: &[f64], y: &[f64], columns: usize, penalty: f64) -> Result<Vec<f64>>;
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Model {
    pub means: Vec<f64>,
    pub scales: Vec<f64>,
    pub weights: Vec<f64>,
}
impl Model {
    pub fn fit(x: &[Vec<f64>], y: &[f64], penalty: f64) -> Result<Self> {
        let p = x.first().ok_or("empty training set")?.len();
        if p == 0
            || p > 63
            || x.len() != y.len()
            || x.len() < 2
            || x.iter()
                .any(|r| r.len() != p || r.iter().any(|v| !v.is_finite()))
            || y.iter().any(|v| !v.is_finite())
        {
            return Err("invalid training matrix".into());
        }
        let n = x.len() as f64;
        let means: Vec<f64> = (0..p)
            .map(|j| x.iter().map(|r| r[j]).sum::<f64>() / n)
            .collect();
        let scales: Vec<f64> = (0..p)
            .map(|j| {
                (x.iter().map(|r| (r[j] - means[j]).powi(2)).sum::<f64>() / n)
                    .sqrt()
                    .max(1e-12)
            })
            .collect();
        let mut matrix = Vec::with_capacity(x.len() * (p + 1));
        for row in x {
            matrix.push(1.0);
            for j in 0..p {
                matrix.push((row[j] - means[j]) / scales[j]);
            }
        }
        let weights = ffi::ridge_solve(&matrix, y, p + 1, penalty).map_err(|e| e.to_string())?;
        Ok(Self {
            means,
            scales,
            weights,
        })
    }
    pub fn predict(&self, row: &[f64]) -> Result<f64> {
        if row.len() != self.means.len()
            || self.weights.len() != row.len() + 1
            || self.scales.len() != row.len()
            || row.iter().any(|v| !v.is_finite())
        {
            return Err("prediction dimensions/value".into());
        }
        let prediction = self.weights[0]
            + row
                .iter()
                .enumerate()
                .map(|(j, v)| (v - self.means[j]) / self.scales[j] * self.weights[j + 1])
                .sum::<f64>();
        if !prediction.is_finite() {
            return Err("nonfinite prediction".into());
        }
        Ok(prediction)
    }
}
