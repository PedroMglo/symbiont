"""Real-time session management.

Each connected client (WebSocket) gets a session that tracks:
- Audio buffer (accumulates frames until VAD detects speech end)
- VAD state (speaking/silent)
- Partial transcript buffer
- Timestamps
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    SPEAKING = "speaking"
    PROCESSING = "processing"
    CLOSED = "closed"


@dataclass
class StreamSession:
    """Stateful session for a real-time audio stream."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: SessionState = SessionState.IDLE
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    # Audio state
    audio_buffer: bytearray = field(default_factory=bytearray)
    sample_rate: int = 16000
    frames_received: int = 0
    bytes_received: int = 0

    # VAD state
    speech_start: float | None = None
    silence_start: float | None = None
    is_speaking: bool = False

    # Transcript state
    partial_text: str = ""
    segments_completed: int = 0
    total_audio_duration: float = 0.0

    # Config
    language: str = "auto"

    def append_audio(self, data: bytes) -> None:
        """Append audio frames to buffer."""
        self.audio_buffer.extend(data)
        self.frames_received += 1
        self.bytes_received += len(data)
        self.last_activity = time.time()
        # Calculate duration: 16-bit PCM mono @ 16kHz = 2 bytes/sample
        self.total_audio_duration = len(self.audio_buffer) / (self.sample_rate * 2)

    def consume_buffer(self) -> bytes:
        """Consume and return the audio buffer, resetting it."""
        data = bytes(self.audio_buffer)
        self.audio_buffer = bytearray()
        return data

    def get_buffer_duration_ms(self) -> float:
        """Get current buffer duration in milliseconds."""
        return (len(self.audio_buffer) / (self.sample_rate * 2)) * 1000

    def close(self) -> None:
        self.state = SessionState.CLOSED
        self.audio_buffer = bytearray()


class SessionManager:
    """Manages active streaming sessions."""

    def __init__(self, max_sessions: int = 10):
        self._sessions: dict[str, StreamSession] = {}
        self._max_sessions = max_sessions
        self._lock = asyncio.Lock()

    async def create_session(self, language: str = "auto") -> StreamSession:
        """Create a new streaming session."""
        async with self._lock:
            if len(self._sessions) >= self._max_sessions:
                # Evict oldest idle session
                self._evict_oldest()

            session = StreamSession(language=language)
            session.state = SessionState.LISTENING
            self._sessions[session.session_id] = session
            return session

    async def get_session(self, session_id: str) -> StreamSession | None:
        return self._sessions.get(session_id)

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                session.close()

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": s.session_id,
                "state": s.state.value,
                "duration": s.total_audio_duration,
                "segments": s.segments_completed,
                "created_at": s.created_at,
            }
            for s in self._sessions.values()
        ]

    def _evict_oldest(self) -> None:
        """Remove the oldest idle session."""
        idle = [
            s for s in self._sessions.values()
            if s.state in (SessionState.IDLE, SessionState.LISTENING)
        ]
        if idle:
            oldest = min(idle, key=lambda s: s.last_activity)
            oldest.close()
            del self._sessions[oldest.session_id]
