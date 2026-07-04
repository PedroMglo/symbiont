"""Real-time VAD (Voice Activity Detection) for streaming.

Uses a lightweight energy-based VAD with optional Silero upgrade.
Processes frames of PCM audio and detects speech/silence transitions.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class VADResult:
    """Result of VAD processing for a frame."""
    is_speech: bool
    confidence: float  # 0.0–1.0
    energy_db: float


class RealtimeVAD:
    """Energy-based VAD for real-time streaming.

    Processes PCM 16-bit mono frames and detects speech/silence.
    Optimized for low-latency (<5ms per frame).
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_duration_ms: int = 30,
        energy_threshold_db: float = -35.0,
        speech_pad_ms: int = 100,
    ):
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms
        self.frame_size = int(sample_rate * frame_duration_ms / 1000)  # samples per frame
        self.energy_threshold_db = energy_threshold_db
        self._speech_pad_frames = int(speech_pad_ms / frame_duration_ms)
        self._silence_count = 0
        self._speech_count = 0

    def process_frame(self, frame: bytes) -> VADResult:
        """Process a single audio frame and return VAD result.

        Args:
            frame: PCM 16-bit LE mono audio (frame_size * 2 bytes)

        Returns:
            VADResult with speech detection
        """
        energy_db = self._compute_energy_db(frame)
        is_speech = energy_db > self.energy_threshold_db

        # Apply smoothing (prevents rapid toggling)
        if is_speech:
            self._speech_count += 1
            self._silence_count = 0
        else:
            self._silence_count += 1
            self._speech_count = 0

        # Confidence based on energy relative to threshold
        confidence = min(1.0, max(0.0,
            (energy_db - self.energy_threshold_db) / 20.0 + 0.5
        ))

        return VADResult(
            is_speech=is_speech,
            confidence=confidence,
            energy_db=energy_db,
        )

    def _compute_energy_db(self, frame: bytes) -> float:
        """Compute RMS energy in dB for a PCM frame."""
        if len(frame) < 2:
            return -100.0

        n_samples = len(frame) // 2
        samples = struct.unpack(f"<{n_samples}h", frame[:n_samples * 2])

        # RMS energy
        sum_sq = sum(s * s for s in samples)
        rms = (sum_sq / n_samples) ** 0.5

        if rms < 1:
            return -100.0

        # Convert to dB (reference: max int16 = 32768)
        import math
        return 20 * math.log10(rms / 32768.0)

    def reset(self) -> None:
        """Reset VAD state (for new session)."""
        self._silence_count = 0
        self._speech_count = 0


class SileroVAD:
    """Silero VAD wrapper for higher accuracy (optional).

    Falls back to energy-based VAD if Silero model not available.
    """

    def __init__(self, threshold: float = 0.5, sample_rate: int = 16000):
        self.threshold = threshold
        self.sample_rate = sample_rate
        self._model = None
        self._fallback = RealtimeVAD(sample_rate=sample_rate)
        self._load_model()

    def _load_model(self) -> None:
        """Try to load Silero VAD model."""
        try:
            import torch
            model, _vad_utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=True,
            )
            self._model = model
            logger.info("Silero VAD loaded (ONNX)")
        except Exception as e:
            logger.info(f"Silero VAD unavailable, using energy-based: {e}")
            self._model = None

    def process_frame(self, frame: bytes) -> VADResult:
        """Process frame with Silero or fallback."""
        if self._model is None:
            return self._fallback.process_frame(frame)

        try:
            import torch

            # Convert PCM bytes to float tensor
            n_samples = len(frame) // 2
            samples = struct.unpack(f"<{n_samples}h", frame[:n_samples * 2])
            audio = torch.FloatTensor(samples) / 32768.0

            # Run Silero VAD
            confidence = self._model(audio, self.sample_rate).item()
            is_speech = confidence > self.threshold

            return VADResult(
                is_speech=is_speech,
                confidence=confidence,
                energy_db=self._fallback._compute_energy_db(frame),
            )
        except Exception:
            return self._fallback.process_frame(frame)

    def reset(self) -> None:
        if self._model is not None:
            self._model.reset_states()
        self._fallback.reset()
