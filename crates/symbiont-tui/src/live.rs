use std::env;
use std::io::{self, IsTerminal};
use std::process::Command;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use crossterm::event::{self, DisableBracketedPaste, EnableBracketedPaste, Event, KeyCode, KeyEvent, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::{Frame, Terminal};
use serde_json::Value;
use tokio::sync::mpsc;
use tokio::time;

use crate::api::ApiClient;
use crate::app::{self, AppConfig};
use crate::events::{sanitize_terminal, FileChange, TimelineSnapshot};
use crate::render::diff;

#[derive(Debug, Clone)]
pub struct LiveConfig {
    pub api_url: String,
    pub api_key: String,
    pub cwd: String,
    pub model: String,
    pub mode: String,
    pub status: String,
    pub session_filter: Option<String>,
    pub trace_filter: Option<String>,
    pub path_filter: Option<String>,
    pub limit: usize,
    pub poll_seconds: f64,
    pub use_alt_screen: bool,
    pub privacy: bool,
}

#[derive(Debug, Clone, Default)]
pub struct LiveSnapshot {
    pub seq: u64,
    pub server_time: f64,
    pub counts: LiveCounts,
    pub sessions: Vec<SessionNode>,
    pub tasks: Vec<TaskNode>,
}

#[derive(Debug, Clone, Default)]
pub struct LiveCounts {
    pub sessions: usize,
    pub tasks: usize,
    pub running: usize,
    pub failed: usize,
    pub recent: usize,
}

#[derive(Debug, Clone, Default)]
pub struct SessionNode {
    pub session_id: String,
    pub status: String,
    pub cwd: String,
    pub model: String,
    pub updated_at: f64,
    pub last_prompt_preview: String,
    pub task_ids: Vec<String>,
    pub running_task_count: usize,
    pub failed_task_count: usize,
}

#[derive(Debug, Clone, Default)]
pub struct TaskNode {
    pub task_id: String,
    pub session_id: String,
    pub name: String,
    pub trace_id: String,
    pub status: String,
    pub active_phase: String,
    pub goal_preview: String,
    pub updated_at: f64,
    pub elapsed_seconds: f64,
    pub cwd: String,
    pub model: String,
    pub file_summary: Vec<FileChange>,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum FocusFilter {
    Running,
    Failed,
    Recent,
    All,
}

impl FocusFilter {
    fn status(self) -> &'static str {
        match self {
            FocusFilter::Running => "running,recent",
            FocusFilter::Failed => "failed,recent",
            FocusFilter::Recent => "recent",
            FocusFilter::All => "all",
        }
    }

    fn label(self) -> &'static str {
        match self {
            FocusFilter::Running => "running",
            FocusFilter::Failed => "failed",
            FocusFilter::Recent => "recent",
            FocusFilter::All => "all",
        }
    }
}

#[derive(Debug)]
struct LiveState {
    config: LiveConfig,
    snapshot: LiveSnapshot,
    selected: usize,
    detail: Option<TimelineSnapshot>,
    last_error: Option<String>,
    connection: String,
    filter: FocusFilter,
    full_diff: bool,
    raw: bool,
    last_refresh: Option<Instant>,
    should_quit: bool,
    open_target: Option<PendingOpen>,
}

#[derive(Debug)]
enum LiveEvent {
    Snapshot(LiveSnapshot),
    Detail(TimelineSnapshot),
    Failed(String),
    Input(KeyEvent),
}

#[derive(Debug, Clone)]
struct AttachTarget {
    session_id: String,
    task_id: String,
    cwd: String,
    model: String,
}

#[derive(Debug, Clone)]
struct PendingOpen {
    target: AttachTarget,
    mode: OpenMode,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum OpenMode {
    InPlace,
    NewTerminal,
}

impl LiveSnapshot {
    pub fn from_value(value: &Value) -> Self {
        let counts = value.get("counts").unwrap_or(&Value::Null);
        Self {
            seq: value.get("seq").and_then(Value::as_u64).unwrap_or_default(),
            server_time: value.get("server_time").and_then(Value::as_f64).unwrap_or_default(),
            counts: LiveCounts {
                sessions: usize_at(counts, "sessions"),
                tasks: usize_at(counts, "tasks"),
                running: usize_at(counts, "running"),
                failed: usize_at(counts, "failed"),
                recent: usize_at(counts, "recent"),
            },
            sessions: value
                .get("sessions")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(SessionNode::from_value).collect())
                .unwrap_or_default(),
            tasks: value
                .get("tasks")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(TaskNode::from_value).collect())
                .unwrap_or_default(),
        }
    }
}

