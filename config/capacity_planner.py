"""Single-source runtime capacity decisions derived from effective envelopes."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_COMPOSE_MEMORY = re.compile(r"^(?P<value>[0-9]+(?:\.[0-9]+)?)(?P<unit>[kmg])$", re.IGNORECASE)


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def compose_memory_mb(value: str) -> int:
    """Convert a generated Compose k/m/g memory limit into integral MiB."""
    match = _COMPOSE_MEMORY.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"invalid generated Compose memory limit: {value!r}")
    amount = float(match.group("value"))
    unit = match.group("unit").lower()
    multiplier = {"k": 1 / 1024, "m": 1, "g": 1024}[unit]
    result = math.floor(amount * multiplier)
    if result < 1:
        raise ValueError(f"generated Compose memory limit is below 1 MiB: {value!r}")
    return result


def infer_rag_parallel_jobs(workers: int) -> int:
    return clamp_int(max(1, workers) * 2, 1, 4)


def infer_embedding_batch(batch_size: int) -> int:
    return clamp_int(max(1, batch_size) * 2, 16, 50)


@dataclass(frozen=True)
class RagCapacityPlan:
    max_cpu_percent: int
    max_memory_percent: int
    max_parallel_jobs: int
    graph_parallel_jobs: int
    parser_workers: int
    parser_worker_memory_limit_mb: int
    embedding_batch_size: int
    embedding_cache_size: int
    embedding_batch_max_chars: int
    chunks_queue_max: int
    files_queue_max: int
    pause_memory_percent: int
    abort_memory_percent: int
    max_swap_percent: int
    pause_swap_percent: int
    abort_swap_percent: int
    embedding_concurrency: int
    embedding_timeout_seconds: int
    query_timeout_seconds: int
    router_timeout_seconds: int
    graph_timeout_seconds: int
    enrich_timeout_seconds: int
    graphify_max_concurrency: int
    community_max_workers: int
    llm_http_connect_timeout_seconds: int
    llm_http_read_timeout_seconds: int
    llm_http_write_timeout_seconds: int
    llm_http_pool_timeout_seconds: int
    llm_http_max_connections: int
    llm_http_max_keepalive_connections: int


@dataclass(frozen=True)
class TranslationCapacityPlan:
    intra_threads: int
    inter_threads: int
    ollama_timeout_seconds: int
    cache_max_entries: int


@dataclass(frozen=True)
class ObservabilityCapacityPlan:
    memory_limit_percentage: int
    memory_spike_limit_percentage: int
    queue_consumers: int
    queue_size: int
    queue_min_batch_size: int


@dataclass(frozen=True)
class ExecutionValidationCapacityPlan:
    cpu_limit: float
    memory_limit: str
    pids_limit: int


def infer_execution_validation_capacity(*, profile: str, cpu_threads: int, ram_available_gb: float | None) -> ExecutionValidationCapacityPlan:
    if profile not in {"standard", "polyglot"} or cpu_threads < 1:
        raise ValueError("Execution validation capacity inputs are invalid")
    usable_ram_gb = ram_available_gb if ram_available_gb and ram_available_gb > 0 else 4.0
    if profile == "standard":
        cpu_limit = max(0.5, min(2.0, math.floor(cpu_threads * 0.08 * 2) / 2))
        raw_memory_mb = math.floor(usable_ram_gb * 0.06 * 1024)
        memory_mb = clamp_int((raw_memory_mb // 256) * 256, 512, 2048)
        pids_limit = clamp_int(128 + round(cpu_limit * 128), 192, 512)
    else:
        cpu_limit = max(1.0, min(4.0, math.floor(cpu_threads * 0.15 * 2) / 2))
        raw_memory_mb = math.floor(usable_ram_gb * 0.20 * 1024)
        memory_mb = clamp_int((raw_memory_mb // 512) * 512, 2048, 8192)
        pids_limit = clamp_int(256 + round(cpu_limit * 128), 384, 1024)
    return ExecutionValidationCapacityPlan(cpu_limit=cpu_limit, memory_limit=f"{memory_mb}m", pids_limit=pids_limit)


def infer_observability_capacity(*, memory_budget_fraction: float, batch_size: int) -> ObservabilityCapacityPlan:
    if not 0 < memory_budget_fraction <= 1 or batch_size < 1:
        raise ValueError("Observability capacity inputs are invalid")
    memory_limit = clamp_int(round(memory_budget_fraction * 100) + 10, 65, 90)
    minimum_batch = clamp_int(batch_size * 100, 1_000, 10_000)
    return ObservabilityCapacityPlan(memory_limit_percentage=memory_limit, memory_spike_limit_percentage=100 - memory_limit, queue_consumers=clamp_int(math.ceil(batch_size / 16), 1, 8), queue_size=minimum_batch * 2, queue_min_batch_size=minimum_batch)


def infer_translation_capacity(*, workers: int, ram_available_gb: float | None, llm_timeout_seconds: int) -> TranslationCapacityPlan:
    if workers < 1 or llm_timeout_seconds < 1:
        raise ValueError("Translation capacity inputs must be positive")
    if ram_available_gb is not None and ram_available_gb <= 0:
        raise ValueError("available RAM must be positive when known")
    usable_ram_gb = ram_available_gb if ram_available_gb is not None else 2.0
    return TranslationCapacityPlan(intra_threads=clamp_int(workers, 1, 8), inter_threads=1, ollama_timeout_seconds=clamp_int(llm_timeout_seconds, 30, 300), cache_max_entries=clamp_int(round(usable_ram_gb * 256), 512, 8192))


def infer_rag_capacity(*, workers: int, batch_size: int, llm_timeout_seconds: int, quality_latency: str, gpu_available: bool, cpu_budget_fraction: float, memory_budget_fraction: float, service_memory_limit_mb: int, cpu_pressure_percent: int, memory_pressure_percent: int, swap_hard_percent: int, embedding_lane_concurrency: int) -> RagCapacityPlan:
    if workers < 1 or batch_size < 1 or llm_timeout_seconds < 1:
        raise ValueError("RAG capacity inputs must be positive")
    if quality_latency not in {"fast", "balanced", "quality"}:
        raise ValueError("quality_latency must be fast, balanced or quality")
    if not 0 < cpu_budget_fraction <= 1 or not 0 < memory_budget_fraction <= 1:
        raise ValueError("RAG capacity fractions must be in (0, 1]")
    if service_memory_limit_mb < 512:
        raise ValueError("RAG service memory envelope must be at least 512 MiB")
    max_parallel_jobs = infer_rag_parallel_jobs(workers)
    graph_parallel_jobs = clamp_int(workers, 1, 2)
    parser_workers = 1 if gpu_available else clamp_int(workers, 1, 2)
    parser_budget_mb = math.floor(service_memory_limit_mb * 0.75)
    parser_limit_raw_mb = math.floor(parser_budget_mb / parser_workers)
    parser_worker_memory_limit_mb = (parser_limit_raw_mb // 64) * 64
    if parser_worker_memory_limit_mb < 512:
        raise ValueError("RAG parser workers do not fit inside the service memory envelope")
    embedding_batch_size = infer_embedding_batch(batch_size)
    cache_capacity = max(64, service_memory_limit_mb // 16)
    embedding_cache_size = clamp_int(2 ** math.floor(math.log2(cache_capacity)), 64, 1024)
    pause_memory_percent = clamp_int(memory_pressure_percent, 50, 95)
    abort_memory_percent = clamp_int(pause_memory_percent + 10, pause_memory_percent + 1, 100)
    swap_hard_percent = clamp_int(swap_hard_percent, 30, 90)
    graph_base_seconds = 1800 if quality_latency == "fast" else 3600
    graph_timeout_seconds = min(7200, graph_base_seconds * max(1, math.ceil(4 / graph_parallel_jobs)))
    enrich_timeout_seconds = clamp_int(llm_timeout_seconds * 3, 180, 300)
    query_timeout_seconds = clamp_int(math.ceil(llm_timeout_seconds / 4), 30, 60)
    router_timeout_seconds = clamp_int(math.ceil(llm_timeout_seconds / 8), 8, 30)
    http_connections = clamp_int(max_parallel_jobs * 2 + 2, 4, 10)
    return RagCapacityPlan(max_cpu_percent=clamp_int(round((cpu_budget_fraction + 0.25) * 100), 50, clamp_int(cpu_pressure_percent, 50, 100)), max_memory_percent=clamp_int(round(memory_budget_fraction * 100), 45, min(85, pause_memory_percent - 1)), max_parallel_jobs=max_parallel_jobs, graph_parallel_jobs=graph_parallel_jobs, parser_workers=parser_workers, parser_worker_memory_limit_mb=parser_worker_memory_limit_mb, embedding_batch_size=embedding_batch_size, embedding_cache_size=embedding_cache_size, embedding_batch_max_chars=embedding_batch_size * 1200, chunks_queue_max=max(64, embedding_batch_size), files_queue_max=max(128, embedding_batch_size * 2), pause_memory_percent=pause_memory_percent, abort_memory_percent=abort_memory_percent, max_swap_percent=max(0, swap_hard_percent - 30), pause_swap_percent=max(1, swap_hard_percent - 10), abort_swap_percent=min(100, swap_hard_percent + 10), embedding_concurrency=max(1, embedding_lane_concurrency), embedding_timeout_seconds=max(120, llm_timeout_seconds), query_timeout_seconds=query_timeout_seconds, router_timeout_seconds=router_timeout_seconds, graph_timeout_seconds=graph_timeout_seconds, enrich_timeout_seconds=enrich_timeout_seconds, graphify_max_concurrency=1, community_max_workers=clamp_int(workers + 1, 1, 3), llm_http_connect_timeout_seconds=max(5, min(10, query_timeout_seconds // 6)), llm_http_read_timeout_seconds=max(300, llm_timeout_seconds), llm_http_write_timeout_seconds=max(30, router_timeout_seconds), llm_http_pool_timeout_seconds=max(10, router_timeout_seconds // 2), llm_http_max_connections=http_connections, llm_http_max_keepalive_connections=max(2, http_connections // 2))
