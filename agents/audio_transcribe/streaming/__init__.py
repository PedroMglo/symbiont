"""Unified Audio Intelligence Platform — Streaming Sub-Agent.

Architecture:
┌─────────────────────────────────────────────────────────┐
│                   UNIFIED GATEWAY                         │
│  WebSocket (real-time) │ REST (batch) │ SSE (output)     │
└───────────────┬─────────────────────────┬───────────────┘
                │                         │
     ┌──────────▼──────────┐    ┌────────▼─────────┐
     │  REAL-TIME ENGINE   │    │  BATCH PIPELINE  │
     │  VAD + Sessions     │    │  Chunk + Queue   │
     └──────────┬──────────┘    └────────┬─────────┘
                │                         │
        ┌───────▼─────────────────────────▼───────┐
        │          REDIS STREAMS EVENT BUS        │
        │  audio.stream.segment / audio.batch.chunk│
        └───────────────────┬─────────────────────┘
                            │
              ┌─────────────▼──────────────┐
              │   GPU WORKER POOL (8GB)    │
              │   faster-whisper unified   │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │      MERGE ENGINE          │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │   LLM AGENTS (Ollama)      │
              └────────────────────────────┘

Supports:
- Real-time microphone streaming (WebSocket, <300ms target)
- Batch file processing (REST API)
- SHA-256 + Chromaprint dedup (global)
- Unified GPU scheduling (real-time priority)
- SSE output streaming
"""

from __future__ import annotations

import sys as _sys

from . import realtime_types as types

__version__ = "2.0.0"

_sys.modules[f"{__name__}.types"] = types