impl SessionNode {
    fn from_value(value: &Value) -> Self {
        Self {
            session_id: text_at(value, "session_id"),
            status: text_at(value, "status"),
            cwd: text_at(value, "cwd"),
            model: text_at(value, "model"),
            updated_at: value.get("updated_at").and_then(Value::as_f64).unwrap_or_default(),
            last_prompt_preview: text_at(value, "last_prompt_preview"),
            task_ids: value
                .get("task_ids")
                .and_then(Value::as_array)
                .map(|items| items.iter().filter_map(Value::as_str).map(sanitize_terminal).collect())
                .unwrap_or_default(),
            running_task_count: usize_at(value, "running_task_count"),
            failed_task_count: usize_at(value, "failed_task_count"),
        }
    }
}

impl TaskNode {
    fn from_value(value: &Value) -> Self {
        Self {
            task_id: text_at(value, "task_id"),
            session_id: text_at(value, "session_id"),
            name: text_at(value, "name"),
            trace_id: text_at(value, "trace_id"),
            status: text_at(value, "status"),
            active_phase: text_at(value, "active_phase"),
            goal_preview: text_at(value, "goal_preview"),
            updated_at: value.get("updated_at").and_then(Value::as_f64).unwrap_or_default(),
            elapsed_seconds: value.get("elapsed_seconds").and_then(Value::as_f64).unwrap_or_default(),
            cwd: text_at(value, "cwd"),
            model: text_at(value, "model"),
            file_summary: value
                .get("file_summary")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(file_from_value).collect())
                .unwrap_or_default(),
        }
    }
}

pub async fn run(config: LiveConfig) -> Result<()> {
    let api = ApiClient::new(config.api_url.clone(), config.api_key.clone())?;
    let interactive = config.use_alt_screen && io::stdout().is_terminal();
    let filter = filter_from_status(&config.status);
    let mut state = LiveState {
        snapshot: LiveSnapshot::default(),
        selected: 0,
        detail: None,
        last_error: None,
        connection: "connecting".to_string(),
        filter,
        full_diff: false,
        raw: false,
        last_refresh: None,
        should_quit: false,
        open_target: None,
        config,
    };
    if !interactive {
        refresh_snapshot(&api, &mut state).await;
        if let Some(task_id) = state.selected_task().map(|task| task.task_id.clone()) {
            refresh_detail(&api, &mut state, task_id).await;
        }
        print!("{}", render_plain(&state));
        return Ok(());
    }
    if let Some(target) = run_interactive(api, state).await? {
        let config = AppConfig {
            api_url: target.api_url,
            api_key: target.api_key,
            cwd: target.cwd,
            model: target.model,
            mode: target.mode,
            session_id: Some(target.session_id),
            task_id: Some(target.task_id),
            attach: true,
            watch: true,
            use_alt_screen: true,
            live_events: true,
        };
        app::run(config, None).await?;
    }
    Ok(())
}

#[derive(Debug, Clone)]
struct OpenChatRequest {
    api_url: String,
    api_key: String,
    cwd: String,
    model: String,
    mode: String,
    session_id: String,
    task_id: String,
    open_mode: OpenMode,
}

impl OpenChatRequest {
    fn symbiont_args(&self) -> Vec<String> {
        vec![
            "--session".to_string(),
            self.session_id.clone(),
            "--task".to_string(),
            self.task_id.clone(),
            "--watch".to_string(),
            "--model".to_string(),
            self.model.clone(),
            "--mode".to_string(),
            self.mode.clone(),
        ]
    }

    fn shell_command(&self) -> String {
        let mut parts = vec!["symbiont".to_string()];
        parts.extend(self.symbiont_args().into_iter().map(|item| shell_escape(&item)));
        parts.join(" ")
    }
}

