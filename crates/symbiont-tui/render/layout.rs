use ratatui::layout::{Constraint, Direction, Layout, Rect};

#[derive(Debug, Clone, Copy)]
pub struct ChatLayout {
    pub header: Rect,
    pub prompt: Rect,
    pub feed: Rect,
    pub review: Option<Rect>,
    pub composer: Rect,
}

pub fn chat_layout(area: Rect, review_visible: bool) -> ChatLayout {
    let constraints = if review_visible {
        vec![
            Constraint::Length(4),
            Constraint::Length(2),
            Constraint::Min(8),
            Constraint::Length(2),
            Constraint::Length(3),
        ]
    } else {
        vec![
            Constraint::Length(4),
            Constraint::Length(2),
            Constraint::Min(8),
            Constraint::Length(3),
        ]
    };
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints(constraints)
        .split(area);
    if review_visible {
        ChatLayout {
            header: chunks[0],
            prompt: chunks[1],
            feed: chunks[2],
            review: Some(chunks[3]),
            composer: chunks[4],
        }
    } else {
        ChatLayout {
            header: chunks[0],
            prompt: chunks[1],
            feed: chunks[2],
            review: None,
            composer: chunks[3],
        }
    }
}
