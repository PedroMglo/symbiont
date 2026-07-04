"""Rule-based semantic extraction for RAG-ready output."""

from __future__ import annotations

import logging
import re

from audio_transcribe.config import get_config
from audio_transcribe.types import (
    ActionItem,
    CleanTranscript,
    Decision,
    Entity,
    KeyQuote,
    SemanticSummary,
    SpeakerNote,
    Topic,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)

# Patterns for decision detection
DECISION_PATTERNS = [
    re.compile(r"(?:decid|agreed|decided|let's go with|vamos com|ficou decidido|decidimos)", re.I),
    re.compile(r"(?:the decision is|a decisão é|aprovado|approved)", re.I),
]

# Patterns for action items
ACTION_PATTERNS = [
    re.compile(r"(?:TODO|FIXME|action item|tarefa|vou fazer|need to|preciso de)", re.I),
    re.compile(r"(?:should|deve|has to|tem que|will do|vai fazer|fica para)", re.I),
    re.compile(r"(?:next step|próximo passo|follow up|seguimento)", re.I),
]

# Patterns for topic detection
TOPIC_INDICATORS = [
    re.compile(r"(?:about|sobre|regarding|em relação a|no que toca a)", re.I),
    re.compile(r"(?:the topic|o tema|o assunto|discussing|a discutir)", re.I),
]

# Date patterns
DATE_PATTERN = re.compile(
    r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)"
    r"\s+\d{1,2}(?:,?\s+\d{4})?)\b",
    re.I,
)


