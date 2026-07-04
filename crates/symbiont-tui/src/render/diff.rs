use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};

use super::truncate;

pub fn patch_to_lines(patch: &str, width: usize, max_lines: usize) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    let mut old_line = 0usize;
    let mut new_line = 0usize;
    for (index, raw) in patch.lines().enumerate() {
        if index >= max_lines {
            lines.push(Line::from(Span::styled(
                format!("... {} hidden diff lines · press d for full diff", patch.lines().count() - max_lines),
                Style::default().fg(Color::DarkGray),
            )));
            break;
        }
        if let Some((old, new)) = parse_hunk(raw) {
            old_line = old;
            new_line = new;
            lines.push(Line::from(Span::styled(
                truncate(raw, width.saturating_sub(2)),
                Style::default().fg(Color::Cyan),
            )));
            continue;
        }
        if raw.starts_with("---") || raw.starts_with("+++") {
            lines.push(Line::from(Span::styled(
                truncate(raw, width.saturating_sub(2)),
                Style::default().fg(Color::DarkGray),
            )));
            continue;
        }
        if raw.starts_with('+') {
            lines.push(Line::from(Span::styled(
                format!("{new_line:>5} {}", truncate(raw, width.saturating_sub(8))),
                Style::default().fg(Color::Green),
            )));
            new_line += 1;
            continue;
        }
        if raw.starts_with('-') {
            lines.push(Line::from(Span::styled(
                format!("{old_line:>5} {}", truncate(raw, width.saturating_sub(8))),
                Style::default().fg(Color::Red),
            )));
            old_line += 1;
            continue;
        }
        let text = raw.strip_prefix(' ').unwrap_or(raw);
        lines.push(Line::from(Span::styled(
            format!("{new_line:>5}  {}", truncate(text, width.saturating_sub(9))),
            Style::default().fg(Color::Gray),
        )));
        old_line += 1;
        new_line += 1;
    }
    lines
}

pub fn ansi_numbered_patch(patch: &str, max_lines: usize) -> String {
    const RESET: &str = "\x1b[0m";
    const GREEN: &str = "\x1b[32m";
    const RED: &str = "\x1b[31m";
    const CYAN: &str = "\x1b[36m";
    const GRAY: &str = "\x1b[90m";

    let mut old_line = 0usize;
    let mut new_line = 0usize;
    let mut out = Vec::new();
    for (index, raw) in patch.lines().enumerate() {
        if index >= max_lines {
            out.push(format!("{GRAY}... hidden diff lines · press d for full diff{RESET}"));
            break;
        }
        if let Some((old, new)) = parse_hunk(raw) {
            old_line = old;
            new_line = new;
            out.push(format!("{CYAN}{raw}{RESET}"));
        } else if raw.starts_with("---") || raw.starts_with("+++") {
            out.push(format!("{GRAY}{raw}{RESET}"));
        } else if raw.starts_with('+') {
            out.push(format!("{GREEN}{new_line:>5} {raw}{RESET}"));
            new_line += 1;
        } else if raw.starts_with('-') {
            out.push(format!("{RED}{old_line:>5} {raw}{RESET}"));
            old_line += 1;
        } else {
            let text = raw.strip_prefix(' ').unwrap_or(raw);
            out.push(format!("{GRAY}{new_line:>5}  {text}{RESET}"));
            old_line += 1;
            new_line += 1;
        }
    }
    out.join("\n")
}

fn parse_hunk(raw: &str) -> Option<(usize, usize)> {
    if !raw.starts_with("@@") {
        return None;
    }
    let mut old_line = None;
    let mut new_line = None;
    for part in raw.split_whitespace() {
        if let Some(rest) = part.strip_prefix('-') {
            old_line = rest.split(',').next()?.parse::<usize>().ok();
        }
        if let Some(rest) = part.strip_prefix('+') {
            new_line = rest.split(',').next()?.parse::<usize>().ok();
        }
    }
    Some((old_line.unwrap_or(0), new_line.unwrap_or(0)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ansi_patch_adds_line_numbers() {
        let patch = "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1,2 @@\n+one\n+two";
        let rendered = ansi_numbered_patch(patch, 80);
        assert!(rendered.contains("    1 +one"));
        assert!(rendered.contains("    2 +two"));
    }
}
