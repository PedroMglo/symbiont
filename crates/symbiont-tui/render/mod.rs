pub mod diff;
pub mod layout;
pub mod markdown_blocks;

use ratatui::widgets::{Block, Paragraph, Wrap};
use ratatui::Frame;

use crate::reducer::AppState;
use crate::widgets;

pub fn draw(frame: &mut Frame<'_>, state: &mut AppState) {
    let area = frame.area();
    let review_visible = widgets::review::ReviewSummary::from_files(&state.files).visible();
    let layout = layout::chat_layout(area, review_visible);

    frame.render_widget(widgets::header::header(state), layout.header);
    frame.render_widget(widgets::header::prompt_line(state), layout.prompt);

    let body = widgets::feed::render_chat_feed(state, layout.feed.width as usize);
    let visible_rows = layout.feed.height as usize;
    state.viewport.update_viewport_height(visible_rows);
    state.viewport.update_total_lines(body.len());
    let scroll_top = state.viewport.offset;
    let scroll_label = widgets::scrollbar::label(&state.viewport);
    frame.render_widget(
        Paragraph::new(body)
            .block(Block::default().title(format!(
                " conversation · {} · {} ",
                if state.running { "live" } else { "ready" },
                scroll_label
            )))
            .scroll((scroll_top as u16, 0))
            .wrap(Wrap { trim: false }),
        layout.feed,
    );

    if let Some(review_area) = layout.review {
        frame.render_widget(
            widgets::review::review_bar(&state.files, review_area.width as usize),
            review_area,
        );
    }
    frame.render_widget(widgets::composer::input_box(state), layout.composer);

    if let Some(modal) = &state.modal {
        widgets::modal::render(frame, area, modal);
    }
}

pub fn render_plain(state: &AppState) -> String {
    let mut lines = Vec::new();
    lines.push(format!(
        "Symbiont · {} · {} · {}",
        state.model,
        state.mode.label(),
        home_path(&state.cwd)
    ));
    if !state.last_prompt.is_empty() {
        lines.push(format!("> {}", state.last_prompt));
    }
    lines.push(format!(
        "task group: main · {} · {:.1}s",
        state.task_status,
        state.elapsed_seconds()
    ));
    if !state.status.is_empty() {
        lines.push(format!("status: {}", state.status));
    }
    for file in &state.files {
        lines.push(format!(
            "{} {} (+{} -{})",
            status_label(&file.status),
            file.path,
            file.additions,
            file.deletions
        ));
        if let Some(patch) = &file.patch {
            lines.push(diff::ansi_numbered_patch(
                patch,
                if state.full_diff { 10_000 } else { 80 },
            ));
        }
    }
    for command in &state.commands {
        lines.push(format!(
            "command: {} · {} · exit {}",
            command.command,
            command.status,
            command
                .exit_code
                .map(|value| value.to_string())
                .unwrap_or_else(|| "-".to_string())
        ));
        if let Some(stdout) = &command.stdout_preview {
            lines.push(format!("stdout: {stdout}"));
        }
        if let Some(stderr) = &command.stderr_preview {
            lines.push(format!("stderr: {stderr}"));
        }
    }
    if !state.answer.trim().is_empty() {
        lines.push("Resposta".to_string());
        lines.push(state.answer.trim().to_string());
    }
    if let Some(error) = &state.last_error {
        lines.push(format!("error: {error}"));
    }
    lines.join("\n") + "\n"
}

fn status_label(status: &str) -> &'static str {
    match status {
        "added" | "created" => "Added",
        "deleted" | "removed" => "Deleted",
        "modified" => "Modified",
        _ => "Changed",
    }
}

pub fn truncate(value: &str, limit: usize) -> String {
    if value.chars().count() <= limit {
        return value.to_string();
    }
    let mut out: String = value.chars().take(limit.saturating_sub(1)).collect();
    out.push('…');
    out
}

fn home_path(path: &str) -> String {
    if let Ok(home) = std::env::var("HOME") {
        if let Some(rest) = path.strip_prefix(&home) {
            return format!("~{rest}");
        }
    }
    path.to_string()
}
