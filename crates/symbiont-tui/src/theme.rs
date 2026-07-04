#![allow(dead_code)]

use ratatui::style::Color;

#[derive(Debug, Clone, Copy)]
pub struct Theme {
    pub bg: Color,
    pub fg: Color,
    pub muted: Color,
    pub border: Color,
    pub border_active: Color,
    pub accent: Color,
    pub success: Color,
    pub warning: Color,
    pub error: Color,
    pub added_fg: Color,
    pub added_bg: Color,
    pub removed_fg: Color,
    pub removed_bg: Color,
    pub selected_bg: Color,
    pub header_fg: Color,
}

pub const CODEX_LIKE: Theme = Theme {
    bg: Color::Rgb(15, 15, 16),
    fg: Color::Rgb(220, 220, 220),
    muted: Color::Rgb(132, 136, 143),
    border: Color::Rgb(72, 76, 84),
    border_active: Color::Rgb(150, 154, 162),
    accent: Color::Rgb(63, 185, 200),
    success: Color::Rgb(118, 184, 128),
    warning: Color::Rgb(218, 170, 80),
    error: Color::Rgb(218, 100, 100),
    added_fg: Color::Rgb(160, 220, 170),
    added_bg: Color::Rgb(24, 56, 34),
    removed_fg: Color::Rgb(235, 154, 154),
    removed_bg: Color::Rgb(72, 32, 32),
    selected_bg: Color::Rgb(42, 44, 49),
    header_fg: Color::Rgb(236, 236, 236),
};
