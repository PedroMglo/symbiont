use ratatui::style::Color;
use ratatui::text::Line;

use super::{body_line, card_footer, card_header, wrap_text};

pub fn render_error_card(lines: &mut Vec<Line<'static>>, message: &str, width: usize) {
    card_header(lines, Color::Red, "!", "Error", "runtime", false, width);
    for wrapped in wrap_text(message, width.saturating_sub(4)) {
        lines.push(body_line(wrapped));
    }
    card_footer(lines);
}
