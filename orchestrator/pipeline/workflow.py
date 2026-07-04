"""Symbiont workflow — builds and compiles the LangGraph StateGraph.

This is the decoupled version: all agent and context provider interactions
happen via HTTP through the dispatch layer. No in-process agent instantiation.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from orchestrator.config import (
    CollaborationConfig,
    DynamicRoutingConfig,
    IntelligentPipelineConfig,
    PipelineConfig,
)
from orchestrator.dispatch.agent_client import AgentClient
from orchestrator.dispatch.feature_client import FeatureClient
from orchestrator.pipeline.nodes.classify import classify_node
from orchestrator.pipeline.nodes.critique import after_critique, create_critique_node, should_critique
from orchestrator.pipeline.nodes.direct import create_direct_respond_node
from orchestrator.pipeline.nodes.dispatch_agents import (
    create_collaborate_node,
    create_decompose_node,
    create_dispatch_agents_node,
    create_peer_review_node,
)
from orchestrator.pipeline.nodes.dispatch_context import create_dispatch_context_node
from orchestrator.pipeline.nodes.filter_agents import create_filter_agents_node
from orchestrator.pipeline.nodes.learn import create_learn_node
from orchestrator.pipeline.nodes.llm_fallback import create_llm_fallback_node
from orchestrator.pipeline.nodes.route import route_decision, route_node
from orchestrator.pipeline.nodes.synthesize import create_synthesize_node
from orchestrator.pipeline.state import SymbiontState
from orchestrator.types import Complexity

log = logging.getLogger(__name__)


def build_workflow(
    *,
    llm_adapter: Any,
    agent_client: AgentClient,
    feature_client: FeatureClient,
    routing_log: Any = None,
    pattern_store: Any = None,
    collaboration_config: CollaborationConfig | None = None,
    pipeline_config: PipelineConfig | None = None,
    dynamic_routing_config: DynamicRoutingConfig | None = None,
    intelligent_config: IntelligentPipelineConfig | None = None,
) -> Any:
    """Build and compile the symbiont StateGraph.

    This is the decoupled version: no in-process agents or context providers.
    All external calls go through agent_client and feature_client.

    Args:
        llm_adapter: SymbiontChatModel wrapping the LLM client.
        agent_client: HTTP client for invoking agent services.
        feature_client: HTTP client for querying feature services.
        routing_log: Optional RoutingDecisionLog for learning.
        pattern_store: Optional PatternStore for few-shot injection.
        collaboration_config: Config for agent collaboration.
        pipeline_config: Config for pipeline parallelism.
        dynamic_routing_config: Config for dynamic routing.
        intelligent_config: Config for intelligent pipeline features.

    Returns:
        Compiled LangGraph graph ready for .invoke() / .stream().
    """
    if collaboration_config is None:
        collaboration_config = CollaborationConfig()
    if pipeline_config is None:
        pipeline_config = PipelineConfig()
    if dynamic_routing_config is None:
        dynamic_routing_config = DynamicRoutingConfig()
    if intelligent_config is None:
        intelligent_config = IntelligentPipelineConfig()

    # Create node functions with injected dependencies
    llm_fallback = create_llm_fallback_node(llm_adapter)
    direct_respond = create_direct_respond_node(llm_adapter)
    dispatch_context = create_dispatch_context_node(feature_client)
    dispatch_agents = create_dispatch_agents_node(agent_client)
    critique = create_critique_node(agent_client)
    synthesize = create_synthesize_node(llm_adapter, intelligent_config=intelligent_config)
    learn = create_learn_node(routing_log, pattern_store)
    collaborate = create_collaborate_node(agent_client)
    decompose = create_decompose_node(agent_client)
    peer_review = create_peer_review_node(agent_client)
    filter_agents = create_filter_agents_node(intelligent_config)

    # Determine if dynamic routing is active
    dynamic_mode = dynamic_routing_config.mode in ("dynamic", "ab_test")

    # -----------------------------------------------------------------------
    # Build the graph
    # -----------------------------------------------------------------------
    workflow = StateGraph(SymbiontState)

    # --- Core nodes ---
    workflow.add_node("classify", classify_node)
    workflow.add_node("route", route_node)
    workflow.add_node("llm_fallback", llm_fallback)
    workflow.add_node("direct_respond", direct_respond)

    # --- Dispatch nodes (HTTP-based) ---
    workflow.add_node("dispatch_context", dispatch_context)
    workflow.add_node("dispatch_agents", dispatch_agents)

    # --- Post-processing nodes ---
    workflow.add_node("collaborate", collaborate)
    workflow.add_node("critic", critique)
    workflow.add_node("synthesize", synthesize)
    workflow.add_node("learn", learn)

    # --- Optional nodes ---
    if intelligent_config.dead_path_elimination_enabled:
        workflow.add_node("filter_agents", filter_agents)

    if intelligent_config.early_termination_enabled:
        workflow.add_node("early_terminate", _early_terminate_node)

    if dynamic_mode:
        workflow.add_node("decompose", decompose)
        workflow.add_node("peer_review", peer_review)

    # -----------------------------------------------------------------------
    # Edges
    # -----------------------------------------------------------------------

    # Entry point: classify
    workflow.set_entry_point("classify")

    # classify -> conditional: route_decision
    _route_map: dict[str, str] = {
        "direct_respond": "direct_respond",
        "llm_fallback": "llm_fallback",
        "gather": "route",
    }
    if dynamic_mode:
        _route_map["decompose"] = "decompose"
    workflow.add_conditional_edges("classify", route_decision, _route_map)

    # direct_respond -> learn -> END
    workflow.add_edge("direct_respond", "learn")
    workflow.add_edge("learn", END)

    # llm_fallback -> route -> dispatch_context
    workflow.add_edge("llm_fallback", "route")

    # route -> dispatch_context
    workflow.add_edge("route", "dispatch_context")

    # v1.5: decompose -> route (sets selected_agents then gathers context)
    if dynamic_mode:
        workflow.add_edge("decompose", "route")

    # dispatch_context -> filter_agents (if enabled) -> dispatch_agents
    if intelligent_config.dead_path_elimination_enabled:
        workflow.add_edge("dispatch_context", "filter_agents")
        workflow.add_edge("filter_agents", "dispatch_agents")
    else:
        workflow.add_edge("dispatch_context", "dispatch_agents")

    # dispatch_agents -> conditional: collaborate/peer_review/early_terminate/critic/synthesize
    _post_agents_map: dict[str, str] = {
        "collaborate": "collaborate",
        "critic": "critic",
        "synthesize": "synthesize",
    }
    if dynamic_mode:
        _post_agents_map["peer_review"] = "peer_review"
    if intelligent_config.early_termination_enabled:
        _post_agents_map["early_terminate"] = "early_terminate"
    workflow.add_conditional_edges(
        "dispatch_agents",
        _should_collaborate_or_review(collaboration_config, dynamic_routing_config, intelligent_config),
        _post_agents_map,
    )

    # collaborate -> conditional: should_critique
    workflow.add_conditional_edges(
        "collaborate",
        should_critique,
        {"critic": "critic", "synthesize": "synthesize"},
    )

    # peer_review -> critic or synthesize
    if dynamic_mode:
        workflow.add_conditional_edges(
            "peer_review",
            should_critique,
            {"critic": "critic", "synthesize": "synthesize"},
        )

    # early_terminate -> synthesize
    if intelligent_config.early_termination_enabled:
        workflow.add_edge("early_terminate", "synthesize")

    # critic -> conditional: after_critique
    workflow.add_conditional_edges(
        "critic",
        after_critique,
        {"synthesize": "synthesize", "gather": "route"},
    )

    # synthesize -> learn -> END
    workflow.add_edge("synthesize", "learn")

    # -----------------------------------------------------------------------
    # Compile
    # -----------------------------------------------------------------------
    compiled = workflow.compile()
    log.info(
        "Workflow compiled: collaboration=%s, dynamic=%s, intelligent=%s",
        collaboration_config.enabled,
        dynamic_mode,
        intelligent_config.smart_retry_enabled
        or intelligent_config.early_termination_enabled
        or intelligent_config.dead_path_elimination_enabled,
    )
    return compiled


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _early_terminate_node(state: SymbiontState) -> dict:
    """v1.6: Set early termination flag, skipping critic/collaboration."""
    return {
        "early_terminated": True,
        "execution_trace": ["early_terminate:high_confidence"],
    }


def _should_collaborate_or_review(
    collab_config: CollaborationConfig,
    dyn_config: DynamicRoutingConfig,
    intelligent_config: IntelligentPipelineConfig | None = None,
):
    """Conditional edge: early_terminate, collaborate, peer_review, critic, or synthesize."""
    def check(state: SymbiontState) -> str:
        # SAFETY: If all agents failed or max iterations reached, go straight to synthesize
        if state.get("all_agents_failed", False):
            log.warning("All agents failed — falling back to synthesize without context")
            return "synthesize"
        if state.get("iterations", 0) >= 3:
            log.warning("Max iterations (%d) reached — forcing synthesize", state.get("iterations", 0))
            return "synthesize"

        # v1.6: Early termination for high-confidence SIMPLE results
        if intelligent_config and intelligent_config.early_termination_enabled:
            complexity = state.get("complexity", Complexity.NORMAL)
            if complexity == Complexity.SIMPLE:
                results = state.get("agent_results", [])
                threshold = intelligent_config.early_termination_confidence
                if any(r.confidence >= threshold for r in results if r.success):
                    return "early_terminate"

        # Check collaboration
        if collab_config.enabled:
            current_round = state.get("collaboration_round", 0)
            if current_round < collab_config.max_rounds:
                results = state.get("agent_results", [])
                has_handoff = any(
                    getattr(r, "suggested_handoff", None)
                    for r in results if r.success
                )
                if has_handoff:
                    return "collaborate"

        # v1.5: peer review for COMPLEX+ in dynamic mode
        if dyn_config.peer_review_enabled and dyn_config.mode in ("dynamic", "ab_test"):
            complexity = state.get("complexity", Complexity.NORMAL)
            if complexity in (Complexity.COMPLEX, Complexity.DEEP):
                results = state.get("agent_results", [])
                if len(results) >= 2:
                    return "peer_review"

        return should_critique(state)
    return check
