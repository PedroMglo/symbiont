use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};

use crate::events::{CommandRun, EventLine, FileChange};
use crate::reducer::{AppState, UiMode};
use crate::render::markdown_blocks::{parse_markdown_blocks, split_inline_code, InlineSpan, MarkdownBlock};

use super::{body_line, body_spans, card_footer, card_header, collapsed_line, home_path, truncate, wrap_text};

#[derive(Debug, Clone)]
pub struct ChatFeed {
    pub items: Vec<ChatItem>,
}

#[derive(Debug, Clone)]
pub struct ChatItem {
    pub id: String,
    pub title: String,
    pub meta: String,
    pub collapsed: bool,
    pub payload: ChatPayload,
}

#[derive(Debug, Clone)]
pub enum ChatPayload {
    System(Vec<String>),
    UserPrompt(String),
    AssistantText { text: String, chunks: usize },
    Code { language: Option<String>, code: String },
    ShellSnippet { language: Option<String>, script: String },
    Command(CommandRun),
    Diff(FileChange),
    Tool(EventLine),
    Error(String),
    Raw(String),
}

pub fn build_chat_feed(state: &AppState) -> ChatFeed {
    let mut items = Vec::new();
    if is_empty_conversation(state) {
        items.push(ChatItem {
            id: "welcome".to_string(),
            title: "Symbiont is ready".to_string(),
            meta: "terminal chat".to_string(),
            collapsed: false,
            payload: ChatPayload::System(vec![
                format!("model {} · mode {} · cwd {}", state.model, state.mode.label(), home_path(&state.cwd)),
                "Type a prompt below. Closing this terminal detaches only the UI; tasks keep running.".to_string(),
                "Use symbiont -l to inspect running sessions and tasks.".to_string(),
            ]),
        });
        return ChatFeed { items };
    }

    for (index, prompt) in state.prompt_history.iter().enumerate() {
        items.push(ChatItem {
            id: format!("prompt-{index}"),
            title: "You".to_string(),
            meta: format!("turn {}", index + 1),
            collapsed: state.collapsed.prompt,
            payload: ChatPayload::UserPrompt(prompt.clone()),
        });
    }

    if state.running && state.answer.trim().is_empty() {
        items.push(ChatItem {
            id: "assistant-waiting".to_string(),
            title: "Assistant".to_string(),
            meta: "waiting for tokens".to_string(),
            collapsed: state.collapsed.answer,
            payload: ChatPayload::AssistantText {
                text: "stream open; waiting for model tokens...".to_string(),
                chunks: state.answer_delta_count,
            },
        });
    } else if !state.answer.trim().is_empty() {
        for (index, block) in parse_markdown_blocks(state.answer.trim()).into_iter().enumerate() {
            match block {
                MarkdownBlock::Text(text) => items.push(ChatItem {
                    id: format!("answer-text-{index}"),
                    title: "Assistant".to_string(),
                    meta: format!("{} chunks · {} chars", state.answer_delta_count, text.chars().count()),
                    collapsed: state.collapsed.answer,
                    payload: ChatPayload::AssistantText {
                        text,
                        chunks: state.answer_delta_count,
                    },
                }),
                MarkdownBlock::Code { language, code } => items.push(ChatItem {
                    id: format!("answer-code-{index}"),
                    title: "Code".to_string(),
                    meta: language.clone().unwrap_or_else(|| "text".to_string()),
                    collapsed: state.collapsed.answer,
                    payload: ChatPayload::Code { language, code },
                }),
                MarkdownBlock::Shell { language, script } => items.push(ChatItem {
                    id: format!("answer-shell-{index}"),
                    title: "Shell snippet".to_string(),
                    meta: language.clone().unwrap_or_else(|| "shell".to_string()),
                    collapsed: state.collapsed.answer,
                    payload: ChatPayload::ShellSnippet { language, script },
                }),
            }
        }
    }

    for file in &state.files {
        items.push(ChatItem {
            id: format!("file-{}", file.path),
            title: file.path.clone(),
            meta: format!("+{} -{}", file.additions, file.deletions),
            collapsed: state.collapsed.files,
            payload: ChatPayload::Diff(file.clone()),
        });
    }

    for command in &state.commands {
        items.push(ChatItem {
            id: format!("command-{}", command.id),
            title: "Command".to_string(),
            meta: command.status.clone(),
            collapsed: state.collapsed.commands || state.mode == UiMode::Compact,
            payload: ChatPayload::Command(command.clone()),
        });
    }

    let events: Vec<&EventLine> = if state.mode == UiMode::Smart {
        state.events.iter().rev().take(20).collect()
    } else {
        state.events.iter().rev().collect()
    };
    for event in events.into_iter().rev() {
        items.push(ChatItem {
            id: format!("event-{}-{}", event.seq.unwrap_or_default(), event.kind),
            title: event.kind.clone(),
            meta: event.status.clone(),
            collapsed: state.collapsed.events,
            payload: ChatPayload::Tool(event.clone()),
        });
    }

    if state.mode == UiMode::Raw {
        for (index, raw) in state.raw_events.iter().enumerate() {
            items.push(ChatItem {
                id: format!("raw-{index}"),
                title: "Raw stream".to_string(),
                meta: "sse".to_string(),
                collapsed: state.collapsed.raw,
                payload: ChatPayload::Raw(raw.clone()),
            });
        }
    }

    if let Some(error) = &state.last_error {
        items.push(ChatItem {
            id: "last-error".to_string(),
            title: "Error".to_string(),
            meta: "runtime".to_string(),
            collapsed: false,
            payload: ChatPayload::Error(error.clone()),
        });
    }

    ChatFeed { items }
}