class SemanticExtractor:
    """Rule-based semantic extraction from transcripts.

    Extracts structured information without LLM dependency:
    - Decisions
    - Action items
    - Topics
    - Entities (names, dates, technical terms)
    - Key quotes
    - Speaker notes
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._cfg = cfg.semantic_extraction

    def extract(self, transcript: CleanTranscript) -> SemanticSummary:
        """Extract semantic information from cleaned transcript."""
        summary = SemanticSummary()

        if not transcript.segments:
            return summary

        # Short summary
        summary.short = self._generate_short_summary(transcript)
        summary.detailed = self._generate_detailed_summary(transcript)

        # Structured extraction
        if self._cfg.extract_decisions:
            summary.decisions = self._extract_decisions(transcript.segments)
        if self._cfg.extract_action_items:
            summary.action_items = self._extract_action_items(transcript.segments)
        if self._cfg.extract_topics:
            summary.topics = self._extract_topic_names(transcript.segments)
            summary.technical_topics = self._extract_topics(transcript.segments)
        if self._cfg.extract_entities:
            summary.entities = self._extract_entities(transcript.segments)
        if self._cfg.extract_key_quotes:
            summary.key_quotes = self._extract_key_quotes(transcript.segments)
        if self._cfg.extract_speaker_notes:
            summary.speaker_notes = self._extract_speaker_notes(transcript)

        return summary

    def _generate_short_summary(self, transcript: CleanTranscript) -> str:
        """Generate a short summary (first meaningful sentences)."""
        texts = [s.text for s in transcript.segments[:5] if len(s.text) > 20]
        if not texts:
            return "Audio transcription"
        # Take first 2 sentences or 200 chars
        combined = " ".join(texts)
        if len(combined) > 200:
            combined = combined[:197] + "..."
        return combined

    def _generate_detailed_summary(self, transcript: CleanTranscript) -> str:
        """Generate a more detailed summary."""
        duration = transcript.duration_seconds
        num_speakers = len(transcript.speakers)
        num_segments = len(transcript.segments)

        parts = [
            f"Transcription with {num_segments} segments",
            f"({duration / 60:.0f} minutes)" if duration > 60 else f"({duration:.0f} seconds)",
        ]
        if num_speakers > 1:
            parts.append(f"involving {num_speakers} speakers")

        # Add first few content sentences
        content = [s.text for s in transcript.segments[:10] if len(s.text) > 30]
        if content:
            parts.append(f"— {content[0]}")

        return " ".join(parts)

    def _extract_decisions(self, segments: list[TranscriptSegment]) -> list[Decision]:
        """Extract decision statements."""
        decisions: list[Decision] = []
        for seg in segments:
            for pattern in DECISION_PATTERNS:
                if pattern.search(seg.text):
                    decisions.append(Decision(
                        text=seg.text,
                        timestamp=seg.start,
                        speaker=seg.speaker,
                    ))
                    break
        return decisions[:20]  # Limit

    def _extract_action_items(self, segments: list[TranscriptSegment]) -> list[ActionItem]:
        """Extract action items / tasks."""
        items: list[ActionItem] = []
        for seg in segments:
            for pattern in ACTION_PATTERNS:
                if pattern.search(seg.text):
                    items.append(ActionItem(
                        text=seg.text,
                        assignee=seg.speaker,
                        timestamp=seg.start,
                        speaker=seg.speaker,
                    ))
                    break
        return items[:30]  # Limit

    def _extract_topic_names(self, segments: list[TranscriptSegment]) -> list[str]:
        """Extract topic name strings for summary."""
        topics = self._extract_topics(segments)
        return [t.name for t in topics[:10]]

    def _extract_topics(self, segments: list[TranscriptSegment]) -> list[Topic]:
        """Extract mentioned topics using noun phrase frequency."""
        # Simple approach: extract capitalized multi-word phrases
        phrase_counts: dict[str, tuple[int, float]] = {}

        for seg in segments:
            # Find capitalized phrases (potential topics/names)
            phrases = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", seg.text)
            for phrase in phrases:
                if len(phrase) > 3:  # Skip short ones
                    if phrase not in phrase_counts:
                        phrase_counts[phrase] = (0, seg.start)
                    count, first_ts = phrase_counts[phrase]
                    phrase_counts[phrase] = (count + 1, first_ts)

        topics = [
            Topic(name=name, mentions=count, first_timestamp=ts)
            for name, (count, ts) in phrase_counts.items()
            if count >= 2  # Only topics mentioned 2+ times
        ]
        topics.sort(key=lambda t: t.mentions, reverse=True)
        return topics[:20]

    def _extract_entities(self, segments: list[TranscriptSegment]) -> list[Entity]:
        """Extract named entities (simple pattern-based)."""
        entities: dict[str, tuple[str, int]] = {}

        for seg in segments:
            # Dates
            for match in DATE_PATTERN.finditer(seg.text):
                name = match.group(0)
                entities[name] = ("date", entities.get(name, ("date", 0))[1] + 1)

            # Capitalized proper nouns (2+ words)
            for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", seg.text):
                name = match.group(0)
                entities[name] = ("name", entities.get(name, ("name", 0))[1] + 1)

            # URLs / technical terms with dots
            for match in re.finditer(r"\b\w+\.\w+(?:\.\w+)+\b", seg.text):
                name = match.group(0)
                entities[name] = ("technical", entities.get(name, ("technical", 0))[1] + 1)

        return [
            Entity(name=name, entity_type=etype, mentions=count)
            for name, (etype, count) in entities.items()
        ][:30]

    def _extract_key_quotes(self, segments: list[TranscriptSegment]) -> list[KeyQuote]:
        """Extract key quotes: longer, meaningful segments."""
        quotes: list[KeyQuote] = []
        for seg in segments:
            # Key quotes are longer segments with substance
            if len(seg.text) > 80 and seg.no_speech_prob is not None and seg.no_speech_prob < 0.3:
                importance = "high" if len(seg.text) > 150 else "medium"
                quotes.append(KeyQuote(
                    text=seg.text,
                    start=seg.start,
                    end=seg.end,
                    speaker=seg.speaker,
                    importance=importance,
                ))
            elif len(seg.text) > 100:
                quotes.append(KeyQuote(
                    text=seg.text,
                    start=seg.start,
                    end=seg.end,
                    speaker=seg.speaker,
                    importance="medium",
                ))

        # Limit and sort by importance
        quotes.sort(key=lambda q: (q.importance == "high", len(q.text)), reverse=True)
        return quotes[:15]

    def _extract_speaker_notes(self, transcript: CleanTranscript) -> list[SpeakerNote]:
        """Generate per-speaker summaries."""
        speaker_data: dict[str, list[TranscriptSegment]] = {}

        for seg in transcript.segments:
            speaker = seg.speaker or "UNKNOWN"
            if speaker not in speaker_data:
                speaker_data[speaker] = []
            speaker_data[speaker].append(seg)

        notes: list[SpeakerNote] = []
        for speaker, segs in speaker_data.items():
            total_dur = sum(s.end - s.start for s in segs)
            # Short summary: first segment text
            summary = segs[0].text[:100] if segs else ""
            notes.append(SpeakerNote(
                speaker=speaker,
                summary=summary,
                segment_count=len(segs),
                total_duration_seconds=total_dur,
            ))

        return notes
