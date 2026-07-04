"""Merge + Alignment Engine.

Handles:
- Real-time: incremental merging of partial transcripts
- Batch: overlap trimming, timestamp realignment, duplicate removal
- Cross-mode: unified final transcript assembly
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """A single transcript segment."""
    index: int
    start: float
    end: float
    text: str
    language: str = ""
    confidence: float = 0.0
    is_final: bool = True
    source: str = "realtime"  # realtime | batch


@dataclass
class MergedTranscript:
    """Final merged transcript output."""
    segments: list[TranscriptSegment] = field(default_factory=list)
    full_text: str = ""
    language: str = ""
    total_duration: float = 0.0
    total_segments: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_text": self.full_text,
            "language": self.language,
            "total_duration": self.total_duration,
            "total_segments": self.total_segments,
            "segments": [
                {
                    "index": s.index,
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                    "confidence": s.confidence,
                }
                for s in self.segments
            ],
        }


class MergeEngine:
    """Merges transcript segments from real-time and batch sources.

    Handles:
    - Overlap detection and removal (batch chunks with 1-2s overlap)
    - Timestamp alignment (real-time segments may have clock drift)
    - Duplicate text detection (same audio processed twice)
    - Gap filling (silence between segments)
    """

    def __init__(self, overlap_threshold: float = 1.5):
        self._overlap_threshold = overlap_threshold
        self._segments: list[TranscriptSegment] = []

    def add_segment(self, segment: TranscriptSegment) -> None:
        """Add a segment to the merge buffer."""
        self._segments.append(segment)

    def add_segments(self, segments: list[TranscriptSegment]) -> None:
        """Add multiple segments."""
        self._segments.extend(segments)

    def merge(self) -> MergedTranscript:
        """Perform full merge and return final transcript."""
        if not self._segments:
            return MergedTranscript()

        # Sort by start time
        sorted_segs = sorted(self._segments, key=lambda s: s.start)

        # Remove overlapping segments
        merged = self._remove_overlaps(sorted_segs)

        # Remove duplicate text (consecutive identical segments)
        merged = self._remove_duplicates(merged)

        # Build final text
        full_text = " ".join(seg.text for seg in merged if seg.text.strip())

        # Detect majority language
        languages = [s.language for s in merged if s.language]
        lang = max(set(languages), key=languages.count) if languages else ""

        total_duration = merged[-1].end if merged else 0.0

        return MergedTranscript(
            segments=merged,
            full_text=full_text,
            language=lang,
            total_duration=total_duration,
            total_segments=len(merged),
        )

    def _remove_overlaps(self, segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
        """Remove overlapping segment text from batch processing."""
        if len(segments) <= 1:
            return segments

        result = [segments[0]]

        for seg in segments[1:]:
            prev = result[-1]
            overlap = prev.end - seg.start

            if overlap > self._overlap_threshold:
                # Significant overlap — trim the overlapping text
                seg = self._trim_overlap(prev, seg, overlap)

            if overlap > 0 and overlap <= self._overlap_threshold:
                # Minor overlap — adjust timestamps only
                seg = TranscriptSegment(
                    index=seg.index,
                    start=prev.end,
                    end=seg.end,
                    text=seg.text,
                    language=seg.language,
                    confidence=seg.confidence,
                    is_final=seg.is_final,
                    source=seg.source,
                )

            if seg.text.strip():
                result.append(seg)

        return result

    def _trim_overlap(
        self, prev: TranscriptSegment, current: TranscriptSegment, overlap: float
    ) -> TranscriptSegment:
        """Trim overlapping text from the beginning of current segment."""
        if not current.text or not prev.text:
            return current

        # Estimate words to trim (based on overlap fraction)
        total_duration = current.end - current.start
        if total_duration <= 0:
            return current

        overlap_fraction = overlap / total_duration
        words = current.text.split()
        words_to_trim = int(len(words) * overlap_fraction)

        if words_to_trim > 0 and words_to_trim < len(words):
            trimmed_text = " ".join(words[words_to_trim:])
            return TranscriptSegment(
                index=current.index,
                start=prev.end,
                end=current.end,
                text=trimmed_text,
                language=current.language,
                confidence=current.confidence,
                is_final=current.is_final,
                source=current.source,
            )

        return current

    def _remove_duplicates(self, segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
        """Remove consecutive segments with identical or near-identical text."""
        if len(segments) <= 1:
            return segments

        result = [segments[0]]

        for seg in segments[1:]:
            prev_text = result[-1].text.strip().lower()
            curr_text = seg.text.strip().lower()

            # Exact duplicate
            if curr_text == prev_text:
                continue

            # Near-duplicate (one contains the other)
            if len(curr_text) > 10 and (curr_text in prev_text or prev_text in curr_text):
                # Keep the longer one
                if len(curr_text) > len(prev_text):
                    result[-1] = seg
                continue

            result.append(seg)

        return result

    def reset(self) -> None:
        """Clear the merge buffer."""
        self._segments = []
