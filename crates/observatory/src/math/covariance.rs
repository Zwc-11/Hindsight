use crate::model::*;
use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct Covariance {
    pub shrinkage: f64,
    pub covariance: Vec<Vec<f64>>,
    pub partial: Vec<Vec<Option<f64>>>,
    pub means: Vec<f64>,
    pub samples: usize,
    pub convention: String,
}
/// Complete-case empirical covariance with the Ledoit-Wolf spherical-target coefficient.
pub fn ledoit_wolf(rows: &[Vec<f64>]) -> Result<Covariance> {
    let n = rows.len();
    let p = rows.first().map_or(0, Vec::len);
    if n < 3
        || p == 0
        || p > 64
        || n > 100_000
        || rows
            .iter()
            .any(|r| r.len() != p || r.iter().any(|x| !x.is_finite()))
    {
        return Err(invalid(
            "Covariance requires 3..100000 complete finite rows and 1..64 columns",
        ));
    }
    let mut means = vec![0.0; p];
    for r in rows {
        for j in 0..p {
            means[j] += r[j] / n as f64;
        }
    }
    let mut s = vec![vec![0.0; p]; p];
    let mut fourth = 0.0;
    for r in rows {
        let z: Vec<_> = r.iter().zip(&means).map(|(x, m)| x - m).collect();
        let norm = z.iter().map(|x| x * x).sum::<f64>();
        fourth += norm * norm / n as f64;
        for i in 0..p {
            for j in 0..=i {
                s[i][j] += z[i] * z[j] / n as f64;
            }
        }
    }
    for i in 0..p {
        for j in 0..i {
            s[j][i] = s[i][j];
        }
    }
    let mu = (0..p).map(|i| s[i][i]).sum::<f64>() / p as f64;
    if !mu.is_finite() || mu <= 0.0 || !fourth.is_finite() {
        return Err(invalid("All-zero variance or covariance scale overflow"));
    }
    let norm_s = s.iter().flatten().map(|x| x * x).sum::<f64>();
    let delta = s
        .iter()
        .enumerate()
        .map(|(i, r)| {
            r.iter()
                .enumerate()
                .map(|(j, v)| (v - if i == j { mu } else { 0.0 }).powi(2))
                .sum::<f64>()
        })
        .sum::<f64>()
        / p as f64;
    let beta = ((fourth - norm_s) / (p * n) as f64).max(0.0).min(delta);
    let shrinkage = if delta > 0.0 {
        (beta / delta).clamp(0.0, 1.0)
    } else {
        0.0
    };
    let covariance: Vec<Vec<f64>> = s
        .iter()
        .enumerate()
        .map(|(i, r)| {
            r.iter()
                .enumerate()
                .map(|(j, x)| (1.0 - shrinkage) * x + if i == j { shrinkage * mu } else { 0.0 })
                .collect()
        })
        .collect();
    let l = cholesky(&covariance)?;
    let mut precision = vec![vec![0.0; p]; p];
    for j in 0..p {
        let mut e = vec![0.0; p];
        e[j] = 1.0;
        let x = solve_cholesky(&l, &e)?;
        for i in 0..p {
            precision[i][j] = x[i];
        }
    }
    let mut partial = vec![vec![None; p]; p];
    for i in 0..p {
        for j in 0..p {
            if s[i][i] > 0.0 && s[j][j] > 0.0 {
                partial[i][j] = Some(if i == j {
                    1.0
                } else {
                    (-precision[i][j] / (precision[i][i] * precision[j][j]).sqrt()).clamp(-1.0, 1.0)
                });
            }
        }
    }
    Ok(Covariance{shrinkage,covariance,partial,means,samples:n,convention:"complete-case; centered MLE covariance / n; Ledoit-Wolf estimated spherical target; partial association is not causal".into()})
}
pub fn cholesky(a: &[Vec<f64>]) -> Result<Vec<Vec<f64>>> {
    let n = a.len();
    if n == 0 || a.iter().any(|r| r.len() != n) {
        return Err(invalid("Expected a square matrix"));
    }
    let scale = (0..n).map(|i| a[i][i].abs()).fold(0.0, f64::max);
    let mut l = vec![vec![0.0; n]; n];
    for i in 0..n {
        for j in 0..=i {
            let v = a[i][j] - (0..j).map(|k| l[i][k] * l[j][k]).sum::<f64>();
            if !v.is_finite() {
                return Err(invalid("Matrix factorization overflow"));
            }
            if i == j {
                if v <= scale * 1e-14 {
                    return Err(invalid(
                        "Matrix is singular or numerically ill-conditioned; no hidden jitter added",
                    ));
                }
                l[i][j] = v.sqrt();
            } else {
                l[i][j] = v / l[j][j];
            }
        }
    }
    Ok(l)
}
pub fn solve_cholesky(l: &[Vec<f64>], b: &[f64]) -> Result<Vec<f64>> {
    let n = l.len();
    if b.len() != n || l.iter().any(|r| r.len() != n) {
        return Err(invalid("Solve dimensions mismatch"));
    }
    let mut y = vec![0.0; n];
    for i in 0..n {
        y[i] = (b[i] - (0..i).map(|j| l[i][j] * y[j]).sum::<f64>()) / l[i][i];
    }
    let mut x = vec![0.0; n];
    for i in (0..n).rev() {
        x[i] = (y[i] - ((i + 1)..n).map(|j| l[j][i] * x[j]).sum::<f64>()) / l[i][i];
    }
    if x.iter().any(|v| !v.is_finite()) {
        return Err(invalid("Nonfinite solution"));
    }
    Ok(x)
}
