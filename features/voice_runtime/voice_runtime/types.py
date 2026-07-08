"""Typed contracts for the local-first voice runtime boundary."""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, Field

HOST_AUDIO_STREAM_URL = "wss://127.0.0.1:9010/ws/stream"
PCM16_16KHZ_30MS_FRAME_BYTES = 960


class VoiceTurnState(str, enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR_RECOVERABLE = "error_recoverable"


class VoiceRuntimeConfig(BaseModel):
    """Runtime config for a host-side voice gateway session."""

    gateway_id: str = "local-voice-gateway"
    audio_stream_url: str = HOST_AUDIO_STREAM_URL
    language: str = "auto"
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    sample_format: Literal["pcm16le"] = "pcm16le"
    chunk_bytes: int = Field(default=PCM16_16KHZ_30MS_FRAME_BYTES, ge=2)
    receive_timeout_seconds: float = Field(default=2.0, ge=0.1, le=30.0)
    tls_verify: bool = True
    mode: Literal["fake_pcm", "push_to_talk"] = "fake_pcm"


class VoiceTurnEvent(BaseModel):
    """Internal voice turn event emitted by the voice runtime."""

    type: Literal[
        "state_changed",
        "partial_transcript",
        "final_transcript",
        "stream_warning",
        "stream_error",
        "session_closed",
    ]
    turn_id: str
    state: VoiceTurnState
    text: str | None = None
    final: bool = False
    source_event_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TranscriptForwardEvent(BaseModel):
    """Typed event forwarded to orchestrator boundaries after STT finalization."""

    type: Literal["voice.final_transcript"] = "voice.final_transcript"
    turn_id: str
    text: str
    language: str = "auto"
    gateway_id: str
    source: Literal["audio_transcribe"] = "audio_transcribe"
    metadata: dict[str, Any] = Field(default_factory=dict)


class VoiceGatewayStatus(BaseModel):
    """Operational status for the host-side gateway."""

    gateway_id: str
    state: VoiceTurnState
    connected: bool = False
    mode: str = "fake_pcm"
    active_turn_id: str | None = None
    mic_backend: str | None = None
    playback_backend: str | None = None
    metrics: dict[str, float | int | None] = Field(default_factory=dict)
