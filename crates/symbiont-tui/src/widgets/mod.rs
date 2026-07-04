pub mod activity;
pub mod code;
pub mod composer;
pub mod diff;
pub mod error;
pub mod feed;
pub mod header;
pub mod modal;
pub mod review;
pub mod scrollbar;
pub mod shell;
pub mod tool;

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};

pub(crate) fn card_header(
    lines: &mut Vec<Line<'static>>,
    accent: Color,
    icon: &str,
    title: &str,
    meta: &str,
    collapsed: bool,
    width: usize,
) {
    let marker = if collapsed { ">" } else { "v" };
    let suffix = if meta.is_empty() {
        String::new()
    } else {
        format!(" · {meta}")
    };
    let available = width.saturating_sub(title.chars().count() + icon.chars().count() + 10);
    lines.push(Line::from(vec![
        Span::styled("╭─ ", Style::default().fg(Color::DarkGray)),
        Span::styled(marker, Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
        Span::raw(" "),
        Span::styled(icon.to_string(), Style::default().fg(accent)),
        Span::raw(" "),
        Span::styled(title.to_string(), Style::default().fg(accent).add_modifier(Modifier::BOLD)),
        Span::styled(truncate(&suffix, available), Style::default().fg(Color::Gray)),
    ]));
}

pub(crate) fn card_footer(lines: &mut Vec<Line<'static>>) {
    lines.push(Line::from(Span::styled("╰", Style::default().fg(Color::DarkGray))));
    lines.push(Line::from(""));
}

pub(crate) fn body_line(text: impl Into<String>) -> Line<'static> {
    Line::from(vec![
        Span::styled("│ ", Style::default().fg(Color::DarkGray)),
        Span::raw(text.into()),
    ])
}

pub(crate) fn body_spans(spans: Vec<Span<'static>>) -> Line<'static> {
    let mut all = vec![Span::styled("│ ", Style::default().fg(Color::DarkGray))];
    all.extend(spans);
    Line::from(all)
}

pub(crate) fn collapsed_line(lines: &mut Vec<Line<'static>>, detail: &str) {
    lines.push(Line::from(vec![
        Span::styled("╰─ ", Style::default().fg(Color::DarkGray)),
        Span::styled(detail.to_string(), Style::default().fg(Color::DarkGray)),
    ]));
    lines.push(Line::from(""));
}

pub(crate) fn wrap_text(text: &str, width: usize) -> Vec<String> {
    let width = width.max(16);
    let mut out = Vec::new();
    for raw in text.lines() {
        if raw.trim().is_empty() {
            out.push(String::new());
            continue;
        }
        let mut line = String::new();
        for word in raw.split_whitespace() {
            let next_len = line.chars().count() + word.chars().count() + usize::from(!line.is_empty());
            if next_len > width && !line.is_empty() {
                out.push(std::mem::take(&mut line));
            }
            if !line.is_empty() {
                line.push(' ');
            }
            line.push_str(word);
        }
        out.push(line);
    }
    out
}

pub(crate) fn truncate(value: &str, limit: usize) -> String {
    if value.chars().count() <= limit {
        return value.to_string();
    }
    let mut out: String = value.chars().take(limit.saturating_sub(1)).collect();
    out.push('…');
    out
}

pub(crate) fn home_path(path: &str) -> String {
    if let Ok(home) = std::env::var("HOME") {
        if let Some(rest) = path.strip_prefix(&home) {
            return format!("~{rest}");
        }
    }
    path.to_string()
}
