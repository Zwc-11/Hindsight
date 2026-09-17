use crate::{
    demo,
    engine::{self, Run},
    evidence, ingest, output,
    types::*,
};
use serde::Serialize;
use serde_json::{json, Value};
use std::path::Path;
#[derive(Serialize)]
pub struct Case {
    pub name: String,
    pub passed: bool,
    pub evidence: String,
}
#[derive(Serialize)]
pub struct Audit {
    pub all_passed: bool,
    pub cases: Vec<Case>,
    pub scope: String,
}
pub fn signature(run: &Run, cutoff: Time) -> Value {
    json!(run.trials.iter().map(|trial|json!({"variant":trial.variant,"decisions":trial.decisions.iter().filter(|d|d.at<=cutoff).map(|d|json!({"at":d.at,"prediction":d.prediction,"target":d.target_shares,"trained_through":d.trained_through,"training_indices":d.training_indices,"model_hash":d.model_hash,"values":d.features.as_ref().map(|f|&f.values),"observations":d.features.as_ref().map(|f|&f.observation_ids),"relationships":d.features.as_ref().map(|f|&f.relationship_ids)})).collect::<Vec<_>>(),"events":trial.account.events.iter().filter(|e|e.at<=cutoff).collect::<Vec<_>>()})).collect::<Vec<_>>())
}
pub fn falsify() -> Result<Audit> {
    let input = demo::input()?;
    let cutoff = input.dataset.sessions[40].decision;
    let base = engine::run(&input)?;
    let original = signature(&base, cutoff);
    let mut cases = vec![];
    let mut check = |name: &str, changed: Input| -> Result<()> {
        let result = engine::run(&changed)?;
        cases.push(Case{name:name.into(),passed:signature(&result,cutoff)==original,evidence:"Full feature construction, past-only scaling, C++ fitting, decisions and account replay rebuilt; compare past semantic state, not global file hash".into()});
        Ok(())
    };
    let mut future = input.clone();
    for b in &mut future.dataset.bars {
        if b.start > cutoff {
            b.open += 7 * MONEY;
            b.close += 7 * MONEY;
            b.high += 7 * MONEY;
            b.low += 7 * MONEY;
        }
    }
    check("future price perturbation", future)?;
    let mut correction = input.clone();
    let mut f = correction.dataset.facts[0].clone();
    f.id = "future-correction-audit".into();
    f.revision = 999;
    f.value_micros += 999 * MONEY;
    f.availability.published_at = cutoff + DAY;
    f.availability.modeled_available_at = Some(cutoff + DAY);
    f.availability.first_seen_at = Some(cutoff + DAY);
    f.availability.extraction_completed_at = Some(cutoff + DAY);
    correction.dataset.facts.push(f);
    check("future correction cannot rewrite past", correction)?;
    let mut shuffled = input.clone();
    shuffled.dataset.sources.reverse();
    shuffled.dataset.facts.reverse();
    shuffled.dataset.relationships.reverse();
    shuffled.dataset.bars.reverse();
    check("reingestion order invariance", shuffled)?;
    let mut metadata = input.clone();
    for s in &mut metadata.dataset.sources {
        s.title = "Different display title".into();
        s.excerpt = "Different explanatory wording".into();
    }
    check("display metadata cannot affect model/orders", metadata)?;
    let mut future_volume = input.clone();
    for b in &mut future_volume.dataset.bars {
        if b.start > cutoff {
            b.volume = 1;
        }
    }
    check(
        "future minute volume cannot size earlier orders",
        future_volume,
    )?;
    let mut future_edge = input.clone();
    let mut e = future_edge.dataset.relationships[0].clone();
    e.id = "future-edge-audit".into();
    e.availability.published_at = cutoff + DAY;
    e.availability.modeled_available_at = Some(cutoff + DAY);
    e.availability.first_seen_at = Some(cutoff + DAY);
    e.availability.extraction_completed_at = Some(cutoff + DAY);
    future_edge.dataset.relationships.push(e);
    check(
        "future relationship cannot enter historical graph",
        future_edge,
    )?;
    let view = evidence::snapshot(&input.dataset, cutoff, EvidenceMode::Observed)?;
    let observed_ok = view.facts.iter().all(|f| {
        f.availability
            .ready(EvidenceMode::Observed)
            .is_some_and(|t| t <= cutoff)
    });
    cases.push(Case {
        name: "observed readiness gate".into(),
        passed: observed_ok,
        evidence: "Every visible fact has completed publication, receipt and extraction".into(),
    });
    let labels_ok = base
        .trials
        .iter()
        .flat_map(|t| &t.decisions)
        .all(|d| d.trained_through.is_none_or(|t| t <= d.at));
    cases.push(Case {
        name: "label maturity gate".into(),
        passed: labels_ok,
        evidence: "Every fitted model records a last eligible outcome at or before its decision"
            .into(),
    });
    let order_ok = base.trials.iter().all(|t| {
        t.account
            .events
            .iter()
            .filter(|e| e.phase == "simulated_fill")
            .all(|fill| {
                t.account
                    .events
                    .iter()
                    .find(|e| e.order_id == fill.order_id && e.phase == "arrived")
                    .is_some_and(|arrival| arrival.at < fill.at)
            })
    });
    cases.push(Case {
        name: "post-arrival execution".into(),
        passed: order_ok,
        evidence: "Every simulated fill is strictly after the corresponding order arrival".into(),
    });
    Ok(Audit{all_passed:cases.iter().all(|c|c.passed),cases,scope:"Synthetic end-to-end regression invariants; not a proof of all possible leakage, a live-data validation, or a malicious-code sandbox".into()})
}
pub fn scenarios(input: &Input, out: &Path) -> Result<()> {
    if out.exists() {
        return Err("scenario output already exists".into());
    }
    std::fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let mut candidates = vec![("base", input.clone(), false)];
    let mut delayed = input.clone();
    delayed.config.observation_delay_ms += 15 * MINUTE;
    candidates.push(("observation_delay_15m", delayed, false));
    let mut cost = input.clone();
    cost.config.fee_bps *= 2;
    cost.config.spread_bps *= 2;
    cost.config.impact_bps *= 2;
    candidates.push(("costs_2x", cost, false));
    let mut ablation = input.clone();
    ablation
        .dataset
        .facts
        .retain(|f| f.key.entity != input.config.industry_entity);
    candidates.push(("remove_industry_facts", ablation, false));
    let mut no_graph = input.clone();
    no_graph.dataset.relationships.clear();
    candidates.push(("without_graph", no_graph, false));
    let mut invalid = input.clone();
    let start = input.dataset.sessions[0].open - DAY;
    for e in &mut invalid.dataset.relationships {
        e.valid_from = start;
        e.availability.published_at = start;
        e.availability.modeled_available_at = Some(start);
        e.availability.first_seen_at = Some(start);
        e.availability.extraction_completed_at = Some(start);
    }
    candidates.push(("INVALID_current_graph_backfill", invalid, true));
    let mut summary = vec![];
    for (name, candidate, invalid) in candidates {
        let run = engine::run(&candidate)?;
        output::write_run(&candidate, &run, &out.join(name))?;
        summary.push(json!({"scenario":name,"intentionally_invalid":invalid,"input_hash":run.input_hash,"paired_loss_gain":run.paired_loss_gain,"trials":run.trials.iter().map(|t|json!({"variant":t.variant,"net_return":t.net_return,"mse":t.mse,"mean_gross_exposure":t.mean_gross_exposure})).collect::<Vec<_>>()}));
    }
    ingest::immutable(&out.join("scenario_summary.json"),&serde_json::to_vec_pretty(&json!({"selection_policy":"predeclared scenarios; no winner selected","scenarios":summary,"not_implemented":"Matched degree/sector placebo graphs require a larger evidence-backed graph; the one-edge synthetic graph is insufficient."})).map_err(|e|e.to_string())?)?;
    Ok(())
}