pub fn render_chat_feed(state: &AppState, width: usize) -> Vec<Line<'static>> {
    let feed = build_chat_feed(state);
    let mut lines = Vec::new();
    for item in feed.items {
        render_item(&mut lines, &item, state.full_diff, width);
    }
    lines
}

fn render_item(lines: &mut Vec<Line<'static>>, item: &ChatItem, full_diff: bool, width: usize) {
    let _ = &item.id;
    match &item.payload {
        ChatPayload::System(body) => render_system(lines, item, body, width),
        ChatPayload::UserPrompt(prompt) => render_user(lines, item, prompt, width),
        ChatPayload::AssistantText { text, chunks } => render_assistant_text(lines, item, text, *chunks, width),
        ChatPayload::Code { language, code } => {
            super::code::render_code_card(lines, &item.title, language.as_deref(), code, item.collapsed, width)
        }
        ChatPayload::ShellSnippet { language, script } => {
            super::shell::render_shell_snippet(lines, language.as_deref(), script, item.collapsed, width)
        }
        ChatPayload::Command(command) => {
            super::shell::render_command_card(lines, command, item.collapsed, width)
        }
        ChatPayload::Diff(file) => {
            super::diff::render_diff_card(lines, file, item.collapsed, full_diff, width)
        }
        ChatPayload::Tool(event) => super::tool::render_tool_card(lines, event, item.collapsed, width),
        ChatPayload::Error(error) => super::error::render_error_card(lines, error, width),
        ChatPayload::Raw(raw) => render_raw(lines, item, raw, width),
    }
}

fn render_system(lines: &mut Vec<Line<'static>>, item: &ChatItem, body: &[String], width: usize) {
    card_header(lines, Color::Cyan, "i", &item.title, &item.meta, false, width);
    for entry in body {
        lines.push(body_line(truncate(entry, width.saturating_sub(4))));
    }
    card_footer(lines);
}

fn render_user(lines: &mut Vec<Line<'static>>, item: &ChatItem, prompt: &str, width: usize) {
    card_header(lines, Color::Yellow, ">", &item.title, &item.meta, item.collapsed, width);
    if item.collapsed {
        collapsed_line(lines, &truncate(prompt, width.saturating_sub(8)));
        return;
    }
    for wrapped in wrap_text(prompt, width.saturating_sub(4)) {
        lines.push(body_line(wrapped));
    }
    card_footer(lines);
}

fn render_assistant_text(
    lines: &mut Vec<Line<'static>>,
    item: &ChatItem,
    text: &str,
    chunks: usize,
    width: usize,
) {
    let meta = if chunks == 0 {
        item.meta.clone()
    } else {
        format!("{chunks} chunks · {} chars", text.chars().count())
    };
    card_header(lines, Color::Cyan, "●", &item.title, &meta, item.collapsed, width);
    if item.collapsed {
        collapsed_line(lines, &truncate(text, width.saturating_sub(8)));
        return;
    }
    for wrapped in wrap_text(text, width.saturating_sub(4)) {
        lines.push(render_inline_text(&wrapped));
    }
    card_footer(lines);
}

fn render_inline_text(line: &str) -> Line<'static> {
    let mut spans = Vec::new();
    for span in split_inline_code(line) {
        match span {
            InlineSpan::Text(text) => spans.push(Span::raw(text)),
            InlineSpan::Code(code) => spans.push(Span::styled(
                code,
                Style::default()
                    .fg(Color::Yellow)
                    .bg(Color::DarkGray)
                    .add_modifier(Modifier::BOLD),
            )),
        }
    }
    body_spans(spans)
}

fn render_raw(lines: &mut Vec<Line<'static>>, item: &ChatItem, raw: &str, width: usize) {
    card_header(lines, Color::Gray, "≡", &item.title, &item.meta, item.collapsed, width);
    if item.collapsed {
        collapsed_line(lines, "raw stream folded");
        return;
    }
    for wrapped in wrap_text(raw, width.saturating_sub(4)) {
        lines.push(body_line(wrapped));
    }
    card_footer(lines);
}

fn is_empty_conversation(state: &AppState) -> bool {
    state.prompt_history.is_empty()
        && state.answer.trim().is_empty()
        && state.task_id.is_none()
        && state.files.is_empty()
        && state.commands.is_empty()
        && state.events.is_empty()
        && state.raw_events.is_empty()
        && state.last_error.is_none()
        && !state.running
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_state_builds_welcome_card() {
        let state = AppState::new(
            "session".to_string(),
            "https://127.0.0.1:8586".to_string(),
            "/tmp".to_string(),
            "@".to_string(),
            UiMode::Smart,
            true,
        );
        let feed = build_chat_feed(&state);
        assert_eq!(feed.items.len(), 1);
        assert_eq!(feed.items[0].id, "welcome");
    }

    #[test]
    fn answer_markdown_builds_code_cards() {
        let mut state = AppState::new(
            "session".to_string(),
            "https://127.0.0.1:8586".to_string(),
            "/tmp".to_string(),
            "@".to_string(),
            UiMode::Smart,
            true,
        );
        state.answer = "text\n```bash\npytest -q\n```".to_string();
        let feed = build_chat_feed(&state);
        assert!(feed
            .items
            .iter()
            .any(|item| matches!(&item.payload, ChatPayload::ShellSnippet { .. })));
    }
}
