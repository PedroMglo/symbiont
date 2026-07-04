"""Optional OCR adapter."""

from __future__ import annotations

from pathlib import Path

from extrator.config import get_config
from extrator.errors import AdapterUnavailable


def parse(path: Path):
    if not get_config().parsers.ocr_enabled:
        raise AdapterUnavailable("OCR is disabled by configuration")
    raise AdapterUnavailable(f"OCR adapter is configured but not implemented for: {path}")
