"""Derived RAG runtime generated environment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RagRuntimeValue:
    env: str
    value: str
    origin: str
    reason: str
    formula: str
    override: str


def _decision(resolved: dict[str, Any], field: str, default: object) -> object:
    for decision in resolved.get("decisions", []):
        if decision.get("field") == field:
            return decision.get("value")
    return default


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def resolve_rag_runtime(resolved: dict[str, Any]) -> list[RagRuntimeValue]:
    """Return explainable, non-secret RAG runtime env values."""

    config = resolved["config"]
    runtime = resolved["runtime"]
    resource_governor_policy = resolved.get("resource_governor_policy") or {}
    rag_policy = resource_governor_policy.get("rag") if isinstance(resource_governor_policy, dict) else {}
    pressure_policy = resource_governor_policy.get("pressure_policy") if isinstance(resource_governor_policy, dict) else {}
    rag_policy = rag_policy if isinstance(rag_policy, dict) else {}
    pressure_policy = pressure_policy if isinstance(pressure_policy, dict) else {}
    cleanup_policy = pressure_policy.get("owner_job_end_cleanup") if isinstance(pressure_policy, dict) else {}
    cleanup_policy = cleanup_policy if isinstance(cleanup_policy, dict) else {}
    workers = int(_decision(resolved, "runtime.workers.final", 1))
    batch = int(_decision(resolved, "runtime.batch_size", 16))
    llm_timeout = int(_decision(resolved, "timeouts.llm_request_seconds", 120))
    quality_latency = config["llm"]["quality_latency"]
    gpu_available = bool(runtime.get("gpu_available"))

    cpu_budget = float(config["limits"]["cpu_budget_fraction"])
    memory_budget = float(config["limits"]["memory_budget_fraction"])
    max_cpu_percent = _clamp(round((cpu_budget + 0.25) * 100), 50, 85)
    max_memory_percent = _clamp(round(memory_budget * 100), 45, 85)
    max_parallel_jobs = _clamp(workers * 2, 1, 4)
    graph_parallel_jobs = _clamp(workers, 1, 2)
    parser_workers = 1 if gpu_available else _clamp(workers, 1, 2)
    embedding_lane_concurrency = int(rag_policy.get("embedding_lane_concurrency") or 1)
    source_scan_parallel_jobs = int(rag_policy.get("source_scan_parallel_jobs") or max_parallel_jobs)
    resource_pause_max_seconds = int(rag_policy.get("resource_pause_max_seconds") or 120)
    resource_pause_total_budget_seconds = int(rag_policy.get("resource_pause_total_budget_seconds") or 600)
    resource_retry_max_attempts = int(rag_policy.get("resource_retry_max_attempts") or 12)
    swap_pause_requires_active_pressure = bool(
        rag_policy.get(
            "swap_pause_requires_active_pressure",
            pressure_policy.get("swap_pause_requires_active_pressure", True),
        )
    )
    swap_active_growth_gb = float(
        rag_policy.get("swap_active_growth_gb") or pressure_policy.get("swap_active_growth_gb") or 0.25
    )
    swap_pressure_ram_percent = int(
        rag_policy.get("swap_pressure_ram_percent") or pressure_policy.get("swap_pressure_ram_percent") or max_memory_percent
    )
    job_end_memory_cleanup = bool(
        rag_policy.get("job_end_memory_cleanup", cleanup_policy.get("mode", "owner_process_local") == "owner_process_local")
    )
    job_end_malloc_trim = bool(rag_policy.get("job_end_malloc_trim", cleanup_policy.get("malloc_trim", True)))
    job_end_clear_embedder_cache = bool(
        rag_policy.get("job_end_clear_embedder_cache", cleanup_policy.get("clear_process_caches", True))
    )
    embedding_batch_size = _clamp(batch * 2, 16, 50)
    embedding_batch_max_chars = embedding_batch_size * 1200
    chunks_queue_max = max(64, embedding_batch_size)
    files_queue_max = max(128, embedding_batch_size * 2)
    manifest_batch_size = _clamp(embedding_batch_size, 20, 100)
    community_max_workers = _clamp(workers + 1, 1, 3)

    graph_base = 1800 if quality_latency == "fast" else 3600
    graph_timeout = min(7200, graph_base * max(1, math.ceil(4 / graph_parallel_jobs)))
    enrich_timeout = _clamp(llm_timeout * 3, 180, 300)
    query_timeout = _clamp(math.ceil(llm_timeout / 4), 30, 60)
    router_timeout = _clamp(math.ceil(llm_timeout / 8), 8, 30)

    return [
        RagRuntimeValue(
            env="RAG_PERFORMANCE_MAX_CPU_PERCENT",
            value=str(max_cpu_percent),
            origin="inferred",
            reason="RAG CPU ceiling follows the global CPU budget with headroom for parsing and embedding bursts.",
            formula="clamp(round((limits.cpu_budget_fraction + 0.25) * 100), 50, 85)",
            override="RAG_PERFORMANCE_MAX_CPU_PERCENT",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_MAX_MEMORY_PERCENT",
            value=str(max_memory_percent),
            origin="inferred",
            reason="RAG memory ceiling follows the global memory budget/profile.",
            formula="clamp(round(limits.memory_budget_fraction * 100), 45, 85)",
            override="RAG_PERFORMANCE_MAX_MEMORY_PERCENT",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_MAX_PARALLEL_JOBS",
            value=str(max_parallel_jobs),
            origin="inferred",
            reason="Source scan/parse child job parallelism is bounded by resolved runtime workers.",
            formula="clamp(runtime.workers.final * 2, 1, 4)",
            override="RAG_PERFORMANCE_MAX_PARALLEL_JOBS",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_SOURCE_SCAN_PARALLEL_JOBS",
            value=str(source_scan_parallel_jobs),
            origin="inferred",
            reason="Repo/document source child jobs may scan/parse in parallel, while embedding/write lanes stay separate.",
            formula="RAG_PERFORMANCE_MAX_PARALLEL_JOBS",
            override="RAG_PERFORMANCE_SOURCE_SCAN_PARALLEL_JOBS",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_GRAPH_PARALLEL_JOBS",
            value=str(graph_parallel_jobs),
            origin="inferred",
            reason="Graph jobs are kept below the general pipeline cap because each job may call local LLMs.",
            formula="clamp(runtime.workers.final, 1, 2)",
            override="RAG_PERFORMANCE_GRAPH_PARALLEL_JOBS",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_PARSER_WORKERS",
            value=str(parser_workers),
            origin="inferred",
            reason="Parser workers stay conservative on GPU hosts to leave CPU/RAM headroom for model serving.",
            formula="1 if gpu_available else clamp(runtime.workers.final, 1, 2)",
            override="RAG_PERFORMANCE_PARSER_WORKERS",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_EMBEDDING_BATCH_SIZE",
            value=str(embedding_batch_size),
            origin="inferred",
            reason="Embedding batch size scales from the RAM-aware global batch decision while preserving Ollama limits.",
            formula="clamp(runtime.batch_size * 2, 16, 50)",
            override="RAG_PERFORMANCE_EMBEDDING_BATCH_SIZE",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_EMBEDDING_BATCH_MAX_CHARS",
            value=str(embedding_batch_max_chars),
            origin="inferred",
            reason="Embedding batch character budget tracks batch size using an average chunk budget.",
            formula="embedding_batch_size * 1200",
            override="RAG_PERFORMANCE_EMBEDDING_BATCH_MAX_CHARS",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_CHUNKS_QUEUE_MAX",
            value=str(chunks_queue_max),
            origin="inferred",
            reason="Chunk queue capacity follows embedding batch size with a safe minimum.",
            formula="max(64, embedding_batch_size)",
            override="RAG_PERFORMANCE_CHUNKS_QUEUE_MAX",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_FILES_QUEUE_MAX",
            value=str(files_queue_max),
            origin="inferred",
            reason="File queue capacity leaves room for at least two embedding batches.",
            formula="max(128, embedding_batch_size * 2)",
            override="RAG_PERFORMANCE_FILES_QUEUE_MAX",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_MANIFEST_BATCH_SIZE",
            value=str(manifest_batch_size),
            origin="inferred",
            reason="Manifest flush batch tracks embedding batches without becoming too chatty.",
            formula="clamp(embedding_batch_size, 20, 100)",
            override="RAG_PERFORMANCE_MANIFEST_BATCH_SIZE",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_EMBEDDING_CONCURRENCY",
            value="1",
            origin="inferred",
            reason="Local Ollama embeddings are serialized to avoid overloading the host model server.",
            formula="1 for local Ollama embedding backend",
            override="RAG_PERFORMANCE_EMBEDDING_CONCURRENCY",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_EMBEDDING_LANE_CONCURRENCY",
            value=str(embedding_lane_concurrency),
            origin="inferred",
            reason="Only one embedding lane may run at a time across child source jobs.",
            formula="1 for owner-safe local RAG embedding lane",
            override="RAG_PERFORMANCE_EMBEDDING_LANE_CONCURRENCY",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_RESOURCE_PAUSE_MAX_SECONDS",
            value=str(resource_pause_max_seconds),
            origin="policy",
            reason="A single Resource Governor pause has a bounded wait before defer/retry.",
            formula="config/resource_governor.yaml rag.resource_pause_max_seconds",
            override="RAG_PERFORMANCE_RESOURCE_PAUSE_MAX_SECONDS",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_RESOURCE_PAUSE_TOTAL_BUDGET_SECONDS",
            value=str(resource_pause_total_budget_seconds),
            origin="policy",
            reason="Total wait budget prevents invisible resource-pressure hangs.",
            formula="config/resource_governor.yaml rag.resource_pause_total_budget_seconds",
            override="RAG_PERFORMANCE_RESOURCE_PAUSE_TOTAL_BUDGET_SECONDS",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_RESOURCE_RETRY_MAX_ATTEMPTS",
            value=str(resource_retry_max_attempts),
            origin="policy",
            reason="Resource-pressure retries are finite and visible in admin job status.",
            formula="config/resource_governor.yaml rag.resource_retry_max_attempts",
            override="RAG_PERFORMANCE_RESOURCE_RETRY_MAX_ATTEMPTS",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_SWAP_PAUSE_REQUIRES_ACTIVE_PRESSURE",
            value=str(swap_pause_requires_active_pressure).lower(),
            origin="policy",
            reason="Residual swap is observed by default and does not pause jobs unless pressure is active.",
            formula="config/resource_governor.yaml pressure_policy.swap_pause_requires_active_pressure",
            override="RAG_PERFORMANCE_SWAP_PAUSE_REQUIRES_ACTIVE_PRESSURE",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_SWAP_ACTIVE_GROWTH_GB",
            value=str(swap_active_growth_gb),
            origin="policy",
            reason="Swap growth over this window is treated as active pressure, not stale swap residency.",
            formula="config/resource_governor.yaml pressure_policy.swap_active_growth_mb / 1024",
            override="RAG_PERFORMANCE_SWAP_ACTIVE_GROWTH_GB",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_SWAP_PRESSURE_RAM_PERCENT",
            value=str(swap_pressure_ram_percent),
            origin="policy",
            reason="High swap only pauses when RAM budget or PSI also indicate real pressure.",
            formula="config/resource_governor.yaml pressure_policy.swap_pressure_ram_percent auto",
            override="RAG_PERFORMANCE_SWAP_PRESSURE_RAM_PERCENT",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_JOB_END_MEMORY_CLEANUP",
            value=str(job_end_memory_cleanup).lower(),
            origin="policy",
            reason="Owners release only their own process-local memory and caches when jobs finish.",
            formula="config/resource_governor.yaml pressure_policy.owner_job_end_cleanup.mode",
            override="RAG_PERFORMANCE_JOB_END_MEMORY_CLEANUP",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_JOB_END_MALLOC_TRIM",
            value=str(job_end_malloc_trim).lower(),
            origin="policy",
            reason="glibc malloc arenas may be returned to the OS after owner jobs finish.",
            formula="config/resource_governor.yaml pressure_policy.owner_job_end_cleanup.malloc_trim",
            override="RAG_PERFORMANCE_JOB_END_MALLOC_TRIM",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_JOB_END_CLEAR_EMBEDDER_CACHE",
            value=str(job_end_clear_embedder_cache).lower(),
            origin="policy",
            reason="Embedding caches are owner-local process caches and may be cleared after reprocess jobs.",
            formula="config/resource_governor.yaml pressure_policy.owner_job_end_cleanup.clear_process_caches",
            override="RAG_PERFORMANCE_JOB_END_CLEAR_EMBEDDER_CACHE",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_EMBEDDING_TIMEOUT",
            value=str(max(120, llm_timeout)),
            origin="inferred",
            reason="Embedding timeout must be at least the normal LLM request timeout.",
            formula="max(120, timeouts.llm_request_seconds)",
            override="RAG_PERFORMANCE_EMBEDDING_TIMEOUT",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_QUERY_TIMEOUT_SECONDS",
            value=str(query_timeout),
            origin="inferred",
            reason="RAG query timeout is shorter than generation timeout but large enough for retrieval/rerank.",
            formula="clamp(ceil(timeouts.llm_request_seconds / 4), 30, 60)",
            override="RAG_PERFORMANCE_QUERY_TIMEOUT_SECONDS",
        ),
        RagRuntimeValue(
            env="RAG_ROUTER_TIMEOUT",
            value=str(float(router_timeout)),
            origin="inferred",
            reason="Router timeout is a small fraction of full generation timeout.",
            formula="clamp(ceil(timeouts.llm_request_seconds / 8), 8, 30)",
            override="RAG_ROUTER_TIMEOUT",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_GRAPH_TIMEOUT",
            value=str(graph_timeout),
            origin="inferred",
            reason="Graph extraction gets a larger watchdog when local LLM graph jobs are serialized.",
            formula="min(7200, graph_base * ceil(4 / graph_parallel_jobs)); graph_base=1800 fast else 3600",
            override="RAG_PERFORMANCE_GRAPH_TIMEOUT",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_PIPELINE_TIMEOUT",
            value=str(graph_timeout),
            origin="inferred",
            reason="Embedding pipeline watchdog follows graph timeout for large local repositories.",
            formula="same as RAG_PERFORMANCE_GRAPH_TIMEOUT",
            override="RAG_PERFORMANCE_PIPELINE_TIMEOUT",
        ),
        RagRuntimeValue(
            env="RAG_PERFORMANCE_ENRICH_TIMEOUT",
            value=str(enrich_timeout),
            origin="inferred",
            reason="Graph enrichment LLM calls need more time than routing but less than whole-pipeline watchdogs.",
            formula="clamp(timeouts.llm_request_seconds * 3, 180, 300)",
            override="RAG_PERFORMANCE_ENRICH_TIMEOUT",
        ),
        RagRuntimeValue(
            env="RAG_GRAPHIFY_MAX_CONCURRENCY",
            value="1",
            origin="inferred",
            reason="Graphify uses local Ollama extraction; concurrency stays serialized unless explicitly overridden.",
            formula="1 for local Ollama graph extraction",
            override="RAG_GRAPHIFY_MAX_CONCURRENCY",
        ),
        RagRuntimeValue(
            env="RAG_GRAPHIFY_COMMUNITY_MAX_WORKERS",
            value=str(community_max_workers),
            origin="inferred",
            reason="Community summarization workers are bounded by runtime workers with one extra slot for IO waits.",
            formula="clamp(runtime.workers.final + 1, 1, 3)",
            override="RAG_GRAPHIFY_COMMUNITY_MAX_WORKERS",
        ),
    ]


def resolve_rag_env(resolved: dict[str, Any]) -> dict[str, str]:
    return {item.env: item.value for item in resolve_rag_runtime(resolved)}
