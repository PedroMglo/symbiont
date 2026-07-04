#![allow(dead_code)]

use ratatui::layout::Rect;

use crate::timeline::{BlockId, SessionId, TaskId};

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct HitBox {
    pub rect: Rect,
    pub action: MouseAction,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum MouseAction {
    SelectBlock(BlockId),
    ToggleBlock(BlockId),
    SelectTask(TaskId),
    ToggleTask(TaskId),
    ToggleSession(SessionId),
    OpenDiff(BlockId),
    OpenReview(TaskId),
    CopyBlock(BlockId),
    CopyTrace(String),
    ScrollbarDrag,
}

#[derive(Debug, Clone, Default)]
pub struct HitMap {
    pub boxes: Vec<HitBox>,
}

impl HitMap {
    pub fn clear(&mut self) {
        self.boxes.clear();
    }

    pub fn push(&mut self, hitbox: HitBox) {
        self.boxes.push(hitbox);
    }

    pub fn hit_test(&self, x: u16, y: u16) -> Option<MouseAction> {
        self.boxes
            .iter()
            .rev()
            .find(|hitbox| point_in_rect(x, y, hitbox.rect))
            .map(|hitbox| hitbox.action.clone())
    }
}

fn point_in_rect(x: u16, y: u16, rect: Rect) -> bool {
    x >= rect.x
        && x < rect.x.saturating_add(rect.width)
        && y >= rect.y
        && y < rect.y.saturating_add(rect.height)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hit_test_returns_topmost_matching_action() {
        let mut map = HitMap::default();
        map.push(HitBox {
            rect: Rect::new(0, 0, 10, 10),
            action: MouseAction::SelectBlock("outer".to_string()),
        });
        map.push(HitBox {
            rect: Rect::new(2, 2, 4, 4),
            action: MouseAction::ToggleBlock("inner".to_string()),
        });

        assert_eq!(map.hit_test(3, 3), Some(MouseAction::ToggleBlock("inner".to_string())));
        assert_eq!(map.hit_test(9, 9), Some(MouseAction::SelectBlock("outer".to_string())));
        assert_eq!(map.hit_test(11, 11), None);
    }
}
