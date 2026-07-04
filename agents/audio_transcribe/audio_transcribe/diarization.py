"""Speaker diarization using pyannote.audio (optional)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from audio_transcribe.config import get_config
from audio_transcribe.errors import DiarizationError
from audio_transcribe.types import SpeakerSegment

logger = logging.getLogger(__name__)


class SpeakerDiarizer:
    """Speaker diarization using pyannote.audio.

    - Optional: disabled by default
    - Graceful degradation: warns and continues if unavailable
    - Requires HF_TOKEN for pyannote model download
    """

    def __init__(self) -> None:
        self._pipeline = None
        self._available: Optional[bool] = None

    @property
    def available(self) -> bool:
        """Check if pyannote.audio is importable and configured."""
        if self._available is not None:
            return self._available

        try:
            import pyannote.audio  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False
            logger.info("pyannote.audio not installed — diarization unavailable")

        return self._available

    def _get_hf_token(self) -> Optional[str]:
        """Get HuggingFace token from environment."""
        cfg = get_config()
        token = os.environ.get(cfg.diarization.hf_token_env, "").strip()
        return token if token else None

    def _load_pipeline(self) -> None:
        """Load the pyannote diarization pipeline."""
        if self._pipeline is not None:
            return

        token = self._get_hf_token()
        if not token:
            raise DiarizationError(
                message="HF_TOKEN not set",
                detail=f"Set {get_config().diarization.hf_token_env} environment variable "
                "for pyannote.audio model access",
            )

        try:
            from pyannote.audio import Pipeline

            self._pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=token,
            )
            logger.info("Pyannote diarization pipeline loaded")
        except Exception as e:
            raise DiarizationError(
                message="Failed to load diarization pipeline",
                detail=str(e),
            )

    def diarize(
        self,
        audio_path: Path,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
    ) -> list[SpeakerSegment]:
        """Run speaker diarization on audio file.

        Args:
            audio_path: Path to WAV audio file
            min_speakers: Minimum expected speakers
            max_speakers: Maximum expected speakers

        Returns:
            List of SpeakerSegment with speaker labels and timestamps
        """
        cfg = get_config()

        if not cfg.diarization.enabled:
            return []

        if not self.available:
            if cfg.diarization.continue_without_diarization_on_error:
                logger.warning("Diarization unavailable, continuing without it")
                return []
            raise DiarizationError(
                message="Diarization enabled but pyannote.audio not available"
            )

        try:
            self._load_pipeline()
        except DiarizationError:
            if cfg.diarization.continue_without_diarization_on_error:
                logger.warning("Failed to load diarization pipeline, continuing without it")
                return []
            raise

        min_sp = min_speakers or cfg.diarization.min_speakers
        max_sp = max_speakers or cfg.diarization.max_speakers

        try:
            kwargs = {}
            if min_sp is not None:
                kwargs["min_speakers"] = min_sp
            if max_sp is not None:
                kwargs["max_speakers"] = max_sp

            diarization = self._pipeline(str(audio_path), **kwargs)

            segments: list[SpeakerSegment] = []
            for turn, _, speaker in diarization.itertracks(yield_label=True):
                segments.append(SpeakerSegment(
                    speaker=speaker,
                    start=turn.start,
                    end=turn.end,
                ))

            logger.info(
                f"Diarization complete: {len(segments)} turns, "
                f"{len(set(s.speaker for s in segments))} speakers"
            )
            return segments

        except Exception as e:
            if cfg.diarization.continue_without_diarization_on_error:
                logger.warning(f"Diarization failed: {e}, continuing without it")
                return []
            raise DiarizationError(
                message="Diarization processing failed",
                detail=str(e),
            )


# Module singleton
_diarizer: Optional[SpeakerDiarizer] = None


def get_diarizer() -> SpeakerDiarizer:
    """Get or create the global diarizer instance."""
    global _diarizer
    if _diarizer is None:
        _diarizer = SpeakerDiarizer()
    return _diarizer


def reset_diarizer() -> None:
    """Reset diarizer singleton (for testing)."""
    global _diarizer
    _diarizer = None
