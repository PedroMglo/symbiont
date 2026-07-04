"""Configuration loader for audio_transcribe service."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 12):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


def _default_model_cache_dir() -> str:
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return str(Path(hf_home) / "audio-transcribe")
    hf_cache_dir = os.environ.get("HF_CACHE_DIR")
    if hf_cache_dir:
        return str(Path(hf_cache_dir) / "audio-transcribe")
    return "/models/huggingface/audio-transcribe"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    workers: int = 1


@dataclass
class PathsConfig:
    input_dir: str = "/temp/audio/input"
    output_dir: str = "/temp/audio/output"
    models_dir: str = field(default_factory=_default_model_cache_dir)
    tmp_dir: str = "/temp/audio/tmp"


@dataclass
class JobsConfig:
    max_concurrent_jobs: int = 1
    max_queued_jobs: int = 20
    keep_failed_outputs: bool = True
    delete_upload_after_processing: bool = False
    job_ttl_hours: int = 168
    redis_retry_attempts: int = 2
    redis_processing_timeout_seconds: int = 3600


@dataclass
class PerformanceConfig:
    max_concurrent_gpu_transcriptions: int = 1
    max_cpu_preprocessing_jobs: int = 2
    cpu_workers: int = 4
    batch_size: int = 8
    enable_segment_checkpoints: bool = True
    segment_strategy: str = "vad_then_window"
    max_segment_duration_seconds: int = 300
    segment_overlap_seconds: int = 2
    experimental_parallel_gpu_workers: bool = False


@dataclass
class LongAudioConfig:
    enabled: bool = True
    checkpoint_every_segment: bool = True
    resume_failed_jobs: bool = True
    preserve_absolute_timestamps: bool = True
    progress_by_audio_duration: bool = True
    max_audio_duration_seconds: Optional[int] = None


@dataclass
class TranscriptionConfig:
    model: str = "distil-large-v3"
    device: str = "auto"
    compute_type: str = "int8_float16"
    beam_size: int = 5
    language: str = "auto"
    download_root: str = field(default_factory=_default_model_cache_dir)
    word_timestamps: bool = True
    vad_filter: bool = False


@dataclass
class GpuPolicyConfig:
    prefer_gpu: bool = True
    min_free_vram_mb: int = 768
    wait_timeout_seconds: int = 8
    wait_poll_seconds: float = 1.5
    allow_model_downgrade: bool = True
    fallback_model: str = "small"
    cpu_fallback_enabled: bool = True


@dataclass
class PreprocessingConfig:
    sample_rate: int = 16000
    mono: bool = True
    normalize_loudness: bool = True
    noise_reduction: bool = False
    keep_processed_audio: bool = True


@dataclass
class AudioQualityConfig:
    enabled: bool = True
    warn_on_clipping: bool = True
    warn_on_low_volume: bool = True
    warn_on_high_silence_ratio: bool = True


@dataclass
class VADConfig:
    enabled: bool = True
    min_speech_duration_ms: int = 250
    min_silence_duration_ms: int = 500
    speech_pad_ms: int = 200
    fallback_to_window_segmentation: bool = True


@dataclass
class DiarizationConfig:
    enabled: bool = False
    provider: str = "pyannote"
    hf_token_env: str = "HF_TOKEN"
    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None
    persist_speaker_profiles: bool = False
    continue_without_diarization_on_error: bool = True


@dataclass
class PostprocessingConfig:
    remove_fillers: bool = True
    remove_repetitions: bool = True
    paragraphs: bool = True
    preserve_timestamps: bool = True
    preserve_speakers: bool = True
    conservative_cleanup: bool = True


@dataclass
class SemanticExtractionConfig:
    enabled: bool = True
    mode: str = "rules"
    extract_decisions: bool = True
    extract_action_items: bool = True
    extract_topics: bool = True
    extract_entities: bool = True
    extract_key_quotes: bool = True
    extract_speaker_notes: bool = True


@dataclass
class ExportConfig:
    formats: list[str] = field(default_factory=lambda: ["json", "txt", "md", "srt", "vtt"])
    rag_ready: bool = True
    include_speakers_in_subtitles: bool = True
    publish_policy: str = "required"


@dataclass
class ObservabilityConfig:
    enabled: bool = True
    log_level: str = "INFO"
    write_metrics_json: bool = True
    write_stage_timings: bool = True


@dataclass
class SecurityConfig:
    max_upload_size_mb: int = 2048
    api_key_env: str = "AUDIO_TRANSCRIBE_API_KEY"
    api_key_file_env: str = "AUDIO_TRANSCRIBE_API_KEY_FILE"
    allow_unauthenticated_dev: bool = False
    cors_enabled: bool = False
    allowed_input_extensions: list[str] = field(
        default_factory=lambda: [
            "mp3", "wav", "m4a", "flac", "ogg", "mp4", "mkv", "webm", "mov", "avi"
        ]
    )


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    jobs: JobsConfig = field(default_factory=JobsConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    long_audio: LongAudioConfig = field(default_factory=LongAudioConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    gpu_policy: GpuPolicyConfig = field(default_factory=GpuPolicyConfig)
    preprocessing: PreprocessingConfig = field(default_factory=PreprocessingConfig)
    audio_quality: AudioQualityConfig = field(default_factory=AudioQualityConfig)
    vad: VADConfig = field(default_factory=VADConfig)
    diarization: DiarizationConfig = field(default_factory=DiarizationConfig)
    postprocessing: PostprocessingConfig = field(default_factory=PostprocessingConfig)
    semantic_extraction: SemanticExtractionConfig = field(default_factory=SemanticExtractionConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


def _apply_section(target, data: dict) -> None:
    """Apply dictionary values to a dataclass instance."""
    for key, value in data.items():
        if hasattr(target, key):
            setattr(target, key, value)


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from TOML file with environment variable overrides."""
    cfg = AppConfig()

    # Determine config path
    path = config_path or os.environ.get("AUDIO_TRANSCRIBE_CONFIG")
    if path:
        config_file = Path(path)
        if config_file.exists():
            with open(config_file, "rb") as f:
                data = tomllib.load(f)

            section_map = {
                "server": cfg.server,
                "paths": cfg.paths,
                "jobs": cfg.jobs,
                "performance": cfg.performance,
                "long_audio": cfg.long_audio,
                "transcription": cfg.transcription,
                "gpu_policy": cfg.gpu_policy,
                "preprocessing": cfg.preprocessing,
                "audio_quality": cfg.audio_quality,
                "vad": cfg.vad,
                "diarization": cfg.diarization,
                "postprocessing": cfg.postprocessing,
                "semantic_extraction": cfg.semantic_extraction,
                "export": cfg.export,
                "observability": cfg.observability,
                "security": cfg.security,
            }

            for section_name, section_obj in section_map.items():
                if section_name in data:
                    _apply_section(section_obj, data[section_name])

    section_map = {
        "server": cfg.server,
        "paths": cfg.paths,
        "jobs": cfg.jobs,
        "performance": cfg.performance,
        "long_audio": cfg.long_audio,
        "transcription": cfg.transcription,
        "gpu_policy": cfg.gpu_policy,
        "preprocessing": cfg.preprocessing,
        "audio_quality": cfg.audio_quality,
        "vad": cfg.vad,
        "diarization": cfg.diarization,
        "postprocessing": cfg.postprocessing,
        "semantic_extraction": cfg.semantic_extraction,
        "export": cfg.export,
        "observability": cfg.observability,
        "security": cfg.security,
    }

    # Environment variable overrides (AUDIO_TRANSCRIBE_SECTION_FIELD).
    # Section names can contain underscores, so match the longest section name.
    prefix = "AUDIO_TRANSCRIBE_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix):].lower()
        section_obj = None
        field_name = ""
        for section_name in sorted(section_map, key=len, reverse=True):
            marker = f"{section_name}_"
            if suffix.startswith(marker):
                section_obj = section_map[section_name]
                field_name = suffix[len(marker):]
                break
        if section_obj is None:
            continue
        if not hasattr(section_obj, field_name):
            continue
        current = getattr(section_obj, field_name)
        if isinstance(current, bool):
            setattr(section_obj, field_name, value.lower() in ("true", "1", "yes"))
        elif isinstance(current, float):
            try:
                setattr(section_obj, field_name, float(value))
            except ValueError:
                pass
        elif isinstance(current, int):
            try:
                setattr(section_obj, field_name, int(value))
            except ValueError:
                pass
        elif isinstance(current, str):
            setattr(section_obj, field_name, value)

    if "AUDIO_TRANSCRIBE_TRANSCRIPTION_DOWNLOAD_ROOT" not in os.environ:
        cfg.transcription.download_root = cfg.paths.models_dir

    if os.environ.get("STORAGE_GUARDIAN_REQUIRED", "").lower() in {"1", "true", "yes", "on"}:
        cfg.export.publish_policy = "required"

    return cfg


# Module-level singleton
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """Get or create the global configuration singleton."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Reset config singleton (for testing)."""
    global _config
    _config = None


# Alias for test convenience
_reset_config = reset_config
