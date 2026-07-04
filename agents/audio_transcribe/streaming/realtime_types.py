"""Typed public contract for audio_transcribe realtime streaming."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


RealtimeEventType = Literal[
    "session_started",
    "speech_started",
    "segment_submitted",
    "partial_transcript",
    "final_transcript",
    "stream_warning",
    "stream_error",
    "session_closed",
]


class RealtimeStreamConfig(BaseModel):
    """Initial client config for /ws/stream."""

    language: str = "auto"
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    sample_format: Literal["pcm16le"] = "pcm16le"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RealtimeTranscriptEvent(BaseModel):
    """Server event emitted by the realtime stream."""

    type: RealtimeEventType
    session_id: str
    message: str | None = None
    text: str | None = None
    final: bool | None = None
    segment_index: int | None = None
    duration: float | None = None
    timestamp: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RealtimeStreamError(BaseModel):
    """Typed realtime stream error event."""

    type: Literal["stream_error"] = "stream_error"
    session_id: str | None = None
    code: str
    message: str
    recoverable: bool = False


class RealtimeSessionSnapshot(BaseModel):
    """Operational snapshot for active realtime sessions."""

    session_id: str
    state: str
    duration: float = 0.0
    segments: int = 0
    created_at: float
    frames_received: int = 0
    bytes_received: int = 0
