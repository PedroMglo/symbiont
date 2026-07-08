"""Noise reduction interface for explicitly configured backends."""

from __future__ import annotations

import logging
from pathlib import Path

from audio_transcribe.errors import NoiseReductionError

logger = logging.getLogger(__name__)


class NoiseReducer:
    """Interface for noise reduction with pluggable backends.

    Backends must be implemented before they can be selected.
    """

    def __init__(self, backend: str = "none") -> None:
        self._backend = backend

    @property
    def available(self) -> bool:
        """Check if the configured backend is actually available."""
        return False

    def process(self, audio_path: Path, output_path: Path) -> Path:
        """Apply noise reduction to audio file."""

        if not self.available:
            raise NoiseReductionError(
                message=f"Noise reduction backend '{self._backend}' is not available",
                detail="Disable noise_reduction or configure an implemented backend.",
            )

        return self._apply_backend(audio_path, output_path)

    def _apply_backend(self, audio_path: Path, output_path: Path) -> Path:
        """Apply the configured backend. Override in subclasses."""
        raise NoiseReductionError(
            message=f"Noise reduction backend '{self._backend}' is not implemented",
            detail=f"input={audio_path} output={output_path}",
        )


def create_noise_reducer(enabled: bool = False, backend: str = "none") -> NoiseReducer | None:
    """Factory function to create appropriate noise reducer."""
    if not enabled:
        return None
    if backend == "none":
        raise NoiseReductionError(
            message="Noise reduction enabled but no backend is configured",
            detail="Set preprocessing.noise_reduction_backend to an implemented backend or disable noise_reduction.",
        )
    return NoiseReducer(backend=backend)
