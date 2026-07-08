use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};

use crate::events::CommandRun;

use super::{body_line, body_spans, card_footer, card_header, collapsed_line, home_path, truncate, wrap_text};

pub fn render_shell_snippet(
    lines: &mut Vec<Line<'static>>,
    language: Option<&str>,
    script: &str,
    collapsed: bool,
    width: usize,
) {
    let language = language.unwrap_or("shell");
    card_header(
        lines,
        Color::Yellow,
        "$",
        "Shell snippet",
        &format!("{language} · {} lines", script.lines().count()),
        collapsed,
        width,
    );
    if collapsed {
        collapsed_line(lines, "shell snippet folded");
        return;
    }
    for raw in script.lines().take(80) {
        lines.push(body_spans(vec![
            Span::styled("$ ", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
            Span::raw(truncate(raw, width.saturating_sub(6))),
        ]));
    }
    if script.lines().count() > 80 {
        lines.push(body_line("large shell block folded after 80 lines"));
    }
    card_footer(lines);
}

pub fn render_command_card(
    lines: &mut Vec<Line<'static>>,
    command: &CommandRun,
    collapsed: bool,
    width: usize,
) {
    let exit = command
        .exit_code
        .map(|value| value.to_string())
        .unwrap_or_else(|| "-".to_string());
    let duration = command
        .duration_seconds
        .map(|seconds| format!("{seconds:.1}s"))
        .unwrap_or_else(|| "-".to_string());
    card_header(
        lines,
        command_color(&command.status),
        "⌘",
        "Command",
        &format!("{} · {duration} · exit {exit}", command.status),
        collapsed,
        width,
    );
    if collapsed {
        collapsed_line(lines, &truncate(&command.command, width.saturating_sub(8)));
        return;
    }

    lines.push(body_spans(vec![
        Span::styled("$ ", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
        Span::raw(truncate(&command.command, width.saturating_sub(6))),
    ]));
    if !command.cwd.is_empty() {
        lines.push(body_spans(vec![
            Span::styled("cwd ", Style::default().fg(Color::DarkGray)),
            Span::raw(home_path(&command.cwd)),
        ]));
    }
    push_output(lines, "stdout", command.stdout_preview.as_deref(), Color::Green, width);
    push_output(lines, "stderr", command.stderr_preview.as_deref(), Color::Red, width);

    let refs = [
        command.stdout_ref.as_ref().map(|value| format!("stdout={value}")),
        command.stderr_ref.as_ref().map(|value| format!("stderr={value}")),
        command.diff_ref.as_ref().map(|value| format!("diff={value}")),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join(" · ");
    if !refs.is_empty() {
        lines.push(body_spans(vec![
            Span::styled("refs ", Style::default().fg(Color::DarkGray)),
            Span::raw(truncate(&refs, width.saturating_sub(7))),
        ]));
    }
    if command.stdout_preview.is_none()
        && command.stderr_preview.is_none()
        && refs.is_empty()
        && command.status == "running"
    {
        lines.push(body_line("command is still running; waiting for output..."));
    }
    card_footer(lines);
}

fn push_output(
    lines: &mut Vec<Line<'static>>,
    label: &str,
    output: Option<&str>,
    color: Color,
    width: usize,
) {
    let Some(output) = output.filter(|value| !value.trim().is_empty()) else {
        return;
    };
    lines.push(body_spans(vec![Span::styled(
        format!("{label}:"),
        Style::default().fg(color).add_modifier(Modifier::BOLD),
    )]));
    for wrapped in wrap_text(output, width.saturating_sub(4)).into_iter().take(40) {
        lines.push(body_line(wrapped));
    }
    if output.lines().count() > 40 {
        lines.push(body_line(format!("{label} folded after 40 lines")));
    }
}

fn command_color(status: &str) -> Color {
    match status {
        "completed" | "success" => Color::Green,
        "failed" | "error" => Color::Red,
        "running" => Color::Yellow,
        _ => Color::White,
    }
}
