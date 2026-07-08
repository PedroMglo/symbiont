use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};

use crate::events::FileChange;
use crate::render;

use super::{body_line, body_spans, card_footer, card_header, collapsed_line, truncate};

pub fn render_diff_card(
    lines: &mut Vec<Line<'static>>,
    file: &FileChange,
    collapsed: bool,
    full_diff: bool,
    width: usize,
) {
    let meta = format!(
        "{} · +{} -{}{}",
        status_label(&file.status),
        file.additions,
        file.deletions,
        if file.binary { " · binary" } else { "" }
    );
    card_header(
        lines,
        file_color(&file.status),
        "±",
        &truncate(&file.path, width.saturating_sub(24)),
        &meta,
        collapsed,
        width,
    );
    if collapsed {
        collapsed_line(lines, "diff folded");
        return;
    }
    if file.binary {
        lines.push(body_line("binary file changed"));
    } else if let Some(patch) = &file.patch {
        let max = if full_diff { 10_000 } else { 80 };
        for line in render::diff::patch_to_lines(patch, width.saturating_sub(4), max) {
            lines.push(indent_patch_line(line));
        }
    } else if let Some(diff_ref) = &file.diff_ref {
        lines.push(body_spans(vec![
            Span::styled("large diff folded · ref ", Style::default().fg(Color::DarkGray)),
            Span::raw(diff_ref.clone()),
        ]));
    } else {
        lines.push(body_line("file changed; no patch preview available"));
    }
    card_footer(lines);
}

fn indent_patch_line(mut line: Line<'static>) -> Line<'static> {
    let mut spans = vec![Span::styled("│ ", Style::default().fg(Color::DarkGray))];
    spans.append(&mut line.spans);
    Line::from(spans)
}

fn status_label(status: &str) -> &'static str {
    match status {
        "added" | "created" => "added",
        "deleted" | "removed" => "deleted",
        "modified" => "modified",
        _ => "changed",
    }
}

fn file_color(status: &str) -> Color {
    match status {
        "deleted" | "removed" => Color::Red,
        "added" | "created" => Color::Green,
        _ => Color::Yellow,
    }
}