fn launch_chat_terminal(request: &OpenChatRequest) -> Result<()> {
    if let Ok(template) = env::var("SYMBIONT_TERMINAL_CMD") {
        let command_line = if template.contains("{cmd}") {
            template.replace("{cmd}", &request.shell_command())
        } else {
            format!("{} {}", template, request.shell_command())
        };
        spawn_shell(&command_line, request).context("SYMBIONT_TERMINAL_CMD failed")?;
        return Ok(());
    }

    if env::var_os("TMUX").is_some() {
        let shell_command = request.shell_command();
        let mut command = Command::new("tmux");
        command.args(["new-window", "-n", "symbiont", shell_command.as_str()]);
        attach_process_context(&mut command, request);
        if spawn_optional(command)? {
            return Ok(());
        }
    }

    if env::var_os("DISPLAY").is_some() || env::var_os("WAYLAND_DISPLAY").is_some() {
        for spec in TERMINAL_SPECS {
            let mut command = Command::new(spec.program);
            command.args(spec.prefix);
            command.arg("symbiont");
            command.args(request.symbiont_args());
            attach_process_context(&mut command, request);
            if spawn_optional(command)? {
                return Ok(());
            }
        }
    }
    anyhow::bail!("no supported terminal emulator found")
}

struct TerminalSpec {
    program: &'static str,
    prefix: &'static [&'static str],
}

const TERMINAL_SPECS: &[TerminalSpec] = &[
    TerminalSpec {
        program: "wezterm",
        prefix: &["start", "--"],
    },
    TerminalSpec {
        program: "kitty",
        prefix: &[],
    },
    TerminalSpec {
        program: "alacritty",
        prefix: &["-e"],
    },
    TerminalSpec {
        program: "gnome-terminal",
        prefix: &["--"],
    },
    TerminalSpec {
        program: "konsole",
        prefix: &["-e"],
    },
    TerminalSpec {
        program: "x-terminal-emulator",
        prefix: &["-e"],
    },
    TerminalSpec {
        program: "xterm",
        prefix: &["-e"],
    },
];

fn spawn_shell(command_line: &str, request: &OpenChatRequest) -> io::Result<()> {
    let mut command = Command::new("sh");
    command.args(["-lc", command_line]);
    attach_process_context(&mut command, request);
    command.spawn().map(|_| ())
}

