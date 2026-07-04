use std::io::{self, IsTerminal};
use std::time::Duration;

use anyhow::{Context, Result};
use crossterm::event::{
    self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyModifiers, MouseEvent, MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use tokio::sync::mpsc;
use tokio::time;

use crate::api::ApiClient;
use crate::events::{sanitize_terminal, RuntimeEvent};
use crate::reducer::{AppState, ModalState, UiMode};
use crate::render;

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub api_url: String,
    pub api_key: String,
    pub cwd: String,
    pub model: String,
    pub mode: String,
    pub session_id: Option<String>,
    pub task_id: Option<String>,
    pub attach: bool,
    pub watch: bool,
    pub use_alt_screen: bool,
    pub live_events: bool,
}

#[derive(Debug)]
enum InputEvent {
    Key(KeyEvent),
    Paste(String),
    Resize,
    Mouse(MouseEvent),
}

pub async fn run(config: AppConfig, initial_prompt: Option<String>) -> Result<()> {
    let api = ApiClient::new(config.api_url.clone(), config.api_key.clone())?;
    let session_id = config.session_id.unwrap_or_else(new_session_id);
    let mut state = AppState::new(
        session_id,
        config.api_url,
        config.cwd,
        config.model,
        UiMode::parse(&config.mode),
        config.live_events,
    );
    if !api.api_key_present() {
        state.last_error = Some("API key missing".to_string());
    }
    if let Some(task_id) = config.task_id.clone() {
        state.task_id = Some(task_id.clone());
        state.task_status = if config.watch { "watching".to_string() } else { "attached".to_string() };
        state.status = if config.attach { "attached to existing task".to_string() } else { "ready".to_string() };
        state.running = config.watch;
    }

    let interactive = config.use_alt_screen && io::stdout().is_terminal();
    if !interactive {
        return run_plain(api, state, initial_prompt).await;
    }
    run_interactive(api, state, initial_prompt, config.watch).await
}

async fn run_plain(api: ApiClient, mut state: AppState, initial_prompt: Option<String>) -> Result<()> {
    if let Some(task_id) = state.task_id.clone().filter(|_| initial_prompt.is_none()) {
        match api.fetch_timeline(&task_id).await {
            Ok(timeline) => state.apply(RuntimeEvent::Timeline(timeline)),
            Err(error) => state.apply(RuntimeEvent::Failed(format!("{error:#}"))),
        }
    } else if let Some(prompt) = initial_prompt {
        let (tx, mut rx) = mpsc::channel(512);
        submit_prompt(api, tx, &mut state, prompt);
        while let Some(event) = rx.recv().await {
            state.apply(event);
        }
    }
    print!("{}", render::render_plain(&state));
    Ok(())
}

async fn run_interactive(
    api: ApiClient,
    mut state: AppState,
    initial_prompt: Option<String>,
    watch: bool,
) -> Result<()> {
    let mut terminal = setup_terminal().context("failed to initialize terminal")?;
    let (api_tx, mut api_rx) = mpsc::channel(512);
    let (input_tx, mut input_rx) = mpsc::channel(128);
    spawn_input_reader(input_tx);

    if let Some(prompt) = initial_prompt {
        submit_prompt(api.clone(), api_tx.clone(), &mut state, prompt);
    } else if let Some(task_id) = state.task_id.clone() {
        spawn_timeline_fetch(api.clone(), api_tx.clone(), task_id);
    }

    let mut tick = time::interval(Duration::from_millis(66));
    let mut watch_poll = time::interval(Duration::from_secs(2));
    let mut dirty = true;
    loop {
        tokio::select! {
            Some(event) = api_rx.recv() => {
                state.apply(event);
                dirty = true;
            }
            Some(input) = input_rx.recv() => {
                handle_input(input, &api, &api_tx, &mut state);
                dirty = true;
                if state.should_quit {
                    break;
                }
            }
            _ = watch_poll.tick(), if watch || state.running => {
                if let Some(task_id) = state.task_id.clone() {
                    if !is_terminal_task_status(&state.task_status) {
                        spawn_timeline_fetch(api.clone(), api_tx.clone(), task_id);
                    } else {
                        state.running = false;
                    }
                }
            }
            _ = tick.tick() => {
                if dirty {
                    terminal.draw(|frame| render::draw(frame, &mut state))?;
                    dirty = false;
                }
            }
        }
    }
    restore_terminal(&mut terminal)?;
    Ok(())
}

