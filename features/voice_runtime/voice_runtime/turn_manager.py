"""Small deterministic turn manager for voice_runtime."""

from __future__ import annotations

import uuid

from voice_runtime.types import TranscriptForwardEvent, VoiceRuntimeConfig, VoiceTurnEvent, VoiceTurnState


class VoiceTurnManager:
    """Track one voice interaction turn.

    This manager intentionally does not call STT, TTS, playback, or orchestrator.
    It only reduces stream events into voice turn state and typed final
    transcript events.
    """

    def __init__(self, config: VoiceRuntimeConfig):
        self._config = config
        self.turn_id = str(uuid.uuid4())
        self.state = VoiceTurnState.IDLE

    def start_listening(self) -> VoiceTurnEvent:
        return self._transition(VoiceTurnState.LISTENING, "state_changed")

    def mark_user_speaking(self) -> VoiceTurnEvent:
        return self._transition(VoiceTurnState.USER_SPEAKING, "state_changed")

    def mark_transcribing(self) -> VoiceTurnEvent:
        return self._transition(VoiceTurnState.TRANSCRIBING, "state_changed")

    def accept_partial(self, text: str, *, source_event_type: str = "partial_transcript") -> VoiceTurnEvent:
        if self.state == VoiceTurnState.IDLE:
            self.start_listening()
        return VoiceTurnEvent(
            type="partial_transcript",
            turn_id=self.turn_id,
            state=self.state,
            text=text,
            final=False,
            source_event_type=source_event_type,
        )

    def accept_final(
        self,
        text: str,
        *,
        source_event_type: str = "final_transcript",
        metadata: dict | None = None,
    ) -> tuple[VoiceTurnEvent, TranscriptForwardEvent]:
        self.state = VoiceTurnState.THINKING
        event = VoiceTurnEvent(
            type="final_transcript",
            turn_id=self.turn_id,
            state=self.state,
            text=text,
            final=True,
            source_event_type=source_event_type,
            metadata=dict(metadata or {}),
        )
        forwarded = TranscriptForwardEvent(
            turn_id=self.turn_id,
            text=text,
            language=self._config.language,
            gateway_id=self._config.gateway_id,
            metadata=dict(metadata or {}),
        )
        return event, forwarded

    def mark_speaking(self) -> VoiceTurnEvent:
        return self._transition(VoiceTurnState.SPEAKING, "state_changed")

    def interrupt(self) -> VoiceTurnEvent:
        return self._transition(VoiceTurnState.INTERRUPTED, "state_changed")

    def recoverable_error(self, message: str) -> VoiceTurnEvent:
        self.state = VoiceTurnState.ERROR_RECOVERABLE
        return VoiceTurnEvent(
            type="stream_error",
            turn_id=self.turn_id,
            state=self.state,
            text=message,
            metadata={"recoverable": True},
        )

    def close(self) -> VoiceTurnEvent:
        self.state = VoiceTurnState.IDLE
        return VoiceTurnEvent(type="session_closed", turn_id=self.turn_id, state=self.state)

    def _transition(self, state: VoiceTurnState, event_type: str) -> VoiceTurnEvent:
        self.state = state
        return VoiceTurnEvent(type=event_type, turn_id=self.turn_id, state=self.state)
