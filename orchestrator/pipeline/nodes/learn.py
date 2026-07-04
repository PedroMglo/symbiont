"""Learn node — persists routing decisions and emits observability events."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from orchestrator.pipeline.state import SymbiontState

log = logging.getLogger(__name__)


def create_learn_node(routing_log: Any = None, pattern_store: Any = None):
    """Factory that creates the learn node with injected dependencies."""

    def learn_node(state: SymbiontState) -> dict:
        """Persist routing decision for future learning."""
        if routing_log is None:
            return {"execution_trace": ["learn:skipped(no_log)"]}

        try:
            from orchestrator.routing.decision_log import RoutingRecord

            intent = state.get("intent")
            complexity = state.get("complexity")

            record = RoutingRecord(
                request_id=str(uuid.uuid4()),
                query=state.get("query", ""),
                intent=intent.value if intent else "unknown",
                complexity=complexity.value if complexity else "unknown",
                action="invoke",
                agents_planned=state.get("selected_agents", []),
                agents_executed=[
                    r.agent_name for r in state.get("agent_results", [])
                    if r.success
                ],
                total_tokens=state.get("tokens_used", 0),
                success=bool(state.get("response", "").strip()),
                fallback_used=state.get("fallback_used", False),
                critic_score=state.get("critique_score"),
                session_id=state.get("session_id", ""),
            )

            routing_log.record(record)
            log.debug("learn: recorded routing decision")
            return {"execution_trace": ["learn:recorded"]}

        except Exception as e:
            log.warning("learn: failed to record: %s", e)
            return {"execution_trace": [f"learn:error({e})"]}

    return learn_node
