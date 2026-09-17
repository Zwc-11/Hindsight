use super::{pearson, spearman};
use crate::model::*;
use serde::Serialize;

pub fn distance_correlation(x: &[f64], y: &[f64]) -> Result<f64> {
    let n = x.len();
    if n != y.len() || !(3..=2000).contains(&n) || x.iter().chain(y).any(|v| !v.is_finite()) {
        return Err(invalid(
            "Distance correlation requires 3..2000 finite paired points",
        ));
    }
    fn center(v: &[f64]) -> (Vec<f64>, Vec<f64>, f64) {
        let n = v.len();
        let mut m = vec![0.0; n * n];
        let mut rows = vec![0.0; n];
        for i in 0..n {
            for j in 0..n {
                let d = (v[i] - v[j]).abs();
                m[i * n + j] = d;
                rows[i] += d / n as f64;
            }
        }
        let grand = rows.iter().sum::<f64>() / n as f64;
        (m, rows, grand)
    }
    let (a, ar, ag) = center(x);
    let (b, br, bg) = center(y);
    let (mut ab, mut aa, mut bb) = (0.0, 0.0, 0.0);
    for i in 0..n {
        for j in 0..n {
            let ax = a[i * n + j] - ar[i] - ar[j] + ag;
            let by = b[i * n + j] - br[i] - br[j] + bg;
            ab += ax * by;
            aa += ax * ax;
            bb += by * by;
        }
    }
    if aa == 0.0 || bb == 0.0 {
        return Ok(0.0);
    }
    let value = (ab / (aa * bb).sqrt()).max(0.0).sqrt();
    if !value.is_finite() {
        return Err(invalid("Distance correlation overflow"));
    }
    Ok(value.clamp(0.0, 1.0))
}
/// Adjust the ENTIRE declared family. BY accounts for arbitrary p-value dependence,
/// but still requires valid marginal p-values.
pub fn adjust(p: &[f64], method: &str) -> Result<Vec<f64>> {
    if p.iter().any(|v| !v.is_finite() || !(0.0..=1.0).contains(v))
        || !["BH", "BY"].contains(&method)
    {
        return Err(invalid("Invalid p-values or adjustment method"));
    }
    let n = p.len();
    if n == 0 {
        return Ok(vec![]);
    }
    let harmonic = if method == "BY" {
        (1..=n).map(|i| 1.0 / i as f64).sum::<f64>()
    } else {
        1.0
    };
    let mut order: Vec<_> = (0..n).collect();
    order.sort_by(|&i, &j| p[i].total_cmp(&p[j]));
    let mut out = vec![0.0; n];
    let mut next: f64 = 1.0;
    for k in (0..n).rev() {
        next = next
            .min(p[order[k]] * n as f64 * harmonic / (k + 1) as f64)
            .min(1.0);
        out[order[k]] = next;
    }
    Ok(out)
}
#[derive(Debug, Serialize)]
pub struct Rolling {
    pub end: usize,
    pub n: usize,
    pub pearson: Option<f64>,
    pub spearman: Option<f64>,
}
pub fn rolling(x: &[f64], y: &[f64], window: usize) -> Result<Vec<Rolling>> {
    if x.len() != y.len() || window < 3 || window > x.len() {
        return Err(invalid("Invalid rolling window"));
    }
    Ok((window..=x.len())
        .map(|end| Rolling {
            end,
            n: window,
            pearson: pearson(&x[end - window..end], &y[end - window..end]).ok(),
            spearman: spearman(&x[end - window..end], &y[end - window..end]).ok(),
        })
        .collect())
}
pub fn ew_correlation(x: &[f64], y: &[f64], alpha: f64) -> Result<f64> {
    if x.len() != y.len()
        || x.len() < 3
        || !(0.0..1.0).contains(&alpha)
        || x.iter().chain(y).any(|x| !x.is_finite())
    {
        return Err(invalid("Invalid exponentially weighted correlation inputs"));
    }
    let n = x.len();
    let weights: Vec<_> = (0..n)
        .map(|i| (1.0 - alpha).powi((n - 1 - i) as i32))
        .collect();
    let total = weights.iter().sum::<f64>();
    let mx = x.iter().zip(&weights).map(|(v, w)| v * w).sum::<f64>() / total;
    let my = y.iter().zip(&weights).map(|(v, w)| v * w).sum::<f64>() / total;
    let (mut xx, mut yy, mut xy) = (0.0, 0.0, 0.0);
    for i in 0..n {
        xx += weights[i] * (x[i] - mx).powi(2);
        yy += weights[i] * (y[i] - my).powi(2);
        xy += weights[i] * (x[i] - mx) * (y[i] - my);
    }
    if xx == 0.0 || yy == 0.0 {
        return Err(invalid("Weighted correlation undefined for constant data"));
    }
    Ok((xy / (xx * yy).sqrt()).clamp(-1.0, 1.0))
}
#[derive(Debug, Serialize)]
pub struct ShiftNull {
    pub statistic: f64,
    pub p_value: f64,
    pub shifts: usize,
    pub method: String,
    pub limitations: String,
}
pub fn circular_shift_null(x: &[f64], y: &[f64], distance: bool) -> Result<ShiftNull> {
    let n = x.len();
    if n != y.len() || !(20..=400).contains(&n) {
        return Err(invalid(
            "Shift-null budget requires 20..400 equally spaced paired observations",
        ));
    }
    let statistic = if distance {
        distance_correlation(x, y)?
    } else {
        pearson(x, y)?.abs()
    };
    let mut exceed = 1;
    for shift in 1..n {
        let rotated: Vec<_> = (0..n).map(|i| y[(i + shift) % n]).collect();
        let value = if distance {
            distance_correlation(x, &rotated)?
        } else {
            pearson(x, &rotated)?.abs()
        };
        if value >= statistic - 1e-14 {
            exceed += 1;
        }
    }
    Ok(ShiftNull{statistic,p_value:exceed as f64/n as f64,shifts:n,method:"full cyclic-shift reference including identity".into(),limitations:"Exploratory for ordinary time series; exact only under a cyclic-shift-invariant null. Stationarity, irregular cadence, common factors and boundary effects require calibration. Not automatically a valid causal p-value.".into()})
}
