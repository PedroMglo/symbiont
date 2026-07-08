"""Dispatch agents node — invokes agent services via HTTP.

Replaces the old per-agent nodes, collaborate node, review node, and decompose node.
All agent interactions are now HTTP calls to external services.
"""

from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path

from orchestrator.config import get_settings
from orchestrator.dispatch.agent_client import AgentClient
from orchestrator.dispatch.types import AgentInvokeRequest, AgentInvokeResponse
from orchestrator.pipeline.nodes.conversation_shortcuts import memory_write_ack
from orchestrator.pipeline.state import SymbiontState

log = logging.getLogger(__name__)
_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE: dict[str, str] = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


def _language_metadata_from_state(state: SymbiontState, query: str) -> dict:
    language_context = state.get("language_context", {}) or {}
    original_query = state.get("original_query", query)
    response_language = str(language_context.get("response_language") or "same_as_user")
    return {
        "language_context": language_context,
        "original_query": original_query,
        "working_query": query,
        "working_language": "en",
        "response_language": response_language,
        "internal_contract_language": "en",
    }


def _query_requests_composed_answer(query: str) -> bool:
    lowered = (query or "").lower()
    hints = (
        "entrega final",
        "produce:",
        "deliverable",
        "corrig",
        "mitig",
        "root cause",
        "causa provável",
        "causa provavel",
        "hipóteses",
        "hipoteses",
        "evidência",
        "evidencia",
        "validação",
        "validacao",
        "reconciliation",
        "reconciliação",
        "reconciliacao",
    )
    return any(hint in lowered for hint in hints)


def _should_invoke_agent_with_context(agent_client: AgentClient | None, agents: list[str], query: str) -> bool:
    return agent_client is not None and bool(agents) and _query_requests_composed_answer(query)


def _has_specialist_context(context_blocks: list) -> bool:
    specialist_sources = {"sql", "bash", "data", "incident", "evidence"}
    return any(
        getattr(block, "source", "") in specialist_sources and getattr(block, "content", "")
        for block in context_blocks
    )


def _dispatchable_agent_names() -> set[str]:
    try:
        names = set(get_settings().dispatch.agent_endpoints)
    except Exception:
        names = {"reasoning_and_response", "audio_transcribe"}
    return names or {"reasoning_and_response"}


def _sanitize_dispatch_agents(candidates: list[str]) -> tuple[list[str], list[str]]:
    if not candidates:
        return [], []
    valid_agents = _dispatchable_agent_names()
    selected: list[str] = []
    dropped: list[str] = []
    for candidate in candidates:
        name = str(candidate)
        if name in valid_agents:
            if name not in selected:
                selected.append(name)
        elif name not in dropped:
            dropped.append(name)
    if selected:
        return selected, dropped
    default_agent = "reasoning_and_response" if "reasoning_and_response" in valid_agents else next(iter(valid_agents), "")
    return ([default_agent] if default_agent else []), dropped


