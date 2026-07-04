"""Audio segmentation for long files: VAD-based with window fallback."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from audio_transcribe.config import get_config
from audio_transcribe.types import AudioSegment
from audio_transcribe.vad import SpeechSegment, VADProcessor

logger = logging.getLogger(__name__)


class LongAudioSegmenter:
    """Segments long audio files using VAD-first strategy with window fallback.

    Strategy 'vad_then_window':
    1. Run VAD to find speech regions
    2. Group nearby speech segments into chunks ≤ max_segment_duration
    3. Apply overlap between chunks to avoid cutting mid-sentence
    4. Fall back to fixed windows if VAD fails or produces no results
    """

    def __init__(
        self,
        max_segment_duration: float = 300.0,
        overlap_seconds: float = 2.0,
        strategy: str = "vad_then_window",
        vad_processor: VADProcessor | None = None,
    ) -> None:
        self._max_duration = max_segment_duration
        self._overlap = overlap_seconds
        self._strategy = strategy
        self._vad = vad_processor

    def segment(
        self,
        audio_path: Path,
        total_duration: float,
        segments_dir: Path,
    ) -> list[AudioSegment]:
        """Segment audio file into manageable chunks.

        Args:
            audio_path: Path to preprocessed WAV file
            total_duration: Total audio duration in seconds
            segments_dir: Directory to store segment metadata

        Returns:
            List of AudioSegment records with absolute timestamps
        """
        cfg = get_config()

        # Short audio: single segment
        if total_duration <= self._max_duration:
            seg = AudioSegment(
                segment_id=str(uuid.uuid4()),
                index=0,
                start=0.0,
                end=total_duration,
                duration=total_duration,
                file_path=str(audio_path),
            )
            return [seg]

        # Try VAD-based segmentation first
        if self._strategy in ("vad_then_window", "vad"):
            segments = self._segment_by_vad(audio_path, total_duration)
            if segments:
                logger.info(f"VAD segmentation: {len(segments)} segments")
                return segments
            if self._strategy == "vad":
                logger.warning("VAD produced no segments, no fallback configured")
                return self._segment_by_window(total_duration)

        # Fallback: fixed window segmentation
        if cfg.vad.fallback_to_window_segmentation or self._strategy == "window":
            segments = self._segment_by_window(total_duration)
            logger.info(f"Window segmentation fallback: {len(segments)} segments")
            return segments

        # Last resort
        return self._segment_by_window(total_duration)

    def _segment_by_vad(
        self, audio_path: Path, total_duration: float
    ) -> list[AudioSegment]:
        """Segment using VAD: group speech regions into chunks."""
        if self._vad is None:
            cfg = get_config()
            self._vad = VADProcessor(
                min_speech_duration_ms=cfg.vad.min_speech_duration_ms,
                min_silence_duration_ms=cfg.vad.min_silence_duration_ms,
                speech_pad_ms=cfg.vad.speech_pad_ms,
            )

        speech_segments = self._vad.detect_speech(audio_path)
        if not speech_segments:
            return []

        # Group speech segments into chunks respecting max duration
        chunks = self._group_speech_segments(speech_segments, total_duration)
        return chunks

    def _group_speech_segments(
        self, speech_segments: list[SpeechSegment], total_duration: float
    ) -> list[AudioSegment]:
        """Group VAD speech segments into chunks ≤ max_segment_duration."""
        segments: list[AudioSegment] = []
        current_start = speech_segments[0].start
        current_end = speech_segments[0].end
        index = 0

        for i in range(1, len(speech_segments)):
            speech = speech_segments[i]
            potential_end = speech.end
            chunk_duration = potential_end - current_start

            if chunk_duration > self._max_duration:
                # Current chunk is full, emit it
                segments.append(AudioSegment(
                    segment_id=str(uuid.uuid4()),
                    index=index,
                    start=max(0, current_start - self._overlap),
                    end=min(total_duration, current_end + self._overlap),
                    duration=current_end - current_start,
                    file_path="",
                ))
                index += 1
                current_start = speech.start
                current_end = speech.end
            else:
                current_end = speech.end

        # Emit last chunk
        segments.append(AudioSegment(
            segment_id=str(uuid.uuid4()),
            index=index,
            start=max(0, current_start - self._overlap),
            end=min(total_duration, current_end + self._overlap),
            duration=current_end - current_start,
            file_path="",
        ))

        return segments

    def _segment_by_window(self, total_duration: float) -> list[AudioSegment]:
        """Fixed window segmentation with overlap."""
        segments: list[AudioSegment] = []
        step = self._max_duration - self._overlap
        index = 0
        pos = 0.0

        while pos < total_duration:
            end = min(pos + self._max_duration, total_duration)
            segments.append(AudioSegment(
                segment_id=str(uuid.uuid4()),
                index=index,
                start=pos,
                end=end,
                duration=end - pos,
                file_path="",
            ))
            index += 1
            pos += step
            if end >= total_duration:
                break

        return segments
