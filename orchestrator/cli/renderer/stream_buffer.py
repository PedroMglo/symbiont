"""StreamBuffer — accumulates streaming tokens for final re-render."""

from __future__ import annotations


class StreamBuffer:
    """Thread-safe buffer that collects streaming tokens.

    Used by TerminalRenderer to accumulate the full response text
    during streaming, so it can be re-rendered with full Markdown
    formatting at the end.
    """

    def __init__(self) -> None:
        self._chunks: list[str] = []

    def append(self, token: str) -> None:
        """Append a token to the buffer."""
        self._chunks.append(token)

    def get_text(self) -> str:
        """Get the full accumulated text."""
        return "".join(self._chunks)

    def reset(self) -> None:
        """Clear the buffer for reuse."""
        self._chunks.clear()

    def __len__(self) -> int:
        """Number of tokens buffered."""
        return len(self._chunks)

    @property
    def is_empty(self) -> bool:
        return len(self._chunks) == 0
