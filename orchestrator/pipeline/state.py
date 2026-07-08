"""Symbiont graph state schema.

Defines the TypedDict used as the shared state across all LangGraph nodes.
Uses Annotated with operator.add for fields that accumulate across parallel nodes.
"""

from __future__ import annotations

import operator
from typing import Annotated

from typing_extensions import TypedDict

from orchestrator.pipeline.planning.collaboration import HandoffRequest, MemoryEntry
from orchestrator.types import AgentResult, Complexity, ContextBlock, Intent


class SymbiontState(TypedDict, total=False):
    """Shared state flowing through the orchestration graph.

    Fields with Annotated[list, operator.add] are merge-friendly:
    parallel nodes can each return a partial list that gets concatenated.
    """

    # --- Input (set once at entry) ---
    query: str
    original_query: str
    history: list[dict[str, str]]
    session_id: str
    language_context: dict
    client_cwd: str
    client_system: dict
    client_files: list[dict]

    # --- Classification (set by classify node) ---
    intent: Intent
    complexity: Complexity
    confidence: float  # 0.0-1.0, below threshold triggers LLM fallback

    # --- Routing (set by route / llm_fallback node) ---
    selected_agents: list[str]
    context_sources: list[str]
    local_evidence_required: bool
    model_used: str
    profile_key: str

    # --- Context gathering (accumulated by parallel context nodes) ---
    context_blocks: Annotated[list[ContextBlock], operator.add]

    # --- Speculative prefetch (v1.2 — Pipeline Parallelism) ---
    speculative_context: Annotated[list[ContextBlock], operator.add]
    speculation_sources: list[str]
    speculation_hit: bool

    # --- Agent execution (accumulated by parallel agent nodes) ---
    agent_results: Annotated[list[AgentResult], operator.add]

    # --- Collaboration (v1.1 — shared working memory + handoffs) ---
    working_memory: Annotated[list[MemoryEntry], operator.add]
    collaboration_round: int
    pending_handoffs: Annotated[list[HandoffRequest], operator.add]

    # --- Task Decomposition (v1.5 — Meta-Symbiont v2) ---
    execution_plan: list[dict]               # SubTask dicts
    parallel_groups: list[list[int]]         # groups of subtask IDs runnable in parallel
    peer_reviews: Annotated[list[dict], operator.add]  # ReviewFeedback dicts

    # --- Critic (set by critic node) ---
    critique_score: float  # 0.0-1.0
    critique_acceptable: bool
    critique_issues: list[str]

    # --- Synthesis (set by synthesize / direct_respond node) ---
    response: str
    agentic_deliberation: dict

    # --- Learning & observability ---
    iterations: int
    tokens_used: int
    fallback_used: bool
    execution_trace: Annotated[list[str], operator.add]
    latency_breakdown: dict[str, float]

    # --- v1.6 — Intelligent Execution Pipeline ---
    escalation_count: int
    escalated_agents: list[str]
    refinement_round: int                   # 0=initial/draft, 1=polish
    early_terminated: bool
    eliminated_agents: list[str]

    # --- v2.1 — Execution Layer ---
    execution_failed: bool
    all_agents_failed: bool

    # --- v2.4 — Streaming through pipeline ---
    stream_mode: bool                       # True = skip final LLM call, store messages for streaming
    stream_messages: list[dict[str, str]]   # Messages prepared for streaming (set by synthesize/direct)
