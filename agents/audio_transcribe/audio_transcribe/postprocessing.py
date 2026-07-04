"""Transcript postprocessing: filler removal, cleanup, paragraphing."""

from __future__ import annotations

import logging
import re
from typing import Optional

from audio_transcribe.config import get_config
from audio_transcribe.types import CleanTranscript, TranscriptSegment

logger = logging.getLogger(__name__)

# Fillers in Portuguese and English (conservative list)
FILLERS_PT = [
    r"\bhã\b", r"\bhum\b", r"\btipo\b", r"\bpronto\b",
    r"\bok\s+ok\b",
]
FILLERS_EN = [
    r"\byou know\b", r"\blike\b", r"\bum\b", r"\buh\b",
    r"\berm\b", r"\bhmm\b",
]

# Combined filler patterns
FILLER_PATTERNS = [re.compile(p, re.IGNORECASE) for p in FILLERS_PT + FILLERS_EN]

# Repetition pattern: word repeated 2+ times consecutively
REPETITION_PATTERN = re.compile(r"\b(\w+)(\s+\1){2,}\b", re.IGNORECASE)


class TranscriptPostProcessor:
    """Clean up transcription output while preserving meaning.

    Rules:
    - Conservative cleanup: don't remove words that may have meaning
    - Preserve timestamps and speakers
    - Keep raw + clean versions separate
    - Don't destroy technical content
    - Don't invent content
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._remove_fillers = cfg.postprocessing.remove_fillers
        self._remove_repetitions = cfg.postprocessing.remove_repetitions
        self._paragraphs = cfg.postprocessing.paragraphs
        self._conservative = cfg.postprocessing.conservative_cleanup

    def process(
        self, segments: list[TranscriptSegment], speakers: Optional[list[str]] = None
    ) -> CleanTranscript:
        """Process raw transcript segments into clean transcript.

        Returns CleanTranscript with cleaned segments.
        """
        clean_segments: list[TranscriptSegment] = []

        for seg in segments:
            cleaned = self._clean_segment(seg)
            if cleaned:
                clean_segments.append(cleaned)

        # Re-index
        for i, seg in enumerate(clean_segments):
            seg.index = i

        # Merge very short adjacent segments from same speaker if paragraphing
        if self._paragraphs:
            clean_segments = self._apply_paragraphs(clean_segments)

        all_speakers = list(set(
            s.speaker for s in clean_segments if s.speaker
        ))
        all_speakers.sort()

        duration = clean_segments[-1].end if clean_segments else 0.0

        return CleanTranscript(
            segments=clean_segments,
            language=segments[0].language or "" if segments else "",
            speakers=speakers or all_speakers,
            duration_seconds=duration,
        )

    def _clean_segment(self, segment: TranscriptSegment) -> Optional[TranscriptSegment]:
        """Clean a single segment. Returns None if segment becomes empty."""
        text = segment.text.strip()

        if not text:
            return None

        # Remove fillers
        if self._remove_fillers:
            text = self._remove_filler_words(text)

        # Remove repetitions
        if self._remove_repetitions:
            text = self._remove_repetition_words(text)

        # Normalize whitespace
        text = re.sub(r"\s+", " ", text).strip()

        # Skip empty or too-short segments after cleanup
        if not text or len(text) < 2:
            return None

        # Basic capitalization
        text = self._capitalize(text)

        return TranscriptSegment(
            index=segment.index,
            start=segment.start,
            end=segment.end,
            text=text,
            speaker=segment.speaker,
            confidence=segment.confidence,
            language=segment.language,
            no_speech_prob=segment.no_speech_prob,
            words=segment.words,
        )

    def _remove_filler_words(self, text: str) -> str:
        """Remove filler words conservatively."""
        for pattern in FILLER_PATTERNS:
            # Only remove if not the entire content
            cleaned = pattern.sub("", text).strip()
            if cleaned:  # Don't remove if it would empty the text
                text = cleaned
        return text

    def _remove_repetition_words(self, text: str) -> str:
        """Remove consecutive word repetitions (3+)."""
        # Replace "word word word" with "word"
        return REPETITION_PATTERN.sub(r"\1", text)

    def _capitalize(self, text: str) -> str:
        """Basic sentence capitalization."""
        if not text:
            return text
        # Capitalize first letter
        result = text[0].upper() + text[1:]
        # Capitalize after sentence endings
        result = re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), result)
        return result

    def _apply_paragraphs(self, segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
        """Group consecutive segments from same speaker into paragraphs.

        Only merge if:
        - Same speaker
        - Gap between segments < 2s
        - Combined text not too long
        """
        if not segments:
            return segments

        merged: list[TranscriptSegment] = []
        current = segments[0]

        for seg in segments[1:]:
            same_speaker = current.speaker == seg.speaker
            small_gap = (seg.start - current.end) < 2.0
            short_enough = len(current.text) + len(seg.text) < 500

            if same_speaker and small_gap and short_enough:
                # Merge into current
                current = TranscriptSegment(
                    index=current.index,
                    start=current.start,
                    end=seg.end,
                    text=f"{current.text} {seg.text}",
                    speaker=current.speaker,
                    confidence=current.confidence,
                    language=current.language,
                    words=current.words + seg.words,
                )
            else:
                merged.append(current)
                current = seg

        merged.append(current)

        # Re-index
        for i, seg in enumerate(merged):
            seg.index = i

        return merged