fn spawn_optional(mut command: Command) -> io::Result<bool> {
    match command.spawn() {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

fn attach_process_context(command: &mut Command, request: &OpenChatRequest) {
    if !request.cwd.is_empty() {
        command.current_dir(&request.cwd);
    }
    command.env("ORC_API_URL", &request.api_url);
    command.env("ORC_SYMBIONT_API_KEY", &request.api_key);
}

fn shell_escape(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

async fn run_interactive(api: ApiClient, mut state: LiveState) -> Result<Option<OpenChatRequest>> {
    let mut terminal = setup_terminal().context("failed to initialize live terminal")?;
    let (tx, mut rx) = mpsc::channel(256);
    spawn_input_reader(tx.clone());
    spawn_snapshot_fetch(api.clone(), tx.clone(), state.filter.status().to_string(), state.config.limit);
    let mut poll = time::interval(Duration::from_millis((state.config.poll_seconds.max(0.5) * 1000.0) as u64));
    let mut tick = time::interval(Duration::from_millis(66));
    let mut dirty = true;
    loop {
        tokio::select! {
            Some(event) = rx.recv() => {
                apply_event(&api, &tx, &mut state, event);
                dirty = true;
                if state.should_quit || state.open_target.is_some() {
                    break;
                }
            }
            _ = poll.tick() => {
                spawn_snapshot_fetch(api.clone(), tx.clone(), state.filter.status().to_string(), state.config.limit);
            }
            _ = tick.tick() => {
                if dirty {
                    terminal.draw(|frame| draw(frame, &state))?;
                    dirty = false;
                }
            }
        }
    }
    let request = state.open_target.take().map(|pending| OpenChatRequest {
        api_url: state.config.api_url.clone(),
        api_key: state.config.api_key.clone(),
        cwd: pending.target.cwd,
        model: pending.target.model,
        mode: state.config.mode.clone(),
        session_id: pending.target.session_id,
        task_id: pending.target.task_id,
        open_mode: pending.mode,
    });
    restore_terminal(&mut terminal)?;
    match request {
        Some(request) if request.open_mode == OpenMode::NewTerminal => {
            if let Err(error) = launch_chat_terminal(&request) {
                eprintln!("Could not open a new terminal ({error}). Opening the task here instead.");
                Ok(Some(request))
            } else {
                Ok(None)
            }
        }
        request => Ok(request),
    }
}

fn apply_event(api: &ApiClient, tx: &mpsc::Sender<LiveEvent>, state: &mut LiveState, event: LiveEvent) {
    match event {
        LiveEvent::Snapshot(snapshot) => {
            state.connection = "live".to_string();
            state.last_error = None;
            state.snapshot = filter_snapshot(snapshot, state);
            if state.selected >= state.snapshot.tasks.len() {
                state.selected = state.snapshot.tasks.len().saturating_sub(1);
            }
            state.last_refresh = Some(Instant::now());
            if let Some(task) = state.selected_task() {
                spawn_detail_fetch(api.clone(), tx.clone(), task.task_id.clone());
            }
        }
        LiveEvent::Detail(detail) => {
            state.detail = Some(detail);
        }
        LiveEvent::Failed(error) => {
            state.connection = "error".to_string();
            state.last_error = Some(error);
        }
        LiveEvent::Input(key) => handle_key(api, tx, state, key),
    }
}

fn handle_key(api: &ApiClient, tx: &mpsc::Sender<LiveEvent>, state: &mut LiveState, key: KeyEvent) {
    match key.code {
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => state.should_quit = true,
        KeyCode::Char('q') | KeyCode::Esc => state.should_quit = true,
        KeyCode::Down | KeyCode::Char('j') => {
            if state.selected + 1 < state.snapshot.tasks.len() {
                state.selected += 1;
                if let Some(task) = state.selected_task() {
                    spawn_detail_fetch(api.clone(), tx.clone(), task.task_id.clone());
                }
            }
        }
        KeyCode::Up | KeyCode::Char('k') => {
            if state.selected > 0 {
                state.selected -= 1;
                if let Some(task) = state.selected_task() {
                    spawn_detail_fetch(api.clone(), tx.clone(), task.task_id.clone());
                }
            }
        }
        KeyCode::Char('d') => state.full_diff = !state.full_diff,
        KeyCode::Char('r') | KeyCode::Char('R') => {
            spawn_snapshot_fetch(api.clone(), tx.clone(), state.filter.status().to_string(), state.config.limit);
        }
        KeyCode::Char('1') => set_filter(api, tx, state, FocusFilter::Running),
        KeyCode::Char('2') => set_filter(api, tx, state, FocusFilter::Failed),
        KeyCode::Char('3') => set_filter(api, tx, state, FocusFilter::Recent),
        KeyCode::Char('4') => set_filter(api, tx, state, FocusFilter::All),
        KeyCode::Char('o') => {
            if let Some(task) = state.selected_task() {
                state.open_target = Some(PendingOpen {
                    target: attach_target_for_task(task, state),
                    mode: OpenMode::InPlace,
                });
            }
        }
        KeyCode::Enter => {
            if let Some(task) = state.selected_task() {
                state.open_target = Some(PendingOpen {
                    target: attach_target_for_task(task, state),
                    mode: OpenMode::InPlace,
                });
            }
        }
        KeyCode::Char('n') => {
            if let Some(task) = state.selected_task() {
                state.open_target = Some(PendingOpen {
                    target: attach_target_for_task(task, state),
                    mode: OpenMode::NewTerminal,
                });
            }
        }
        KeyCode::Char('h') | KeyCode::Char('?') => {
            state.last_error = Some("keys: ↑/↓ select · Enter/o open here · n new terminal · 1 running · 2 failed · 3 recent · 4 all · d diff · q detach".to_string());
        }
        KeyCode::Char('x') => state.raw = !state.raw,
        _ => {}
    }
}

fn set_filter(api: &ApiClient, tx: &mpsc::Sender<LiveEvent>, state: &mut LiveState, filter: FocusFilter) {
    state.filter = filter;
    state.selected = 0;
    state.detail = None;
    spawn_snapshot_fetch(api.clone(), tx.clone(), state.filter.status().to_string(), state.config.limit);
}

async fn refresh_snapshot(api: &ApiClient, state: &mut LiveState) {
    match api.fetch_live_snapshot(state.filter.status(), state.config.limit, 900).await {
        Ok(snapshot) => {
            state.snapshot = filter_snapshot(snapshot, state);
            state.connection = "live".to_string();
        }
        Err(error) => {
            state.connection = "error".to_string();
            state.last_error = Some(format!("{error:#}"));
        }
    }
}

async fn refresh_detail(api: &ApiClient, state: &mut LiveState, task_id: String) {
    match api.fetch_timeline(&task_id).await {
        Ok(detail) => state.detail = Some(detail),
        Err(error) => state.last_error = Some(format!("{error:#}")),
    }
}

fn spawn_snapshot_fetch(api: ApiClient, tx: mpsc::Sender<LiveEvent>, status: String, limit: usize) {
    tokio::spawn(async move {
        match api.fetch_live_snapshot(&status, limit, 900).await {
            Ok(snapshot) => {
                let _ = tx.send(LiveEvent::Snapshot(snapshot)).await;
            }
            Err(error) => {
                let _ = tx.send(LiveEvent::Failed(format!("{error:#}"))).await;
            }
        }
    });
}

fn spawn_detail_fetch(api: ApiClient, tx: mpsc::Sender<LiveEvent>, task_id: String) {
    tokio::spawn(async move {
        match api.fetch_timeline(&task_id).await {
            Ok(detail) => {
                let _ = tx.send(LiveEvent::Detail(detail)).await;
            }
            Err(error) => {
                let _ = tx.send(LiveEvent::Failed(format!("{error:#}"))).await;
            }
        }
    });
}

fn filter_snapshot(mut snapshot: LiveSnapshot, state: &LiveState) -> LiveSnapshot {
    snapshot.tasks.retain(|task| {
        state
            .config
            .session_filter
            .as_ref()
            .map(|session| task.session_id.contains(session))
            .unwrap_or(true)
            && state
                .config
                .trace_filter
                .as_ref()
                .map(|trace| task.trace_id.contains(trace))
                .unwrap_or(true)
            && state
                .config
                .path_filter
                .as_ref()
                .map(|path| task.file_summary.iter().any(|file| file.path.contains(path)))
                .unwrap_or(true)
    });
    snapshot
}

fn draw(frame: &mut Frame<'_>, state: &LiveState) {
    let area = frame.area();
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(4), Constraint::Min(10), Constraint::Length(8), Constraint::Length(2)])
        .split(area);
    frame.render_widget(header(state), chunks[0]);
    let columns = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(48), Constraint::Percentage(52)])
        .split(chunks[1]);
    frame.render_widget(task_tree(state, columns[0].width as usize), columns[0]);
    frame.render_widget(detail_panel(state, columns[1].width as usize), columns[1]);
    frame.render_widget(diff_panel(state, chunks[2].width as usize), chunks[2]);
    frame.render_widget(footer(state), chunks[3]);
}

