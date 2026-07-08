use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::reducer::AppState;
use crate::widgets::activity;

use super::{home_path, truncate};

pub fn header(state: &AppState) -> Paragraph<'static> {
    let session = state.session_id.trim_start_matches("symbiont-");
    let activity = activity::status_label(state.running, state.task_id.is_some());
    Paragraph::new(vec![
        Line::from(vec![
            Span::styled("Symbiont", Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
            Span::raw("  "),
            activity::chip(activity),
            Span::raw(format!(
                "  {}  {}  session {}",
                state.model,
                state.mode.label(),
                truncate(session, 12)
            )),
        ]),
        Line::from(vec![
            Span::styled("cwd ", Style::default().fg(Color::DarkGray)),
            Span::raw(home_path(&state.cwd)),
            Span::styled("  api ", Style::default().fg(Color::DarkGray)),
            Span::raw(state.api_url.clone()),
        ]),
        Line::from(vec![
            Span::styled("Enter", Style::default().fg(Color::Yellow)),
            Span::raw(" send  "),
            Span::styled("PgUp/PgDn", Style::default().fg(Color::Yellow)),
            Span::raw(" scroll  "),
            Span::styled("d", Style::default().fg(Color::Yellow)),
            Span::raw(" diff  "),
            Span::styled("p/a/f/c/e/r", Style::default().fg(Color::Yellow)),
            Span::raw(" collapse  "),
            Span::styled("q/Esc", Style::default().fg(Color::Yellow)),
            Span::raw(" detach UI only"),
        ]),
    ])
    .block(Block::default().borders(Borders::BOTTOM))
}

pub fn prompt_line(state: &AppState) -> Paragraph<'static> {
    let prompt = if state.running {
        state.status.clone()
    } else if state.last_prompt.is_empty() {
        "new conversation".to_string()
    } else {
        truncate(&state.last_prompt, 160)
    };
    Paragraph::new(Line::from(vec![
        Span::styled(
            if state.running { "● " } else { "> " },
            Style::default().fg(if state.running { Color::Yellow } else { Color::DarkGray }),
        ),
        Span::raw(prompt),
    ]))
}
