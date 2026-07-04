"""Markdown→ANSI renderer for terminal output.

Uses Rich's Markdown engine for headings, lists, code blocks, bold/italic.
Intercepts tables for custom formatting via TableFormatter.
"""

from __future__ import annotations

import re

from rich.console import Console
from rich.markdown import Markdown

from orchestrator.cli.renderer.tables import TableFormatter


class MarkdownRenderer:
    """Renders Markdown content beautifully in the terminal."""

    # Pattern to detect Markdown tables (pipes with header separator)
    _TABLE_PATTERN = re.compile(
        r"((?:^[ \t]*\|[^\n]+\|[ \t]*\n){2,})",
        re.MULTILINE,
    )
    # More specific: header + separator + rows
    _TABLE_BLOCK_PATTERN = re.compile(
        r"(^[ \t]*\|[^\n]+\|\s*\n"  # header row
        r"[ \t]*\|[\s:|-]+\|\s*\n"  # separator row (---|---|---)
        r"(?:[ \t]*\|[^\n]+\|\s*\n?)+)",  # data rows
        re.MULTILINE,
    )

    def __init__(self, console: Console | None = None):
        self._console = console or Console(highlight=False)
        self._table_fmt = TableFormatter(console=self._console)

    def render(self, text: str) -> None:
        """Render Markdown text to the terminal.

        Strategy:
        1. Split text into segments (normal markdown vs tables)
        2. Render tables with TableFormatter (aligned columns)
        3. Render other content with Rich Markdown
        """
        if not text.strip():
            return

        # Split text around table blocks
        segments = self._split_tables(text)

        for segment_type, content in segments:
            if not content.strip():
                continue

            if segment_type == "table":
                self._table_fmt.render_table(content)
            else:
                self._render_markdown_segment(content)

    def _split_tables(self, text: str) -> list[tuple[str, str]]:
        """Split text into ('markdown', content) and ('table', content) segments."""
        segments: list[tuple[str, str]] = []
        last_end = 0

        for match in self._TABLE_BLOCK_PATTERN.finditer(text):
            start, end = match.span()

            # Text before the table
            if start > last_end:
                before = text[last_end:start]
                if before.strip():
                    segments.append(("markdown", before))

            # The table itself
            segments.append(("table", match.group(0)))
            last_end = end

        # Text after last table
        if last_end < len(text):
            after = text[last_end:]
            if after.strip():
                segments.append(("markdown", after))

        # No tables found — entire text is markdown
        if not segments:
            segments.append(("markdown", text))

        return segments

    def _render_markdown_segment(self, text: str) -> None:
        """Render a non-table Markdown segment using Rich."""
        md = Markdown(text, code_theme="monokai")
        self._console.print(md)
