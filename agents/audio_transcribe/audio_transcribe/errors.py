"""Custom exceptions for audio_transcribe service."""

from __future__ import annotations


class AudioTranscribeError(Exception):
    """Base exception for all audio_transcribe errors."""

    def __init__(self, message: str = "", detail: str = ""):
        self.message = message
        self.detail = detail
        super().__init__(message)


class UnsupportedMediaError(AudioTranscribeError):
    """File format/extension is not supported."""


class FFmpegNotFoundError(AudioTranscribeError):
    """FFmpeg binary not found on system."""


class InvalidInputError(AudioTranscribeError):
    """Input file is invalid, corrupted, or missing."""


class PathSecurityError(AudioTranscribeError):
    """Path traversal or unauthorized access attempt detected."""


class ModelLoadError(AudioTranscribeError):
    """Failed to load the transcription model."""


class TranscriptionError(AudioTranscribeError):
    """Error during transcription processing."""


class DiarizationError(AudioTranscribeError):
    """Error during speaker diarization."""


class ExportError(AudioTranscribeError):
    """Error during output export."""


class JobNotFoundError(AudioTranscribeError):
    """Requested job does not exist."""


class JobCancelledError(AudioTranscribeError):
    """Job was cancelled during processing."""


class CheckpointError(AudioTranscribeError):
    """Error reading/writing segment checkpoints."""


class AudioQualityError(AudioTranscribeError):
    """Audio quality is too poor to process."""


class SegmentationError(AudioTranscribeError):
    """Error during audio segmentation."""
