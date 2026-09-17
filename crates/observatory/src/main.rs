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
    };
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}
