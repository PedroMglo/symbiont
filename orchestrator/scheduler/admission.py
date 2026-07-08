"""RoutePlan builders for gateway admission checks."""

from __future__ import annotations

from orchestrator.scheduler.leases import RoutePlan


def interactive_chat_plan(*, session_id: str | None = None, max_tokens: int = 512) -> RoutePlan:
    return RoutePlan(
        route="reasoning_and_response",
        owner="agents/reasoning_and_response",
        lane="interactive_chat",
        requires_gpu=True,
        requires_rag=False,
        max_latency_s=60,
        max_tokens=max_tokens,
        can_degrade=True,
        evidence_required=False,
        session_id=session_id,
    )