fn header(state: &LiveState) -> Paragraph<'static> {
    let latency = state.last_refresh.map(|at| format!("updated {:.1}s ago", at.elapsed().as_secs_f64())).unwrap_or_else(|| "updating".to_string());
    Paragraph::new(vec![
        Line::from(vec![
            Span::styled("Symbiont Live", Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
            Span::raw(format!(
                " · {} · {} · {} running · {} recent · {}",
                state.config.api_url,
                home_path(&state.config.cwd),
                state.snapshot.counts.running,
                state.snapshot.counts.recent,
                latency
            )),
        ]),
        Line::from(format!(
            "/search /running /failed /all /open /session /task /copy /exit · filter {} · seq {} · connection {}",
            state.filter.label(),
            state.snapshot.seq,
            state.connection
        )),
    ])
    .block(Block::default().borders(Borders::ALL))
}

fn task_tree(state: &LiveState, width: usize) -> Paragraph<'static> {
    let mut lines = Vec::new();
    if state.snapshot.sessions.is_empty() {
        lines.push(Line::from("No background sessions/tasks in current filter."));
    }
    for session in &state.snapshot.sessions {
        lines.push(Line::from(vec![
            Span::styled("▼ session ", Style::default().fg(Color::DarkGray)),
            Span::styled(short_id(&session.session_id), Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
            Span::raw(format!(
                " · {} · {} tasks · run {} fail {} · upd {:.0} · {} · {}",
                session.status,
                session.task_ids.len(),
                session.running_task_count,
                session.failed_task_count,
                session.updated_at,
                truncate(&session.model, 12),
                truncate(&home_path(&session.cwd), width.saturating_sub(36))
            )),
        ]));
        if !state.config.privacy && !session.last_prompt_preview.is_empty() {
            lines.push(Line::from(format!("  prompt: {}", truncate(&session.last_prompt_preview, width.saturating_sub(12)))));
        }
        for task in state.snapshot.tasks.iter().filter(|task| task.session_id == session.session_id) {
            let selected = state.selected_task().map(|item| item.task_id == task.task_id).unwrap_or(false);
            let marker = if selected { ">" } else { " " };
            lines.push(Line::from(vec![
                Span::styled(format!(" {marker} "), Style::default().fg(if selected { Color::Yellow } else { Color::DarkGray })),
                Span::styled(status_symbol(&task.status), Style::default().fg(status_color(&task.status))),
                Span::raw(format!(
                    " {} · {} · {:.0}s · {}",
                    truncate(&task.name, 12),
                    short_id(&task.task_id),
                    task.elapsed_seconds,
                    truncate(&task.active_phase, width.saturating_sub(38))
                )),
            ]));
            if !task.file_summary.is_empty() {
                let files = task
                    .file_summary
                    .iter()
                    .take(3)
                    .map(|file| format!("{} {} +{} -{}", file_letter(&file.status), file.path, file.additions, file.deletions))
                    .collect::<Vec<_>>()
                    .join("   ");
                lines.push(Line::from(Span::styled(
                    format!("    {}", truncate(&files, width.saturating_sub(6))),
                    Style::default().fg(Color::DarkGray),
                )));
            }
        }
        lines.push(Line::from(""));
    }
    Paragraph::new(lines)
        .block(Block::default().title(" Sessions / Tasks ").borders(Borders::ALL))
        .wrap(Wrap { trim: false })
}

