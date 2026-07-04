use serde_json::Value;

#[derive(Debug, Clone)]
pub enum RuntimeEvent {
    Gateway(GatewayEvent),
    Timeline(TimelineSnapshot),
    TaskEvents(Vec<EventLine>),
    Finished,
    Failed(String),
}

#[derive(Debug, Clone)]
pub enum GatewayEvent {
    Agentic { task_id: String, trace_id: String },
    Status { kind: String, text: String },
    AnswerDelta(String),
    Done,
    Raw { kind: String, data: String },
}

#[derive(Debug, Clone, Default)]
pub struct TimelineSnapshot {
    pub task_id: String,
    pub trace_id: String,
    pub status: String,
    pub elapsed_seconds: f64,
    pub files: Vec<FileChange>,
    pub commands: Vec<CommandRun>,
    pub events: Vec<EventLine>,
    pub terminal: bool,
}

#[derive(Debug, Clone, Default)]
pub struct FileChange {
    pub path: String,
    pub status: String,
    pub additions: i64,
    pub deletions: i64,
    pub patch: Option<String>,
    pub diff_ref: Option<String>,
    pub binary: bool,
}

#[derive(Debug, Clone, Default)]
pub struct CommandRun {
    pub id: String,
    pub command: String,
    pub cwd: String,
    pub status: String,
    pub exit_code: Option<i64>,
    pub duration_seconds: Option<f64>,
    pub stdout_preview: Option<String>,
    pub stderr_preview: Option<String>,
    pub stdout_ref: Option<String>,
    pub stderr_ref: Option<String>,
    pub diff_ref: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct EventLine {
    pub seq: Option<u64>,
    pub kind: String,
    pub title: String,
    pub summary: String,
    pub status: String,
}

pub fn gateway_event_from_sse(kind: &str, data: &str) -> GatewayEvent {
    if data == "[DONE]" {
        return GatewayEvent::Done;
    }
    match kind {
        "agentic" => {
            let parsed = serde_json::from_str::<Value>(data).unwrap_or(Value::Null);
            let task_id = text_at(&parsed, "task_id")
                .or_else(|| text_at(&parsed, "id"))
                .unwrap_or_default();
            let trace_id = text_at(&parsed, "trace_id").unwrap_or_default();
            if task_id.is_empty() {
                GatewayEvent::Raw {
                    kind: kind.to_string(),
                    data: sanitize_terminal(data),
                }
            } else {
                GatewayEvent::Agentic { task_id, trace_id }
            }
        }
        "status_start" | "status" | "status_done" => GatewayEvent::Status {
            kind: kind.to_string(),
            text: sanitize_terminal(data),
        },
        "" | "message" => GatewayEvent::AnswerDelta(sanitize_terminal(data)),
        other => GatewayEvent::Raw {
            kind: other.to_string(),
            data: sanitize_terminal(data),
        },
    }
}

impl TimelineSnapshot {
    pub fn from_value(value: &Value) -> Self {
        let task = value.get("task").unwrap_or(&Value::Null);
        let mut snapshot = TimelineSnapshot {
            task_id: text_at(task, "id").unwrap_or_default(),
            trace_id: text_at(task, "trace_id").unwrap_or_default(),
            status: text_at(task, "status").unwrap_or_else(|| "unknown".to_string()),
            elapsed_seconds: number_at(task, "elapsed_seconds").unwrap_or_default(),
            terminal: bool_at(task, "terminal").unwrap_or(false),
            ..TimelineSnapshot::default()
        };
        if let Some(files) = value.get("file_activity").and_then(Value::as_array) {
            snapshot.files = files.iter().map(file_from_value).collect();
        }
        if let Some(commands) = value.get("command_runs").and_then(Value::as_array) {
            snapshot.commands = commands.iter().map(command_from_value).collect();
        }
        if let Some(events) = value.get("events").and_then(Value::as_array) {
            snapshot.events = events.iter().map(event_from_value).collect();
        }
        snapshot
    }
}

pub fn events_from_feed(value: &Value) -> Vec<EventLine> {
    value
        .get("events")
        .and_then(Value::as_array)
        .map(|events| events.iter().map(event_from_value).collect())
        .unwrap_or_default()
}

fn file_from_value(value: &Value) -> FileChange {
    FileChange {
        path: text_at(value, "path").unwrap_or_default(),
        status: text_at(value, "status").unwrap_or_else(|| "changed".to_string()),
        additions: integer_at(value, "additions").unwrap_or_default(),
        deletions: integer_at(value, "deletions").unwrap_or_default(),
        patch: text_at(value, "patch").filter(|item| !item.is_empty()),
        diff_ref: text_at(value, "diff_ref").filter(|item| !item.is_empty()),
        binary: bool_at(value, "binary").unwrap_or(false),
    }
}

fn command_from_value(value: &Value) -> CommandRun {
    let output = value.get("output").unwrap_or(&Value::Null);
    CommandRun {
        id: text_at(value, "id").unwrap_or_default(),
        command: text_at(value, "command").unwrap_or_default(),
        cwd: text_at(value, "cwd").unwrap_or_default(),
        status: text_at(value, "status").unwrap_or_else(|| "unknown".to_string()),
        exit_code: integer_at(value, "exit_code"),
        duration_seconds: number_at(value, "duration_seconds"),
        stdout_preview: first_text_at(
            &[output, value],
            &["stdout_preview", "stdout_tail", "stdout", "out"],
        ),
        stderr_preview: first_text_at(
            &[output, value],
            &["stderr_preview", "stderr_tail", "stderr", "err"],
        ),
        stdout_ref: first_text_at(&[output, value], &["stdout_ref"]),
        stderr_ref: first_text_at(&[output, value], &["stderr_ref"]),
        diff_ref: first_text_at(&[output, value], &["diff_ref"]),
    }
}

fn event_from_value(value: &Value) -> EventLine {
    EventLine {
        seq: value.get("seq").and_then(Value::as_u64),
        kind: text_at(value, "kind")
            .or_else(|| text_at(value, "event_type"))
            .unwrap_or_else(|| "event".to_string()),
        title: text_at(value, "title").unwrap_or_default(),
        summary: text_at(value, "summary").unwrap_or_default(),
        status: text_at(value, "status").unwrap_or_default(),
    }
}

fn text_at(value: &Value, key: &str) -> Option<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(sanitize_terminal)
}

