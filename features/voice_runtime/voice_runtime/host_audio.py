"""Host-side audio capture helpers for voice_gateway prototypes."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field


class PipeWireRecorderConfig(BaseModel):
    """Configuration for recording raw PCM through PipeWire's pw-record CLI."""

    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    sample_format: str = "s16"
    duration_seconds: float = Field(default=4.0, gt=0.0, le=120.0)
    target: str | None = None


def build_pw_record_command(output_path: Path, config: PipeWireRecorderConfig) -> list[str]:
    """Build a pw-record command that writes raw PCM16 to output_path."""
    sample_count = int(config.sample_rate * config.duration_seconds)
    command = [
        "pw-record",
        "--raw",
        "--rate",
        str(config.sample_rate),
        "--channels",
        str(config.channels),
        "--format",
        config.sample_format,
        "--sample-count",
        str(sample_count),
    ]
    if config.target:
        command.extend(["--target", config.target])
    command.append(str(output_path))
    return command


def record_pcm_with_pw_record(
    output_path: Path,
    config: PipeWireRecorderConfig,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    """Record raw PCM to output_path using host PipeWire.

    This function is intentionally host-side and optional. It must not run in
    the audio_transcribe container.
    """
    if shutil.which("pw-record") is None:
        raise RuntimeError("pw-record is not available on PATH")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_pw_record_command(output_path, config)
    result = run(command, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return output_path
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path
    details = "\n".join(
        part
        for part in (
            f"pw-record failed with exit code {result.returncode}",
            f"command: {' '.join(command)}",
            f"stdout: {result.stdout.strip()}" if result.stdout else "",
            f"stderr: {result.stderr.strip()}" if result.stderr else "",
        )
        if part
    )
    raise RuntimeError(details)
