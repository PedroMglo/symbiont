"""Voice Activity Detection using the configured Silero backend."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from audio_transcribe.errors import VADUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class SpeechSegment:
    """A detected speech segment with timestamps."""
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class VADProcessor:
    """Voice Activity Detection using Silero VAD.

    VAD is fail-closed: when enabled, the Silero backend must be importable and
    loadable. Window segmentation policy is owned by the segmenter, not hidden
    inside this detector.
    """

    def __init__(
        self,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 500,
        speech_pad_ms: int = 200,
        sample_rate: int = 16000,
    ) -> None:
        self._min_speech_ms = min_speech_duration_ms
        self._min_silence_ms = min_silence_duration_ms
        self._speech_pad_ms = speech_pad_ms
        self._sample_rate = sample_rate
        self._model = None
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        """Check if Silero VAD is available."""
        if self._available is not None:
            return self._available
        try:
            import torch  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False
        return self._available

    def _load_model(self) -> None:
        """Load Silero VAD model (lazy)."""
        if self._model is not None:
            return
        try:
            import torch
            model, vad_utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
            self._model = model
            self._get_speech_timestamps = vad_utils[0]  # get_speech_timestamps
            logger.info("Silero VAD model loaded")
        except Exception as e:
            self._available = False
            raise VADUnavailableError(
                message="Silero VAD backend could not be loaded",
                detail=str(e),
            ) from e

    def detect_speech(self, audio_path: Path) -> list[SpeechSegment]:
        """Detect speech segments in audio file.

        Returns list of SpeechSegment with timestamps in seconds.
        """
        audio = self._load_audio(audio_path)
        if not self.available:
            raise VADUnavailableError(
                message="VAD is enabled but torch/Silero is not available",
                detail="Install the configured Silero VAD backend or choose window segmentation.",
            )
        self._load_model()
        return self._detect_silero(audio)

    def _load_audio(self, audio_path: Path) -> np.ndarray:
        """Load audio file as numpy array."""
        try:
            import wave
            with wave.open(str(audio_path), "rb") as wf:
                n_frames = wf.getnframes()
                audio_bytes = wf.readframes(n_frames)
                audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                return audio
        except Exception as e:
            raise VADUnavailableError(
                message="Failed to load audio for VAD",
                detail=str(e),
            ) from e

    def _detect_silero(self, audio: np.ndarray) -> list[SpeechSegment]:
        """Use Silero VAD model for detection."""
        import torch

        audio_tensor = torch.from_numpy(audio)
        speech_timestamps = self._get_speech_timestamps(
            audio_tensor,
            self._model,
            sampling_rate=self._sample_rate,
            min_speech_duration_ms=self._min_speech_ms,
            min_silence_duration_ms=self._min_silence_ms,
            speech_pad_ms=self._speech_pad_ms,
        )

        segments = []
        for ts in speech_timestamps:
            start = ts["start"] / self._sample_rate
            end = ts["end"] / self._sample_rate
            segments.append(SpeechSegment(start=start, end=end))

        logger.info(f"Silero VAD: detected {len(segments)} speech segments")
        return segments
