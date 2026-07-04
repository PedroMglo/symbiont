"""Real-time WebSocket streaming engine.

Handles:
1. Incoming PCM audio frames from WebSocket
2. VAD processing (speech/silence detection)
3. Speech segment extraction
4. Dispatching to GPU worker via event bus
5. Returning partial/final transcripts to client
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from streaming.config import get_config
from streaming.realtime.session import SessionManager, SessionState, StreamSession
from streaming.realtime.vad import RealtimeVAD

logger = logging.getLogger(__name__)

# Singleton session manager
_session_mgr: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _session_mgr
    if _session_mgr is None:
        _session_mgr = SessionManager(max_sessions=10)
    return _session_mgr


class StreamEngine:
    """Processes a WebSocket audio stream for a single session.

    Pipeline per frame:
    frame → VAD → buffer accumulation → speech segment → event bus → GPU → result
    """

    def __init__(self, session: StreamSession):
        self.session = session
        cfg = get_config()
        self.vad = RealtimeVAD(
            sample_rate=cfg.realtime.sample_rate,
            frame_duration_ms=cfg.realtime.frame_duration_ms,
            energy_threshold_db=cfg.realtime.vad_energy_threshold_db,
        )
        self._cfg = cfg.realtime
        self._speech_buffer = bytearray()
        self._speech_start_time: float | None = None
        self._silence_frames = 0
        self._min_speech_frames = int(cfg.realtime.min_speech_ms / cfg.realtime.frame_duration_ms)
        self._max_speech_frames = int(cfg.realtime.max_speech_ms / cfg.realtime.frame_duration_ms)
        self._silence_threshold_frames = int(cfg.realtime.silence_threshold_ms / cfg.realtime.frame_duration_ms)
        self._speech_frames = 0
        self._segment_index = 0
        # Hysteresis: consecutive speech frames seen (resets on any silence frame)
        self._consecutive_speech = 0
        # Frames needed to confirm speech onset / break silence streak (noise rejection)
        self._speech_onset_min = int(os.environ.get("VAD_ONSET_FRAMES", "3"))

    async def process_frame(self, frame: bytes) -> dict[str, Any] | None:
        """Process a single audio frame.

        Returns a transcript event dict if a segment was completed, None otherwise.

        Uses VAD hysteresis: silence counter is only reset after N consecutive speech
        frames (default 3 = 90ms). This prevents single Bluetooth noise bursts from
        resetting the silence window and causing 30s forced cuts.
        """
        vad_result = self.vad.process_frame(frame)

        if vad_result.is_speech:
            # Speech detected
            self._consecutive_speech += 1
            self._speech_frames += 1

            if not self.session.is_speaking:
                # Transition: silence → speech
                self.session.is_speaking = True
                self.session.state = SessionState.SPEAKING
                self._speech_start_time = time.time()

            self._speech_buffer.extend(frame)

            # Only reset silence streak after N consecutive speech frames (noise rejection)
            if self._consecutive_speech >= self._speech_onset_min:
                self._silence_frames = 0

            # Check max speech duration (force cut)
            if self._speech_frames >= self._max_speech_frames:
                return await self._finalize_segment(final=False)

        else:
            # Silence detected — reset consecutive speech counter
            self._consecutive_speech = 0
            self._silence_frames += 1

            if self.session.is_speaking:
                # Still accumulating (speech padding)
                self._speech_buffer.extend(frame)

                # Check if silence long enough to finalize
                if self._silence_frames >= self._silence_threshold_frames:
                    if self._speech_frames >= self._min_speech_frames:
                        return await self._finalize_segment(final=True)
                    else:
                        # Too short — discard (noise/click)
                        self._reset_speech()

        return None

    async def _finalize_segment(self, final: bool) -> dict[str, Any]:
        """Finalize a speech segment and dispatch for transcription."""
        audio_data = bytes(self._speech_buffer)
        segment_duration = len(audio_data) / (self._cfg.sample_rate * 2)
        start_time = self._speech_start_time or time.time()

        self._segment_index += 1
        segment_id = f"{self.session.session_id}:{self._segment_index}"

        # Reset speech state
        self._reset_speech()
        self.session.state = SessionState.PROCESSING

        # Dispatch to event bus for GPU processing
        from streaming.event_bus.redis_streams import get_event_bus

        event_bus = get_event_bus()
        await event_bus.publish_segment(
            session_id=self.session.session_id,
            segment_id=segment_id,
            audio_data=audio_data,
            segment_index=self._segment_index,
            duration=segment_duration,
            language=self.session.language,
            priority="realtime",
        )
        self.session.segments_completed = self._segment_index

        # Return immediate acknowledgment (transcript comes async via event bus)
        self.session.state = SessionState.LISTENING
        return {
            "type": "segment_submitted",
            "session_id": self.session.session_id,
            "segment_index": self._segment_index,
            "duration": round(segment_duration, 2),
            "timestamp": start_time,
            "metadata": {"segment_id": segment_id},
        }

    async def flush(self) -> dict[str, Any] | None:
        """Flush remaining audio buffer (on session close)."""
        if len(self._speech_buffer) > 0 and self._speech_frames >= self._min_speech_frames:
            return await self._finalize_segment(final=True)
        self._reset_speech()
        return None

    def _reset_speech(self) -> None:
        """Reset speech accumulation state."""
        self._speech_buffer = bytearray()
        self._speech_frames = 0
        self._silence_frames = 0
        self._consecutive_speech = 0
        self._speech_start_time = None
        self.session.is_speaking = False