fn submit_prompt(api: ApiClient, tx: mpsc::Sender<RuntimeEvent>, state: &mut AppState, prompt: String) {
    let prompt = sanitize_terminal(&prompt);
    state.start_turn(prompt.clone());
    let model = state.model.clone();
    let cwd = state.cwd.clone();
    let session_id = state.session_id.clone();
    let live_events = state.live_events;
    tokio::spawn(async move {
        let task_id = match api
            .stream_query(&prompt, &model, &cwd, &session_id, tx.clone())
            .await
        {
            Ok(task_id) => task_id,
            Err(error) => {
                let _ = tx.send(RuntimeEvent::Failed(format!("{error:#}"))).await;
                return;
            }
        };
        if let Some(task_id) = task_id {
            if let Ok(timeline) = api.fetch_timeline(&task_id).await {
                let _ = tx.send(RuntimeEvent::Timeline(timeline)).await;
            }
            if live_events {
                if let Ok(events) = api.fetch_events(&task_id, 0).await {
                    let _ = tx.send(RuntimeEvent::TaskEvents(events)).await;
                }
            }
        }
        let _ = tx.send(RuntimeEvent::Finished).await;
    });
}

fn handle_input(
    input: InputEvent,
    api: &ApiClient,
    api_tx: &mpsc::Sender<RuntimeEvent>,
    state: &mut AppState,
) {
    match input {
        InputEvent::Resize => {}
        InputEvent::Paste(text) => state.input.push_str(&sanitize_terminal(&text)),
        InputEvent::Mouse(mouse) => match mouse.kind {
            MouseEventKind::ScrollUp => scroll_up(state, 4),
            MouseEventKind::ScrollDown => scroll_down(state, 4),
            _ => {}
        },
        InputEvent::Key(key) => match key.code {
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                state.should_quit = true;
            }
            KeyCode::Char('j') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                state.input.push('\n');
            }
            KeyCode::PageUp => state.viewport.page_up(),
            KeyCode::PageDown => state.viewport.page_down(),
            KeyCode::Home if state.input.is_empty() => state.viewport.go_top(),
            KeyCode::End if state.input.is_empty() => state.viewport.go_bottom(),
            KeyCode::Up if state.input.is_empty() => scroll_up(state, 3),
            KeyCode::Down if state.input.is_empty() => scroll_down(state, 3),
            KeyCode::Esc => {
                if state.modal.is_some() {
                    state.modal = None;
                } else if state.input.trim().is_empty() {
                    state.should_quit = true;
                } else {
                    state.input.clear();
                }
            }
            KeyCode::Enter => {
                let prompt = state.input.trim().to_string();
                state.input.clear();
                if prompt.is_empty() {
                    state.viewport.go_bottom();
                    return;
                }
                if prompt.starts_with('/') {
                    handle_command(&prompt, api, api_tx, state);
                } else if !state.running {
                    submit_prompt(api.clone(), api_tx.clone(), state, prompt);
                } else {
                    state.status = "task running in background; use Ctrl+C or /exit to detach UI only".to_string();
                }
            }
            KeyCode::Char('q') if state.modal.is_some() && state.input.trim().is_empty() => state.modal = None,
            KeyCode::Char('q') if state.input.trim().is_empty() => state.should_quit = true,
            KeyCode::Char('?') if state.input.is_empty() => show_help_modal(state),
            KeyCode::Backspace => {
                state.input.pop();
            }
            KeyCode::Tab => {
                state.mode = match state.mode {
                    UiMode::Smart => UiMode::Compact,
                    UiMode::Compact => UiMode::Verbose,
                    UiMode::Verbose => UiMode::Raw,
                    UiMode::Raw => UiMode::Smart,
                };
            }
            KeyCode::Char('d') if state.input.is_empty() => {
                state.full_diff = !state.full_diff;
            }
            KeyCode::Char('p') if state.input.is_empty() => state.collapsed.prompt = !state.collapsed.prompt,
            KeyCode::Char('a') if state.input.is_empty() => state.collapsed.answer = !state.collapsed.answer,
            KeyCode::Char('f') if state.input.is_empty() => state.collapsed.files = !state.collapsed.files,
            KeyCode::Char('c') if state.input.is_empty() => state.collapsed.commands = !state.collapsed.commands,
            KeyCode::Char('e') if state.input.is_empty() => state.collapsed.events = !state.collapsed.events,
            KeyCode::Char('r') if state.input.is_empty() => state.collapsed.raw = !state.collapsed.raw,
            KeyCode::Char(ch) => {
                state.input.push(ch);
            }
            _ => {}
        },
    }
}

fn scroll_up(state: &mut AppState, amount: usize) {
    state.viewport.scroll_up(amount);
}

fn scroll_down(state: &mut AppState, amount: usize) {
    state.viewport.scroll_down(amount);
}

