#!/usr/bin/env python3
"""Streaming Markdown→ANSI renderer for terminal (coprocess helper).

This script is invoked as a coprocess by model alias scripts (.local/bin/gemma3, etc.).
It reads raw streaming tokens from stdin, displays them in real-time,
and when stdin closes (stream complete), re-renders the full response
with beautiful Markdown formatting.

Protocol:
    - Reads UTF-8 text from stdin continuously (no line buffering required)
    - Writes ANSI-colored output to stdout in real-time
    - On EOF: clears raw output, re-renders with full Markdown/tables
    - Exits with code 0

Usage (from bash alias):
    coproc RENDER { python3 "$ORC_RENDER"; }
    printf '%s' "$chunk" >&${RENDER[1]}   # send tokens
    exec {RENDER[1]}>&-                    # close input → triggers final render
    wait $RENDER_PID

Standalone testing:
    echo "## Hello\n\n| A | B |\n|---|---|\n| 1 | 2 |" | python3 render.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    # Read all input from stdin (the accumulated response text)
    try:
        text = sys.stdin.read()
    except KeyboardInterrupt:
        return

    if not text.strip():
        return

    # Ensure we can import the renderer. The script lives in
    # orchestrator/cli/ but is copied to ~/.local/bin/.
    _setup_path()

    try:
        from orchestrator.cli.renderer.core import TerminalRenderer
    except ModuleNotFoundError:
        sys.stdout.write(text)
        return

    renderer = TerminalRenderer(
        stream_live=False,  # Don't stream tokens (they were already shown by bash)
        rerender_on_end=False,
    )
    renderer.render_final(text)


def _setup_path() -> None:
    """Add symbiont package to path if not already available."""
    try:
        import orchestrator.cli.renderer  # noqa: F401
        return
    except ImportError:
        pass

    here = Path(__file__).resolve()
    cwd = Path.cwd()

    # Try common source-tree and installed-script locations.
    candidates = [
        os.environ.get("ORC_SYMBIONT_SRC", ""),
        str(Path(os.environ["AI_LOCAL_ROOT"]).expanduser() / "symbiont") if os.environ.get("AI_LOCAL_ROOT") else "",
        str(cwd / "symbiont"),
        str(cwd.parent / "ai-local" / "symbiont"),
        os.path.expanduser("~/_projects/ai-local/symbiont"),
        os.path.expanduser("~/ai-local/symbiont"),
        str(here.parent.parent),
        str(here.parent.parent.parent),
    ]
    for path in candidates:
        if path and os.path.isdir(os.path.join(path, "symbiont")):
            sys.path.insert(0, path)
            return


if __name__ == "__main__":
    main()