def create_dispatch_agents_node(agent_client: AgentClient):
    """Factory that creates the agent dispatch node with injected agent client.

    This single node replaces all individual agent_* nodes. It invokes
    all selected agents in parallel via HTTP.
    """

    def dispatch_agents_node(state: SymbiontState) -> dict:
        """Invoke selected agents in parallel via HTTP."""
        query = state["query"]
        agents = state.get("selected_agents", [])
        context_blocks = state.get("context_blocks", [])
        stream_mode = state.get("stream_mode", False)
        has_evidence_context = any(
            getattr(b, "source", "") == "evidence" and getattr(b, "content", "")
            for b in context_blocks
        )

        # Storage-control requests are already executed while gathering context.
        # Return that result directly so a failing generic responder cannot turn
        # a completed storage action into a generic shell-command suggestion.
        storage_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "storage" and getattr(b, "content", "")
        ]
        if storage_context:
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in storage_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_storage_context",
                    agent_name="storage",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "storage_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:storage_context"],
            }

        extrator_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "extrator" and getattr(b, "content", "")
        ]
        if extrator_context and not has_evidence_context:
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in extrator_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_extrator_context",
                    agent_name="extrator",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "extrator_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:extrator_context"],
            }

        log_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "logs" and getattr(b, "content", "")
        ]
        if log_context and not has_evidence_context:
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in log_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_logs_context",
                    agent_name="logs",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "logs_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:logs_context"],
            }

        git_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "git" and getattr(b, "content", "")
        ]
        if git_context and not has_evidence_context:
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in git_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_git_context",
                    agent_name="git",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "git_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:git_context"],
            }

        compose_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "compose" and getattr(b, "content", "")
        ]
        if compose_context and not has_evidence_context:
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in compose_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_compose_context",
                    agent_name="compose",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "compose_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:compose_context"],
            }

        security_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "security" and getattr(b, "content", "")
        ]
        if security_context and not has_evidence_context:
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in security_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_security_context",
                    agent_name="security",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "security_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:security_context"],
            }

        sql_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "sql" and getattr(b, "content", "")
        ]
        if sql_context and not has_evidence_context and not _should_invoke_agent_with_context(agent_client, agents, query):
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in sql_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_sql_context",
                    agent_name="sql",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "sql_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:sql_context"],
            }

        bash_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "bash" and getattr(b, "content", "")
        ]
        if bash_context and not has_evidence_context:
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in bash_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_bash_context",
                    agent_name="bash",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "bash_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:bash_context"],
            }

        data_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "data" and getattr(b, "content", "")
        ]
        if data_context and not has_evidence_context:
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in data_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_data_context",
                    agent_name="data",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "data_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:data_context"],
            }

        incident_context = [
            b for b in context_blocks
            if getattr(b, "source", "") == "incident" and getattr(b, "content", "")
        ]
        if incident_context and not has_evidence_context:
            context_text = "\n\n".join(f"[{b.source}] {b.content}" for b in incident_context)
            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_incident_context",
                    agent_name="incident",
                    output=context_text,
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                    metadata={"source": "incident_context"},
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": ["dispatch_agents:incident_context"],
            }

        agents, dropped_non_dispatchable = _sanitize_dispatch_agents(list(agents))
        dispatch_filter_trace = (
            [f"dispatch_agents:dropped_non_dispatchable({','.join(dropped_non_dispatchable)})"]
            if dropped_non_dispatchable else []
        )
        if dropped_non_dispatchable:
            log.info(
                "Agent dispatch: dropped non-dispatchable agents=%s keeping=%s",
                dropped_non_dispatchable,
                agents,
            )

        if not agents:
            return {
                "agent_results": [],
                "all_agents_failed": True,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": dispatch_filter_trace + ["dispatch_agents:no_agents"],
            }

        active_agents: list[str] = []
        degraded_results = []
        for agent_name in agents:
            if agent_client is None:
                active_agents.append(agent_name)
                continue
            flag = agent_client.degraded_runtime_flag(agent_name)
            if flag is None:
                active_agents.append(agent_name)
                continue
            agent_client.record_degraded_skip(agent_name, flag=flag, phase="dispatch_filter")
            from orchestrator.types import AgentResult
            degraded_results.append(AgentResult(
                task_id=f"dispatch_{agent_name}_degraded",
                agent_name=agent_name,
                output="",
                success=False,
                confidence=0.0,
                tokens_used=0,
                duration_ms=0,
                metadata={
                    "degraded_by_runtime_flag": True,
                    "runtime_flag": flag,
                    "error": "Agent skipped because a runtime degraded flag is active",
                    "fallback_expected": True,
                },
            ))
        if degraded_results:
            log.info(
                "Agent dispatch: skipped degraded agents=%s active=%s",
                [r.agent_name for r in degraded_results],
                active_agents,
            )
        agents = active_agents
        if not agents:
            return {
                "agent_results": degraded_results,
                "all_agents_failed": True,
                "iterations": state.get("iterations", 0) + 1,
                "execution_trace": dispatch_filter_trace + [
                    "dispatch_agents:runtime_degraded("
                    + ",".join(r.agent_name for r in degraded_results)
                    + ")"
                ],
            }

        # Build shared context from gathered blocks
        context_text = "\n\n".join(
            f"[{b.source}] {b.content}" for b in context_blocks if b.content
        )
        composed_specialist_task = (
            bool(context_text)
            and (
                _query_requests_composed_answer(query)
                or any(getattr(b, "source", "") == "evidence" for b in context_blocks)
            )
            and _has_specialist_context(context_blocks)
        )
        if composed_specialist_task and "reasoning_and_response" in agents:
            agents = ["reasoning_and_response"]

        # Fast-path: when routing selected only reasoning_and_response
        # in stream_mode → bypass container, stream locally via vLLM router.
        _BYPASS_AGENTS = {"reasoning_and_response"}
        if (
            stream_mode
            and set(agents).issubset(_BYPASS_AGENTS)
            and "reasoning_and_response" in agents
            and not context_blocks
        ):
            memory_ack = memory_write_ack(query)
            if memory_ack is not None:
                from orchestrator.types import AgentResult, Complexity
                return {
                    "agent_results": [AgentResult(
                        task_id="dispatch_memory_write_ack",
                        agent_name="reasoning_and_response",
                        output=memory_ack,
                        success=True,
                        confidence=1.0,
                        tokens_used=0,
                        duration_ms=0,
                    )],
                    "all_agents_failed": False,
                    "complexity": Complexity.SIMPLE,
                    "iterations": state.get("iterations", 0) + 1,
                    "execution_trace": dispatch_filter_trace + ["dispatch_agents:memory_write_ack"],
                }

            history = state.get("history", [])
            original_query = state.get("original_query", query)
            language_context = state.get("language_context", {}) or {}
            response_language = str(language_context.get("response_language") or "same_as_user")
            _SYS = _prompt("direct_answer_system.md").format(
                response_language=response_language,
                original_query=original_query,
            )
            messages = [{"role": "system", "content": _SYS}]
            for msg in (history or [])[-4:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in {"user", "assistant", "system"} and content:
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": query})

            from orchestrator.types import AgentResult
            return {
                "agent_results": [AgentResult(
                    task_id="dispatch_reasoning_and_response",
                    agent_name="reasoning_and_response",
                    output="",
                    success=True,
                    confidence=1.0,
                    tokens_used=0,
                    duration_ms=0,
                )],
                "all_agents_failed": False,
                "iterations": state.get("iterations", 0) + 1,
                "stream_messages": messages,
                    "context_blocks": context_blocks,
                    "execution_trace": dispatch_filter_trace + ["dispatch_agents:direct_stream_bypass"],
                }

        # Invoke all agents in parallel
        results: list[AgentInvokeResponse] = []
        base_agent_metadata = _language_metadata_from_state(state, query)

        def _invoke(agent_name: str) -> AgentInvokeResponse:
            timeout_seconds = 90.0 if composed_specialist_task else None
            budget_tokens = 4000 if composed_specialist_task else None
            metadata = dict(base_agent_metadata)
            if composed_specialist_task:
                metadata.update({
                    "composed_specialist_task": True,
                    "llm_output_budget_tokens": budget_tokens,
                    "transport_retries": 0,
                })
            return agent_client.invoke(
                agent_name,
                AgentInvokeRequest(
                    query=query,
                    context={"context": context_text} if context_text else {},
                    budget_tokens=budget_tokens,
                    timeout_seconds=timeout_seconds,
                    history=state.get("history", []),
                    metadata=metadata,
                ),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(agents), 6)) as pool:
            futures = {pool.submit(_invoke, name): name for name in agents}
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    agent_name = futures[future]
                    results.append(AgentInvokeResponse(
                        output="",
                        success=False,
                        agent_name=agent_name,
                        error=str(exc)[:200],
                    ))

        # Convert service responses to the state result format.
        from orchestrator.types import AgentResult
        agent_results = list(degraded_results)
        for resp in results:
            metadata = dict(resp.metadata or {})
            if resp.agent_decision is not None:
                metadata.setdefault("agent_decision", resp.agent_decision)
            if resp.error:
                metadata.setdefault("error", resp.error)
            agent_results.append(AgentResult(
                task_id=f"dispatch_{resp.agent_name}",
                agent_name=resp.agent_name,
                output=resp.output,
                success=resp.success,
                confidence=resp.confidence,
                tokens_used=resp.tokens_used,
                duration_ms=resp.latency_ms,
                metadata=metadata,
            ))

        succeeded = [r.agent_name for r in agent_results if r.success]
        failed = [r.agent_name for r in agent_results if not r.success]

        log.info(
            "Agent dispatch: requested=%d, succeeded=%s, failed=%s",
            len(agents), succeeded, failed,
        )

        # Mark agents as used for prewarm tracking
        try:
            from orchestrator.prewarming import get_prewarm_engine
            pw_engine = get_prewarm_engine()
            if pw_engine is not None:
                session_id = state.get("session_id", "")
                for agent_name in succeeded:
                    pw_engine.mark_used(session_id, agent_name)
        except Exception:
            pass

        all_failed = len(succeeded) == 0 and len(failed) > 0
        if all_failed and context_text and composed_specialist_task:
            agent_results.append(AgentResult(
                task_id="dispatch_context_fallback",
                agent_name="context",
                output=context_text,
                success=True,
                confidence=0.7,
                tokens_used=0,
                duration_ms=0,
                metadata={
                    "source": "context",
                    "fallback_after_agents_failed": True,
                    "failed_agents": failed,
                },
            ))
            succeeded = ["context"]
            all_failed = False
        iterations = state.get("iterations", 0) + 1

        return {
            "agent_results": agent_results,
            "all_agents_failed": all_failed,
            "iterations": iterations,
            "execution_trace": dispatch_filter_trace + [f"dispatch_agents:{','.join(succeeded)}"],
        }

    return dispatch_agents_node


