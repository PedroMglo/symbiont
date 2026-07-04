"""Speaker alignment: map diarization speakers to transcript segments."""

from __future__ import annotations

import logging
from typing import Optional

from audio_transcribe.types import SpeakerSegment, TranscriptSegment

logger = logging.getLogger(__name__)


class SpeakerAligner:
    """Aligns speaker diarization results with transcript segments.

    Uses temporal intersection to assign speaker labels (SPEAKER_01, SPEAKER_02, etc.)
    to transcript segments.
    """

    def align(
        self,
        transcript_segments: list[TranscriptSegment],
        speaker_segments: list[SpeakerSegment],
    ) -> list[TranscriptSegment]:
        """Assign speaker labels to transcript segments based on temporal overlap.

        Args:
            transcript_segments: Transcription output with timestamps
            speaker_segments: Diarization output with speaker labels

        Returns:
            Updated transcript segments with speaker field populated
        """
        if not speaker_segments:
            return transcript_segments

        # Build normalized speaker map (SPEAKER_01, SPEAKER_02, ...)
        raw_speakers = sorted(set(s.speaker for s in speaker_segments))
        speaker_map = {
            raw: f"SPEAKER_{i+1:02d}" for i, raw in enumerate(raw_speakers)
        }

        for seg in transcript_segments:
            best_speaker = self._find_best_speaker(seg, speaker_segments, speaker_map)
            if best_speaker:
                seg.speaker = best_speaker

        assigned = sum(1 for s in transcript_segments if s.speaker)
        logger.info(
            f"Speaker alignment: {assigned}/{len(transcript_segments)} segments assigned, "
            f"{len(raw_speakers)} unique speakers"
        )
        return transcript_segments

    def _find_best_speaker(
        self,
        segment: TranscriptSegment,
        speaker_segments: list[SpeakerSegment],
        speaker_map: dict[str, str],
    ) -> Optional[str]:
        """Find the speaker with maximum overlap for a transcript segment."""
        best_overlap = 0.0
        best_speaker: Optional[str] = None

        for sp_seg in speaker_segments:
            overlap = self._compute_overlap(
                segment.start, segment.end, sp_seg.start, sp_seg.end
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = speaker_map.get(sp_seg.speaker, sp_seg.speaker)

        return best_speaker

    def _compute_overlap(
        self, start1: float, end1: float, start2: float, end2: float
    ) -> float:
        """Compute temporal overlap duration between two intervals."""
        overlap_start = max(start1, start2)
        overlap_end = min(end1, end2)
        return max(0.0, overlap_end - overlap_start)

    def get_speaker_list(self, segments: list[TranscriptSegment]) -> list[str]:
        """Extract unique ordered speaker list from aligned segments."""
        seen = set()
        speakers = []
        for seg in segments:
            if seg.speaker and seg.speaker not in seen:
                seen.add(seg.speaker)
                speakers.append(seg.speaker)
        return speakers
