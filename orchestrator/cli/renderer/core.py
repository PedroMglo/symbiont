"""Core TerminalRenderer — the single entry point for all CLI output.

Usage:
    renderer = TerminalRenderer()
    renderer.start_response(model="gemma3:4b")
    renderer.stream_token("Hello ")
    renderer.stream_token("world!")
    renderer.end_stream()
    # Final Markdown-rendered output is automatically displayed.

    # Or for non-streaming:
    renderer.render_final("## Result\n| Col1 | Col2 |\n|---|---|\n| a | b |")

    # Errors and warnings:
    renderer.render_error("Connection refused")
    renderer.render_warning("Model fallback active")
    renderer.render_progress("Gathering context...")
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from rich.console import Console

from orchestrator.cli.renderer.markdown import MarkdownRenderer
from orchestrator.cli.renderer.stream_buffer import StreamBuffer


class TerminalRenderer:
    """Central rendering layer for all terminal output.

    Handles:
    - Real-time token streaming (raw, for low-latency feel)
    - Final Markdown re-render (beautiful tables, headings, etc.)
    - Error, warning, and progress messages with distinct styling
    - Metadata display (model, latency, tokens)
    """

    def __init__(
        self,
        *,
        file: Any = None,
        force_color: bool | None = None,
        width: int | None = None,
        stream_live: bool = True,
        rerender_on_end: bool = True,
    ):
        self._file = file or sys.stdout
        self._is_tty = hasattr(self._file, "isatty") and self._file.isatty()

        # Console for rich rendering
        self._console = Console(
            file=self._file,
            force_terminal=force_color,
            width=width or self._detect_width(),
            highlight=False,
        )
        self._err_console = Console(file=sys.stderr, highlight=False)

        self._md_renderer = MarkdownRenderer(console=self._console)
        self._buffer = StreamBuffer()
        self._stream_live = stream_live
        self._rerender_on_end = rerender_on_end

        # State
        self._streaming = False
        self._start_time: float = 0
        self._token_count: int = 0
        self._model: str = ""

    def _detect_width(self) -> int:
        """Get terminal width, defaulting to 100 for non-TTY."""
        try:
            cols = os.get_terminal_size().columns
            return min(cols, 160)  # Cap at 160 for readability
        except (OSError, ValueError):
            return 100

    # ------------------------------------------------------------------
    # Streaming API
    # ------------------------------------------------------------------

    def start_response(self, *, model: str = "", metadata: dict | None = None) -> None:
        """Signal the start of a streaming response."""
        self._streaming = True
        self._start_time = time.time()
        self._token_count = 0
        self._model = model
        self._buffer.reset()

    def stream_token(self, token: str) -> None:
        """Display a streaming token in real-time and buffer for final render.

        During streaming we print raw tokens (no Markdown rendering) for
        low latency. The final render happens in end_stream().
        """
        if not token:
            return

        self._buffer.append(token)
        self._token_count += 1

        if self._stream_live and self._is_tty:
            # Print raw token immediately for real-time feel
            sys.stdout.write(token)
            sys.stdout.flush()

    def end_stream(self) -> None:
        """End streaming — optionally re-render the full response with Markdown."""
        if not self._streaming:
            return

        self._streaming = False
        elapsed = time.time() - self._start_time
        full_text = self._buffer.get_text()

        if self._rerender_on_end and self._is_tty and full_text.strip():
            # Clear the raw streamed output and re-render with Markdown formatting
            self._clear_streamed_output()
            self._md_renderer.render(full_text)
        elif not self._is_tty:
            # Non-TTY (piped output): just write the plain text
            sys.stdout.write(full_text)

        # Print metadata footer
        if self._is_tty and (self._model or elapsed > 0):
            self._render_metadata(elapsed)

        sys.stdout.write("\n")
        sys.stdout.flush()

    def _clear_streamed_output(self) -> None:
        """Clear the raw streamed text to replace with rendered version.

        Uses ANSI escape: move up N lines and clear each.
        """
        full_text = self._buffer.get_text()
        if not full_text:
            return

        # Count lines in the streamed output
        terminal_width = self._detect_width()
        lines = full_text.split("\n")
        total_lines = 0
        for line in lines:
            # Account for line wrapping
            line_len = len(line) if line else 1
            total_lines += max(1, (line_len + terminal_width - 1) // terminal_width)

        # Move cursor up and clear
        if total_lines > 0:
            sys.stdout.write(f"\033[{total_lines}A")  # Move up
            sys.stdout.write("\033[J")  # Clear from cursor to end
            sys.stdout.flush()

    def _render_metadata(self, elapsed: float) -> None:
        """Print a subtle metadata line after the response."""
        parts = []
        if self._model:
            parts.append(f"model: {self._model}")
        if elapsed > 0:
            parts.append(f"{elapsed:.1f}s")
        if self._token_count > 0:
            tps = self._token_count / elapsed if elapsed > 0 else 0
            parts.append(f"{self._token_count} tokens ({tps:.0f} t/s)")

        if parts:
            meta_line = " · ".join(parts)
            self._console.print(f"\n[dim]─── {meta_line} ───[/dim]")

    # ------------------------------------------------------------------
    # Direct rendering (non-streaming)
    # ------------------------------------------------------------------

    def render_final(self, text: str, *, metadata: dict | None = None) -> None:
        """Render a complete response with full Markdown formatting."""
        if not text.strip():
            return

        if self._is_tty:
            self._md_renderer.render(text)
        else:
            sys.stdout.write(text)
            sys.stdout.write("\n")
            sys.stdout.flush()

    # ------------------------------------------------------------------
    # Status messages
    # ------------------------------------------------------------------

    def render_error(self, message: str) -> None:
        """Display an error message."""
        self._err_console.print(f"[bold red]✗ Error:[/bold red] {message}")

    def render_warning(self, message: str) -> None:
        """Display a warning message."""
        self._err_console.print(f"[yellow]⚠ Warning:[/yellow] {message}")

    def render_progress(self, message: str) -> None:
        """Display a progress/status message."""
        if self._is_tty:
            self._err_console.print(f"[dim]⟳ {message}[/dim]")

    def render_info(self, message: str) -> None:
        """Display an informational message."""
        self._console.print(f"[cyan]ℹ[/cyan] {message}")

    # ------------------------------------------------------------------
    # Pipe-friendly output
    # ------------------------------------------------------------------

    def write_plain(self, text: str) -> None:
        """Write plain text (for piped output or non-Markdown content)."""
        sys.stdout.write(text)
        sys.stdout.flush()
