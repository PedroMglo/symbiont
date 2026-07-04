"""Derived Docker Compose resource limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DockerResourceValue:
    env: str
    value: str
    origin: str
    reason: str
    formula: str
    override: str


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def _mem(value_gb: float) -> str:
    if value_gb < 1:
        mb = int(round(value_gb * 1024 / 64) * 64)
        return f"{max(128, mb)}m"
    rounded = round(value_gb * 2) / 2
    return f"{int(rounded)}g" if rounded.is_integer() else f"{rounded:g}g"


def _cpu(value: float) -> str:
    rounded = round(value * 2) / 2
    return f"{rounded:.1f}"


def _ram_total(resolved: dict[str, Any]) -> float:
    runtime = resolved.get("runtime", {})
    return float(runtime.get("ram_total_gb") or runtime.get("ram_available_gb") or 16.0)


def _cpu_threads(resolved: dict[str, Any]) -> int:
    runtime = resolved.get("runtime", {})
    return max(1, int(runtime.get("cpu_threads") or 4))


def _bool_env(value: bool) -> str:
    return "true" if value else "false"


def _compose_parallel_limit(resolved: dict[str, Any], *, ram_gb: float, threads: int) -> int:
    docker_config = resolved.get("config", {}).get("docker", {})
    configured = docker_config.get("compose_parallel_limit", "auto")
    if configured != "auto":
        return max(1, int(configured))
    if ram_gb < 12 or threads <= 4:
        return 2
    if ram_gb >= 48 and threads >= 16:
        return 6
    return 4


def _build_cache_max(resolved: dict[str, Any], *, ram_gb: float) -> str:
    docker_config = resolved.get("config", {}).get("docker", {})
    configured = str(docker_config.get("build_cache_max", "auto"))
    if configured != "auto":
        return configured
    if ram_gb < 12:
        return "20gb"
    if ram_gb >= 48:
        return "50gb"
    return "30gb"


def _resource(
    env: str,
    value: str,
    *,
    reason: str,
    formula: str,
    origin: str = "inferred",
) -> DockerResourceValue:
    return DockerResourceValue(
        env=env,
        value=value,
        origin=origin,
        reason=reason,
        formula=formula,
        override=env,
    )


def _mem_limit(ram_gb: float, fraction: float, minimum: float, maximum: float) -> str:
    return _mem(_clamp(ram_gb * fraction, minimum, maximum))


def _mem_reservation(limit: str, fallback_mb: int = 128) -> str:
    if limit.endswith("m"):
        raw_mb = int(limit.removesuffix("m"))
    else:
        raw_mb = int(float(limit.removesuffix("g")) * 1024)
    reservation = max(fallback_mb, int(raw_mb * 0.25 / 64) * 64)
    return f"{reservation}m"


def _cpu_limit(threads: int, fraction: float, minimum: float, maximum: float) -> str:
    return _cpu(_clamp(threads * fraction, minimum, maximum))


def _append_service(
    values: list[DockerResourceValue],
    prefix: str,
    *,
    mem_limit: str,
    cpus: str,
    pids: int,
    mem_reservation: str | None = None,
    reason: str,
    formula: str,
) -> None:
    values.append(_resource(f"{prefix}_MEM_LIMIT", mem_limit, reason=reason, formula=formula))
    values.append(_resource(f"{prefix}_CPUS_LIMIT", cpus, reason=reason, formula=formula))
    values.append(
        _resource(
            f"{prefix}_PIDS_LIMIT",
            str(pids),
            reason="Pids are bounded per service class to reduce runaway process risk.",
            formula="static safe bound by service class",
            origin="safe_default",
        )
    )
    if mem_reservation is not None:
        values.append(
            _resource(
                f"{prefix}_MEM_RESERVATION",
                mem_reservation,
                reason="Reservation follows a conservative share of the generated memory limit.",
                formula="max(service minimum, 25% of memory limit)",
            )
        )


def resolve_docker_resources(resolved: dict[str, Any]) -> list[DockerResourceValue]:
    """Return non-secret Docker resource env values derived from host capacity."""

    ram_gb = _ram_total(resolved)
    threads = _cpu_threads(resolved)
    runtime = resolved.get("runtime", {})
    gpu_available = bool(runtime.get("gpu_available"))
    vram_total = float(runtime.get("vram_total_gb") or 0.0)
    docker_config = resolved.get("config", {}).get("docker", {})
    compose_parallel = _compose_parallel_limit(resolved, ram_gb=ram_gb, threads=threads)
    build_cache_max = _build_cache_max(resolved, ram_gb=ram_gb)

    values: list[DockerResourceValue] = [
        _resource(
            "DOCKER_BUILDKIT",
            "1" if bool(docker_config.get("buildkit", True)) else "0",
            reason="BuildKit is enabled by default for cache mounts and efficient multi-stage Docker builds.",
            formula="config.docker.buildkit",
            origin="config",
        ),
        _resource(
            "AI_LOCAL_COMPOSE_PARALLEL_LIMIT",
            str(compose_parallel),
            reason="Compose parallelism is bounded by resolved machine capacity and can be overridden per user.",
            formula="config override else 2 for low capacity, 6 for large workstations, otherwise 4",
        ),
        _resource(
            "COMPOSE_PARALLEL_LIMIT",
            str(compose_parallel),
            reason="Compose reads this variable to cap concurrent Docker Engine operations.",
            formula="mirrors AI_LOCAL_COMPOSE_PARALLEL_LIMIT",
        ),
        _resource(
            "AI_LOCAL_DOCKER_BUILD_CACHE_MAX",
            build_cache_max,
            reason="BuildKit cache cap keeps SSD usage bounded without deleting Docker volumes.",
            formula="config override else 20gb low RAM, 50gb workstation, otherwise 30gb",
        ),
        _resource(
            "AI_LOCAL_DOCKER_UP_NO_BUILD",
            _bool_env(bool(docker_config.get("up_no_build", True))),
            reason="Runtime startup is separated from image build by default.",
            formula="config.docker.up_no_build",
            origin="config",
        ),
        _resource(
            "AI_LOCAL_DOCKER_UP_WAIT",
            _bool_env(bool(docker_config.get("up_wait", True))),
            reason="Startup waits for running/healthy services when supported by Docker Compose.",
            formula="config.docker.up_wait",
            origin="config",
        ),
        _resource(
            "AI_LOCAL_DOCKER_UP_WAIT_TIMEOUT",
            str(int(docker_config.get("up_wait_timeout_seconds", 120))),
            reason="Compose wait timeout is user-configurable for slower machines.",
            formula="config.docker.up_wait_timeout_seconds",
            origin="config",
        ),
        _resource(
            "AI_LOCAL_DOCKER_REMOVE_ORPHANS",
            _bool_env(bool(docker_config.get("remove_orphans", False))),
            reason="Orphan removal is explicit because it can stop containers outside the selected profile set.",
            formula="config.docker.remove_orphans",
            origin="config",
        ),
    ]

    llama_aux_mem = _mem_limit(ram_gb, 0.18, 3.0, 6.0)
    _append_service(
        values,
        "LLAMA_CPP_AUX",
        mem_limit=llama_aux_mem,
        cpus=_cpu_limit(threads, 0.25, 2.0, 6.0),
        pids=512,
        mem_reservation=_mem_reservation(llama_aux_mem, 1024),
        reason="Auxiliary llama.cpp serving scales with host RAM/CPU but stays below the main LLM budget.",
        formula="memory=clamp(ram_total_gb*0.18, 3g, 6g); cpus=clamp(cpu_threads*0.25, 2, 6)",
    )

    llama_fast_mem = _mem_limit(ram_gb, 0.10, 2.0, 4.0)
    _append_service(
        values,
        "LLAMA_CPP_FAST",
        mem_limit=llama_fast_mem,
        cpus=_cpu_limit(threads, 0.17, 1.0, 4.0),
        pids=512,
        mem_reservation=_mem_reservation(llama_fast_mem, 512),
        reason="Fast classifier serving remains small and CPU-bound.",
        formula="memory=clamp(ram_total_gb*0.10, 2g, 4g); cpus=clamp(cpu_threads*0.17, 1, 4)",
    )

    vllm_max = 16.0 if ram_gb >= 48 else 12.0
    vllm_min = 8.0 if gpu_available or vram_total else 6.0
    vllm_mem = _mem_limit(ram_gb, 0.38, vllm_min, vllm_max)
    _append_service(
        values,
        "VLLM",
        mem_limit=vllm_mem,
        cpus=_cpu_limit(threads, 0.20, 2.0, 6.0),
        pids=1024,
        reason="vLLM host memory/CPU follows GPU-serving capacity while preserving desktop headroom.",
        formula="memory=clamp(ram_total_gb*0.38, 8g if GPU else 6g, 12g/16g); cpus=clamp(cpu_threads*0.20, 2, 6)",
    )

    storage_mem = _mem_limit(ram_gb, 0.03, 0.5, 1.0)
    _append_service(
        values,
        "STORAGE_GUARDIAN",
        mem_limit=storage_mem,
        cpus=_cpu_limit(threads, 0.06, 0.5, 1.0),
        pids=256,
        mem_reservation=_mem_reservation(storage_mem, 128),
        reason="Storage Guardian is mostly I/O-bound and should stay light on every machine.",
        formula="memory=clamp(ram_total_gb*0.03, 512m, 1g); cpus=clamp(cpu_threads*0.06, 0.5, 1)",
    )

    symbiont_mem = _mem_limit(ram_gb, 0.06, 1.0, 2.0)
    _append_service(
        values,
        "SYMBIONT",
        mem_limit=symbiont_mem,
        cpus=_cpu_limit(threads, 0.12, 1.0, 3.0),
        pids=512,
        mem_reservation=_mem_reservation(symbiont_mem, 256),
        reason="The main symbiont scales with local routing/concurrency but remains below model-serving budgets.",
        formula="memory=clamp(ram_total_gb*0.06, 1g, 2g); cpus=clamp(cpu_threads*0.12, 1, 3)",
    )

    _append_service(
        values,
        "DOCKER_PROXY",
        mem_limit="64m",
        cpus="0.25",
        pids=64,
        mem_reservation="16m",
        reason="Docker proxy is a tiny control-plane sidecar with a fixed low resource envelope.",
        formula="static sidecar envelope",
    )

    agent_mem = _mem_limit(ram_gb, 0.03, 0.5, 1.0)
    for prefix in (
        "REASONING_AND_RESPONSE",
        "RESEARCH",
        "LOCAL_EVIDENCE_OPERATOR",
        "EXECUTION_POLICY_OPERATOR",
        "PERSONAL_CONTEXT",
    ):
        _append_service(
            values,
            prefix,
            mem_limit=agent_mem,
            cpus=_cpu_limit(threads, 0.06, 0.5, 1.5),
            pids=512,
            mem_reservation=_mem_reservation(agent_mem, 128),
            reason="Agent and lightweight feature services share a compact envelope derived from host capacity.",
            formula="memory=clamp(ram_total_gb*0.03, 512m, 1g); cpus=clamp(cpu_threads*0.06, 0.5, 1.5)",
        )

    extrator_mem = _mem_limit(ram_gb, 0.08, 1.5, 3.0)
    _append_service(
        values,
        "EXTRATOR",
        mem_limit=extrator_mem,
        cpus=_cpu_limit(threads, 0.10, 1.0, 3.0),
        pids=512,
        mem_reservation=_mem_reservation(extrator_mem, 256),
        reason="Extraction can process larger files, so it receives a larger RAM/CPU envelope.",
        formula="memory=clamp(ram_total_gb*0.08, 1.5g, 3g); cpus=clamp(cpu_threads*0.10, 1, 3)",
    )

    translation_mem = _mem_limit(ram_gb, 0.06, 1.0, 2.5)
    _append_service(
        values,
        "TRANSLATION",
        mem_limit=translation_mem,
        cpus=_cpu_limit(threads, 0.10, 1.0, 2.5),
        pids=256,
        mem_reservation=_mem_reservation(translation_mem, 512),
        reason="Translation keeps enough memory for local model/cache use while scaling below heavy services.",
        formula="memory=clamp(ram_total_gb*0.06, 1g, 2.5g); cpus=clamp(cpu_threads*0.10, 1, 2.5)",
    )

    audio_transcribe_min = 4.0 if gpu_available else 2.0
    audio_transcribe_mem = _mem_limit(ram_gb, 0.16, audio_transcribe_min, 8.0)
    _append_service(
        values,
        "AUDIO_TRANSCRIBE",
        mem_limit=audio_transcribe_mem,
        cpus=_cpu_limit(threads, 0.16, 2.0, 4.0),
        pids=1024,
        mem_reservation=_mem_reservation(audio_transcribe_mem, 1024),
        reason="Audio transcription is a heavy optional service and scales with host RAM/GPU capacity.",
        formula="memory=clamp(ram_total_gb*0.16, 4g if GPU else 2g, 8g); cpus=clamp(cpu_threads*0.16, 2, 4)",
    )

    audio_streaming_mem = _mem_limit(ram_gb, 0.10, 2.0, 4.0)
    _append_service(
        values,
        "AUDIO_STREAMING",
        mem_limit=audio_streaming_mem,
        cpus=_cpu_limit(threads, 0.14, 1.5, 4.0),
        pids=1024,
        mem_reservation=_mem_reservation(audio_streaming_mem, 1024),
        reason="Audio streaming is optional and gets enough headroom for local streaming buffers and post-processing.",
        formula="memory=clamp(ram_total_gb*0.10, 2g, 4g); cpus=clamp(cpu_threads*0.14, 1.5, 4)",
    )

    redis_mem = _mem_limit(ram_gb, 0.03, 0.5, 1.0)
    _append_service(
        values,
        "REDIS",
        mem_limit=redis_mem,
        cpus=_cpu_limit(threads, 0.06, 0.5, 1.0),
        pids=512,
        mem_reservation=_mem_reservation(redis_mem, 256),
        reason="Redis is bounded as a local queue/cache and scales lightly with host RAM.",
        formula="memory=clamp(ram_total_gb*0.03, 512m, 1g); cpus=clamp(cpu_threads*0.06, 0.5, 1)",
    )

    rag_mem = _mem_limit(ram_gb, 0.08, 2.0, 4.0)
    values.append(
        _resource(
            "RAG_DEFAULT_PIDS_LIMIT",
            "512",
            reason="Base secure-service pids limit for RAG-side services.",
            formula="static safe bound for RAG compose secure-service anchor",
            origin="safe_default",
        )
    )
    _append_service(
        values,
        "RAG",
        mem_limit=rag_mem,
        cpus=_cpu_limit(threads, 0.12, 1.0, 3.0),
        pids=512,
        mem_reservation=_mem_reservation(rag_mem, 512),
        reason="RAG memory/CPU scales with local indexing and retrieval capacity.",
        formula="memory=clamp(ram_total_gb*0.08, 2g, 4g); cpus=clamp(cpu_threads*0.12, 1, 3)",
    )

    qdrant_mem = _mem_limit(ram_gb, 0.06, 1.0, 4.0)
    _append_service(
        values,
        "QDRANT",
        mem_limit=qdrant_mem,
        cpus=_cpu_limit(threads, 0.12, 1.0, 3.0),
        pids=512,
        mem_reservation=_mem_reservation(qdrant_mem, 256),
        reason="Qdrant vector storage scales with host RAM while staying capped for local desktops.",
        formula="memory=clamp(ram_total_gb*0.06, 1g, 4g); cpus=clamp(cpu_threads*0.12, 1, 3)",
    )

    clickhouse_mem = _mem_limit(ram_gb, 0.06, 1.0, 4.0)
    _append_service(
        values,
        "CLICKHOUSE",
        mem_limit=clickhouse_mem,
        cpus=_cpu_limit(threads, 0.16, 1.0, 4.0),
        pids=2048,
        mem_reservation=_mem_reservation(clickhouse_mem, 512),
        reason="ClickHouse observability storage scales with host RAM but stays capped for local use.",
        formula="memory=clamp(ram_total_gb*0.06, 1g, 4g); cpus=clamp(cpu_threads*0.16, 1, 4)",
    )

    for prefix, fraction, minimum, maximum, cpu_fraction, cpu_min, cpu_max, min_res in (
        ("GRAFANA", 0.02, 0.5, 1.0, 0.06, 0.5, 1.0, 128),
        ("OTEL_COLLECTOR", 0.02, 0.5, 1.0, 0.06, 0.5, 1.0, 128),
        ("LANGFUSE_DB", 0.03, 0.5, 1.0, 0.08, 0.5, 1.5, 128),
        ("LANGFUSE", 0.04, 0.75, 2.0, 0.10, 1.0, 2.0, 256),
    ):
        mem_limit = _mem_limit(ram_gb, fraction, minimum, maximum)
        _append_service(
            values,
            prefix,
            mem_limit=mem_limit,
            cpus=_cpu_limit(threads, cpu_fraction, cpu_min, cpu_max),
            pids=512,
            mem_reservation=_mem_reservation(mem_limit, min_res),
            reason="Observability service limits scale with local machine capacity.",
            formula=(
                f"memory=clamp(ram_total_gb*{fraction:g}, {_mem(minimum)}, {_mem(maximum)}); "
                f"cpus=clamp(cpu_threads*{cpu_fraction:g}, {cpu_min:g}, {cpu_max:g})"
            ),
        )

    return values


def resolve_docker_resource_env(resolved: dict[str, Any]) -> dict[str, str]:
    return {item.env: item.value for item in resolve_docker_resources(resolved)}
