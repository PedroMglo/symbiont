"""Context source requirements inferred from user queries."""

from __future__ import annotations

from orchestrator.capabilities.owner_capabilities import (
    owner_context_sources_for_query,
    owner_evidence_required_for_query,
)
from orchestrator.routing.path_intents import (
    is_code_path_request,
    is_extrator_path_request,
    is_storage_request,
    needs_storage_context,
)


def needs_system_context(query: str) -> bool:
    q = " ".join((query or "").lower().split())
    if not q:
        return False
    terms = (
        "ram",
        "swap",
        "gpu",
        "vram",
        "cpu",
        "containers",
        "container",
        "docker ps",
        "estado real",
        "sistema",
        "memória",
        "memoria",
        "disco",
        "processos",
        "processes",
        "nvidia",
    )
    return any(term in q for term in terms)


def needs_repo_context(query: str) -> bool:
    """Return True when a task asks for repo/code/docs evidence."""
    q = " ".join((query or "").lower().split())
    if not q:
        return False
    terms = (
        ".py",
        "bash",
        "benchmark",
        "benchmarks",
        "codigo",
        "código",
        "code",
        "local_evidence",
        "local-evidence",
        "compose",
        "docker",
        "docs/",
        "endpoint",
        "ficheiro",
        "ficheiros",
        "llm_fallback",
        "makefile",
        "pipeline",
        "repo",
        "repository",
        "router",
        "routing",
        "sandbox",
        "synthesize",
        "teste",
        "testes",
    )
    return any(term in q for term in terms)


def context_sources_for_query(query: str, default_sources: list[str]) -> list[str]:
    """Apply query-derived source requirements to configured router sources."""
    if is_storage_request(query):
        return ["storage"]

    context_sources = list(default_sources)
    for source in owner_context_sources_for_query(query):
        if source not in context_sources:
            context_sources.append(source)
    if is_extrator_path_request(query):
        context_sources = ["extrator"]
    elif is_code_path_request(query) or needs_repo_context(query):
        for source in ("graph", "repo"):
            if source not in context_sources:
                context_sources.append(source)

    if needs_system_context(query) and "system" not in context_sources:
        context_sources.insert(0, "system")
    if needs_storage_context(query) and "storage" not in context_sources:
        context_sources.append("storage")
    return context_sources


def requires_context_gather(query: str) -> bool:
    return (
        is_storage_request(query)
        or is_extrator_path_request(query)
        or needs_system_context(query)
        or owner_evidence_required_for_query(query)
    )


def requires_local_evidence(query: str) -> bool:
    return owner_evidence_required_for_query(query)
