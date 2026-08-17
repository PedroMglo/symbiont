"""Typed Config Center projection for the Audio Transcribe owner runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .schema import AudioInput, ConfigError
from .storage_guardian_runtime import (
    STORAGE_GUARDIAN_CONFIG_PATH,
    storage_guardian_max_inline_bytes,
)

AUDIO_RUNTIME_CONTRACT = "ai-local.audio-runtime.v1"
AUDIO_RUNTIME_OWNER = "agents/audio_transcribe"


def _field(item: object, name: str) -> object:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _runtime_field(runtime: object, name: str) -> object:
    if isinstance(runtime, Mapping):
        return runtime.get(name)
    return getattr(runtime, name, None)


def _endpoint(
    service_endpoints: Sequence[object],
    service: str,
    *,
    expected_scheme: str,
) -> dict[str, Any]:
    matches = [item for item in service_endpoints if _field(item, "service") == service]
    if len(matches) != 1:
        raise ConfigError(f"{service} must occur exactly once in the central service registry")
    item = matches[0]
    url = str(_field(item, "url") or "").strip()
    parsed = urlsplit(url)
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{service} registry endpoint contains an invalid port") from exc
    allowed_paths = {"", "/", "/0"} if expected_scheme == "redis" else {"", "/"}
    if (
        parsed.scheme != expected_scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in allowed_paths
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(f"{service} registry endpoint must be a credential-free {expected_scheme} origin")
    port = _field(item, "port")
    workers = _field(item, "workers")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise ConfigError(f"{service} registry port must be between 1 and 65535")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ConfigError(f"{service} registry workers must be a positive integer")
    host = str(_field(item, "host") or "").strip()
    if not host:
        raise ConfigError(f"{service} registry host is required")
    if parsed.hostname != host or parsed_port != port:
        raise ConfigError(f"{service} registry URL, host and port must describe one endpoint")
    return {
        "service": service,
        "url": url.rstrip("/"),
        "host": host,
        "port": port,
        "workers": workers,
    }


def _auto_worker(value: int | str, inferred: int, field: str) -> int:
    if value == "auto":
        return inferred
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{field} must be auto or a non-negative integer")
    return value


def _resolved_device(value: str, machine_runtime: object) -> str:
    if value != "auto":
        return value
    return "cuda" if bool(_runtime_field(machine_runtime, "gpu_available")) else "cpu"


def _resolved_cpu_threads(value: int | str, machine_runtime: object) -> int:
    if value != "auto":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError("audio.streaming.gpu.cpu_threads must be auto or >= 1")
        return value
    host_threads = _runtime_field(machine_runtime, "cpu_threads")
    if isinstance(host_threads, bool) or not isinstance(host_threads, int) or host_threads < 1:
        raise ConfigError("runtime.cpu_threads is required to infer audio CPU workers")
    return max(1, min(8, host_threads // 2))


def _host_home_prefix(value: Path) -> str:
    expanded = value.expanduser()
    if not expanded.is_absolute() or expanded == Path("/"):
        raise ConfigError("audio host home prefix must be an absolute non-root path")
    text = str(expanded)
    if any(char in text for char in ("\n", "\r", ":")):
        raise ConfigError("audio host home prefix is not safe for Docker projection")
    return text


def resolve_audio_runtime(
    config: AudioInput,
    service_endpoints: Sequence[object],
    machine_runtime: object,
    *,
    host_home_prefix: Path,
    storage_guardian_config_path: Path = STORAGE_GUARDIAN_CONFIG_PATH,
) -> dict[str, Any]:
    """Resolve all non-secret Audio Transcribe and realtime runtime values."""

    batch_endpoint = _endpoint(service_endpoints, "audio_transcribe", expected_scheme="https")
    streaming_endpoint = _endpoint(service_endpoints, "audio_streaming", expected_scheme="https")
    redis_endpoint = _endpoint(service_endpoints, "redis", expected_scheme="redis")
    storage_endpoint = _endpoint(service_endpoints, "storage_guardian", expected_scheme="https")

    batch_workers = batch_endpoint["workers"]
    concurrent_jobs = _auto_worker(config.jobs.max_concurrent_jobs, batch_workers, "audio.jobs.max_concurrent_jobs")
    if concurrent_jobs < 1:
        raise ConfigError("audio.jobs.max_concurrent_jobs must resolve to >= 1")
    if config.jobs.max_queued_jobs < concurrent_jobs:
        raise ConfigError("audio.jobs.max_queued_jobs must be >= resolved max_concurrent_jobs")
    gpu_transcriptions = _auto_worker(config.performance.max_concurrent_gpu_transcriptions, 1, "audio.performance.max_concurrent_gpu_transcriptions")
    if gpu_transcriptions < 1:
        raise ConfigError("audio.performance.max_concurrent_gpu_transcriptions must resolve to >= 1")
    streaming_workers = _auto_worker(config.streaming.gpu.max_workers, streaming_endpoint["workers"], "audio.streaming.gpu.max_workers")
    paths = asdict(config.paths)
    paths["allowed_input_roots"] = list(config.paths.allowed_input_roots)
    paths["host_home_prefix"] = _host_home_prefix(host_home_prefix)

    transcription = asdict(config.transcription)
    transcription["device"] = _resolved_device(config.transcription.device, machine_runtime)
    transcription["download_root"] = config.paths.models_dir

    streaming_gpu = asdict(config.streaming.gpu)
    streaming_gpu["max_workers"] = streaming_workers
    streaming_gpu["device"] = _resolved_device(config.streaming.gpu.device, machine_runtime)
    streaming_gpu["cpu_threads"] = _resolved_cpu_threads(config.streaming.gpu.cpu_threads, machine_runtime)
    streaming_gpu["download_root"] = config.paths.models_dir

    return {
        "contract": AUDIO_RUNTIME_CONTRACT,
        "owner": AUDIO_RUNTIME_OWNER,
        "redis_image": config.streaming.redis.image,
        "batch": {
            "server": {"host": "0.0.0.0", "port": batch_endpoint["port"], "workers": batch_workers},
            "paths": paths,
            "jobs": {"max_concurrent_jobs": concurrent_jobs, "max_queued_jobs": config.jobs.max_queued_jobs, "job_ttl_hours": config.jobs.job_ttl_hours},
            "performance": {**asdict(config.performance), "max_concurrent_gpu_transcriptions": gpu_transcriptions},
            "transcription": transcription,
            "gpu_policy": asdict(config.gpu_policy),
            "preprocessing": asdict(config.preprocessing),
            "audio_quality": asdict(config.audio_quality),
            "vad": asdict(config.vad),
            "diarization": asdict(config.diarization),
            "postprocessing": asdict(config.postprocessing),
            "semantic_extraction": asdict(config.semantic_extraction),
            "export": {**asdict(config.export), "formats": list(config.export.formats)},
            "log_level": config.log_level,
            "security": {**asdict(config.security), "allowed_input_extensions": list(config.security.allowed_input_extensions)},
            "storage_guardian": {**asdict(config.storage_guardian), "url": storage_endpoint["url"], "verify_tls": True, "max_inline_bytes": storage_guardian_max_inline_bytes(storage_guardian_config_path)},
        },
        "streaming": {
            "server": {**asdict(config.streaming.server), "host": "0.0.0.0", "port": streaming_endpoint["port"], "workers": streaming_endpoint["workers"]},
            "redis": {**asdict(config.streaming.redis), "url": redis_endpoint["url"]},
            "gpu": streaming_gpu,
            "realtime": asdict(config.streaming.realtime),
            "monitor": {**asdict(config.streaming.monitor), "batch_url": batch_endpoint["url"]},
        },
    }


def validate_audio_runtime(runtime: Mapping[str, Any], *, expected_config: AudioInput, service_endpoints: Sequence[object], machine_runtime: object, host_home_prefix: Path, storage_guardian_config_path: Path = STORAGE_GUARDIAN_CONFIG_PATH) -> list[str]:
    try:
        expected = resolve_audio_runtime(expected_config, service_endpoints, machine_runtime, host_home_prefix=host_home_prefix, storage_guardian_config_path=storage_guardian_config_path)
    except (ConfigError, TypeError, ValueError) as exc:
        return [f"audio_runtime source projection is invalid: {exc}"]
    if dict(runtime) != expected:
        return ["audio_runtime must exactly match Config Center owner policy, machine inference, service topology, and Storage Guardian authority"]
    return []


def _bool(value: bool) -> str:
    return "true" if value else "false"


def audio_env_values(runtime: Mapping[str, Any]) -> dict[str, str]:
    if runtime.get("contract") != AUDIO_RUNTIME_CONTRACT:
        raise ConfigError(f"audio runtime must use {AUDIO_RUNTIME_CONTRACT}")
    if runtime.get("owner") != AUDIO_RUNTIME_OWNER:
        raise ConfigError(f"audio runtime owner must remain {AUDIO_RUNTIME_OWNER}")
    batch = runtime["batch"]
    paths = batch["paths"]
    jobs = batch["jobs"]
    performance = batch["performance"]
    transcription = batch["transcription"]
    gpu_policy = batch["gpu_policy"]
    preprocessing = batch["preprocessing"]
    quality = batch["audio_quality"]
    vad = batch["vad"]
    diarization = batch["diarization"]
    postprocessing = batch["postprocessing"]
    semantic = batch["semantic_extraction"]
    export = batch["export"]
    security = batch["security"]
    storage = batch["storage_guardian"]
    streaming = runtime["streaming"]
    stream_server = streaming["server"]
    redis = streaming["redis"]
    stream_gpu = streaming["gpu"]
    realtime = streaming["realtime"]
    monitor = streaming["monitor"]
    return {
        "AI_LOCAL_AUDIO_RUNTIME_CONTRACT": str(runtime["contract"]),
        "AI_LOCAL_REDIS_IMAGE": str(runtime["redis_image"]),
        "AUDIO_TRANSCRIBE_PATHS_INPUT_DIR": str(paths["input_dir"]),
        "AUDIO_TRANSCRIBE_PATHS_OUTPUT_DIR": str(paths["output_dir"]),
        "AUDIO_TRANSCRIBE_PATHS_MODELS_DIR": str(paths["models_dir"]),
        "AUDIO_TRANSCRIBE_PATHS_TMP_DIR": str(paths["tmp_dir"]),
        "AUDIO_TRANSCRIBE_DATA_MOUNT_ROOT": str(paths["data_mount_root"]),
        "AUDIO_TRANSCRIBE_TEMP_MOUNT_ROOT": str(paths["temp_mount_root"]),
        "AUDIO_TRANSCRIBE_SYSTEM_TMP_MOUNT_ROOT": str(paths["system_tmp_mount_root"]),
        "AUDIO_TRANSCRIBE_TEMP_TMPFS_SIZE": str(paths["temp_tmpfs_size"]),
        "AUDIO_TRANSCRIBE_SYSTEM_TMPFS_SIZE": str(paths["system_tmpfs_size"]),
        "AUDIO_TRANSCRIBE_HOST_HOME_PREFIX": str(paths["host_home_prefix"]),
        "AUDIO_TRANSCRIBE_HOST_HOME_MOUNT": str(paths["host_home_mount_root"]),
        "AUDIO_TRANSCRIBE_ALLOWED_DIRS": ",".join(paths["allowed_input_roots"]),
        "AUDIO_TRANSCRIBE_JOBS_MAX_CONCURRENT_JOBS": str(jobs["max_concurrent_jobs"]),
        "AUDIO_TRANSCRIBE_JOBS_MAX_QUEUED_JOBS": str(jobs["max_queued_jobs"]),
        "AUDIO_TRANSCRIBE_JOBS_JOB_TTL_HOURS": str(jobs["job_ttl_hours"]),
        "AUDIO_TRANSCRIBE_PERFORMANCE_MAX_CONCURRENT_GPU_TRANSCRIPTIONS": str(performance["max_concurrent_gpu_transcriptions"]),
        "AUDIO_TRANSCRIBE_PERFORMANCE_BATCH_SIZE": str(performance["batch_size"]),
        "AUDIO_TRANSCRIBE_PERFORMANCE_SEGMENT_STRATEGY": str(performance["segment_strategy"]),
        "AUDIO_TRANSCRIBE_PERFORMANCE_MAX_SEGMENT_DURATION_SECONDS": str(performance["max_segment_duration_seconds"]),
        "AUDIO_TRANSCRIBE_PERFORMANCE_SEGMENT_OVERLAP_SECONDS": str(performance["segment_overlap_seconds"]),
        "AUDIO_TRANSCRIBE_TRANSCRIPTION_MODEL": str(transcription["model"]),
        "AUDIO_TRANSCRIBE_TRANSCRIPTION_DEVICE": str(transcription["device"]),
        "AUDIO_TRANSCRIBE_TRANSCRIPTION_COMPUTE_TYPE": str(transcription["compute_type"]),
        "AUDIO_TRANSCRIBE_TRANSCRIPTION_BEAM_SIZE": str(transcription["beam_size"]),
        "AUDIO_TRANSCRIBE_TRANSCRIPTION_DOWNLOAD_ROOT": str(transcription["download_root"]),
        "AUDIO_TRANSCRIBE_TRANSCRIPTION_WORD_TIMESTAMPS": _bool(transcription["word_timestamps"]),
        "AUDIO_TRANSCRIBE_TRANSCRIPTION_VAD_FILTER": _bool(transcription["vad_filter"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_PREFER_GPU": _bool(gpu_policy["prefer_gpu"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_MIN_FREE_VRAM_MB": str(gpu_policy["min_free_vram_mb"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_LEASE_ESTIMATED_RAM_MB": str(gpu_policy["lease_estimated_ram_mb"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_LEASE_ESTIMATED_VRAM_MB": str(gpu_policy["lease_estimated_vram_mb"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_WAIT_TIMEOUT_SECONDS": str(gpu_policy["wait_timeout_seconds"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_WAIT_POLL_SECONDS": str(gpu_policy["wait_poll_seconds"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_MAX_DEFER_RETRY_SECONDS": str(gpu_policy["max_defer_retry_seconds"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_ALLOW_MODEL_DOWNGRADE": _bool(gpu_policy["allow_model_downgrade"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_DEGRADED_MODEL": str(gpu_policy["degraded_model"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_FALLBACK_MODELS": ",".join(gpu_policy["fallback_models"]),
        "AUDIO_TRANSCRIBE_GPU_POLICY_ALLOW_CPU_DEGRADATION": _bool(gpu_policy["allow_cpu_degradation"]),
        "AUDIO_TRANSCRIBE_PREPROCESSING_SAMPLE_RATE": str(preprocessing["sample_rate"]),
        "AUDIO_TRANSCRIBE_PREPROCESSING_MONO": _bool(preprocessing["mono"]),
        "AUDIO_TRANSCRIBE_PREPROCESSING_NORMALIZE_LOUDNESS": _bool(preprocessing["normalize_loudness"]),
        "AUDIO_TRANSCRIBE_PREPROCESSING_NOISE_REDUCTION": _bool(preprocessing["noise_reduction"]),
        "AUDIO_TRANSCRIBE_PREPROCESSING_NOISE_REDUCTION_BACKEND": str(preprocessing["noise_reduction_backend"]),
        "AUDIO_TRANSCRIBE_AUDIO_QUALITY_ENABLED": _bool(quality["enabled"]),
        "AUDIO_TRANSCRIBE_AUDIO_QUALITY_WARN_ON_CLIPPING": _bool(quality["warn_on_clipping"]),
        "AUDIO_TRANSCRIBE_AUDIO_QUALITY_WARN_ON_LOW_VOLUME": _bool(quality["warn_on_low_volume"]),
        "AUDIO_TRANSCRIBE_AUDIO_QUALITY_WARN_ON_HIGH_SILENCE_RATIO": _bool(quality["warn_on_high_silence_ratio"]),
        "AUDIO_TRANSCRIBE_VAD_ENABLED": _bool(vad["enabled"]),
        "AUDIO_TRANSCRIBE_VAD_MIN_SPEECH_DURATION_MS": str(vad["min_speech_duration_ms"]),
        "AUDIO_TRANSCRIBE_VAD_MIN_SILENCE_DURATION_MS": str(vad["min_silence_duration_ms"]),
        "AUDIO_TRANSCRIBE_VAD_SPEECH_PAD_MS": str(vad["speech_pad_ms"]),
        "AUDIO_TRANSCRIBE_VAD_ALLOW_WINDOW_SEGMENTATION": _bool(vad["allow_window_segmentation"]),
        "AUDIO_TRANSCRIBE_DIARIZATION_ENABLED": _bool(diarization["enabled"]),
        "AUDIO_TRANSCRIBE_DIARIZATION_PROVIDER": str(diarization["provider"]),
        "AUDIO_TRANSCRIBE_DIARIZATION_HF_TOKEN_FILE_ENV": str(diarization["hf_token_file_env"]),
        "AUDIO_TRANSCRIBE_DIARIZATION_MIN_SPEAKERS": str(diarization["min_speakers"] or 0),
        "AUDIO_TRANSCRIBE_DIARIZATION_MAX_SPEAKERS": str(diarization["max_speakers"] or 0),
        "AUDIO_TRANSCRIBE_POSTPROCESSING_REMOVE_FILLERS": _bool(postprocessing["remove_fillers"]),
        "AUDIO_TRANSCRIBE_POSTPROCESSING_REMOVE_REPETITIONS": _bool(postprocessing["remove_repetitions"]),
        "AUDIO_TRANSCRIBE_POSTPROCESSING_PARAGRAPHS": _bool(postprocessing["paragraphs"]),
        "AUDIO_TRANSCRIBE_POSTPROCESSING_CONSERVATIVE_CLEANUP": _bool(postprocessing["conservative_cleanup"]),
        "AUDIO_TRANSCRIBE_SEMANTIC_EXTRACTION_ENABLED": _bool(semantic["enabled"]),
        "AUDIO_TRANSCRIBE_SEMANTIC_EXTRACTION_EXTRACT_DECISIONS": _bool(semantic["extract_decisions"]),
        "AUDIO_TRANSCRIBE_SEMANTIC_EXTRACTION_EXTRACT_ACTION_ITEMS": _bool(semantic["extract_action_items"]),
        "AUDIO_TRANSCRIBE_SEMANTIC_EXTRACTION_EXTRACT_TOPICS": _bool(semantic["extract_topics"]),
        "AUDIO_TRANSCRIBE_SEMANTIC_EXTRACTION_EXTRACT_ENTITIES": _bool(semantic["extract_entities"]),
        "AUDIO_TRANSCRIBE_SEMANTIC_EXTRACTION_EXTRACT_KEY_QUOTES": _bool(semantic["extract_key_quotes"]),
        "AUDIO_TRANSCRIBE_SEMANTIC_EXTRACTION_EXTRACT_SPEAKER_NOTES": _bool(semantic["extract_speaker_notes"]),
        "AUDIO_TRANSCRIBE_EXPORT_FORMATS": ",".join(export["formats"]),
        "AUDIO_TRANSCRIBE_EXPORT_RAG_READY": _bool(export["rag_ready"]),
        "AUDIO_TRANSCRIBE_EXPORT_INCLUDE_SPEAKERS_IN_SUBTITLES": _bool(export["include_speakers_in_subtitles"]),
        "AUDIO_TRANSCRIBE_EXPORT_PUBLISH_POLICY": str(export["publish_policy"]),
        "AUDIO_TRANSCRIBE_OBSERVABILITY_LOG_LEVEL": str(batch["log_level"]),
        "AUDIO_TRANSCRIBE_SECURITY_MAX_UPLOAD_SIZE_MB": str(security["max_upload_size_mb"]),
        "AUDIO_TRANSCRIBE_SECURITY_API_KEY_FILE_ENV": str(security["api_key_file_env"]),
        "AUDIO_TRANSCRIBE_SECURITY_ALLOW_UNAUTHENTICATED_DEV": _bool(security["allow_unauthenticated_dev"]),
        "AUDIO_TRANSCRIBE_SECURITY_ALLOWED_INPUT_EXTENSIONS": ",".join(security["allowed_input_extensions"]),
        "AUDIO_TRANSCRIBE_STORAGE_GUARDIAN_URL": str(storage["url"]),
        "AUDIO_TRANSCRIBE_STORAGE_GUARDIAN_VERIFY_TLS": _bool(storage["verify_tls"]),
        "AUDIO_TRANSCRIBE_STORAGE_GUARDIAN_TIMEOUT_SECONDS": str(storage["timeout_seconds"]),
        "AUDIO_TRANSCRIBE_STORAGE_GUARDIAN_MAX_INLINE_BYTES": str(storage["max_inline_bytes"]),
        "AUDIO_TRANSCRIBE_STORAGE_GUARDIAN_PUBLISH_RETRY_ATTEMPTS": str(storage["publish_retry_attempts"]),
        "AUDIO_TRANSCRIBE_STORAGE_GUARDIAN_PUBLISH_RETRY_DELAY_SECONDS": str(storage["publish_retry_delay_seconds"]),
        "AUDIO_TRANSCRIBE_STORAGE_GUARDIAN_SEMANTIC_READ_MAX_BYTES": str(storage["semantic_read_max_bytes"]),
        "AUDIO_TRANSCRIBE_STORAGE_GUARDIAN_REUSE_PROBE_MAX_BYTES": str(storage["reuse_probe_max_bytes"]),
        "AUDIO_STREAMING_SERVER_LOG_LEVEL": str(stream_server["log_level"]),
        "AUDIO_STREAMING_SERVER_WEBSOCKET_MAX_SIZE_BYTES": str(stream_server["websocket_max_size_bytes"]),
        "AUDIO_STREAMING_SERVER_CONFIG_MESSAGE_TIMEOUT_SECONDS": str(stream_server["config_message_timeout_seconds"]),
        "AUDIO_STREAMING_REDIS_URL": str(redis["url"]),
        "AUDIO_STREAMING_REDIS_MAX_STREAM_LEN": str(redis["max_stream_len"]),
        "AUDIO_STREAMING_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS": str(redis["socket_connect_timeout_seconds"]),
        "AUDIO_STREAMING_REDIS_CONSUMER_BATCH_SIZE": str(redis["consumer_batch_size"]),
        "AUDIO_STREAMING_REDIS_CONSUMER_BLOCK_MS": str(redis["consumer_block_ms"]),
        "AUDIO_STREAMING_REDIS_RESULT_POLL_TIMEOUT_SECONDS": str(redis["result_poll_timeout_seconds"]),
        "AUDIO_STREAMING_REDIS_IDLE_POLL_INTERVAL_SECONDS": str(redis["idle_poll_interval_seconds"]),
        "AUDIO_STREAMING_GPU_MAX_WORKERS": str(stream_gpu["max_workers"]),
        "AUDIO_STREAMING_GPU_MODEL_NAME": str(stream_gpu["model_name"]),
        "AUDIO_STREAMING_GPU_COMPUTE_TYPE": str(stream_gpu["compute_type"]),
        "AUDIO_STREAMING_GPU_CPU_COMPUTE_TYPE": str(stream_gpu["cpu_compute_type"]),
        "AUDIO_STREAMING_GPU_DEVICE": str(stream_gpu["device"]),
        "AUDIO_STREAMING_GPU_CPU_THREADS": str(stream_gpu["cpu_threads"]),
        "AUDIO_STREAMING_GPU_DOWNLOAD_ROOT": str(stream_gpu["download_root"]),
        "AUDIO_STREAMING_GPU_BEAM_SIZE": str(stream_gpu["beam_size"]),
        "AUDIO_STREAMING_GPU_VAD_FILTER": _bool(stream_gpu["vad_filter"]),
        "AUDIO_STREAMING_GPU_WORKER_ERROR_RETRY_SECONDS": str(stream_gpu["worker_error_retry_seconds"]),
        "AUDIO_STREAMING_REALTIME_SAMPLE_RATE": str(realtime["sample_rate"]),
        "AUDIO_STREAMING_REALTIME_FRAME_DURATION_MS": str(realtime["frame_duration_ms"]),
        "AUDIO_STREAMING_REALTIME_MIN_SPEECH_MS": str(realtime["min_speech_ms"]),
        "AUDIO_STREAMING_REALTIME_MAX_SPEECH_MS": str(realtime["max_speech_ms"]),
        "AUDIO_STREAMING_REALTIME_SILENCE_THRESHOLD_MS": str(realtime["silence_threshold_ms"]),
        "AUDIO_STREAMING_REALTIME_VAD_ENERGY_THRESHOLD_DB": str(realtime["vad_energy_threshold_db"]),
        "AUDIO_STREAMING_REALTIME_VAD_SPEECH_PAD_MS": str(realtime["vad_speech_pad_ms"]),
        "AUDIO_STREAMING_REALTIME_VAD_ONSET_FRAMES": str(realtime["vad_onset_frames"]),
        "AUDIO_STREAMING_REALTIME_FINAL_RESULT_TIMEOUT_SECONDS": str(realtime["final_result_timeout_seconds"]),
        "AUDIO_STREAMING_REALTIME_MAX_SESSIONS": str(realtime["max_sessions"]),
        "AUDIO_STREAMING_MONITOR_BATCH_URL": str(monitor["batch_url"]),
        "AUDIO_STREAMING_MONITOR_API_TIMEOUT_SECONDS": str(monitor["api_timeout_seconds"]),
        "AUDIO_STREAMING_MONITOR_POLL_INTERVAL_SECONDS": str(monitor["poll_interval_seconds"]),
        "AUDIO_STREAMING_MONITOR_TIMEOUT_SECONDS": str(monitor["timeout_seconds"]),
        "AUDIO_STREAMING_MONITOR_PROGRESS_EMIT_DELTA": str(monitor["progress_emit_delta"]),
    }


_AUDIO_OVERRIDE_SECTIONS: dict[str, tuple[str, ...]] = {
    "paths": ("input_dir", "output_dir", "models_dir", "tmp_dir", "data_mount_root", "host_home_mount_root", "temp_mount_root", "system_tmp_mount_root", "temp_tmpfs_size", "system_tmpfs_size", "allowed_input_roots"),
    "jobs": ("max_concurrent_jobs", "max_queued_jobs", "job_ttl_hours"),
    "performance": ("max_concurrent_gpu_transcriptions", "batch_size", "segment_strategy", "max_segment_duration_seconds", "segment_overlap_seconds"),
    "transcription": ("model", "device", "compute_type", "beam_size", "word_timestamps", "vad_filter"),
    "gpu_policy": ("prefer_gpu", "min_free_vram_mb", "lease_estimated_ram_mb", "lease_estimated_vram_mb", "wait_timeout_seconds", "wait_poll_seconds", "max_defer_retry_seconds", "allow_model_downgrade", "degraded_model", "fallback_models", "allow_cpu_degradation"),
    "preprocessing": ("sample_rate", "mono", "normalize_loudness", "noise_reduction", "noise_reduction_backend"),
    "audio_quality": ("enabled", "warn_on_clipping", "warn_on_low_volume", "warn_on_high_silence_ratio"),
    "vad": ("enabled", "min_speech_duration_ms", "min_silence_duration_ms", "speech_pad_ms", "allow_window_segmentation"),
    "diarization": ("enabled", "provider", "hf_token_file_env", "min_speakers", "max_speakers"),
    "postprocessing": ("remove_fillers", "remove_repetitions", "paragraphs", "conservative_cleanup"),
    "semantic_extraction": ("enabled", "extract_decisions", "extract_action_items", "extract_topics", "extract_entities", "extract_key_quotes", "extract_speaker_notes"),
    "export": ("formats", "rag_ready", "include_speakers_in_subtitles", "publish_policy"),
    "security": ("max_upload_size_mb", "api_key_file_env", "allow_unauthenticated_dev", "allowed_input_extensions"),
    "storage_guardian": ("timeout_seconds", "publish_retry_attempts", "publish_retry_delay_seconds", "semantic_read_max_bytes", "reuse_probe_max_bytes"),
}
_AUDIO_STREAMING_OVERRIDE_SECTIONS: dict[str, tuple[str, ...]] = {
    "server": ("log_level", "websocket_max_size_bytes", "config_message_timeout_seconds"),
    "redis": ("image", "max_stream_len", "socket_connect_timeout_seconds", "consumer_batch_size", "consumer_block_ms", "result_poll_timeout_seconds", "idle_poll_interval_seconds"),
    "gpu": ("max_workers", "model_name", "compute_type", "cpu_compute_type", "device", "cpu_threads", "beam_size", "vad_filter", "worker_error_retry_seconds"),
    "realtime": ("sample_rate", "frame_duration_ms", "min_speech_ms", "max_speech_ms", "silence_threshold_ms", "vad_energy_threshold_db", "vad_speech_pad_ms", "vad_onset_frames", "final_result_timeout_seconds", "max_sessions"),
    "monitor": ("api_timeout_seconds", "poll_interval_seconds", "timeout_seconds", "progress_emit_delta"),
}

AUDIO_ENV_OVERRIDES = {
    **{f"AI_AUDIO_{section.upper()}_{field.upper()}": f"audio.{section}.{field}" for section, fields in _AUDIO_OVERRIDE_SECTIONS.items() for field in fields},
    **{f"AI_AUDIO_STREAMING_{section.upper()}_{field.upper()}": f"audio.streaming.{section}.{field}" for section, fields in _AUDIO_STREAMING_OVERRIDE_SECTIONS.items() for field in fields},
    "AI_AUDIO_LOG_LEVEL": "audio.log_level",
}