fn detail_panel(state: &LiveState, width: usize) -> Paragraph<'static> {
    let mut lines = Vec::new();
    if let Some(task) = state.selected_task() {
        lines.push(Line::from(vec![
            Span::styled("task ", Style::default().fg(Color::DarkGray)),
            Span::raw(task.task_id.clone()),
        ]));
        lines.push(Line::from(format!("trace: {}", task.trace_id)));
        lines.push(Line::from(format!("session: {}", task.session_id)));
        lines.push(Line::from(format!("status: {} · active: {}", task.status, task.active_phase)));
        lines.push(Line::from(format!("updated: {:.0} · cwd: {}", task.updated_at, truncate(&home_path(&task.cwd), width.saturating_sub(20)))));
        if !task.model.is_empty() {
            lines.push(Line::from(format!("model: {}", task.model)));
        }
        if !state.config.privacy {
            lines.push(Line::from(format!("goal: {}", truncate(&task.goal_preview, width.saturating_sub(8)))));
        }
        if let Some(detail) = &state.detail {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled("Recent file changes", Style::default().fg(Color::Cyan))));
            for file in detail.files.iter().take(8) {
                lines.push(Line::from(format!(
                    "  {} {} +{} -{}",
                    file_letter(&file.status),
                    truncate(&file.path, width.saturating_sub(16)),
                    file.additions,
                    file.deletions
                )));
            }
            if !detail.commands.is_empty() {
                lines.push(Line::from(""));
                lines.push(Line::from(Span::styled("Commands", Style::default().fg(Color::Cyan))));
                for command in detail.commands.iter().take(5) {
                    lines.push(Line::from(format!(
                        "  {} {} · exit {}",
                        status_symbol(&command.status),
                        truncate(&command.command, width.saturating_sub(18)),
                        command.exit_code.map(|value| value.to_string()).unwrap_or_else(|| "-".to_string())
                    )));
                }
            }
        }
    } else {
        lines.push(Line::from("No task selected."));
    }
    if let Some(error) = &state.last_error {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(truncate(error, width.saturating_sub(2)), Style::default().fg(Color::Yellow))));
    }
    Paragraph::new(lines)
        .block(Block::default().title(" Selected ").borders(Borders::ALL))
        .wrap(Wrap { trim: false })
}

