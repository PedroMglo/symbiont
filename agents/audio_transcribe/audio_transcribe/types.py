"""Data types and Pydantic models for audio_transcribe service."""

from __future__ import annotations

import enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# =============================================================================
# Enumerations
# =============================================================================


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStage(str, enum.Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    PREPROCESSING = "preprocessing"
    AUDIO_QUALITY = "audio_quality"
    NOISE_REDUCTION = "noise_reduction"
    SEGMENTATION = "segmentation"
    VAD = "vad"
    TRANSCRIPTION = "transcription"
    DIARIZATION = "diarization"
    SPEAKER_ALIGNMENT = "speaker_alignment"
    POSTPROCESSING = "postprocessing"
    SEMANTIC_EXTRACTION = "semantic_extraction"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SegmentStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# =============================================================================
# Request/Response Models
# =============================================================================


class TranscriptionOptions(BaseModel):
    model: str = "distil-large-v3"
    language: str = "auto"
    device: str = "auto"
    compute_type: str = "int8_float16"
    diarization: bool = False
    vad: bool = True
    noise_reduction: bool = False
    formats: list[str] = Field(default_factory=lambda: ["json", "md", "txt", "srt", "vtt"])
    rag_ready: bool = True


class TranscriptionJobRequest(BaseModel):
    input_path: Optional[str] = None
    input_uri: Optional[str] = None
    options: TranscriptionOptions = Field(default_factory=TranscriptionOptions)


class TranscriptionJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: str
    status_url: str


class AudioQueryRequest(BaseModel):
    query: str
    wait_seconds: float = Field(default=0.0, ge=0.0, le=1800.0)
    poll_interval_seconds: float = Field(default=3.0, ge=0.25, le=60.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AudioQueryResponse(BaseModel):
    content: str
    success: bool = True
    token_estimate: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    stage: JobStage
    progress: float = 0.0
    processed_duration_seconds: float = 0.0
    total_duration_seconds: float = 0.0
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class JobResultResponse(BaseModel):
    job_id: str
    status: JobStatus
    outputs: dict[str, str] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


class JobListItem(BaseModel):
    job_id: str
    status: JobStatus
    stage: JobStage
    created_at: str
    input_path: Optional[str] = None


class ModelInfo(BaseModel):
    name: str
    description: str = ""
    size: str = ""
    languages: list[str] = Field(default_factory=list)
    recommended_compute_types: list[str] = Field(default_factory=list)


# =============================================================================
# Internal Domain Models
# =============================================================================


class AudioMetadata(BaseModel):
    file_path: str
    format: str = ""
    duration_seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    bit_depth: Optional[int] = None
    file_size_bytes: int = 0
    codec: str = ""


class AudioQualityReport(BaseModel):
    mean_volume_db: Optional[float] = None
    peak_volume_db: Optional[float] = None
    clipping_detected: bool = False
    silence_ratio: float = 0.0
    low_volume: bool = False
    estimated_snr_db: Optional[float] = None
    duration_seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    warnings: list[str] = Field(default_factory=list)


class AudioSegment(BaseModel):
    segment_id: str
    index: int
    start: float
    end: float
    duration: float
    file_path: str = ""
    checkpoint_id: Optional[str] = None


class SegmentCheckpoint(BaseModel):
    segment_id: str
    index: int
    status: SegmentStatus = SegmentStatus.PENDING
    start: float
    end: float
    transcript_text: Optional[str] = None
    result_path: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None
    processing_time_seconds: Optional[float] = None


class WordTimestamp(BaseModel):
    word: str
    start: float
    end: float
    confidence: Optional[float] = None


class TranscriptSegment(BaseModel):
    index: int
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    confidence: Optional[float] = None
    language: Optional[str] = None
    no_speech_prob: Optional[float] = None
    words: list[WordTimestamp] = Field(default_factory=list)


class SpeakerSegment(BaseModel):
    speaker: str
    start: float
    end: float
    confidence: Optional[float] = None


class CleanTranscript(BaseModel):
    segments: list[TranscriptSegment] = Field(default_factory=list)
    language: str = ""
    speakers: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0


class TranscriptionResult(BaseModel):
    segments: list[TranscriptSegment] = Field(default_factory=list)
    language: str = ""
    duration_seconds: float = 0.0


# =============================================================================
# Semantic Extraction Models
# =============================================================================


class Decision(BaseModel):
    text: str
    timestamp: Optional[float] = None
    speaker: Optional[str] = None
    confidence: str = "medium"


class ActionItem(BaseModel):
    text: str
    assignee: Optional[str] = None
    timestamp: Optional[float] = None
    speaker: Optional[str] = None


class Topic(BaseModel):
    name: str
    mentions: int = 1
    first_timestamp: Optional[float] = None


class Entity(BaseModel):
    name: str
    entity_type: str = "unknown"
    mentions: int = 1


class KeyQuote(BaseModel):
    text: str
    start: float
    end: float
    speaker: Optional[str] = None
    importance: str = "medium"


class SpeakerNote(BaseModel):
    speaker: str
    summary: str = ""
    segment_count: int = 0
    total_duration_seconds: float = 0.0


class SemanticSummary(BaseModel):
    short: str = ""
    detailed: str = ""
    topics: list[str] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    technical_topics: list[Topic] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    key_quotes: list[KeyQuote] = Field(default_factory=list)
    speaker_notes: list[SpeakerNote] = Field(default_factory=list)


# =============================================================================
# Metrics & Observability
# =============================================================================


class StageTiming(BaseModel):
    stage: str
    started_at: str
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None


class ProcessingMetrics(BaseModel):
    total_processing_seconds: float = 0.0
    audio_duration_seconds: float = 0.0
    realtime_factor: float = 0.0
    model: str = ""
    device: str = ""
    compute_type: str = ""
    batch_size: int = 0
    segments_count: int = 0
    speakers_count: int = 0
    retries: int = 0
    stage_timings: list[StageTiming] = Field(default_factory=list)
    output_sizes: dict[str, int] = Field(default_factory=dict)


class ExportedArtifacts(BaseModel):
    transcript_raw_json: Optional[str] = None
    transcript_clean_json: Optional[str] = None
    transcript_md: Optional[str] = None
    transcript_txt: Optional[str] = None
    subtitles_srt: Optional[str] = None
    subtitles_vtt: Optional[str] = None
    rag_ready_json: Optional[str] = None
    metadata_json: Optional[str] = None
    metrics_json: Optional[str] = None
    job_json: Optional[str] = None


# =============================================================================
# Job Record (persisted as job.json)
# =============================================================================


class JobRecord(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    stage: JobStage = JobStage.QUEUED
    progress: float = 0.0
    processed_duration_seconds: float = 0.0
    total_duration_seconds: float = 0.0
    input_path: Optional[str] = None
    input_filename: Optional[str] = None
    source_content_hash: Optional[str] = None
    transcription_options_hash: Optional[str] = None
    options: TranscriptionOptions = Field(default_factory=TranscriptionOptions)
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    artifacts: ExportedArtifacts = Field(default_factory=ExportedArtifacts)
    metrics: Optional[ProcessingMetrics] = None

    def to_status_response(self) -> JobStatusResponse:
        return JobStatusResponse(
            job_id=self.job_id,
            status=self.status,
            stage=self.stage,
            progress=self.progress,
            processed_duration_seconds=self.processed_duration_seconds,
            total_duration_seconds=self.total_duration_seconds,
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            error=self.error,
            warnings=self.warnings,
        )

    def to_result_response(self) -> JobResultResponse:
        outputs: dict[str, str] = {}
        for field_name, value in self.artifacts.model_dump().items():
            if value:
                outputs[field_name] = value
        summary = {}
        if self.metrics:
            summary = {
                "duration_seconds": self.metrics.audio_duration_seconds,
                "language": "",
                "speakers": self.metrics.speakers_count,
                "segments": self.metrics.segments_count,
                "realtime_factor": self.metrics.realtime_factor,
                "device": self.metrics.device,
                "model": self.metrics.model,
            }
        return JobResultResponse(
            job_id=self.job_id,
            status=self.status,
            outputs=outputs,
            summary=summary,
        )