fn handle_command(
    command: &str,
    api: &ApiClient,
    api_tx: &mpsc::Sender<RuntimeEvent>,
    state: &mut AppState,
) {
    let mut parts = command.split_whitespace();
    let name = parts.next().unwrap_or("");
    match name {
        "/exit" | "/quit" | "/q" => state.should_quit = true,
        "/clear" => {
            let session_id = state.session_id.clone();
            let api_url = state.api_url.clone();
            let cwd = state.cwd.clone();
            let model = state.model.clone();
            let mode = state.mode;
            let live = state.live_events;
            *state = AppState::new(session_id, api_url, cwd, model, mode, live);
        }
        "/compact" => state.mode = UiMode::Compact,
        "/verbose" | "/watch" => state.mode = UiMode::Verbose,
        "/raw" => state.mode = UiMode::Raw,
        "/smart" => state.mode = UiMode::Smart,
        "/model" => {
            if let Some(model) = parts.next() {
                state.model = model.to_string();
            } else {
                state.status = "usage: /model <name>".to_string();
            }
        }
        "/diff" => state.full_diff = !state.full_diff,
        "/collapse" => set_collapse(parts.next(), state, true),
        "/expand" => set_collapse(parts.next(), state, false),
        "/toggle" => toggle_section(parts.next(), state),
        "/open" | "/watch-task" => {
            if let Some(task_id) = parts.next() {
                spawn_timeline_fetch(api.clone(), api_tx.clone(), task_id.to_string());
            } else {
                state.status = "usage: /open <task_id>".to_string();
            }
        }
        "/help" => {
            show_help_modal(state);
        }
        _ => state.status = format!("unknown command: {command}"),
    }
}

fn show_help_modal(state: &mut AppState) {
    state.modal = Some(ModalState {
        title: "Symbiont UX 1.5".to_string(),
        body: vec![
            "Enter sends a prompt. Ctrl+J inserts a newline.".to_string(),
            "PgUp/PgDn, mouse wheel, Home and End scroll the conversation feed.".to_string(),
            "p a f c e r collapse or expand prompts, tokens, files, commands, events and raw stream.".to_string(),
            "d toggles folded/full diffs. Tab cycles smart, compact, verbose and raw modes.".to_string(),
            "q, Esc, Ctrl+C or /exit detach the terminal UI only; tasks keep running in the background.".to_string(),
            "/open <task_id> attaches this conversation to an existing task timeline.".to_string(),
        ],
    });
}

fn set_collapse(section: Option<&str>, state: &mut AppState, value: bool) {
    match section.unwrap_or("") {
        "all" => {
            state.collapsed.prompt = value;
            state.collapsed.answer = value;
            state.collapsed.files = value;
            state.collapsed.commands = value;
            state.collapsed.events = value;
            state.collapsed.raw = value;
        }
        "prompt" => state.collapsed.prompt = value,
        "answer" | "tokens" | "stream" => state.collapsed.answer = value,
        "files" | "diffs" | "diff" => state.collapsed.files = value,
        "commands" | "cmd" => state.collapsed.commands = value,
        "events" => state.collapsed.events = value,
        "raw" => state.collapsed.raw = value,
        _ => state.status = "usage: /collapse <prompt|answer|files|commands|events|raw|all>".to_string(),
    }
}

fn toggle_section(section: Option<&str>, state: &mut AppState) {
    match section.unwrap_or("") {
        "prompt" => state.collapsed.prompt = !state.collapsed.prompt,
        "answer" | "tokens" | "stream" => state.collapsed.answer = !state.collapsed.answer,
        "files" | "diffs" | "diff" => state.collapsed.files = !state.collapsed.files,
        "commands" | "cmd" => state.collapsed.commands = !state.collapsed.commands,
        "events" => state.collapsed.events = !state.collapsed.events,
        "raw" => state.collapsed.raw = !state.collapsed.raw,
        _ => state.status = "usage: /toggle <prompt|answer|files|commands|events|raw>".to_string(),
    }
}

fn spawn_timeline_fetch(api: ApiClient, tx: mpsc::Sender<RuntimeEvent>, task_id: String) {
    tokio::spawn(async move {
        match api.fetch_timeline(&task_id).await {
            Ok(timeline) => {
                let _ = tx.send(RuntimeEvent::Timeline(timeline)).await;
            }
            Err(error) => {
                let _ = tx.send(RuntimeEvent::Failed(format!("{error:#}"))).await;
            }
        }
    });
}

fn is_terminal_task_status(status: &str) -> bool {
    matches!(status, "completed" | "failed" | "cancelled")
}

fn spawn_input_reader(tx: mpsc::Sender<InputEvent>) {
    std::thread::spawn(move || loop {
        match event::poll(Duration::from_millis(50)) {
            Ok(true) => match event::read() {
                Ok(Event::Key(key)) => {
                    if tx.blocking_send(InputEvent::Key(key)).is_err() {
                        break;
                    }
                }
                Ok(Event::Paste(text)) => {
                    if tx.blocking_send(InputEvent::Paste(text)).is_err() {
                        break;
                    }
                }
                Ok(Event::Resize(_, _)) => {
                    if tx.blocking_send(InputEvent::Resize).is_err() {
                        break;
                    }
                }
                Ok(Event::Mouse(mouse)) => {
                    if tx.blocking_send(InputEvent::Mouse(mouse)).is_err() {
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

fn setup_terminal() -> Result<Terminal<CrosstermBackend<io::Stdout>>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableBracketedPaste, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;
    terminal.clear()?;
    Ok(terminal)
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>) -> Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), DisableBracketedPaste, DisableMouseCapture, LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    Ok(())
}

fn new_session_id() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    format!("symbiont-{nanos:x}")
}
