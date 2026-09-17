use hindsight_economic::{demo, engine, evidence, ingest, output, sec, types::*, validation};
use std::{env, fs, path::Path};
fn load(path: &str) -> Result<Input> {
    let bytes = fs::read(path).map_err(|e| e.to_string())?;
    serde_json::from_slice(&bytes).map_err(|e| e.to_string())
}
fn run() -> Result<()> {
    let args: Vec<String> = env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        None|Some("--help")|Some("help")=>println!("Hindsight economic research (NO LIVE TRADING)\n  demo OUTPUT_DIR\n  demo-input OUTPUT_JSON\n  run INPUT_JSON OUTPUT_DIR\n  coverage INPUT_JSON\n  snapshot INPUT_JSON RFC3339 [observed|reconstructed]\n  capture-public HTTPS_URL ARCHIVE_DIR\n  fetch-alpaca SYMBOLS START_RFC3339 END_RFC3339 ARCHIVE_DIR\n  import-sec COMPANYFACTS_JSON TAXONOMY TAG ENTITY METRIC OUTPUT_JSON\n  falsify OUTPUT_JSON\n  scenarios INPUT_JSON OUTPUT_DIR"),
        Some("demo") if args.len()==2=>{let input=demo::input()?;let result=engine::run(&input)?;output::write_run(&input,&result,Path::new(&args[1]))?;println!("Synthetic research dashboard: {}/index.html\nInput hash: {}",args[1],result.input_hash);},
        Some("demo-input") if args.len()==2=>{let input=demo::input()?;ingest::immutable(Path::new(&args[1]),&serde_json::to_vec_pretty(&input).map_err(|e|e.to_string())?)?;println!("Wrote synthetic input {}",args[1]);},
        Some("run") if args.len()==3=>{let input=load(&args[1])?;let result=engine::run(&input)?;output::write_run(&input,&result,Path::new(&args[2]))?;println!("Research dashboard: {}/index.html\nInput hash: {}",args[2],result.input_hash);},
        Some("coverage") if args.len()==2=>{let input=load(&args[1])?;validation::validate(&input)?;println!("{}",serde_json::to_string_pretty(&engine::coverage(&input)?).map_err(|e|e.to_string())?);},
        Some("snapshot") if args.len()==3||args.len()==4=>{let input=load(&args[1])?;validation::validate(&input)?;let at=chrono::DateTime::parse_from_rfc3339(&args[2]).map_err(|e|e.to_string())?.timestamp_millis();let mode=match args.get(3).map(String::as_str){None=>input.config.mode,Some("observed")=>EvidenceMode::Observed,Some("reconstructed")=>EvidenceMode::Reconstructed,_=>return Err("invalid evidence mode".into())};println!("{}",serde_json::to_string_pretty(&evidence::snapshot(&input.dataset,at,mode)?).map_err(|e|e.to_string())?);},
        Some("capture-public") if args.len()==3=>println!("{}",serde_json::to_string_pretty(&ingest::capture_public(&args[1],Path::new(&args[2]))?).map_err(|e|e.to_string())?),
        Some("fetch-alpaca") if args.len()==5=>println!("{}",ingest::fetch_alpaca(&args[1],&args[2],&args[3],Path::new(&args[4]))?.display()),
        Some("import-sec") if args.len()==7=>{let bytes=fs::read(&args[1]).map_err(|e|e.to_string())?;let imported=sec::companyfacts(&bytes,&args[2],&args[3],&args[4],&args[5],chrono::Utc::now().timestamp_millis())?;ingest::immutable(Path::new(&args[6]),&serde_json::to_vec_pretty(&imported).map_err(|e|e.to_string())?)?;println!("Imported {} facts; inspect declared limitations",imported.facts.len());},
        Some("falsify") if args.len()==2=>{let result=hindsight_economic::audit::falsify()?;ingest::immutable(Path::new(&args[1]),&serde_json::to_vec_pretty(&result).map_err(|e|e.to_string())?)?;if !result.all_passed{return Err("falsification failure".into());}println!("{} falsification checks passed",result.cases.len());},
        Some("scenarios") if args.len()==3=>{hindsight_economic::audit::scenarios(&load(&args[1])?,Path::new(&args[2]))?;},
        _=>return Err("unknown command or argument count; run with --help".into()),
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("Hindsight: {error}");
        std::process::exit(2);
    }
}
