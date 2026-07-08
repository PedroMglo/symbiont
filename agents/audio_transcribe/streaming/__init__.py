"""Audio Transcribe streaming sub-agent.

Owns authenticated realtime WebSocket sessions, Redis Streams dispatch,
GPU-worker transcription, batch job SSE snapshots and active-session metrics.
"""

from __future__ import annotations

import sys as _sys

from . import realtime_types as types

__version__ = "2.0.0"

_sys.modules[f"{__name__}.types"] = types
