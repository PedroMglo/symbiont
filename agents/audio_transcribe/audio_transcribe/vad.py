"""Voice Activity Detection using Silero VAD with fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

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

    Falls back to simple energy-based detection if Silero unavailable.
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
        self._available: Optional[bool] = None

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
            logger.warning("torch not available — VAD will use energy-based fallback")
        return self._available

    def _load_model(self):
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
            logger.warning(f"Failed to load Silero VAD: {e}")
            self._available = False

    def detect_speech(self, audio_path: Path) -> list[SpeechSegment]:
        """Detect speech segments in audio file.

        Returns list of SpeechSegment with timestamps in seconds.
        """
        audio = self._load_audio(audio_path)
        if audio is None:
            return []

        if self.available:
            self._load_model()
            if self._model is not None:
                return self._detect_silero(audio)

        # Fallback: energy-based detection
        return self._detect_energy(audio)

    def _load_audio(self, audio_path: Path) -> Optional[np.ndarray]:
        """Load audio file as numpy array."""
        try:
            import wave
            with wave.open(str(audio_path), "rb") as wf:
                n_frames = wf.getnframes()
                audio_bytes = wf.readframes(n_frames)
                audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                return audio
        except Exception as e:
            logger.error(f"Failed to load audio for VAD: {e}")
            return None

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

    def _detect_energy(self, audio: np.ndarray) -> list[SpeechSegment]:
        """Simple energy-based VAD fallback."""
        frame_size = int(self._sample_rate * 0.03)  # 30ms frames
        hop = frame_size // 2
        threshold = 0.01  # Energy threshold

        segments: list[SpeechSegment] = []
        in_speech = False
        speech_start = 0.0
        min_speech_samples = int(self._min_speech_ms * self._sample_rate / 1000)
        min_silence_samples = int(self._min_silence_ms * self._sample_rate / 1000)

        silence_counter = 0
        speech_counter = 0

        for i in range(0, len(audio) - frame_size, hop):
            frame = audio[i : i + frame_size]
            energy = float(np.mean(frame ** 2))

            if energy > threshold:
                if not in_speech:
                    speech_start = i / self._sample_rate
                    in_speech = True
                    silence_counter = 0
                speech_counter += hop
                silence_counter = 0
            else:
                if in_speech:
                    silence_counter += hop
                    if silence_counter >= min_silence_samples:
                        speech_end = (i - silence_counter) / self._sample_rate
                        if speech_counter >= min_speech_samples:
                            segments.append(SpeechSegment(
                                start=max(0, speech_start - self._speech_pad_ms / 1000),
                                end=speech_end + self._speech_pad_ms / 1000,
                            ))
                        in_speech = False
                        speech_counter = 0
                        silence_counter = 0

        # Handle trailing speech
        if in_speech and speech_counter >= min_speech_samples:
            segments.append(SpeechSegment(
                start=max(0, speech_start - self._speech_pad_ms / 1000),
                end=len(audio) / self._sample_rate,
            ))

        logger.info(f"Energy VAD fallback: detected {len(segments)} speech segments")
        return segments
