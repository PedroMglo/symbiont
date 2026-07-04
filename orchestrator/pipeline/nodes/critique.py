"""Critique node - evaluates response quality via reasoning_and_response."""

from __future__ import annotations

import logging

from orchestrator.dispatch.agent_client import AgentClient
from orchestrator.pipeline.state import SymbiontState
from orchestrator.types import Complexity

log = logging.getLogger(__name__)


def create_critique_node(agent_client: AgentClient):
    """Factory that creates the critique node with injected agent client.

    Invokes the critique mode of the reasoning_and_response service via HTTP.
    """

    def critique_node(state: SymbiontState) -> dict:
        """Evaluate agent results via the reasoning_and_response HTTP service."""
        query = state["query"]
        results = state.get("agent_results", [])

        if not results:
            return {
                "critique_score": 1.0,
                "critique_acceptable": True,
                "critique_issues": [],
                "execution_trace": ["critique:no_results_skip"],
            }

        # Combine results for evaluation
        combined = "\n\n".join(
            f"[{r.agent_name}] {r.output}" for r in results if r.success and r.output
        )

        if not combined:
            return {
                "critique_score": 0.0,
                "critique_acceptable": False,
                "critique_issues": ["No successful agent outputs to evaluate"],
                "execution_trace": ["critique:empty_outputs"],
            }

        resp = agent_client.invoke_critic(
            query=query,
            response=combined,
            timeout=10.0,
            metadata={
                "language_context": state.get("language_context", {}) or {},
                "original_query": state.get("original_query", query),
                "working_query": query,
                "working_language": "en",
                "response_language": (state.get("language_context", {}) or {}).get("response_language", "same_as_user"),
                "internal_contract_language": "en",
            },
        )

        if not resp.success:
            # If critic is unavailable, pass through (don't block)
            log.warning("Critique service unavailable: %s — passing through", resp.error)
            return {
                "critique_score": 0.7,
                "critique_acceptable": True,
                "critique_issues": [],
                "execution_trace": ["critique:service_unavailable_pass"],
            }

        # Parse critic response
        score = resp.confidence
        issues: list[str] = []
        if resp.metadata.get("issues"):
            issues = resp.metadata["issues"]

        acceptable = score >= 0.5

        return {
            "critique_score": score,
            "critique_acceptable": acceptable,
            "critique_issues": issues,
            "execution_trace": [f"critique:score={score:.2f},acceptable={acceptable}"],
        }

    return critique_node


def should_critique(state: SymbiontState) -> str:
    """Conditional edge: decide whether to invoke the critic.

    Skips critique for SIMPLE queries and for stream bypass (no text to evaluate).
    """
    # If dispatch_agents already prepared stream_messages (bypass), skip critique
    if state.get("stream_messages"):
        return "synthesize"
    complexity = state.get("complexity", Complexity.NORMAL)
    if complexity == Complexity.SIMPLE:
        return "synthesize"
    return "critic"


def after_critique(state: SymbiontState) -> str:
    """Conditional edge after critique: retry or proceed to synthesis."""
    acceptable = state.get("critique_acceptable", True)
    iterations = state.get("iterations", 0)

    # Hard cap: never retry more than once, and never if agents all failed
    if not acceptable and iterations < 2:
        escalation = state.get("escalation_count", 0)
        all_failed = state.get("all_agents_failed", False)
        if escalation < 1 and not all_failed:
            return "gather"  # Retry with potentially better model
    return "synthesize"
