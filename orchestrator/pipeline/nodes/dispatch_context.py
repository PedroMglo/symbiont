"""Dispatch context node — gathers context from feature services via HTTP."""

from __future__ import annotations

import logging

from orchestrator.config import get_settings
from orchestrator.core.context_budget import resolve_budget
from orchestrator.dispatch.feature_client import FeatureClient
from orchestrator.observability.capability_trace import emit_capability_event
from orchestrator.pipeline.language_context import (
    english_query_for_assistance,
    language_context_from_state,
    rag_dual_query_enabled,
)
from orchestrator.pipeline.state import SymbiontState
from orchestrator.types import ContextBlock, make_context_block

log = logging.getLogger(__name__)

_ALIAS_WORKSPACE_CONTEXT_PREFIX = "[Contexto local read-only recolhido pelo alias @"


def _workspace_context_from_query(query: str) -> ContextBlock | None:
    """Extract sanitized local workspace evidence injected by the terminal alias."""
    text = query or ""
    marker_start = text.find(_ALIAS_WORKSPACE_CONTEXT_PREFIX)
    if marker_start < 0:
        return None
    marker_end = text.find("]", marker_start)
    if marker_end < 0:
        return None
    content = text[marker_end + 1 :]
    while content.startswith(("\n", "\r", " ", "\\n", "\\r")):
        if content.startswith("\\n") or content.startswith("\\r"):
            content = content[2:]
        else:
            content = content[1:]
    if "\\n" in content and "\n" not in content[:500]:
        content = content.replace("\\n", "\n")
    content = content.strip()
    if not content:
        return None
    return make_context_block(
        source="workspace",
        content=content,
        token_estimate=max(1, len(content) // 4),
        metadata={"source": "alias_client_workspace", "read_only": True},
        read_only=True,
        visibility="user_visible",
        provider_status="complete",
    )


def create_dispatch_context_node(feature_client: FeatureClient):
    """Factory that creates the context dispatch node with injected feature client.

    This node replaces all individual context_* nodes. It gathers context from
    all requested sources via HTTP calls to feature services.
    """

    def dispatch_context_node(state: SymbiontState) -> dict:
        """Gather context from feature services in parallel via HTTP."""
        query = state["query"]
        original_query = str(state.get("original_query") or "") or query
        sources = state.get("context_sources", [])
        workspace_block = _workspace_context_from_query(original_query)

        if not sources:
            if workspace_block is not None:
                emit_capability_event(
                    "context_dispatch",
                    requested_sources=[],
                    gathered_sources=["workspace"],
                    block_count=1,
                    client_cwd=state.get("client_cwd", ""),
                )
                return {
                    "context_blocks": [workspace_block],
                    "execution_trace": ["dispatch_context:workspace"],
                }
            emit_capability_event(
                "context_dispatch",
                requested_sources=[],
                gathered_sources=[],
                block_count=0,
                client_cwd=state.get("client_cwd", ""),
            )
            return {
                "context_blocks": [],
                "execution_trace": ["dispatch_context:no_sources"],
            }

        # Resolve context budget from profile (set by route_node)
        profile_key = state.get("profile_key", "default")
        budget = resolve_budget(profile_key)
        budget_tokens = budget.max_context_tokens
        agentic_limits = state.get("agentic_limits") or {}
        if isinstance(agentic_limits, dict):
            max_context_tokens = agentic_limits.get("max_context_tokens")
            if isinstance(max_context_tokens, int) and max_context_tokens > 0:
                budget_tokens = min(budget_tokens, max_context_tokens)

        settings = get_settings()
        dispatch_cfg = settings.dispatch
        language_context = language_context_from_state(state)
        translated_query = None
        dual_query_sources: set[str] | None = None
        if rag_dual_query_enabled(settings, language_context, query):
            translated_query = english_query_for_assistance(language_context, query)
            dual_query_sources = {"rag"}

        responses = feature_client.gather_context_parallel(
            sources=sources,
            query=original_query,
            budget_tokens=budget_tokens,
            timeout_per_source=dispatch_cfg.context_timeout_per_source,
            translated_query=translated_query,
            dual_query_sources=dual_query_sources,
            metadata={
                "client_cwd": state.get("client_cwd"),
                "original_query": original_query,
            },
        )

        blocks: list[ContextBlock] = [workspace_block] if workspace_block is not None else []
        sources_used: list[str] = ["workspace"] if workspace_block is not None else []

        for resp in responses:
            emit_capability_event(
                "provider_result",
                source=resp.source,
                success=resp.success,
                latency_ms=round(float(resp.latency_ms or 0.0), 1),
                error=resp.error,
                token_estimate=resp.token_estimate,
                metadata={
                    "operation": resp.metadata.get("operation") if isinstance(resp.metadata, dict) else None,
                    "storage_object_uri": resp.metadata.get("storage_object_uri") if isinstance(resp.metadata, dict) else None,
                    "storage_publish_error": resp.metadata.get("storage_publish_error") if isinstance(resp.metadata, dict) else None,
                    "job_kind": resp.metadata.get("job_kind") if isinstance(resp.metadata, dict) else None,
                    "status": resp.metadata.get("status") if isinstance(resp.metadata, dict) else None,
                },
            )
            if resp.success and resp.content:
                blocks.append(make_context_block(
                    source=resp.source,
                    content=resp.content,
                    token_estimate=resp.token_estimate,
                    metadata=resp.metadata,
                    provider_status="complete",
                ))
                sources_used.append(resp.source)
            elif not resp.success:
                log.debug("Context source %s failed: %s", resp.source, resp.error)

        if state.get("local_evidence_required") and not sources_used:
            missing = ", ".join(str(source) for source in sources) or "local"
            blocks.append(make_context_block(
                source="required_context_missing",
                content=(
                    "Evidencia local obrigatoria indisponivel para esta pergunta interna "
                    f"(fontes pedidas: {missing}). Responde recusando inferencias genericas "
                    "e pede/usa evidencia local antes de afirmar estado, owner ou causa."
                ),
                token_estimate=48,
                metadata={"requested_sources": list(sources), "provider_status": "missing"},
                provider_status="missing",
            ))
            sources_used.append("required_context_missing")

        log.info(
            "Context dispatch: requested=%d, gathered=%d sources=%s",
            len(sources), len(blocks), sources_used,
        )
        emit_capability_event(
            "context_dispatch",
            requested_sources=sources,
            gathered_sources=sources_used,
            block_count=len(blocks),
            profile_key=profile_key,
            budget_tokens=budget_tokens,
            client_cwd=state.get("client_cwd", ""),
        )

        # Mark features as used for prewarm tracking
        try:
            from orchestrator.prewarming import get_prewarm_engine
            pw_engine = get_prewarm_engine()
            if pw_engine is not None:
                session_id = state.get("session_id", "")
                for src in sources_used:
                    pw_engine.mark_used(session_id, src)
        except Exception:
            pass

        return {
            "context_blocks": blocks,
            "execution_trace": [
                f"dispatch_context:{','.join(sources_used) or 'empty'}",
                "i18n:rag_dual_query" if translated_query else "i18n:rag_single_query",
            ],
        }

    return dispatch_context_node
