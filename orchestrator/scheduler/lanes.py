"""Declarative scheduler lanes used by Resource Governor leases."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LaneDefinition:
    name: str
    priority: int
    governor_lane: str
    resource_class: str
    capability: str
    preemptible: bool
    requires_llm: bool = False


DEFAULT_LANES: dict[str, LaneDefinition] = {
    "system_status_fast": LaneDefinition(
        "system_status_fast", 100, "system_status_fast", "cpu", "material_orchestration", False, False
    ),
    "interactive_chat": LaneDefinition(
        "interactive_chat", 90, "interactive", "model_runtime", "chat_stream", False, True
    ),
    "audio_gpu": LaneDefinition("audio_gpu", 85, "heavy_gpu", "vram", "audio_transcribe_gpu", False, False),
    "rag_query": LaneDefinition("rag_query", 75, "interactive_enrichment", "model_runtime", "rag_query", True, True),
    "rerank": LaneDefinition("rerank", 60, "interactive_enrichment", "model_runtime", "rerank", True, True),
    "embedding_batch": LaneDefinition("embedding_batch", 45, "background", "vram", "embedding_gpu_batch", True),
    "graphify_background": LaneDefinition("graphify_background", 30, "background", "model_runtime", "graph_llm", True, True),
    "prewarm_gpu": LaneDefinition("prewarm_gpu", 10, "heavy_gpu", "model_runtime", "model_warmup", True, True),
    "workspace_execution": LaneDefinition(
        "workspace_execution", 55, "interactive_enrichment", "cpu", "material_orchestration", True, False
    ),
    "io_write": LaneDefinition("io_write", 20, "storage", "io_write", "storage_archive", True, False),
}


def lane_definition(name: str) -> LaneDefinition:
    try:
        return DEFAULT_LANES[name]
    except KeyError as exc:
        raise ValueError(f"unknown scheduler lane: {name}") from exc