fn diff_panel(state: &LiveState, width: usize) -> Paragraph<'static> {
    let mut lines = Vec::new();
    if state.config.privacy {
        lines.push(Line::from("privacy mode: diff hidden"));
    } else if let Some(detail) = &state.detail {
        if let Some(file) = detail.files.iter().find(|file| file.patch.is_some()) {
            lines.push(Line::from(format!(
                "Recent diff · {} +{} -{}",
                file.path, file.additions, file.deletions
            )));
            let max_lines = if state.full_diff { 10_000 } else { 12 };
            if let Some(patch) = &file.patch {
                lines.extend(diff::patch_to_lines(patch, width, max_lines));
            }
        } else {
            lines.push(Line::from("No inline diff for selected task."));
        }
    } else {
        lines.push(Line::from("Select a task to load diff/details."));
    }
    Paragraph::new(lines)
        .block(Block::default().title(" Diff / Events ").borders(Borders::ALL))
        .wrap(Wrap { trim: false })
}

fn footer(state: &LiveState) -> Paragraph<'static> {
    let selected = state
        .selected_task()
        .map(|task| format!("selected {} · {}", short_id(&task.task_id), task.status))
        .unwrap_or_else(|| "nothing selected".to_string());
    Paragraph::new(Line::from(format!(
        "↑/↓ select · Enter/o open here · n new terminal · 1 running · 2 failed · 3 recent · 4 all · d diff · q detach · {selected}"
    )))
}

fn render_plain(state: &LiveState) -> String {
    let mut out = Vec::new();
    out.push(format!(
        "Symbiont Live · {} · seq {} · server {:.0} · sessions {} · tasks {} · running {} · failed {}",
        state.config.api_url,
        state.snapshot.seq,
        state.snapshot.server_time,
        state.snapshot.counts.sessions,
        state.snapshot.counts.tasks,
        state.snapshot.counts.running,
        state.snapshot.counts.failed
    ));
    for session in &state.snapshot.sessions {
        out.push(format!(
            "session {} · {} · {} tasks · {}",
            session.session_id,
            session.status,
            session.task_ids.len(),
            home_path(&session.cwd)
        ));
        for task in state.snapshot.tasks.iter().filter(|task| task.session_id == session.session_id) {
            out.push(format!(
                "  {} {} · {} · {:.0}s · trace {}",
                status_symbol(&task.status),
                task.task_id,
                task.status,
                task.elapsed_seconds,
                task.trace_id
            ));
            if !state.config.privacy {
                out.push(format!("    goal: {}", task.goal_preview));
            }
        }
    }
    if let Some(detail) = &state.detail {
        out.push(format!("selected timeline: {} · {}", detail.task_id, detail.status));
        for file in detail.files.iter().take(8) {
            out.push(format!("  {} {} +{} -{}", file_letter(&file.status), file.path, file.additions, file.deletions));
        }
    }
    if let Some(error) = &state.last_error {
        out.push(format!("warning: {error}"));
    }
    out.join("\n") + "\n"
}

impl LiveState {
    fn selected_task(&self) -> Option<&TaskNode> {
        self.snapshot.tasks.get(self.selected)
    }
}

fn setup_terminal() -> Result<Terminal<CrosstermBackend<io::Stdout>>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableBracketedPaste)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;
    terminal.clear()?;
    Ok(terminal)
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>) -> Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), DisableBracketedPaste, LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    Ok(())
}

fn spawn_input_reader(tx: mpsc::Sender<LiveEvent>) {
    std::thread::spawn(move || loop {
        match event::poll(Duration::from_millis(50)) {
            Ok(true) => match event::read() {
                Ok(Event::Key(key)) => {
                    if tx.blocking_send(LiveEvent::Input(key)).is_err() {
                        break;
                    }
                }
                Ok(_) => {}
                Err(_) => break,
            },
            Ok(false) => {}
            Err(_) => break,
        }
    });
}

fn text_at(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(sanitize_terminal)
        .unwrap_or_default()
}

fn usize_at(value: &Value, key: &str) -> usize {
    value.get(key).and_then(Value::as_u64).unwrap_or_default() as usize
}

