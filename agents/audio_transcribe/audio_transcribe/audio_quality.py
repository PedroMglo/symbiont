"""Audio quality analysis: volume, clipping, silence, SNR estimation."""

from __future__ import annotations

import logging
import subprocess
import shutil
from pathlib import Path
from typing import Optional

from audio_transcribe.types import AudioQualityReport

logger = logging.getLogger(__name__)


class AudioQualityAnalyzer:
    """Analyzes audio quality and generates warnings."""

    def analyze(self, audio_path: Path, duration_seconds: float = 0.0) -> AudioQualityReport:
        """Analyze audio file quality using ffmpeg volumedetect and silencedetect."""
        report = AudioQualityReport(duration_seconds=duration_seconds)

        # Volume analysis
        vol_info = self._analyze_volume(audio_path)
        if vol_info:
            report.mean_volume_db = vol_info.get("mean")
            report.peak_volume_db = vol_info.get("peak")

        # Clipping detection
        if report.peak_volume_db is not None and report.peak_volume_db >= -0.5:
            report.clipping_detected = True

        # Low volume detection
        if report.mean_volume_db is not None and report.mean_volume_db < -35.0:
            report.low_volume = True

        # Silence analysis
        silence_duration = self._detect_silence_duration(audio_path, duration_seconds)
        if duration_seconds > 0 and silence_duration is not None:
            report.silence_ratio = silence_duration / duration_seconds

        # Sample rate / channels from file
        sr_info = self._get_stream_info(audio_path)
        if sr_info:
            report.sample_rate = sr_info.get("sample_rate", 0)
            report.channels = sr_info.get("channels", 0)

        # Generate warnings
        from audio_transcribe.config import get_config
        cfg = get_config()

        if cfg.audio_quality.warn_on_clipping and report.clipping_detected:
            report.warnings.append("Audio clipping detected — peaks at 0dB")
        if cfg.audio_quality.warn_on_low_volume and report.low_volume:
            report.warnings.append(
                f"Low audio volume: mean={report.mean_volume_db:.1f}dB"
            )
        if cfg.audio_quality.warn_on_high_silence_ratio and report.silence_ratio > 0.7:
            report.warnings.append(
                f"High silence ratio: {report.silence_ratio:.0%} of audio is silent"
            )

        if report.warnings:
            logger.warning(f"Audio quality issues: {report.warnings}")

        return report

    def _analyze_volume(self, audio_path: Path) -> Optional[dict]:
        """Use ffmpeg volumedetect to get mean/peak volume."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None

        cmd = [
            ffmpeg,
            "-i", str(audio_path),
            "-af", "volumedetect",
            "-f", "null",
            "-",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        # Parse stderr for volume info
        output = result.stderr
        info: dict[str, float] = {}

        for line in output.splitlines():
            if "mean_volume" in line:
                try:
                    val = line.split("mean_volume:")[1].strip().split(" ")[0]
                    info["mean"] = float(val)
                except (IndexError, ValueError):
                    pass
            elif "max_volume" in line:
                try:
                    val = line.split("max_volume:")[1].strip().split(" ")[0]
                    info["peak"] = float(val)
                except (IndexError, ValueError):
                    pass

        return info if info else None

    def _detect_silence_duration(
        self, audio_path: Path, total_duration: float
    ) -> Optional[float]:
        """Detect total silence duration using ffmpeg silencedetect."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None

        cmd = [
            ffmpeg,
            "-i", str(audio_path),
            "-af", "silencedetect=noise=-40dB:d=0.5",
            "-f", "null",
            "-",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        # Parse silence durations from stderr
        total_silence = 0.0
        for line in result.stderr.splitlines():
            if "silence_duration" in line:
                try:
                    val = line.split("silence_duration:")[1].strip().split("|")[0].strip()
                    total_silence += float(val)
                except (IndexError, ValueError):
                    pass

        return total_silence

    def _get_stream_info(self, audio_path: Path) -> Optional[dict]:
        """Get basic stream info from ffprobe."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None

        cmd = [
            ffprobe,
            "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-print_format", "json",
            str(audio_path),
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            return None

        if result.returncode != 0:
            return None

        import json
        try:
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
            if streams:
                return {
                    "sample_rate": int(streams[0].get("sample_rate", 0)),
                    "channels": int(streams[0].get("channels", 0)),
                }
        except (json.JSONDecodeError, ValueError, IndexError):
            pass

        return None