def create_decompose_node(agent_client: AgentClient):
    """Factory: decompose a complex query via reasoning_and_response."""

    def decompose_node(state: SymbiontState) -> dict:
        """Decompose query via HTTP to reasoning_and_response."""
        query = state["query"]
        available, dropped_available = _sanitize_dispatch_agents(agent_client.list_available())

        resp = agent_client.invoke_decomposer(
            query,
            available,
            timeout=60.0,
            metadata=_language_metadata_from_state(state, query),
        )

        if not resp.success:
            log.warning("Decomposition failed: %s — falling back to single agent", resp.error)
            return {
                "selected_agents": available[:2],
                "execution_trace": (
                    [f"decompose:dropped_non_dispatchable->{dropped_available}"] if dropped_available else []
                ) + ["decompose:failed_fallback"],
            }

        # Parse decomposition result — expects subtask format
        import json
        try:
            subtasks = json.loads(resp.output) if isinstance(resp.output, str) else resp.output
            if isinstance(subtasks, list):
                # Extract unique agent names from subtasks
                agents = list({
                    agent
                    for task in subtasks
                    if isinstance(task, dict)
                    for agent in task.get("agents", task.get("assigned_agents", []))
                })
                agents, dropped_agents = _sanitize_dispatch_agents(agents)
                if agents:
                    return {
                        "selected_agents": agents,
                        "execution_plan": subtasks,
                        "execution_trace": (
                            [f"decompose:dropped_non_dispatchable->{dropped_agents}"] if dropped_agents else []
                        ) + [f"decompose:subtasks={len(subtasks)}"],
                    }
        except (json.JSONDecodeError, TypeError):
            pass

        return {
            "selected_agents": available[:2],
            "execution_trace": (
                [f"decompose:dropped_non_dispatchable->{dropped_available}"] if dropped_available else []
            ) + ["decompose:parse_failed_fallback"],
        }

    return decompose_node


