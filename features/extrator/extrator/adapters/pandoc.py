"""Pandoc conversion adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path

from extrator.config import get_config
from extrator.errors import AdapterUnavailable, ConversionError


def convert(input_path: Path, output_path: Path) -> Path:
    cfg = get_config()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [cfg.conversion.pandoc_binary, str(input_path), "-o", str(output_path)]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=cfg.conversion.timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise AdapterUnavailable("Pandoc binary is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("Pandoc conversion timed out") from exc
    if result.returncode != 0:
        raise ConversionError(result.stderr.strip() or "Pandoc conversion failed")
    return output_path
