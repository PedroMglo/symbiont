mod api;
mod app;
mod events;
mod hitbox;
mod theme;
mod timeline;
mod viewport;
mod live;
mod reducer;
mod render;
mod widgets;

use std::fs;
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use clap::{Args, Parser};

#[derive(Debug, Parser)]
#[command(name = "symbiont-tui")]
#[command(about = "Interactive Symbiont terminal UX")]
struct ChatCli {
    #[command(flatten)]
    common: CommonArgs,

    #[arg(long)]
    session_id: Option<String>,

    #[arg(long)]
    session: Option<String>,

    #[arg(long)]
    task: Option<String>,

    #[arg(long)]
    attach: bool,

    #[arg(long)]
    watch: bool,

    #[arg(long)]
    no_live: bool,

    #[arg(trailing_var_arg = true)]
    prompt: Vec<String>,
}

#[derive(Debug, Parser)]
#[command(name = "symbiont-tui live")]
struct LiveCli {
    #[command(flatten)]
    common: CommonArgs,

    #[arg(long, default_value = "running,recent")]
    status: String,

    #[arg(long)]
    running: bool,

    #[arg(long)]
    failed: bool,

    #[arg(long)]
    all: bool,

    #[arg(long)]
    session: Option<String>,

    #[arg(long)]
    trace: Option<String>,

    #[arg(long)]
    path: Option<String>,

    #[arg(long, default_value_t = 100)]
    limit: usize,

    #[arg(long, default_value_t = 2.0)]
    poll_seconds: f64,

    #[arg(long)]
    privacy: bool,
}

#[derive(Debug, Args)]
struct CommonArgs {
    #[arg(long, env = "ORC_API_URL", default_value = "https://127.0.0.1:8586")]
    api_url: String,

    #[arg(long, env = "ORC_SYMBIONT_API_KEY")]
    api_key: Option<String>,

    #[arg(long)]
    api_key_file: Option<PathBuf>,

    #[arg(long)]
    cwd: Option<PathBuf>,

    #[arg(long, default_value = "@")]
    model: String,

    #[arg(long, default_value = "smart")]
    mode: String,

    #[arg(long)]
    no_alt_screen: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    let mut args: Vec<String> = std::env::args().collect();
    let mode = args.get(1).map(String::as_str).unwrap_or("chat");
    if mode == "live" {
        args.remove(1);
        let cli = LiveCli::parse_from(args);
        let cwd = resolve_cwd(cli.common.cwd)?;
        let status = if cli.all {
            "all".to_string()
        } else if cli.failed {
            "failed,recent".to_string()
        } else if cli.running {
            "running,recent".to_string()
        } else {
            cli.status
        };
        let config = live::LiveConfig {
            api_url: cli.common.api_url,
            api_key: resolve_api_key(cli.common.api_key, cli.common.api_key_file.as_ref())?,
            cwd: cwd.display().to_string(),
            model: cli.common.model,
            mode: cli.common.mode,
            status,
            session_filter: cli.session,
            trace_filter: cli.trace,
            path_filter: cli.path,
            limit: cli.limit,
            poll_seconds: cli.poll_seconds,
            use_alt_screen: !cli.common.no_alt_screen,
            privacy: cli.privacy,
        };
        return live::run(config).await;
    }
    if mode == "chat" {
        args.remove(1);
    }
    let cli = ChatCli::parse_from(args);
    let cwd = resolve_cwd(cli.common.cwd)?;
    let session_id = cli.session_id.or(cli.session);
    let task_id = cli.task;
    let attach = cli.attach || task_id.is_some();
    let watch = cli.watch;
    let config = app::AppConfig {
        api_url: cli.common.api_url,
        api_key: resolve_api_key(cli.common.api_key, cli.common.api_key_file.as_ref())?,
        cwd: cwd.display().to_string(),
        model: cli.common.model,
        mode: cli.common.mode,
        session_id,
        task_id,
        attach,
        watch,
        use_alt_screen: !cli.common.no_alt_screen,
        live_events: !cli.no_live,
    };
    let prompt = if cli.prompt.is_empty() {
        None
    } else {
        Some(cli.prompt.join(" "))
    };
    app::run(config, prompt).await
}

fn resolve_cwd(value: Option<PathBuf>) -> Result<PathBuf> {
    match value {
        Some(path) => Ok(path),
        None => std::env::current_dir().context("failed to resolve current directory"),
    }
}

fn resolve_api_key(value: Option<String>, file: Option<&PathBuf>) -> Result<String> {
    if let Some(value) = value {
        let trimmed = value.trim();
        if !trimmed.is_empty() {
            return Ok(trimmed.to_string());
        }
    }
    if let Some(path) = file {
        let value = fs::read_to_string(path)
            .with_context(|| format!("failed to read API key file {}", path.display()))?;
        let trimmed = value.trim();
        if !trimmed.is_empty() {
            return Ok(trimmed.to_string());
        }
    }
    bail!("API key not found. Set ORC_SYMBIONT_API_KEY or pass --api-key-file");
}
