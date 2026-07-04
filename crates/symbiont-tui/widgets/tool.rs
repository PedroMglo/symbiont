use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};

use crate::events::EventLine;

use super::{body_line, body_spans, card_footer, card_header, collapsed_line, truncate};

pub fn render_tool_card(
    lines: &mut Vec<Line<'static>>,
    event: &EventLine,
    collapsed: bool,
    width: usize,
) {
    let seq = event.seq.map(|value| format!("#{value}")).unwrap_or_else(|| "event".to_string());
    card_header(
        lines,
        Color::Cyan,
        "◆",
        &truncate(&event.kind, 42),
        &format!("{seq} · {}", event.status),
        collapsed,
        width,
    );
    if collapsed {
        collapsed_line(lines, "event folded");
        return;
    }
    if !event.title.is_empty() {
        lines.push(body_spans(vec![
            Span::styled("title ", Style::default().fg(Color::DarkGray)),
            Span::raw(truncate(&event.title, width.saturating_sub(8))),
        ]));
    }
    if !event.summary.is_empty() {
        lines.push(body_line(truncate(&event.summary, width.saturating_sub(4))));
    }
    if event.title.is_empty() && event.summary.is_empty() {
        lines.push(body_line("event received"));
    }
    card_footer(lines);
}
