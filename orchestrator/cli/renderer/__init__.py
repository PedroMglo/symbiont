"""Terminal Renderer — central output layer for CLI/TTY presentation.

All user-facing output passes through this module to ensure consistent,
beautiful, Markdown-aware rendering in the terminal regardless of agent,
model, provider or command used.

Architecture:
    TerminalRenderer       — High-level API (start_response, stream_token, render_final, etc.)
    MarkdownRenderer       — Markdown→ANSI conversion (rich-based)
    TableFormatter         — Smart table detection and formatting
    StreamBuffer           — Accumulates streaming tokens for final re-render
"""

from orchestrator.cli.renderer.core import TerminalRenderer
from orchestrator.cli.renderer.markdown import MarkdownRenderer
from orchestrator.cli.renderer.stream_buffer import StreamBuffer
from orchestrator.cli.renderer.tables import TableFormatter

__all__ = [
    "TerminalRenderer",
    "MarkdownRenderer",
    "TableFormatter",
    "StreamBuffer",
]
