use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::events::FileChange;

use super::truncate;

#[derive(Debug, Clone, Copy, Default)]
pub struct ReviewSummary {
    pub files: usize,
    pub additions: i64,
    pub deletions: i64,
}

impl ReviewSummary {
    pub fn from_files(files: &[FileChange]) -> Self {
        Self {
            files: files.len(),
            additions: files.iter().map(|file| file.additions).sum(),
            deletions: files.iter().map(|file| file.deletions).sum(),
        }
    }

    pub fn visible(self) -> bool {
        self.files > 0
    }
}

pub fn review_bar(files: &[FileChange], width: usize) -> Paragraph<'static> {
    let summary = ReviewSummary::from_files(files);
    let file_preview = files
        .iter()
        .take(3)
        .map(|file| truncate(&file.path, 34))
        .collect::<Vec<_>>()
        .join(" · ");
    let more = files.len().saturating_sub(3);
    let suffix = if more > 0 {
        format!(" · +{more} more")
    } else {
        String::new()
    };
    let preview = truncate(&format!("{file_preview}{suffix}"), width.saturating_sub(34));
    Paragraph::new(Line::from(vec![
        Span::styled("review ", Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        Span::raw(format!("{} files  ", summary.files)),
        Span::styled(format!("+{} ", summary.additions), Style::default().fg(Color::Green)),
        Span::styled(format!("-{}  ", summary.deletions), Style::default().fg(Color::Red)),
        Span::styled(preview, Style::default().fg(Color::Gray)),
    ]))
    .block(Block::default().borders(Borders::TOP))
}
