use std::collections::HashSet;
use std::time::Instant;

use crate::events::{CommandRun, EventLine, FileChange, GatewayEvent, RuntimeEvent, TimelineSnapshot};
use crate::viewport::ViewportState;

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum UiMode {
    Smart,
    Compact,
    Verbose,
    Raw,
}

impl UiMode {
    pub fn parse(value: &str) -> Self {
        match value.to_ascii_lowercase().as_str() {
            "compact" => UiMode::Compact,
            "verbose" | "watch" | "debug" => UiMode::Verbose,
            "raw" => UiMode::Raw,
            _ => UiMode::Smart,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            UiMode::Smart => "smart",
            UiMode::Compact => "compact",
            UiMode::Verbose => "verbose",
            UiMode::Raw => "raw",
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct CollapsedSections {
    pub prompt: bool,
    pub answer: bool,
    pub files: bool,
    pub commands: bool,
    pub events: bool,
    pub raw: bool,
}

#[derive(Debug, Clone, Default)]
pub struct ModalState {
    pub title: String,
    pub body: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct AppState {
    pub session_id: String,
    pub api_url: String,
    pub cwd: String,
    pub model: String,
    pub mode: UiMode,
    pub input: String,
    pub prompt_history: Vec<String>,
    pub last_prompt: String,
    pub answer: String,
    pub status: String,
    pub task_id: Option<String>,
    pub trace_id: Option<String>,
    pub task_status: String,
    pub task_elapsed_seconds: f64,
    pub files: Vec<FileChange>,
    pub commands: Vec<CommandRun>,
    pub events: Vec<EventLine>,
    pub raw_events: Vec<String>,
    pub answer_delta_count: usize,
    pub running: bool,
    pub turn_started: Option<Instant>,
    pub last_error: Option<String>,
    pub full_diff: bool,
    pub viewport: ViewportState,
    pub collapsed: CollapsedSections,
    pub modal: Option<ModalState>,
    pub event_cursor: u64,
    pub live_events: bool,
    pub should_quit: bool,
}

impl AppState {
    pub fn new(
        session_id: String,
        api_url: String,
        cwd: String,
        model: String,
        mode: UiMode,
        live_events: bool,
    ) -> Self {
        Self {
            session_id,
            api_url,
            cwd,
            model,
            mode,
            input: String::new(),
            prompt_history: Vec::new(),
            last_prompt: String::new(),
            answer: String::new(),
            status: "ready".to_string(),
            task_id: None,
            trace_id: None,
            task_status: "idle".to_string(),
            task_elapsed_seconds: 0.0,
            files: Vec::new(),
            commands: Vec::new(),
            events: Vec::new(),
            raw_events: Vec::new(),
            answer_delta_count: 0,
            running: false,
            turn_started: None,
            last_error: None,
            full_diff: false,
            viewport: ViewportState::default(),
            collapsed: CollapsedSections::default(),
            modal: None,
            event_cursor: 0,
            live_events,
            should_quit: false,
        }
    }

    pub fn start_turn(&mut self, prompt: String) {
        self.last_prompt = prompt.clone();
        self.prompt_history.push(prompt);
        self.answer.clear();
        self.status = "sending prompt".to_string();
        self.task_id = None;
        self.trace_id = None;
        self.task_status = "running".to_string();
        self.task_elapsed_seconds = 0.0;
        self.files.clear();
        self.commands.clear();
        self.events.clear();
        self.raw_events.clear();
        self.answer_delta_count = 0;
        self.viewport.go_bottom();
        self.running = true;
        self.turn_started = Some(Instant::now());
        self.last_error = None;
        self.event_cursor = 0;
    }

    pub fn apply(&mut self, event: RuntimeEvent) {
        match event {
            RuntimeEvent::Gateway(event) => self.apply_gateway(event),
            RuntimeEvent::Timeline(snapshot) => self.apply_timeline(snapshot),
            RuntimeEvent::TaskEvents(events) => {
                self.append_events(events);
            }
            RuntimeEvent::Finished => {
                self.running = false;
                if self.status != "failed" {
                    self.status = "ready".to_string();
                }
            }
            RuntimeEvent::Failed(error) => {
                self.running = false;
                self.status = "failed".to_string();
                self.last_error = Some(error);
            }
        }
    }

    pub fn apply_gateway(&mut self, event: GatewayEvent) {
        match event {
            GatewayEvent::Agentic { task_id, trace_id } => {
                self.task_id = Some(task_id.clone());
                self.trace_id = Some(trace_id.clone());
                self.task_status = "running".to_string();
                self.events.push(EventLine {
                    seq: None,
                    kind: "task.started".to_string(),
                    title: task_id,
                    summary: trace_id,
                    status: "running".to_string(),
                });
            }
            GatewayEvent::Status { kind, text } => {
                self.status = text.clone();
                self.events.push(EventLine {
                    seq: None,
                    kind,
                    title: "status".to_string(),
                    summary: text,
                    status: "running".to_string(),
                });
                self.events = keep_tail(std::mem::take(&mut self.events), 300);
            }
            GatewayEvent::AnswerDelta(text) => {
                if text == "\\n" {
                    self.answer.push('\n');
                } else {
                    self.answer.push_str(&text);
                }
                self.answer_delta_count += 1;
            }
            GatewayEvent::Done => {
                self.running = false;
            }
            GatewayEvent::Raw { kind, data } => {
                self.raw_events.push(format!("{kind}: {data}"));
                self.raw_events = keep_tail(std::mem::take(&mut self.raw_events), 200);
            }
        }
    }

    pub fn apply_timeline(&mut self, snapshot: TimelineSnapshot) {
        if !snapshot.task_id.is_empty() {
            self.task_id = Some(snapshot.task_id);
        }
        if !snapshot.trace_id.is_empty() {
            self.trace_id = Some(snapshot.trace_id);
        }
        self.task_status = snapshot.status;
        self.task_elapsed_seconds = snapshot.elapsed_seconds;
        self.files = snapshot.files;
        self.commands = snapshot.commands;
        self.append_events(snapshot.events);
        if snapshot.terminal {
            self.running = false;
        }
    }

    fn append_events(&mut self, events: Vec<EventLine>) {
        let mut seen_seq = self.events.iter().filter_map(|event| event.seq).collect::<HashSet<_>>();
        for event in events {
            if let Some(seq) = event.seq {
                self.event_cursor = self.event_cursor.max(seq);
                if !seen_seq.insert(seq) {
                    continue;
                }
            }
            self.events.push(event);
        }
        self.events = keep_tail(std::mem::take(&mut self.events), 300);
    }

    pub fn elapsed_seconds(&self) -> f64 {
        if self.task_elapsed_seconds > 0.0 {
            return self.task_elapsed_seconds;
        }
        self.turn_started
            .map(|started| started.elapsed().as_secs_f64())
            .unwrap_or_default()
    }
}

fn keep_tail<T>(mut items: Vec<T>, limit: usize) -> Vec<T> {
    if items.len() > limit {
        items.drain(..items.len() - limit);
    }
    items
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reducer_applies_answer_and_task() {
        let mut state = AppState::new(
            "s".to_string(),
            "https://127.0.0.1:8586".to_string(),
            "/tmp".to_string(),
            "@".to_string(),
            UiMode::Smart,
            true,
        );
        state.start_turn("ola".to_string());
        state.apply_gateway(GatewayEvent::Agentic {
            task_id: "task_1".to_string(),
            trace_id: "trace_1".to_string(),
        });
        state.apply_gateway(GatewayEvent::AnswerDelta("OK".to_string()));
        assert_eq!(state.task_id.as_deref(), Some("task_1"));
        assert_eq!(state.answer, "OK");
    }
}