fn file_from_value(value: &Value) -> FileChange {
    FileChange {
        path: text_at(value, "path"),
        status: text_at(value, "status"),
        additions: value.get("additions").and_then(Value::as_i64).unwrap_or_default(),
        deletions: value.get("deletions").and_then(Value::as_i64).unwrap_or_default(),
        patch: optional_text_at(value, "patch"),
        diff_ref: optional_text_at(value, "patch_ref"),
        binary: value.get("binary").and_then(Value::as_bool).unwrap_or(false),
    }
}

fn optional_text_at(value: &Value, key: &str) -> Option<String> {
    let text = text_at(value, key);
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

fn filter_from_status(status: &str) -> FocusFilter {
    let value = status.to_ascii_lowercase();
    if value.contains("all") {
        FocusFilter::All
    } else if value.contains("failed") {
        FocusFilter::Failed
    } else if value == "recent" {
        FocusFilter::Recent
    } else {
        FocusFilter::Running
    }
}

fn status_symbol(status: &str) -> &'static str {
    match status {
        "completed" => "✓",
        "failed" => "✗",
        "running" | "planning" | "queued" | "recovering" | "waiting_approval" => "▶",
        "cancelled" => "×",
        _ => "•",
    }
}

fn status_color(status: &str) -> Color {
    match status {
        "completed" => Color::Green,
        "failed" | "cancelled" => Color::Red,
        "running" | "planning" | "queued" | "recovering" | "waiting_approval" => Color::Yellow,
        _ => Color::White,
    }
}

fn file_letter(status: &str) -> &'static str {
    match status {
        "added" | "created" => "A",
        "deleted" | "removed" => "D",
        "renamed" => "R",
        "modified" => "M",
        _ => "M",
    }
}

fn attach_target_for_task(task: &TaskNode, state: &LiveState) -> AttachTarget {
    AttachTarget {
        session_id: task.session_id.clone(),
        task_id: task.task_id.clone(),
        cwd: if task.cwd.is_empty() {
            state.config.cwd.clone()
        } else {
            task.cwd.clone()
        },
        model: if task.model.is_empty() {
            state.config.model.clone()
        } else {
            task.model.clone()
        },
    }
}

fn truncate(value: &str, limit: usize) -> String {
    if value.chars().count() <= limit {
        return value.to_string();
    }
    let mut out: String = value.chars().take(limit.saturating_sub(1)).collect();
    out.push('…');
    out
}

fn short_id(value: &str) -> String {
    truncate(value, 14)
}

fn home_path(path: &str) -> String {
    if path.is_empty() {
        return "-".to_string();
    }
    if let Ok(home) = std::env::var("HOME") {
        if let Some(rest) = path.strip_prefix(&home) {
            return format!("~{rest}");
        }
    }
    path.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_live_snapshot() {
        let value = serde_json::json!({
            "seq": 1,
            "server_time": 10.0,
            "counts": {"sessions": 1, "tasks": 1, "running": 1, "failed": 0, "recent": 1},
            "sessions": [{"session_id": "symbiont-a", "status": "running", "task_ids": ["task-a"], "running_task_count": 1}],
            "tasks": [{"task_id": "task-a", "session_id": "symbiont-a", "status": "running", "trace_id": "tr-a", "goal_preview": "demo"}]
        });
        let snapshot = LiveSnapshot::from_value(&value);
        assert_eq!(snapshot.counts.running, 1);
        assert_eq!(snapshot.sessions[0].session_id, "symbiont-a");
        assert_eq!(snapshot.tasks[0].task_id, "task-a");
    }

    #[test]
    fn chat_open_request_builds_safe_symbiont_command() {
        let request = OpenChatRequest {
            api_url: "https://127.0.0.1:8586".to_string(),
            api_key: "secret".to_string(),
            cwd: "/tmp".to_string(),
            model: "@".to_string(),
            mode: "smart".to_string(),
            session_id: "session one".to_string(),
            task_id: "task'quoted".to_string(),
            open_mode: OpenMode::NewTerminal,
        };

        assert_eq!(
            request.symbiont_args(),
            vec![
                "--session",
                "session one",
                "--task",
                "task'quoted",
                "--watch",
                "--model",
                "@",
                "--mode",
                "smart"
            ]
        );
        assert_eq!(
            request.shell_command(),
            "symbiont '--session' 'session one' '--task' 'task'\\''quoted' '--watch' '--model' '@' '--mode' 'smart'"
        );
    }
}
