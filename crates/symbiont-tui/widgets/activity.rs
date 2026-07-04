use ratatui::style::{Color, Style};
use ratatui::text::Span;

pub fn status_label(running: bool, has_task: bool) -> &'static str {
    if running {
        "live"
    } else if has_task {
        "attached"
    } else {
        "idle"
    }
}

pub fn status_color(status: &str) -> Color {
    match status {
        "completed" | "success" | "ready" | "attached" => Color::Green,
        "failed" | "error" | "cancelled" => Color::Red,
        "running" | "pending" | "queued" | "live" | "watching" => Color::Yellow,
        _ => Color::White,
    }
}

pub fn chip(label: &str) -> Span<'static> {
    Span::styled(format!("● {label}"), Style::default().fg(status_color(label)))
}
