use clap::{Parser, Subcommand};
use hindsight_observatory::{model::*, service, sources, worker};
use std::path::PathBuf;
#[derive(Parser)]
#[command(
    name = "hindsight-observatory",
    about = "Read-only economic research observatory. No broker orders."
)]
struct Cli {
    #[arg(
        long,
        default_value = ".observatory",
        env = "HINDSIGHT_OBSERVATORY_ROOT"
    )]
    root: PathBuf,
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Doctor,
    Plan,
    Status,
    Worker {
        #[arg(long, default_value_t = 100)]
        max_steps: usize,
        #[arg(long, default_value_t = 600)]
        max_seconds: u64,
    },
    Action {
        id: String,
        action: String,
    },
    Serve {
        #[arg(long, default_value_t = 8780)]
        port: u16,
        #[arg(long)]
        worker: bool,
        #[arg(long, default_value = "ui/observatory/dist")]
        ui: PathBuf,
    },
    Verify,
    Reconcile,
    Capacity {
        output: PathBuf,
        #[arg(long, default_value_t = 10000000)]
        rows: usize,
    },
    Updates {
        #[arg(long)]
        enable: bool,
    },
    Math {
        input: PathBuf,
    },
    Restore {
        backup: PathBuf,
        output: PathBuf,
    },
    Backup {
        output: PathBuf,
    },
    AtlasMission {
        query: String,
        #[arg(long, default_value_t = 8)]
        max_requests: usize,
        #[arg(long, default_value_t = 8_388_608)]
        max_bytes: u64,
        #[arg(long, default_value_t = 2)]
        max_depth: usize,
        #[arg(long)]
        run: bool,
    },
    AtlasStatus {
        #[arg(long, default_value = "")]
        search: String,
    },
    AtlasAcquire {
        distribution_id: String,
        #[arg(long, default_value_t = 8_388_608)]
        max_bytes: u64,
    },
}
#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        eprintln!("Hindsight Observatory: {e}");
        std::process::exit(1)
    }
}
async fn run() -> Result<()> {
    let cli = Cli::parse();
    let store = Store::open(cli.root)?;
    let out = match cli.command {
        Command::Doctor | Command::Status => store.status()?,
        Command::Plan => sources::plan(&store)?,
        Command::Worker {
            max_steps,
            max_seconds,
        } => tokio::task::spawn_blocking(move || worker::run(&store, max_steps, max_seconds))
            .await
            .map_err(|_| invalid("Worker task failed"))??,
        Command::Action { id, action } => {
            store.action(&id, &action)?;
            serde_json::json!({"id":id,"action":action,"job":store.job(&id)?})
        }
        Command::Serve { port, worker, ui } => {
            service::serve(store, port, ui, worker).await?;
            return Ok(());
        }
        Command::Verify => {
            let v = store.verify_objects()?;
            if v["passed"] != true {
                return Err(Error::Conflict(format!("Integrity check failed: {v}")));
            }
            v
        }
        Command::Reconcile => store.reconcile()?,
        Command::Capacity { output, rows } => hindsight_observatory::capacity::run(&output, rows)?,
        Command::Updates { enable } => store.configure_updates(enable)?,
        Command::Restore { backup, output } => Store::restore_from(&backup, &output)?,
        Command::Math { input } => {
            if std::fs::metadata(&input)?.len() > 16 * 1024 * 1024 {
                return Err(invalid("Numerical input exceeds 16MiB budget"));
            }
            hindsight_observatory::operations::math_request(serde_json::from_reader(
                std::fs::File::open(input)?,
            )?)?
        }
        Command::Backup { output } => store.backup(&output)?,
        Command::AtlasMission {
            query,
            max_requests,
            max_bytes,
            max_depth,
            run,
        } => {
            let request = hindsight_observatory::atlas::MissionRequest {
                query,
                max_requests,
                max_bytes,
                max_depth,
                max_sources: 20,
            };
            let id = store.create_atlas_mission(&request)?;
            if run {
                let runner = store.clone();
                let run_id = id.clone();
                tokio::task::spawn_blocking(move || runner.run_atlas_mission(&run_id))
                    .await
                    .map_err(|_| invalid("Atlas mission task failed"))??
            } else {
                serde_json::json!({"id":id,"state":"queued"})
            }
        }
        Command::AtlasStatus { search } => serde_json::json!({
            "overview": store.atlas_overview()?,
            "datasets": store.atlas_datasets(&search, 100)?,
            "missions": store.atlas_missions(20)?,
        }),
        Command::AtlasAcquire {
            distribution_id,
            max_bytes,
        } => {
            let runner = store.clone();
            tokio::task::spawn_blocking(move || {
                runner.sample_atlas_distribution(&distribution_id, max_bytes)
            })
            .await
            .map_err(|_| invalid("Atlas acquisition task failed"))??
        }
    };
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}
