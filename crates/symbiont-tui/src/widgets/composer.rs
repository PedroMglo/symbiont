use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::reducer::AppState;

pub fn input_box(state: &AppState) -> Paragraph<'static> {
    let placeholder = if state.running {
        "task running · write a follow-up after it finishes · q detaches"
    } else {
        "message Symbiont"
    };
    let text = if state.input.is_empty() {
        placeholder.to_string()
    } else {
        state.input.replace('\n', "  ")
    };
    let text_style = if state.input.is_empty() {
        Style::default().fg(Color::DarkGray)
    } else {
        Style::default().fg(Color::White)
    };
    Paragraph::new(Line::from(vec![
        Span::styled("› ", Style::default().fg(Color::Cyan)),
        Span::styled(text, text_style),
    ]))
    .block(
        Block::default()
            .borders(Borders::ALL)
            .title(" composer · Enter send · Ctrl+J newline · ? help · q detach "),
    )
}
