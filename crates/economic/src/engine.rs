use crate::{
    market::Market, numerics::Model, portfolio::Account, research::*, types::*,
    validation::validate,
};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub fn digest<T: Serialize>(value: &T) -> Result<String> {
    let bytes = serde_json::to_vec(value).map_err(|e| e.to_string())?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Decision {
    pub at: Time,
    pub status: String,
    pub prediction: Option<f64>,
    pub target_shares: i64,
    pub training_count: usize,
    pub trained_through: Option<Time>,
    pub training_indices: Vec<usize>,
    pub model_hash: Option<String>,
    pub model: Option<Model>,
    pub features: Option<Features>,
}
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Score {
    pub at: Time,
    pub target: f64,
    pub prediction: f64,
    pub squared_error: f64,
}
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct Valuation {
    pub at: Time,
    pub equity_micros: i64,
    pub position: i64,
    pub mark_micros: i64,
}
#[derive(Clone, Debug, Serialize)]
pub struct Trial {
    pub variant: Variant,
    pub decisions: Vec<Decision>,
    pub scores: Vec<Score>,
    pub account: Account,
    pub equity: Vec<Valuation>,
    pub mse: Option<f64>,
    pub net_return: Option<f64>,
    pub max_drawdown: f64,
    pub mean_gross_exposure: f64,
    pub turnover: f64,
}
#[derive(Clone, Debug, Serialize)]
pub struct Coverage {
    pub sessions: usize,
    pub bars_by_symbol: BTreeMap<String, usize>,
    pub missing_minutes: BTreeMap<String, usize>,
    pub sources: usize,
    pub independent_release_dates: usize,
    pub facts: usize,
    pub observed_ready_facts: usize,
    pub reconstructed_ready_facts: usize,
    pub relationships: usize,
    pub timestamp_precisions: BTreeMap<String, usize>,
    pub real_data_claim: bool,
}
#[derive(Clone, Debug, Serialize)]
pub struct Run {
    pub schema_version: u32,
    pub engine: String,
    pub synthetic: bool,
    pub label: String,
    pub selection_disclosure: String,
    pub config: Config,
    pub input_hash: String,
    pub semantic_hash: String,
    pub coverage: Coverage,
    pub trials: Vec<Trial>,
    pub paired_loss_gain: BTreeMap<String, Option<Interval>>,
    pub buy_hold_reference_total_return: Option<f64>,
    pub cash_reference_total_return: f64,
    pub limitations: Vec<String>,
}
pub fn coverage(input: &Input) -> Result<Coverage> {
    let d = &input.dataset;
    let mut bars_by_symbol = BTreeMap::new();
    let mut unique = BTreeMap::<String, std::collections::BTreeSet<Time>>::new();
    for b in &d.bars {
        unique.entry(b.symbol.clone()).or_default().insert(b.start);
    }
    let expected = d
        .sessions
        .iter()
        .map(|s| ((s.close - s.open) / MINUTE) as usize)
        .sum::<usize>();
    let mut missing_minutes = BTreeMap::new();
    for (s, bars) in unique {
        bars_by_symbol.insert(s.clone(), bars.len());
        missing_minutes.insert(s, expected.saturating_sub(bars.len()));
    }
    let mut timestamp_precisions = BTreeMap::new();
    for f in &d.facts {
        *timestamp_precisions
            .entry(f.availability.timestamp_precision.clone())
            .or_insert(0) += 1;
    }
    let dates: std::collections::BTreeSet<_> = d
        .facts
        .iter()
        .map(|f| f.availability.published_at / DAY)
        .collect();
    Ok(Coverage {
        sessions: d.sessions.len(),
        bars_by_symbol,
        missing_minutes,
        sources: d.sources.len(),
        independent_release_dates: dates.len(),
        facts: d.facts.len(),
        observed_ready_facts: d
            .facts
            .iter()
            .filter(|f| f.availability.ready(EvidenceMode::Observed).is_some())
            .count(),
        reconstructed_ready_facts: d
            .facts
            .iter()
            .filter(|f| f.availability.ready(EvidenceMode::Reconstructed).is_some())
            .count(),
        relationships: d.relationships.len(),
        timestamp_precisions,
        real_data_claim: !d.synthetic,
    })
}
pub fn run(input: &Input) -> Result<Run> {
    validate(input)?;
    let d = &input.dataset;
    let c = &input.config;
    let market = Market::new(d);
    let rows: Vec<_> = (0..d.sessions.len())
        .map(|i| features(d, c, &market, i))
        .collect::<Result<_>>()?;
    let labels: Vec<_> = (0..d.sessions.len())
        .map(|i| label(d, c, &market, i))
        .collect::<Result<_>>()?;
    let variants = if d.relationships.is_empty() {
        vec![Variant::A, Variant::B, Variant::C]
    } else {
        vec![Variant::A, Variant::B, Variant::C, Variant::D]
    };
    let mut trials = vec![];
    for variant in variants {
        let columns = variant.columns();
        let mut account = Account::new(c.initial_cash_micros);
        let mut decisions = vec![];
        let mut scores = vec![];
        let mut equity = vec![];
        for (i, session) in d.sessions.iter().enumerate() {
            account.apply_actions(d, &c.symbol, session.decision)?;
            let mut decision = Decision {
                at: session.decision,
                status: "warmup".into(),
                prediction: None,
                target_shares: account.shares,
                training_count: 0,
                trained_through: None,
                training_indices: vec![],
                model_hash: None,
                model: None,
                features: rows[i].clone(),
            };
            if let Some(row) = &rows[i] {
                let indices = training_indices(&rows, &labels, i, c, session.decision);
                decision.training_count = indices.len();
                if indices.len() >= c.min_train {
                    let x: Vec<_> = indices
                        .iter()
                        .map(|&j| rows[j].as_ref().unwrap().values[..columns].to_vec())
                        .collect();
                    let y: Vec<_> = indices
                        .iter()
                        .map(|&j| labels[j].as_ref().unwrap().relative_total_return)
                        .collect();
                    let model = Model::fit(&x, &y, c.ridge)?;
                    let prediction = model.predict(&row.values[..columns])?;
                    // Explicit dead-band avoids unstable near-threshold decisions.
                    let target = if prediction > c.signal_threshold + 1e-10 {
                        c.max_shares
                    } else {
                        0
                    };
                    decision.status = "predicted".into();
                    decision.prediction = Some(prediction);
                    decision.target_shares = target;
                    decision.trained_through = indices
                        .iter()
                        .map(|&j| labels[j].as_ref().unwrap().available_at)
                        .max();
                    decision.model_hash = Some(digest(&(&indices, &x, &y, &model))?);
                    decision.training_indices = indices;
                    decision.model = Some(model);
                    account.rebalance(target, session.decision, i as u64 + 1, c, &market)?;
                    // Labels only enter the ex-post score, never the decision record or portfolio mapping.
                    if let Some(outcome) = &labels[i] {
                        scores.push(Score {
                            at: session.decision,
                            target: outcome.relative_total_return,
                            prediction,
                            squared_error: (prediction - outcome.relative_total_return).powi(2),
                        });
                    }
                } else {
                    account.event(
                        i as u64 + 1,
                        session.decision,
                        "no_order",
                        0,
                        0,
                        0,
                        "insufficient completed training targets",
                    );
                }
            } else {
                decision.status = "missing_market_history".into();
                account.event(
                    i as u64 + 1,
                    session.decision,
                    "no_order",
                    0,
                    0,
                    0,
                    "missing or stale market features",
                );
            }
            decisions.push(decision);
            account.apply_actions(d, &c.symbol, session.close)?;
            // Ex-post closing valuation is kept separate from the strategy's observation state.
            if let Some(mark) = market.exact(&c.symbol, session.close - MINUTE) {
                equity.push(Valuation {
                    at: session.close,
                    equity_micros: account.equity(mark.close)?,
                    position: account.shares,
                    mark_micros: mark.close,
                });
            }
        }
        let mse = if scores.is_empty() {
            None
        } else {
            Some(scores.iter().map(|s| s.squared_error).sum::<f64>() / scores.len() as f64)
        };
        let net_return = equity
            .last()
            .map(|v| v.equity_micros as f64 / c.initial_cash_micros as f64 - 1.0);
        let mut peak = c.initial_cash_micros as f64;
        let mut max_drawdown: f64 = 0.0;
        for v in &equity {
            peak = peak.max(v.equity_micros as f64);
            max_drawdown = max_drawdown.max(1.0 - v.equity_micros as f64 / peak);
        }
        let mean_gross_exposure = if equity.is_empty() {
            0.0
        } else {
            equity
                .iter()
                .map(|v| v.position as f64 * v.mark_micros as f64 / v.equity_micros as f64)
                .sum::<f64>()
                / equity.len() as f64
        };
        let turnover = account.traded_notional_micros as f64 / c.initial_cash_micros as f64;
        trials.push(Trial {
            variant,
            decisions,
            scores,
            account,
            equity,
            mse,
            net_return,
            max_drawdown,
            mean_gross_exposure,
            turnover,
        });
    }
    let mut paired_loss_gain = BTreeMap::new();
    let base: BTreeMap<_, _> = trials[0]
        .scores
        .iter()
        .map(|s| (s.at, s.squared_error))
        .collect();
    for trial in &trials[1..] {
        let gains: Vec<_> = trial
            .scores
            .iter()
            .filter_map(|s| base.get(&s.at).map(|loss| loss - s.squared_error))
            .collect();
        paired_loss_gain.insert(
            format!("A_vs_{:?}", trial.variant),
            paired_block_interval(
                &gains,
                c.horizon_sessions.max(2),
                c.bootstrap_repetitions,
                c.bootstrap_seed,
            ),
        );
    }
    let input_hash = digest(input)?;
    let semantic_hash = digest(
        &trials
            .iter()
            .map(|t| (&t.variant, &t.decisions, &t.account.events))
            .collect::<Vec<_>>(),
    )?;
    let first = trials[0]
        .decisions
        .iter()
        .find(|r| r.prediction.is_some())
        .and_then(|r| market.fill_reference(&c.symbol, r.at + c.order_latency_ms));
    let last = d
        .sessions
        .last()
        .and_then(|s| market.exact(&c.symbol, s.close - MINUTE));
    let buy_hold_reference_total_return = match (first, last) {
        (Some(a), Some(b)) => {
            let mut close = b.clone();
            close.open = b.close;
            close.start = b.end;
            Some(crate::market::total_return(d, &c.symbol, a, &close)?)
        }
        _ => None,
    };
    Ok(Run {schema_version:1,engine:"hindsight-economic rust/cxx".into(),synthetic:d.synthetic,label:d.label.clone(),selection_disclosure:d.selection_disclosure.clone(),config:c.clone(),input_hash,semantic_hash,coverage:coverage(input)?,trials,paired_loss_gain,buy_hold_reference_total_return,cash_reference_total_return:0.0,limitations:vec![
        "Synthetic demonstrations establish software behavior, not an investment advantage.".into(),
        "Opening-price execution reference plus modeled half-spread, impact and fees; not observed executable quotes or queue simulation.".into(),
        "Capacity is fixed from prior completed volume; no market-impact feedback.".into(),
        "Daily decisions at 10:00 New York use a supplied, versioned session calendar; completeness needs external coverage verification.".into(),
        "Consolidated fact selection requires declared series, units and reporting periods; source conflicts fail closed.".into(),
        "D uses disclosed supplies edges only and is omitted if no edges exist; graph construction is not causal proof.".into(),
        "Portfolio is long/cash with integer shares; outstanding positions are marked, not forcibly liquidated. Buy-and-hold is an uncosted price reference, not a matched executed portfolio.".into(),
        "Trusted-code visibility contracts, not a sandbox for malicious strategy code.".into(),
        "Walk-forward development evaluation is not a prospectively untouched holdout; overlapping targets use paired time-block uncertainty.".into(),
    ]})
}
