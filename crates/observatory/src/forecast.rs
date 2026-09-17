//! Explicitly bounded forecasts and filtered-state experiments. No inferred release timestamps.
use crate::{
    analysis::AnalysisRequest,
    math::{
        self,
        state_space::{AggregateRelease, FilterInput},
    },
    model::*,
};
use hindsight_economic::numerics::Model;
use serde_json::{json, Value};
use std::collections::BTreeMap;

pub fn crossfit_residual_correlation(x: &[f64], y: &[f64], controls: &[Vec<f64>]) -> Result<Value> {
    let n = x.len();
    let split = n / 2;
    if n != y.len() || controls.len() != n || split < 8 {
        return Ok(
            json!({"state":"insufficient_data","reason":"At least 16 matched observations required for chronological residualization"}),
        );
    }
    let a = Model::fit(&controls[..split], &x[..split], 1.0).map_err(invalid)?;
    let b = Model::fit(&controls[..split], &y[..split], 1.0).map_err(invalid)?;
    let mut rx = Vec::new();
    let mut ry = Vec::new();
    for i in split..n {
        rx.push(x[i] - a.predict(&controls[i]).map_err(invalid)?);
        ry.push(y[i] - b.predict(&controls[i]).map_err(invalid)?);
    }
    Ok(
        json!({"state":"completed","fit_rows":split,"evaluation_rows":n-split,"residual_correlation":math::pearson(&rx,&ry).ok(),"method":"chronological half-split; C++ ridge controls fitted on first half only; descriptive held-out residual association"}),
    )
}
fn readiness(data: &[Vec<Point>]) -> Option<Value> {
    let required = 30;
    let eligible: Vec<_> = data
        .iter()
        .map(|rows| {
            rows.iter()
                .filter(|p| {
                    p.reconstructed_at.is_some() && p.published_at.is_some() && p.value.is_some()
                })
                .count()
        })
        .collect();
    if eligible.iter().any(|n| *n < required) {
        Some(
            json!({"state":"insufficient_data","reason":"This release-time model requires at least 30 nonmissing, publication-supported observations per input. Current-vintage measurements with unknown release times cannot substitute.","eligible_counts":eligible,"required_per_series":required,"latest_vintage_is_not_a_backtest":true}),
        )
    } else {
        None
    }
}
pub fn forecast_from_sources(
    store: &Store,
    request: &AnalysisRequest,
    data: &[Vec<Point>],
    meta: &[Series],
) -> Result<Value> {
    if let Some(v) = readiness(data) {
        return Ok(v);
    }
    if request.series_ids.len() != 2 || !request.controls.is_empty() {
        return Err(invalid("Release forecast currently supports one candidate series and one target; no hidden control selection"));
    }
    if request.transform != "level" || request.max_lag != 0 {
        return Err(invalid(
            "Release forecast currently supports level inputs with zero lag; other transform/lag settings are not implemented",
        ));
    }
    if request.scope.mode != "reconstructed" {
        return Ok(
            json!({"state":"insufficient_data","reason":"Historical release forecasting requires verified reconstructed release clocks; present-day acquisition is not historical availability."}),
        );
    }
    // Calendar periods define the target family; each decision then re-queries eligible vintages.
    let mut periods: BTreeMap<String, &Point> = BTreeMap::new();
    for p in &data[1] {
        if p.value.is_some() {
            periods.insert(p.period_end.clone(), p);
        }
    }
    let ordered: Vec<_> = periods.values().copied().collect();
    let mut rows = Vec::new();
    let mut targets = Vec::new();
    let mut maturity = Vec::new();
    let mut dates = Vec::new();
    let mut provenance = Vec::new();
    for pair in ordered.windows(2) {
        let Some(at) = pair[0].reconstructed_at else {
            continue;
        };
        let scope = Scope {
            as_of: at,
            ..request.scope.clone()
        };
        let x = store.points(
            &request.series_ids[0],
            &scope,
            &request.start,
            &request.end,
            5000,
        )?;
        let y = store.points(
            &request.series_ids[1],
            &scope,
            &request.start,
            &request.end,
            5000,
        )?;
        let x = x
            .iter()
            .filter(|p| p.value.is_some())
            .max_by_key(|p| (&p.period_end, &p.period_start));
        let y = y
            .iter()
            .filter(|p| p.value.is_some())
            .max_by_key(|p| (&p.period_end, &p.period_start));
        let (Some(x), Some(y)) = (x, y) else { continue };
        let Some(target_time) = pair[1].reconstructed_at else {
            continue;
        };
        if target_time <= at {
            continue;
        }
        rows.push(vec![y.value.unwrap(), x.value.unwrap()]);
        targets.push(pair[1].value.unwrap() - y.value.unwrap());
        maturity.push(target_time);
        dates.push(at);
        provenance
            .push(json!({"x":x.id,"y":y.id,"target":pair[1].id,"target_available_at":target_time}));
    }
    let mut scored = Vec::new();
    let mut gain = Vec::new();
    for i in 20..rows.len() {
        let eligible: Vec<_> = (0..i).filter(|j| maturity[*j] < dates[i]).collect();
        if eligible.len() < 16 {
            continue;
        }
        let train: Vec<_> = eligible.iter().map(|j| rows[*j].clone()).collect();
        let base: Vec<_> = train.iter().map(|r| vec![r[0]]).collect();
        let labels: Vec<_> = eligible.iter().map(|j| targets[*j]).collect();
        let baseline = Model::fit(&base, &labels, 1.0).map_err(invalid)?;
        let candidate = Model::fit(&train, &labels, 1.0).map_err(invalid)?;
        let a = baseline.predict(&[rows[i][0]]).map_err(invalid)?;
        let b = candidate.predict(&rows[i]).map_err(invalid)?;
        let diff = (a - targets[i]).powi(2) - (b - targets[i]).powi(2);
        gain.push(diff);
        scored.push(json!({"decision_at":dates[i],"trained_through":eligible.iter().map(|j|maturity[*j]).max(),"training_count":eligible.len(),"target":targets[i],"baseline":a,"candidate":b,"loss_gain":diff,"evidence":provenance[i]}));
    }
    if scored.len() < 6 {
        return Ok(
            json!({"state":"insufficient_data","reason":"Fewer than six matured, chronologically scored release forecasts remain after vintage filtering.","eligible_scores":scored.len()}),
        );
    }
    Ok(
        json!({"state":"completed","kind":"release_forecast","series":meta,"scope":request.scope,"scored":scored,"paired_interval":hindsight_economic::research::paired_block_interval(&gain,3,1000,41),"limitations":["Forecasts next reporting-period changes using the explicitly captured target vintage and its maturity time, not security returns.","Retrospectively selected series; development evaluation, not pristine prospective holdout.","C++ ridge baseline and candidate use identical dates and penalty; no automatic winner selection."]}),
    )
}