fn first_text_at(values: &[&Value], keys: &[&str]) -> Option<String> {
    values
        .iter()
        .flat_map(|value| keys.iter().filter_map(move |key| text_at(value, key)))
        .find(|item| !item.trim().is_empty())
}

fn number_at(value: &Value, key: &str) -> Option<f64> {
    value.get(key).and_then(Value::as_f64)
}

fn integer_at(value: &Value, key: &str) -> Option<i64> {
    value.get(key).and_then(Value::as_i64)
}

fn bool_at(value: &Value, key: &str) -> Option<bool> {
    value.get(key).and_then(Value::as_bool)
}

pub fn sanitize_terminal(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\u{1b}' {
            match chars.peek().copied() {
                Some(']') => {
                    chars.next();
                    while let Some(next) = chars.next() {
                        if next == '\u{7}' {
                            break;
                        }
                        if next == '\u{1b}' && matches!(chars.peek(), Some('\\')) {
                            chars.next();
                            break;
                        }
                    }
                }
                Some('[') => {
                    chars.next();
                    while let Some(next) = chars.next() {
                        if ('@'..='~').contains(&next) {
                            break;
                        }
                    }
                }
                _ => {}
            }
            continue;
        }
        if ch.is_control() && ch != '\n' && ch != '\t' && ch != '\r' {
            continue;
        }
        out.push(ch);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizer_removes_ansi_and_osc() {
        let raw = "ok\u{1b}[31mred\u{1b}[0m\u{1b}]52;c;bad\u{7}done";
        assert_eq!(sanitize_terminal(raw), "okreddone");
    }

    #[test]
    fn parses_agentic_sse() {
        let event = gateway_event_from_sse("agentic", r#"{"task_id":"task_1","trace_id":"tr_1"}"#);
        match event {
            GatewayEvent::Agentic { task_id, trace_id } => {
                assert_eq!(task_id, "task_1");
                assert_eq!(trace_id, "tr_1");
            }
            other => panic!("unexpected event: {other:?}"),
        }
    }

    #[test]
    fn timeline_parses_command_output_previews() {
        let value = serde_json::json!({
            "task": {"id": "task_1", "status": "running"},
            "command_runs": [{
                "id": "cmd_1",
                "command": "pytest -q",
                "status": "completed",
                "output": {
                    "stdout_preview": "3 passed",
                    "stderr_ref": "artifact://stderr"
                }
            }]
        });
        let snapshot = TimelineSnapshot::from_value(&value);
        assert_eq!(snapshot.commands[0].stdout_preview.as_deref(), Some("3 passed"));
        assert_eq!(snapshot.commands[0].stderr_ref.as_deref(), Some("artifact://stderr"));
    }
}
