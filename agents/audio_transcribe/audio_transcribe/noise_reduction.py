"""Noise reduction interface with no-op default implementation."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class NoiseReducer:
    """Interface for noise reduction with pluggable backends.

    Default implementation is a no-op pass-through.
    Future backends: RNNoise, spectral gating, Demucs.
    """

    def __init__(self, backend: str = "none") -> None:
        self._backend = backend

    @property
    def available(self) -> bool:
        """Check if the configured backend is actually available."""
        if self._backend == "none":
            return True
        # Future: check for RNNoise/noisereduce/demucs imports
        return False

    def process(self, audio_path: Path, output_path: Path) -> Path:
        """Apply noise reduction to audio file.

        Returns path to processed audio (may be same as input if no-op).
        """
        if self._backend == "none":
            logger.debug("Noise reduction disabled (no-op)")
            return audio_path

        if not self.available:
            logger.warning(
                f"Noise reduction backend '{self._backend}' not available, "
                "skipping noise reduction"
            )
            return audio_path

        # Future implementations would go here
        return self._apply_backend(audio_path, output_path)

    def _apply_backend(self, audio_path: Path, output_path: Path) -> Path:
        """Apply the configured backend. Override in subclasses."""
        # Placeholder for future backends
        logger.warning(f"Backend '{self._backend}' not implemented, returning original")
        return audio_path


def create_noise_reducer(enabled: bool = False, backend: str = "none") -> NoiseReducer:
    """Factory function to create appropriate noise reducer."""
    if not enabled:
        return NoiseReducer(backend="none")
    return NoiseReducer(backend=backend)
