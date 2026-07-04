"""LibreOffice headless conversion adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path

from extrator.config import get_config
from extrator.errors import AdapterUnavailable, ConversionError


def convert(input_path: Path, output_dir: Path, output_format: str) -> Path:
    cfg = get_config()
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        cfg.conversion.libreoffice_binary,
        "--headless",
        "--convert-to",
        output_format,
        "--outdir",
        str(output_dir),
        str(input_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=cfg.conversion.timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise AdapterUnavailable("LibreOffice binary is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("LibreOffice conversion timed out") from exc
    if result.returncode != 0:
        raise ConversionError(result.stderr.strip() or "LibreOffice conversion failed")
    expected = output_dir / f"{input_path.stem}.{output_format}"
    if not expected.exists():
        matches = list(output_dir.glob(f"{input_path.stem}.*"))
        if matches:
            return matches[0]
        raise ConversionError("LibreOffice did not create an output file")
    return expected
