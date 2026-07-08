#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ViewportState {
    pub offset: usize,
    pub viewport_height: usize,
    pub total_lines: usize,
    pub stick_to_bottom: bool,
}

impl Default for ViewportState {
    fn default() -> Self {
        Self {
            offset: 0,
            viewport_height: 0,
            total_lines: 0,
            stick_to_bottom: true,
        }
    }
}

impl ViewportState {
    pub fn scroll_up(&mut self, lines: usize) {
        let max = self.max_offset();
        self.offset = self.offset.saturating_sub(lines).min(max);
        self.stick_to_bottom = self.offset >= max;
    }

    pub fn scroll_down(&mut self, lines: usize) {
        let max = self.max_offset();
        self.offset = self.offset.saturating_add(lines).min(max);
        self.stick_to_bottom = self.offset >= max;
    }

    pub fn page_up(&mut self) {
        self.scroll_up(self.viewport_height.max(1).saturating_sub(2));
    }

    pub fn page_down(&mut self) {
        self.scroll_down(self.viewport_height.max(1).saturating_sub(2));
    }

    pub fn go_top(&mut self) {
        self.offset = 0;
        self.stick_to_bottom = false;
    }

    pub fn go_bottom(&mut self) {
        self.offset = self.max_offset();
        self.stick_to_bottom = true;
    }

    pub fn update_total_lines(&mut self, total: usize) {
        self.total_lines = total;
        let max = self.max_offset();
        if self.stick_to_bottom {
            self.offset = max;
        } else {
            self.offset = self.offset.min(max);
        }
    }

    pub fn update_viewport_height(&mut self, height: usize) {
        self.viewport_height = height;
        self.update_total_lines(self.total_lines);
    }

    pub fn max_offset(&self) -> usize {
        self.total_lines.saturating_sub(self.viewport_height)
    }

    pub fn from_bottom(&self) -> usize {
        self.max_offset().saturating_sub(self.offset)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manual_scroll_disables_stickiness_until_bottom() {
        let mut viewport = ViewportState::default();
        viewport.update_viewport_height(10);
        viewport.update_total_lines(100);
        assert_eq!(viewport.offset, 90);
        assert!(viewport.stick_to_bottom);

        viewport.scroll_up(20);
        assert_eq!(viewport.offset, 70);
        assert!(!viewport.stick_to_bottom);

        viewport.update_total_lines(120);
        assert_eq!(viewport.offset, 70);
        assert_eq!(viewport.from_bottom(), 40);

        viewport.go_bottom();
        viewport.update_total_lines(130);
        assert_eq!(viewport.offset, 120);
        assert!(viewport.stick_to_bottom);
    }
}
