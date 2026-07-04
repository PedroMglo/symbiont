"""Audio preprocessing: FFmpeg extraction, conversion, normalization."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from audio_transcribe.config import get_config
from audio_transcribe.errors import FFmpegNotFoundError, InvalidInputError
from audio_transcribe.types import AudioMetadata

logger = logging.getLogger(__name__)


@dataclass
class PreprocessResult:
    audio_path: Path
    metadata: AudioMetadata


class AudioPreprocessor:
    """Handles audio extraction, conversion, and normalization via FFmpeg."""

    def __init__(self) -> None:
        self._ffmpeg_path: Optional[str] = None

    def _get_ffmpeg(self) -> str:
        """Find ffmpeg binary path."""
        if self._ffmpeg_path:
            return self._ffmpeg_path
        path = shutil.which("ffmpeg")
        if not path:
            raise FFmpegNotFoundError(
                message="ffmpeg not found",
                detail="Install ffmpeg: apt-get install ffmpeg",
            )
        self._ffmpeg_path = path
        return path

    def _get_ffprobe(self) -> str:
        """Find ffprobe binary path."""
        path = shutil.which("ffprobe")
        if not path:
            raise FFmpegNotFoundError(
                message="ffprobe not found",
                detail="Install ffprobe (included with ffmpeg)",
            )
        return path

    async def probe_file(self, input_path: Path) -> AudioMetadata:
        """Probe media file for metadata using ffprobe."""
        ffprobe = self._get_ffprobe()

        cmd = [
            ffprobe,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(input_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise InvalidInputError(
                    message="File probe timed out",
                    detail="ffprobe took too long — file may be corrupted",
                )
        except OSError as e:
            raise FFmpegNotFoundError(message=f"Failed to run ffprobe: {e}")

        if proc.returncode != 0:
            raise InvalidInputError(
                message="Cannot read media file",
                detail=f"ffprobe error: {stderr.decode()[:500]}",
            )

        import json
        try:
            info = json.loads(stdout.decode())
        except json.JSONDecodeError:
            raise InvalidInputError(message="Invalid ffprobe output")

        fmt = info.get("format", {})
        streams = info.get("streams", [])

        # Find audio stream
        audio_stream = None
        for s in streams:
            if s.get("codec_type") == "audio":
                audio_stream = s
                break

        duration = float(fmt.get("duration", 0))
        file_size = int(fmt.get("size", 0))
        format_name = fmt.get("format_name", "")

        sample_rate = 0
        channels = 0
        codec = ""
        if audio_stream:
            sample_rate = int(audio_stream.get("sample_rate", 0))
            channels = int(audio_stream.get("channels", 0))
            codec = audio_stream.get("codec_name", "")

        return AudioMetadata(
            file_path=str(input_path),
            format=format_name,
            duration_seconds=duration,
            sample_rate=sample_rate,
            channels=channels,
            file_size_bytes=file_size,
            codec=codec,
        )

    async def process(self, input_path: Path, output_dir: Path) -> PreprocessResult:
        """Full preprocessing pipeline: probe → extract → convert → normalize.

        Returns path to processed WAV file and metadata.
        """
        cfg = get_config()

        # Probe input
        metadata = await self.probe_file(input_path)
        if metadata.duration_seconds <= 0:
            raise InvalidInputError(
                message="Cannot determine audio duration",
                detail="File may be empty or corrupted",
            )

        # Output WAV path
        output_path = output_dir / "audio.wav"

        # Build ffmpeg command
        ffmpeg = self._get_ffmpeg()
        cmd = [
            ffmpeg,
            "-y",
            "-i", str(input_path),
            "-vn",  # No video
            "-acodec", "pcm_s16le",
            "-ar", str(cfg.preprocessing.sample_rate),
        ]

        # Mono conversion
        if cfg.preprocessing.mono:
            cmd.extend(["-ac", "1"])

        # Loudness normalization
        if cfg.preprocessing.normalize_loudness:
            cmd.extend(["-af", "loudnorm=I=-16:LRA=11:TP=-1.5"])

        cmd.append(str(output_path))

        logger.info(f"Preprocessing: {input_path.name} → WAV ({cfg.preprocessing.sample_rate}Hz)")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise InvalidInputError(
                    message="Audio extraction timed out",
                    detail="FFmpeg took too long — file may be very large or corrupted",
                )
        except OSError as e:
            raise FFmpegNotFoundError(message=f"Failed to run ffmpeg: {e}")

        if proc.returncode != 0:
            raise InvalidInputError(
                message="Audio extraction failed",
                detail=f"ffmpeg error: {stderr.decode()[:500]}",
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise InvalidInputError(
                message="Audio extraction produced empty output",
                detail="FFmpeg completed but output file is empty",
            )

        # Update metadata with processed info
        metadata.sample_rate = cfg.preprocessing.sample_rate
        if cfg.preprocessing.mono:
            metadata.channels = 1

        return PreprocessResult(audio_path=output_path, metadata=metadata)

    async def extract_segment(
        self,
        input_path: Path,
        output_path: Path,
        start_seconds: float,
        duration_seconds: float,
    ) -> Path:
        """Extract a time segment from an audio file."""
        ffmpeg = self._get_ffmpeg()
        cmd = [
            ffmpeg,
            "-y",
            "-ss", str(start_seconds),
            "-t", str(duration_seconds),
            "-i", str(input_path),
            "-acodec", "pcm_s16le",
            str(output_path),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise InvalidInputError(
                message=f"Segment extraction timed out at {start_seconds}s",
                detail="FFmpeg took too long",
            )

        if proc.returncode != 0:
            raise InvalidInputError(
                message=f"Segment extraction failed at {start_seconds}s",
                detail=stderr.decode()[:300],
            )

        return output_path