def create_collaborate_node(agent_client: AgentClient):
    """Factory: handle agent collaboration (handoff) via HTTP calls."""

    def collaborate_node(state: SymbiontState) -> dict:
        """Process pending handoffs by invoking target agents with peer context."""
        handoffs = state.get("pending_handoffs", [])
        query = state["query"]
        current_round = state.get("collaboration_round", 0)

        if not handoffs:
            return {
                "execution_trace": ["collaborate:no_handoffs"],
            }

        # For each handoff, invoke the target agent with the handoff context
        from orchestrator.types import AgentResult
        new_results: list[AgentResult] = []

        for handoff in handoffs:
            target = handoff.target_agent if hasattr(handoff, "target_agent") else str(handoff)
            reason = handoff.reason if hasattr(handoff, "reason") else ""

            resp = agent_client.invoke(
                target,
                AgentInvokeRequest(
                    query=query,
                    context={"handoff_reason": reason, "collaboration_round": current_round},
                ),
            )

            new_results.append(AgentResult(
                task_id=f"collab_{target}_r{current_round}",
                agent_name=target,
                output=resp.output,
                success=resp.success,
                confidence=resp.confidence,
                tokens_used=resp.tokens_used,
                duration_ms=resp.latency_ms,
            ))

        return {
            "agent_results": new_results,
            "collaboration_round": current_round + 1,
            "pending_handoffs": [],
            "execution_trace": [f"collaborate:round={current_round + 1}"],
        }

    return collaborate_node


def create_peer_review_node(agent_client: AgentClient):
    """Factory: peer review via HTTP — agents review each other's outputs."""

    def peer_review_node(state: SymbiontState) -> dict:
        """Invoke agents to review each other's outputs."""
        results = state.get("agent_results", [])
        if len(results) < 2:
            return {"execution_trace": ["peer_review:skip_insufficient"]}

        reviews: list[dict] = []
        for i, result in enumerate(results):
            if not result.success:
                continue
            # Ask the critic to review this result
            resp = agent_client.invoke_critic(
                query=state["query"],
                response=result.output,
                timeout=5.0,
                metadata=_language_metadata_from_state(state, state["query"]),
            )
            if resp.success:
                reviews.append({
                    "reviewer": "reasoning_and_response",
                    "reviewed_agent": result.agent_name,
                    "score": resp.confidence,
                    "feedback": resp.output,
                })

        return {
            "peer_reviews": reviews,
            "execution_trace": [f"peer_review:reviewed={len(reviews)}"],
        }

    return peer_review_node
