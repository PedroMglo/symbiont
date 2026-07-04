#![allow(dead_code)]

use std::time::SystemTime;

pub type BlockId = String;
pub type TaskId = String;
pub type SessionId = String;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct TimelineBlock {
    pub id: BlockId,
    pub kind: BlockKind,
    pub title: String,
    pub subtitle: Option<String>,
    pub status: BlockStatus,
    pub task_id: Option<TaskId>,
    pub session_id: Option<SessionId>,
    pub trace_id: Option<String>,
    pub lines: Vec<TimelineLine>,
    pub collapsed: bool,
    pub selectable: bool,
    pub priority: BlockPriority,
    pub created_at: Option<SystemTime>,
    pub updated_at: Option<SystemTime>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct TimelineLine {
    pub text: String,
    pub tone: LineTone,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum LineTone {
    Normal,
    Muted,
    Added,
    Removed,
    Warning,
    Error,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum BlockKind {
    UserMessage,
    AssistantMessage,
    StatusMessage,
    TaskCard,
    AgentCard,
    LlmCard,
    ToolCard,
    CommandCard,
    DiffCard,
    FileChangeCard,
    ErrorCard,
    ReviewCard,
    RawEvent,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum BlockStatus {
    Idle,
    Queued,
    Running,
    Done,
    Failed,
    Cancelled,
    TimedOut,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum BlockPriority {
    Low,
    Normal,
    High,
    Critical,
}

impl TimelineBlock {
    pub fn collapsed_summary(&self) -> String {
        let subtitle = self.subtitle.as_deref().unwrap_or("");
        if subtitle.is_empty() {
            self.title.clone()
        } else {
            format!("{} · {}", self.title, subtitle)
        }
    }
}
