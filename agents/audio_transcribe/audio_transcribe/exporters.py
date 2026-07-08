"""Output exporters: JSON, Markdown, TXT, SRT, VTT, RAG-ready."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Optional

from audio_transcribe.storage import get_job_dir, write_json_output, write_text_output
from audio_transcribe.storage_guardian_api import (
    StorageGuardianPublishError,
    publish_file,
    source_reuse_metadata,
)
from audio_transcribe.types import (
    AudioQualityReport,
    CleanTranscript,
    ExportedArtifacts,
    JobRecord,
    ProcessingMetrics,
    SemanticSummary,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)


def export_all(
    job_id: str,
    raw_result: TranscriptionResult,
    clean_transcript: CleanTranscript,
    semantic: SemanticSummary,
    record: JobRecord,
    metrics: ProcessingMetrics,
    quality_report: Optional[AudioQualityReport] = None,
    formats: Optional[list[str]] = None,
    rag_ready: bool = True,
    include_speakers_in_subtitles: bool = True,
) -> ExportedArtifacts:
    """Export all configured output formats."""
    from audio_transcribe.config import get_config
    cfg = get_config()
    formats = formats or cfg.export.formats
    artifacts = ExportedArtifacts()

    # Always export raw JSON
    if "json" in formats:
        artifacts.transcript_raw_json = _export_raw_json(job_id, raw_result)
        artifacts.transcript_clean_json = _export_clean_json(job_id, clean_transcript)

    if "txt" in formats:
        artifacts.transcript_txt = _export_txt(job_id, clean_transcript)

    if "md" in formats:
        artifacts.transcript_md = _export_markdown(
            job_id, clean_transcript, semantic, record, metrics
        )

    if "srt" in formats:
        artifacts.subtitles_srt = _export_srt(
            job_id, clean_transcript, include_speakers_in_subtitles
        )

    if "vtt" in formats:
        artifacts.subtitles_vtt = _export_vtt(
            job_id, clean_transcript, include_speakers_in_subtitles
        )

    if rag_ready:
        artifacts.rag_ready_json = _export_rag_ready(
            job_id, record, clean_transcript, semantic, quality_report
        )

    # Metadata exports (always). The terminal job record is published after the
    # pipeline marks the job completed, so job_json is intentionally excluded
    # from this initial artifact publication pass.
    artifacts.metadata_json = _export_metadata(job_id, record, metrics)
    artifacts.metrics_json = _export_metrics(job_id, metrics)
    artifacts.job_json = str(get_job_dir(job_id) / "metadata" / "job.json")
    artifacts = _publish_artifacts(job_id, artifacts, record=record, skip_fields={"job_json"})

    logger.info(f"Exported outputs for job {job_id}")
    return artifacts


def publish_job_record(job_id: str, record: JobRecord) -> str | None:
    """Publish the terminal job record after final status persistence."""
    job_json = get_job_dir(job_id) / "metadata" / "job.json"
    if not job_json.is_file():
        return None
    published = _publish_artifacts(
        job_id,
        ExportedArtifacts(job_json=str(job_json)),
        record=record,
    )
    return published.job_json


def _publish_artifacts(
    job_id: str,
    artifacts: ExportedArtifacts,
    *,
    record: JobRecord,
    skip_fields: set[str] | None = None,
) -> ExportedArtifacts:
    from audio_transcribe.config import get_config

    publish_required = get_config().export.publish_policy == "required"
    skip_fields = skip_fields or set()
    values = artifacts.model_dump()
    job_dir = get_job_dir(job_id).resolve()
    projection_segment = _projection_segment_for_record(record)
    for field_name, value in values.items():
        if field_name in skip_fields:
            continue
        if not value:
            continue
        path = Path(str(value))
        if not path.is_file():
            continue
        projection_path = _projection_path(projection_segment, path, job_dir)
        try:
            published = publish_file(
                path,
                agent="audio_transcribe",
                store="audio_outputs",
                logical_name=f"{job_id}_{field_name}_{path.name}",
                projection_path=projection_path,
                metadata={
                    "job_id": job_id,
                    "artifact": field_name,
                    **source_reuse_metadata(record.input_path, options=record.options),
                },
            )
        except StorageGuardianPublishError:
            if publish_required:
                raise
            logger.info(
                "Storage Guardian publication skipped for %s.%s; keeping scratch artifact",
                job_id,
                field_name,
            )
            continue
        setattr(artifacts, field_name, published)
    return artifacts


def _projection_path(projection_segment: str, path: Path, job_dir: Path) -> str:
    try:
        relative = path.resolve().relative_to(job_dir)
    except ValueError:
        return f"{projection_segment}/{path.name}"
    return f"{projection_segment}/{relative.as_posix()}"


def _projection_segment_for_record(record: JobRecord) -> str:
    filename = record.input_filename or Path(str(record.input_path or "")).name or "audio"
    timestamp = _record_timestamp(record)
    date_segment = timestamp.strftime("%Y-%m-%d")
    time_segment = timestamp.strftime("%H%M%S%fZ")
    file_segment = _safe_projection_segment(f"{filename}__{time_segment}", empty_value=f"audio__{time_segment}")
    return f"{date_segment}/{file_segment}"


def _record_timestamp(record: JobRecord) -> datetime:
    value = str(record.created_at or "").strip()
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _safe_projection_segment(value: str, *, empty_value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value).strip()).strip("._-")
    return cleaned[:220] or empty_value


def _export_raw_json(job_id: str, result: TranscriptionResult) -> str:
    """Export raw transcription result."""
    data = {
        "language": result.language,
        "duration_seconds": result.duration_seconds,
        "segments": [s.model_dump() for s in result.segments],
    }
    return write_json_output(job_id, "transcripts", "transcript_raw.json", data)


def _export_clean_json(job_id: str, transcript: CleanTranscript) -> str:
    """Export cleaned transcript."""
    data = transcript.model_dump()
    return write_json_output(job_id, "transcripts", "transcript_clean.json", data)


def _export_txt(job_id: str, transcript: CleanTranscript) -> str:
    """Export plain text transcript."""
    lines: list[str] = []
    for seg in transcript.segments:
        prefix = f"[{seg.speaker}] " if seg.speaker else ""
        lines.append(f"{prefix}{seg.text}")
    content = "\n\n".join(lines)
    return write_text_output(job_id, "transcripts", "transcript.txt", content)


def _export_markdown(
    job_id: str,
    transcript: CleanTranscript,
    semantic: SemanticSummary,
    record: JobRecord,
    metrics: ProcessingMetrics,
) -> str:
    """Export Markdown transcript with metadata and structure."""
    lines: list[str] = []

    # Title
    title = record.input_filename or "Transcription"
    lines.append(f"# {title}")
    lines.append("")

    # Metadata
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- **File:** {record.input_filename or 'unknown'}")
    lines.append(f"- **Duration:** {metrics.audio_duration_seconds / 60:.1f} minutes")
    lines.append(f"- **Language:** {transcript.language}")
    lines.append(f"- **Speakers:** {len(transcript.speakers)}")
    lines.append(f"- **Model:** {metrics.model}")
    lines.append(f"- **Device:** {metrics.device}")
    lines.append(f"- **Date:** {record.created_at}")
    lines.append("")

    # Summary
    if semantic.short:
        lines.append("## Summary")
        lines.append("")
        lines.append(semantic.short)
        lines.append("")

    # Speakers
    if transcript.speakers:
        lines.append("## Speakers")
        lines.append("")
        for speaker in transcript.speakers:
            lines.append(f"- {speaker}")
        lines.append("")

    # Decisions
    if semantic.decisions:
        lines.append("## Decisions")
        lines.append("")
        for d in semantic.decisions:
            ts = f" [{_format_time(d.timestamp)}]" if d.timestamp else ""
            speaker = f" ({d.speaker})" if d.speaker else ""
            lines.append(f"- {d.text}{speaker}{ts}")
        lines.append("")

    # Action Items
    if semantic.action_items:
        lines.append("## Action Items")
        lines.append("")
        for item in semantic.action_items:
            assignee = f" @{item.assignee}" if item.assignee else ""
            lines.append(f"- [ ] {item.text}{assignee}")
        lines.append("")

    # Topics
    if semantic.topics:
        lines.append("## Topics")
        lines.append("")
        for topic in semantic.topics:
            lines.append(f"- {topic}")
        lines.append("")

    # Key Quotes
    if semantic.key_quotes:
        lines.append("## Key Quotes")
        lines.append("")
        for q in semantic.key_quotes:
            speaker = f" — {q.speaker}" if q.speaker else ""
            lines.append(f"> {q.text}{speaker} [{_format_time(q.start)}]")
            lines.append("")

    # Transcript
    lines.append("## Transcript")
    lines.append("")
    current_speaker = None
    for seg in transcript.segments:
        if seg.speaker and seg.speaker != current_speaker:
            current_speaker = seg.speaker
            lines.append(f"**{current_speaker}** [{_format_time(seg.start)}]")
            lines.append("")
        lines.append(f"{seg.text}")
        lines.append("")

    content = "\n".join(lines)
    return write_text_output(job_id, "transcripts", "transcript.md", content)


def _export_srt(
    job_id: str, transcript: CleanTranscript, include_speakers: bool
) -> str:
    """Export SRT subtitle file."""
    lines: list[str] = []
    for i, seg in enumerate(transcript.segments, 1):
        lines.append(str(i))
        lines.append(f"{_srt_time(seg.start)} --> {_srt_time(seg.end)}")
        text = seg.text
        if include_speakers and seg.speaker:
            text = f"[{seg.speaker}] {text}"
        # Split long subtitles
        if len(text) > 80:
            mid = len(text) // 2
            space_pos = text.rfind(" ", 0, mid + 20)
            if space_pos > mid - 20:
                text = text[:space_pos] + "\n" + text[space_pos + 1:]
        lines.append(text)
        lines.append("")

    content = "\n".join(lines)
    return write_text_output(job_id, "subtitles", "subtitles.srt", content)


def _export_vtt(
    job_id: str, transcript: CleanTranscript, include_speakers: bool
) -> str:
    """Export WebVTT subtitle file."""
    lines: list[str] = ["WEBVTT", ""]
    for i, seg in enumerate(transcript.segments, 1):
        lines.append(str(i))
        lines.append(f"{_vtt_time(seg.start)} --> {_vtt_time(seg.end)}")
        text = seg.text
        if include_speakers and seg.speaker:
            text = f"<v {seg.speaker}>{text}"
        lines.append(text)
        lines.append("")

    content = "\n".join(lines)
    return write_text_output(job_id, "subtitles", "subtitles.vtt", content)


def _export_rag_ready(
    job_id: str,
    record: JobRecord,
    transcript: CleanTranscript,
    semantic: SemanticSummary,
    quality_report: Optional[AudioQualityReport],
) -> str:
    """Export RAG-ready JSON without raw transcript bulk."""
    job_dir = get_job_dir(job_id)
    data: dict[str, Any] = {
        "source": {
            "file": record.input_filename or "",
            "duration_seconds": transcript.duration_seconds,
            "language": transcript.language,
            "created_at": record.created_at,
            "job_id": record.job_id,
        },
        "summary": {
            "short": semantic.short,
            "detailed": semantic.detailed,
            "topics": semantic.topics,
        },
        "decisions": [d.model_dump() for d in semantic.decisions],
        "action_items": [a.model_dump() for a in semantic.action_items],
        "technical_topics": [t.model_dump() for t in semantic.technical_topics],
        "entities": [e.model_dump() for e in semantic.entities],
        "key_quotes": [q.model_dump() for q in semantic.key_quotes],
        "speaker_notes": [n.model_dump() for n in semantic.speaker_notes],
        "references": _build_references(transcript),
        "quality": {
            "audio_quality": _quality_label(quality_report),
            "transcription_confidence": None,
            "diarization_enabled": record.options.diarization,
            "warnings": quality_report.warnings if quality_report else [],
        },
        "artifacts": {
            "transcript_clean_json": str(job_dir / "transcripts" / "transcript_clean.json"),
            "transcript_md": str(job_dir / "transcripts" / "transcript.md"),
            "subtitles_srt": str(job_dir / "subtitles" / "subtitles.srt"),
        },
    }
    return write_json_output(job_id, "rag_ready", "rag_ready.json", data)


def _build_references(transcript: CleanTranscript) -> list[dict]:
    """Build reference excerpts for RAG (meaningful segments only)."""
    refs = []
    for seg in transcript.segments:
        if len(seg.text) > 50:
            importance = "high" if len(seg.text) > 150 else "medium"
            refs.append({
                "start": seg.start,
                "end": seg.end,
                "speaker": seg.speaker or "",
                "text": seg.text,
                "importance": importance,
            })
    return refs[:50]  # Limit


def _export_metadata(job_id: str, record: JobRecord, metrics: ProcessingMetrics) -> str:
    """Export metadata.json."""
    data = {
        "job_id": record.job_id,
        "input_file": record.input_filename,
        "language": "",
        "duration_seconds": metrics.audio_duration_seconds,
        "speakers_count": metrics.speakers_count,
        "segments_count": metrics.segments_count,
        "model": metrics.model,
        "device": metrics.device,
        "compute_type": metrics.compute_type,
        "created_at": record.created_at,
        "completed_at": record.completed_at,
    }
    return write_json_output(job_id, "metadata", "metadata.json", data)


def _export_metrics(job_id: str, metrics: ProcessingMetrics) -> str:
    """Export metrics.json."""
    return write_json_output(job_id, "metadata", "metrics.json", metrics.model_dump())


# =============================================================================
# Helpers
# =============================================================================


def _format_time(seconds: Optional[float]) -> str:
    """Format seconds as HH:MM:SS."""
    if seconds is None:
        return "00:00:00"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _vtt_time(seconds: float) -> str:
    """Format seconds as VTT timestamp: HH:MM:SS.mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _quality_label(report: Optional[AudioQualityReport]) -> str:
    """Generate a quality label from report."""
    if not report:
        return "unknown"
    if report.clipping_detected or report.low_volume:
        return "poor"
    if report.silence_ratio > 0.5:
        return "fair"
    if report.warnings:
        return "fair"
    return "good"
