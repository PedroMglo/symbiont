"""Central service port and endpoint registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SECURE_SCHEME = "https"

SERVICE_OFFSETS: dict[str, int] = {
    "symbiont": 585,
    "rag": 484,
    "llama_cpp_aux": 90,
    "llama_cpp_fast": 91,
    "vllm": 92,
    "qdrant_http": 833,
    "qdrant_grpc": 834,
    "clickhouse_http": 123,
    "clickhouse_native": 1000,
    "grafana": -5000,
    "langfuse": -4999,
    "otel_grpc": -3683,
    "otel_http": -3682,
    "otel_metrics": 888,
    "translation": 590,
    "storage_guardian": 730
}

STABLE_PORTS: dict[str, int] = {
    "symbiont": 8586,
    "rag": 8484,
    "llama_cpp_aux": 8090,
    "llama_cpp_fast": 8091,
    "vllm": 8092,
    "qdrant_http": 16336,
    "qdrant_grpc": 16337,
    "clickhouse_http": 8123,
    "clickhouse_native": 9000,
    "grafana": 3000,
    "langfuse": 3001,
    "otel_grpc": 4317,
    "otel_http": 4318,
    "otel_metrics": 8888,
    "translation": 8590,
    "storage_guardian": 8730
}


@dataclass(frozen=True)
class ServiceSpec:
    service: str
    env_prefix: str
    host: str
    container_port: int
    kind: Literal["core", "agent", "feature", "heavy", "observability", "llm"]
    published_port_service: str | None = None
    worker_cap: int = 1
    healthcheck_path: str = "/health"
    healthcheck_timeout_seconds: int = 5


SERVICE_SPECS: dict[str, ServiceSpec] = {
    "symbiont": ServiceSpec(
        "symbiont",
        "SYMBIONT",
        "symbiont",
        8585,
        "core",
        "symbiont",
        healthcheck_path="/live",
    ),
    "rag": ServiceSpec("rag", "RAG", "rag", 8484, "core", "rag"),
    "qdrant_http": ServiceSpec("qdrant_http", "QDRANT_HTTP", "qdrant", 6333, "core", "qdrant_http"),
    "qdrant_grpc": ServiceSpec("qdrant_grpc", "QDRANT_GRPC", "qdrant", 6334, "core", "qdrant_grpc"),
    "reasoning_and_response": ServiceSpec(
        "reasoning_and_response",
        "REASONING_AND_RESPONSE",
        "reasoning-and-response",
        8000,
        "agent",
    ),
    "research": ServiceSpec("research", "RESEARCH", "research", 8000, "feature"),
    "personal_context": ServiceSpec("personal_context", "PERSONAL_CONTEXT", "personal-context", 8000, "feature"),
    "local_evidence_operator": ServiceSpec(
        "local_evidence_operator",
        "LOCAL_EVIDENCE_OPERATOR",
        "local-evidence-operator",
        8000,
        "agent",
    ),
    "execution_policy_operator": ServiceSpec(
        "execution_policy_operator",
        "EXECUTION_POLICY_OPERATOR",
        "execution-policy-operator",
        8000,
        "agent",
    ),
    "material_builder": ServiceSpec("material_builder", "MATERIAL_BUILDER", "material-builder", 8000, "agent"),
    "workspace_execution": ServiceSpec(
        "workspace_execution",
        "WORKSPACE_EXECUTION",
        "workspace-execution",
        8000,
        "feature",
    ),
    "material_execution_kernel": ServiceSpec(
        "material_execution_kernel",
        "MATERIAL_EXECUTION_KERNEL",
        "material-execution-kernel",
        8000,
        "feature",
    ),
    "extrator": ServiceSpec("extrator", "EXTRATOR", "extrator", 8000, "feature"),
    "translation": ServiceSpec("translation", "TRANSLATION", "translation", 8590, "feature", "translation"),
    "storage_guardian": ServiceSpec("storage_guardian", "STORAGE_GUARDIAN", "storage-guardian", 8730, "core", "storage_guardian"),
    "audio_transcribe": ServiceSpec("audio_transcribe", "AUDIO_TRANSCRIBE", "audio-transcribe", 8080, "heavy"),
    "audio_streaming": ServiceSpec("audio_streaming", "AUDIO_STREAMING", "audio-streaming", 8087, "heavy"),
    "llama_cpp_aux": ServiceSpec("llama_cpp_aux", "LLAMA_CPP_AUX", "llama-cpp-aux", 8080, "llm", "llama_cpp_aux"),
    "llama_cpp_fast": ServiceSpec("llama_cpp_fast", "LLAMA_CPP_FAST", "llama-cpp-fast", 8080, "llm", "llama_cpp_fast"),
    "vllm": ServiceSpec("vllm", "VLLM", "vllm", 8000, "llm", "vllm"),
    "clickhouse_http": ServiceSpec("clickhouse_http", "CLICKHOUSE_HTTP", "clickhouse", 8123, "observability", "clickhouse_http"),
    "grafana": ServiceSpec("grafana", "GRAFANA", "grafana", 3000, "observability", "grafana"),
    "langfuse": ServiceSpec("langfuse", "LANGFUSE", "langfuse", 3000, "observability", "langfuse"),
    "otel_http": ServiceSpec("otel_http", "OTEL_HTTP", "otel-collector", 4318, "observability", "otel_http"),
    "otel_grpc": ServiceSpec("otel_grpc", "OTEL_GRPC", "otel-collector", 4317, "observability", "otel_grpc"),
    "otel_metrics": ServiceSpec("otel_metrics", "OTEL_METRICS", "otel-collector", 8888, "observability", "otel_metrics"),
}


@dataclass(frozen=True)
class PortDecision:
    service: str
    port: int
    origin: str
    reason: str
    warning: str = ""


@dataclass(frozen=True)
class ServiceEndpointDecision:
    service: str
    env_prefix: str
    host: str
    port: int
    url: str
    workers: int
    healthcheck_path: str
    healthcheck_timeout_seconds: int
    origin: str
    reason: str
    formula: str
    override: str
    warning: str = ""


def resolve_ports(base_port: int, *, preserve_existing: bool = True) -> list[PortDecision]:
    ports: dict[str, int] = {}
    decisions: list[PortDecision] = []
    for service, offset in SERVICE_OFFSETS.items():
        port = STABLE_PORTS[service] if preserve_existing else base_port + offset
        warning = ""
        if port in ports.values():
            warning = f"port conflict on {port}"
        ports[service] = port
        decisions.append(
            PortDecision(
                service=service,
                port=port,
                origin="stable_registry" if preserve_existing else "inferred",
                reason=(
                    "Use the stable published port registry for host-facing endpoints."
                    if preserve_existing
                    else "Derived from base_port + service_offset."
                ),
                warning=warning,
            )
        )
    return decisions


def resolve_service_endpoints(
    base_port: int,
    *,
    runtime_workers: int = 1,
    context: Literal["docker", "host"] = "docker",
    preserve_existing: bool = True,
) -> list[ServiceEndpointDecision]:
    port_map = {decision.service: decision.port for decision in resolve_ports(base_port, preserve_existing=preserve_existing)}
    decisions: list[ServiceEndpointDecision] = []
    for spec in SERVICE_SPECS.values():
        if context == "docker":
            host = spec.host
            port = spec.container_port
            origin = "registry"
            reason = "Docker-internal service URL derived from the central service registry."
            override_prefix = f"ORC_SERVICES_{spec.service.upper()}"
        else:
            host = "127.0.0.1"
            port = port_map.get(spec.published_port_service or spec.service, spec.container_port)
            origin = "registry"
            reason = "Host URL derived from stable published port when one exists."
            override_prefix = f"ORC_SERVICES_{spec.service.upper()}"
        workers = max(1, min(runtime_workers, spec.worker_cap))
        warning = ""
        if context == "host" and spec.published_port_service is None:
            warning = "service is not published to host; host URL is for explicit local dev wiring only"
        decisions.append(
            ServiceEndpointDecision(
                service=spec.service,
                env_prefix=spec.env_prefix,
                host=host,
                port=port,
                url=f"{SECURE_SCHEME}://{host}:{port}",
                workers=workers,
                healthcheck_path=spec.healthcheck_path,
                healthcheck_timeout_seconds=spec.healthcheck_timeout_seconds,
                origin=origin,
                reason=reason,
                formula=f"url={SECURE_SCHEME}://{{host}}:{{port}}; workers=min(runtime.workers.final, service.worker_cap)",
                override=f"{override_prefix}_URL, {override_prefix}_PORT, {override_prefix}_WORKERS",
                warning=warning,
            )
        )
    return decisions


def port_conflicts(decisions: list[PortDecision]) -> dict[int, list[str]]:
    seen: dict[int, list[str]] = {}
    for decision in decisions:
        seen.setdefault(decision.port, []).append(decision.service)
    return {port: services for port, services in seen.items() if len(services) > 1}
