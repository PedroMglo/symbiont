"""Unified Audio Platform — Configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from sharedai.llm.backend_url import validated_base_url


def _require_llm_url(value: str, env_name: str) -> str:
    try:
        safe, _host, _port, _path = validated_base_url(value)
        return safe
    except ValueError as exc:
        raise ValueError(f"{env_name}: {exc}") from exc


def _default_ollama_url() -> str:
    default = "https://host.docker.internal:11434" if os.path.exists("/.dockerenv") else "https://localhost:11434"
    return _require_llm_url(os.environ.get("OLLAMA_BASE_URL", default), "OLLAMA_BASE_URL")


@dataclass
class ServerConfig:
    host: str = os.environ.get("STREAM_HOST", "0.0.0.0")
    port: int = int(os.environ.get("STREAM_PORT", "8087"))
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")


@dataclass
class RedisConfig:
    url: str = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    stream_prefix: str = "audio"
    dedup_db: int = 1
    max_stream_len: int = 10000  # Max events per stream


@dataclass
class GPUConfig:
    max_workers: int = int(os.environ.get("GPU_WORKERS", "1"))
    model_name: str = os.environ.get("WHISPER_MODEL", "distil-large-v3")
    compute_type: str = os.environ.get("WHISPER_COMPUTE_TYPE", "int8_float16")
    device: str = os.environ.get("WHISPER_DEVICE", "auto")
    batch_size: int = int(os.environ.get("WHISPER_BATCH_SIZE", "1"))


@dataclass
class RealtimeConfig:
    sample_rate: int = 16000
    frame_duration_ms: int = 30  # VAD frame size
    min_speech_ms: int = 250  # Min speech to trigger ASR
    max_speech_ms: int = int(os.environ.get("MAX_SPEECH_MS", "6000"))  # Max before forced cut (6s)
    silence_threshold_ms: int = int(os.environ.get("SILENCE_THRESHOLD_MS", "400"))  # Silence to finalize
    vad_threshold: float = 0.5  # Silero VAD threshold
    vad_energy_threshold_db: float = float(os.environ.get("VAD_ENERGY_THRESHOLD_DB", "-35.0"))
    final_result_timeout_seconds: float = float(os.environ.get("FINAL_RESULT_TIMEOUT_SECONDS", "15.0"))


@dataclass
class BatchConfig:
    max_segment_duration: int = 60  # seconds
    overlap_seconds: float = 1.5
    chunk_size: int = 30  # Default chunk size in seconds


@dataclass
class DedupConfig:
    sha256_enabled: bool = True
    fingerprint_enabled: bool = bool(os.environ.get("CHROMAPRINT_ENABLED", ""))
    cache_ttl: int = 7 * 24 * 3600  # 7 days
    redis_db: int = 1


@dataclass
class OllamaConfig:
    url: str = _default_ollama_url()
    model: str = os.environ.get("LLM_POST_MODEL", "qwen3:8b")
    enabled: bool = bool(os.environ.get("LLM_POST_ENABLED", ""))


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    gpu: GPUConfig = field(default_factory=GPUConfig)
    realtime: RealtimeConfig = field(default_factory=RealtimeConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    output_dir: str = os.environ.get("AUDIO_OUTPUT_DIR", "/temp/audio-streaming/output")


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
