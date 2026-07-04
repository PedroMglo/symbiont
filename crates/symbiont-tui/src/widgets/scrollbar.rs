use crate::viewport::ViewportState;

pub fn label(viewport: &ViewportState) -> String {
    if viewport.total_lines <= viewport.viewport_height {
        return "all visible".to_string();
    }
    format!(
        "{} lines · {} from bottom",
        viewport.total_lines,
        viewport.from_bottom()
    )
}
