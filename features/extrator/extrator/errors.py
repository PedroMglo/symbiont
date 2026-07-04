"""Custom errors for extrator."""

from __future__ import annotations


class ExtratorError(RuntimeError):
    """Base error for extrator."""


class AdapterUnavailable(ExtratorError):
    """Raised when an optional parser or converter is unavailable."""


class ConversionError(ExtratorError):
    """Raised when a conversion command fails."""


class ManifestError(ExtratorError):
    """Raised when manifest persistence fails."""
