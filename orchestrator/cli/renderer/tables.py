"""Smart table formatter for terminal output.

Handles:
- Detecting and parsing Markdown tables
- Column width calculation with terminal-aware truncation
- Cell wrapping for long content
- Beautiful border rendering with Rich Table
- Fallback to card layout for very wide tables
"""

from __future__ import annotations

import os
import re

from rich.console import Console
from rich.table import Table


class TableFormatter:
    """Formats Markdown tables for beautiful terminal display."""

    # Max percentage of terminal width a single column can take
    _MAX_COL_WIDTH_RATIO = 0.4

    def __init__(self, console: Console | None = None, max_width: int | None = None):
        self._console = console or Console(highlight=False)
        self._max_width = max_width or self._get_terminal_width()

    def _get_terminal_width(self) -> int:
        """Get terminal width with sane defaults."""
        try:
            return min(os.get_terminal_size().columns, 160)
        except (OSError, ValueError):
            return 100

    def render_table(self, markdown_table: str) -> None:
        """Parse a Markdown table and render it beautifully.

        If the table is too wide for the terminal, switches to card layout.
        """
        rows = self._parse_markdown_table(markdown_table)
        if not rows:
            # Fallback: just print raw if we can't parse it
            self._console.print(markdown_table)
            return

        headers = rows[0]
        data_rows = rows[1:]

        # Decide rendering strategy
        total_content_width = sum(
            max(len(h), max((len(str(r[i])) for r in data_rows), default=0))
            for i, h in enumerate(headers)
        )

        # Use card layout if table would be extremely wide
        if total_content_width > self._max_width * 1.5 and len(headers) > 5:
            self._render_as_cards(headers, data_rows)
        else:
            self._render_as_rich_table(headers, data_rows)

    def _parse_markdown_table(self, text: str) -> list[list[str]]:
        """Parse Markdown table text into a list of rows (list of cell strings)."""
        lines = [line_.strip() for line_ in text.strip().splitlines() if line_.strip()]
        if len(lines) < 2:
            return []

        rows: list[list[str]] = []
        for i, line in enumerate(lines):
            # Skip separator row (|---|---|...)
            if re.match(r"^\|[\s:|-]+\|$", line):
                continue

            # Parse cells
            cells = self._parse_row(line)
            if cells:
                rows.append(cells)

        return rows

    def _parse_row(self, line: str) -> list[str]:
        """Parse a single table row into cells."""
        # Remove leading/trailing pipes
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]

        cells = [cell.strip() for cell in line.split("|")]
        return cells

    def _render_as_rich_table(self, headers: list[str], data_rows: list[list[str]]) -> None:
        """Render using Rich Table with smart column widths."""
        table = Table(
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            pad_edge=True,
            expand=False,
        )

        # Calculate max column widths
        max_col_width = int(self._max_width * self._MAX_COL_WIDTH_RATIO)

        for i, header in enumerate(headers):
            # Determine column width from content
            col_values = [row[i] if i < len(row) else "" for row in data_rows]
            max_content = max(len(header), max((len(v) for v in col_values), default=0))

            # Determine if column is numeric (right-align)
            is_numeric = all(
                re.match(r"^[\d.,\-%+]+$", v.strip()) or v.strip() == ""
                for v in col_values
            )

            col_width = min(max_content + 2, max_col_width)  # noqa: F841
            justify = "right" if is_numeric else "left"

            table.add_column(
                header,
                justify=justify,
                max_width=max_col_width,
                no_wrap=False,
                overflow="fold",
            )

        for row in data_rows:
            # Pad row to match header count
            padded = row + [""] * (len(headers) - len(row))
            table.add_row(*padded[:len(headers)])

        self._console.print(table)

    def _render_as_cards(self, headers: list[str], data_rows: list[list[str]]) -> None:
        """Render as vertical cards when table is too wide.

        Each row becomes a card:
            ┌─ Row 1 ───────────
            │ Column1: value1
            │ Column2: value2
            └────────────────────
        """
        for idx, row in enumerate(data_rows):
            self._console.print(f"[dim]─── Record {idx + 1} ───[/dim]")
            for i, header in enumerate(headers):
                value = row[i] if i < len(row) else ""
                self._console.print(f"  [bold]{header}:[/bold] {value}")
            if idx < len(data_rows) - 1:
                self._console.print()
