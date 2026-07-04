use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};

use super::{body_line, card_footer, card_header, collapsed_line, truncate};

pub fn render_code_card(
    lines: &mut Vec<Line<'static>>,
    title: &str,
    language: Option<&str>,
    code: &str,
    collapsed: bool,
    width: usize,
) {
    let language = language.unwrap_or("text");
    let count = code.lines().count();
    card_header(
        lines,
        Color::Magenta,
        "▢",
        title,
        &format!("{language} · {count} lines"),
        collapsed,
        width,
    );
    if collapsed {
        collapsed_line(lines, "code folded");
        return;
    }
    for (index, raw) in code.lines().enumerate().take(160) {
        lines.push(Line::from(vec![
            Span::styled("│ ", Style::default().fg(Color::DarkGray)),
            Span::styled(format!("{:>4} ", index + 1), Style::default().fg(Color::DarkGray)),
            Span::styled(
                truncate(raw, width.saturating_sub(8)),
                Style::default().fg(Color::White),
            ),
        ]));
    }
    if code.lines().count() > 160 {
        lines.push(body_line("large code block folded after 160 lines"));
    }
    card_footer(lines);
}