pub fn nowcast_from_sources(
    data: &[Vec<Point>],
    meta: &[Series],
    request: &AnalysisRequest,
) -> Result<Value> {
    if let Some(v) = readiness(data) {
        return Ok(v);
    }
    if request.scope.mode != "reconstructed" {
        return Ok(
            json!({"state":"insufficient_data","reason":"Release-time filtering requires a verified original-publication timeline."}),
        );
    }
    // Explicit normalized monthly factor; annual/quarterly totals need user-reviewed measurement loadings.
    if meta.iter().any(|s| s.frequency != "monthly") {
        return Ok(
            json!({"state":"insufficient_data","reason":"Automatic real-data calibration only supports comparable monthly measurements. Annual/quarterly sums require reviewed aggregation/loadings; raw input API remains available to validated research tests."}),
        );
    }
    let dates: Vec<_> = data
        .iter()
        .flatten()
        .filter_map(|p| p.reconstructed_at)
        .collect();
    let first = *dates
        .iter()
        .min()
        .ok_or_else(|| invalid("No release times"))?;
    let last = *dates
        .iter()
        .max()
        .ok_or_else(|| invalid("No release times"))?;
    let epoch = first.div_euclid(86_400_000) - 95;
    let steps = (last.div_euclid(86_400_000) - epoch + 1) as usize;
    if steps > 10_000 {
        return Err(Error::Resource(
            "Nowcast history exceeds 10000-day bounded model; choose a shorter range".into(),
        ));
    }
    let mut releases = Vec::new();
    let mut training_cutoff = first;
    for rows in data {
        let mut sorted = rows
            .iter()
            .filter(|p| p.value.is_some() && p.reconstructed_at.is_some())
            .collect::<Vec<_>>();
        sorted.sort_by_key(|p| p.reconstructed_at);
        let training = &sorted[..12];
        let mean = training.iter().map(|p| p.value.unwrap()).sum::<f64>() / 12.0;
        let sd = (training
            .iter()
            .map(|p| (p.value.unwrap() - mean).powi(2))
            .sum::<f64>()
            / 12.0)
            .sqrt();
        if sd <= 1e-12 {
            return Err(invalid("Constant calibration sample"));
        }
        training_cutoff = training_cutoff.max(training.last().unwrap().reconstructed_at.unwrap());
        for p in &sorted[12..] {
            let start = chrono::NaiveDate::parse_from_str(&p.period_start, "%Y-%m-%d")
                .map_err(|_| invalid("Monthly period date invalid"))?
                .and_hms_opt(0, 0, 0)
                .unwrap()
                .and_utc()
                .timestamp_millis()
                / 86_400_000
                - epoch;
            let end = chrono::NaiveDate::parse_from_str(&p.period_end, "%Y-%m-%d")
                .map_err(|_| invalid("Monthly period date invalid"))?
                .and_hms_opt(0, 0, 0)
                .unwrap()
                .and_utc()
                .timestamp_millis()
                / 86_400_000
                - epoch;
            let release = p.reconstructed_at.unwrap() / 86_400_000 - epoch;
            if start < 0 || end > release || release - start >= 256 {
                continue;
            }
            // Mean loading is an explicit standardized monthly-indicator scenario, not a total-production estimator.
            releases.push(AggregateRelease {
                released_at_step: release as usize,
                period_start_step: start as usize,
                period_end_step: end as usize,
                value: (p.value.unwrap() - mean) / sd,
                aggregation: "mean".into(),
                noise_variance: 1.0,
            });
        }
    }
    releases.retain(|r| ((r.released_at_step as i64) + epoch) * 86_400_000 > training_cutoff);
    if releases.len() < 12 {
        return Ok(
            json!({"state":"insufficient_data","reason":"Insufficient post-calibration releases within supported lag memory."}),
        );
    }
    let config = FilterInput {
        phi: 0.98,
        process_variance: 0.05,
        initial_variance: 1.0,
        memory: 256,
        steps,
        releases,
    };
    let filtered = math::state_space::filter(&config)?;
    Ok(
        json!({"state":"completed","kind":"filtered_indicator_scenario","series":meta,"scope":request.scope,"epoch_day":epoch,"calibrated_through":training_cutoff,"configuration":config,"filtered":filtered.into_iter().filter(|p|((p.step as i64)+epoch)*86_400_000>training_cutoff).collect::<Vec<_>>(),"limitations":["Fixed explicitly declared AR and noise parameters; not maximum-likelihood dynamic-factor estimation.","Standardized monthly-indicator mean loading is a scenario, not units of daily production.","Decision-time states are filtered, never future-smoothed.","Comparable indicator selection and observation loadings require researcher review."]}),
    )
}
