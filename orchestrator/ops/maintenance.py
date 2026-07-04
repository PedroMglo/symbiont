"""Read-only autonomous maintenance playbooks for the agentic runtime."""

from __future__ import annotations

import time
from typing import Any

from orchestrator.agentic.models import AgenticTask, ApprovalStatus, TaskStatus
from orchestrator.agentic.store import AgenticStore

SAFE_MAINTENANCE_PLAYBOOKS = frozenset({
    "health_report",
    "agent_failure_diagnostic",
    "rag_miss_diagnostic",
    "governed_improvement_review",
})


def is_safe_maintenance_task(task: AgenticTask) -> bool:
    metadata = task.metadata or {}
    playbook = str(metadata.get("maintenance_playbook") or "")
    return bool(metadata.get("safe_maintenance") and playbook in SAFE_MAINTENANCE_PLAYBOOKS)


def run_safe_maintenance(task: AgenticTask, store: AgenticStore) -> dict[str, Any]:
    """Run a deterministic read-only maintenance playbook.

    These playbooks deliberately avoid service restarts, writes, broad reindex,
    config mutation, shell execution and any destructive operation. They produce
    ledger-backed diagnostics that can be reviewed or used by later governed
    phases.
    """

    playbook = str((task.metadata or {}).get("maintenance_playbook") or "")
    if playbook == "health_report":
        return _health_report(task, store)
    if playbook == "agent_failure_diagnostic":
        return _agent_failure_diagnostic(task, store)
    if playbook == "rag_miss_diagnostic":
        return _rag_miss_diagnostic(task, store)
    if playbook == "governed_improvement_review":
        return _governed_improvement_review(task, store)
    raise ValueError(f"Unsupported safe maintenance playbook: {playbook}")


def _health_report(task: AgenticTask, store: AgenticStore) -> dict[str, Any]:
    window_seconds = int((task.metadata or {}).get("window_seconds") or 900)
    since = time.time() - max(1, window_seconds)
    agent_failures = _agent_failures(store, since=since)
    rag_misses = store.count_events(event_type="rag.miss", since=since)
    runtime_flags = store.list_runtime_flags()
    pending_approvals = store.list_approvals(status=ApprovalStatus.PENDING.value, limit=500)
    status_counts = store.task_status_counts()
    risks: list[str] = []
    if pending_approvals:
        risks.append("pending_approvals")
    if agent_failures:
        risks.append("agent_failures")
    if rag_misses:
        risks.append("rag_misses")
    if any(flag.get("key") == "block_heavy_tasks" for flag in runtime_flags):
        risks.append("heavy_tasks_blocked")

    return {
        "playbook": "health_report",
        "status": "completed",
        "read_only": True,
        "safe_actions": ["ledger.status_counts", "ledger.runtime_flags", "ledger.recent_events"],
        "window_seconds": window_seconds,
        "task_status_counts": status_counts,
        "queue_depth": status_counts.get(TaskStatus.QUEUED.value, 0) + status_counts.get(TaskStatus.RECOVERING.value, 0),
        "active_task_ids": store.active_task_ids(),
        "pending_approval_count": len(pending_approvals),
        "runtime_flags": runtime_flags,
        "agent_failures": agent_failures,
        "rag_misses": rag_misses,
        "risk_markers": risks,
        "recommendations": _recommendations(agent_failures=agent_failures, rag_misses=rag_misses, runtime_flags=runtime_flags),
        "improvement_candidates": _improvement_candidates(
            source_playbook="health_report",
            agent_failures=agent_failures,
            rag_misses=rag_misses,
            runtime_flags=runtime_flags,
            window_seconds=window_seconds,
        ),
    }


def _agent_failure_diagnostic(task: AgenticTask, store: AgenticStore) -> dict[str, Any]:
    window_seconds = int((task.metadata or {}).get("agent_failure_window_seconds") or 900)
    since = time.time() - max(1, window_seconds)
    failures = _agent_failures(store, since=since)
    return {
        "playbook": "agent_failure_diagnostic",
        "status": "completed",
        "read_only": True,
        "safe_actions": ["ledger.agent_failure_events"],
        "window_seconds": window_seconds,
        "agent_failures": failures,
        "recommendations": _recommendations(agent_failures=failures, rag_misses=0, runtime_flags=[]),
        "improvement_candidates": _improvement_candidates(
            source_playbook="agent_failure_diagnostic",
            agent_failures=failures,
            rag_misses=0,
            runtime_flags=[],
            window_seconds=window_seconds,
        ),
    }


def _rag_miss_diagnostic(task: AgenticTask, store: AgenticStore) -> dict[str, Any]:
    window_seconds = int((task.metadata or {}).get("rag_miss_window_seconds") or 900)
    since = time.time() - max(1, window_seconds)
    misses = store.count_events(event_type="rag.miss", since=since)
    return {
        "playbook": "rag_miss_diagnostic",
        "status": "completed",
        "read_only": True,
        "safe_actions": ["ledger.rag_miss_events"],
        "window_seconds": window_seconds,
        "rag_misses": misses,
        "recommendations": _recommendations(agent_failures={}, rag_misses=misses, runtime_flags=[]),
        "improvement_candidates": _improvement_candidates(
            source_playbook="rag_miss_diagnostic",
            agent_failures={},
            rag_misses=misses,
            runtime_flags=[],
            window_seconds=window_seconds,
        ),
    }


def _governed_improvement_review(task: AgenticTask, store: AgenticStore) -> dict[str, Any]:
    window_seconds = int((task.metadata or {}).get("improvement_review_window_seconds") or 1800)
    since = time.time() - max(1, window_seconds)
    agent_failures = _agent_failures(store, since=since)
    rag_misses = store.count_events(event_type="rag.miss", since=since)
    runtime_flags = store.list_runtime_flags()
    candidates = _improvement_candidates(
        source_playbook="governed_improvement_review",
        agent_failures=agent_failures,
        rag_misses=rag_misses,
        runtime_flags=runtime_flags,
        window_seconds=window_seconds,
    )
    return {
        "playbook": "governed_improvement_review",
        "status": "completed",
        "read_only": True,
        "safe_actions": ["ledger.recent_events", "ledger.runtime_flags", "proposal.synthesis"],
        "window_seconds": window_seconds,
        "agent_failures": agent_failures,
        "rag_misses": rag_misses,
        "runtime_flags": runtime_flags,
        "improvement_candidates": candidates,
        "recommendations": [
            {
                "kind": "governed_self_improvement",
                "risk": "high",
                "action": "review_improvement_candidates",
                "requires_approval": True,
                "note": "Candidates are proposals only; application requires immutable approval.",
            }
        ] if candidates else [],
    }


def _agent_failures(store: AgenticStore, *, since: float) -> dict[str, Any]:
    events = [
        event
        for event in store.list_events(event_type="agent.invoke.failed", limit=1000)
        if float(event.get("timestamp") or 0) >= since
    ]
    agents: dict[str, int] = {}
    examples: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.get("payload") or {}
        agent_name = str(payload.get("agent_name") or "unknown")
        agents[agent_name] = agents.get(agent_name, 0) + 1
        examples.setdefault(agent_name, payload)
    return {
        "agents": agents,
        "examples": examples,
        "total_failures": sum(agents.values()),
    } if agents else {}


def _recommendations(
    *,
    agent_failures: dict[str, Any],
    rag_misses: int,
    runtime_flags: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    if agent_failures:
        recommendations.append({
            "kind": "agent_failure_review",
            "risk": "low",
            "action": "inspect_agent_logs_and_health",
            "requires_approval": False,
            "note": "Read-only diagnostics only; do not restart services automatically.",
        })
    if rag_misses:
        recommendations.append({
            "kind": "rag_retrieval_review",
            "risk": "low",
            "action": "inspect_retrieval_debug",
            "requires_approval": False,
            "note": "Broad reprocess remains high-risk and requires explicit approval.",
        })
    if any(flag.get("key") == "block_heavy_tasks" for flag in runtime_flags):
        recommendations.append({
            "kind": "resource_guardrail",
            "risk": "low",
            "action": "keep_heavy_tasks_deferred",
            "requires_approval": False,
            "note": "Runtime flag is already enforcing a safe defer policy.",
        })
    return recommendations


def _improvement_candidates(
    *,
    source_playbook: str,
    agent_failures: dict[str, Any],
    rag_misses: int,
    runtime_flags: list[dict[str, Any]],
    window_seconds: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    del runtime_flags
    ttl_seconds = min(max(int(window_seconds), 300), 3600)
    agents = agent_failures.get("agents") or {}
    examples = agent_failures.get("examples") or {}
    for agent_name, count in sorted(agents.items()):
        if int(count) < 3:
            continue
        candidates.append({
            "kind": "runtime_routing_guardrail",
            "title": f"Temporarily mark agent {agent_name} as degraded",
            "risk_level": "high",
            "confidence": min(0.95, 0.55 + (int(count) * 0.08)),
            "score": float(count),
            "payload": {
                "operation": "set_runtime_flag",
                "key": f"service_degraded:{agent_name}",
                "ttl_seconds": ttl_seconds,
                "value": {
                    "reason": "repeated_agent_failure",
                    "agent": agent_name,
                    "failures": int(count),
                    "safe_action": "deprioritize_or_skip_for_ttl",
                    "source": "agentic.governed_improvement",
                },
            },
            "evidence": {
                "source_playbook": source_playbook,
                "window_seconds": window_seconds,
                "failure_count": int(count),
                "example": examples.get(agent_name, {}),
            },
        })
    if int(rag_misses) >= 3:
        candidates.append({
            "kind": "retrieval_guardrail",
            "title": "Temporarily mark RAG retrieval as degraded after repeated misses",
            "risk_level": "high",
            "confidence": min(0.9, 0.5 + (int(rag_misses) * 0.06)),
            "score": float(rag_misses),
            "payload": {
                "operation": "set_runtime_flag",
                "key": "rag_retrieval_degraded",
                "ttl_seconds": ttl_seconds,
                "value": {
                    "reason": "repeated_rag_miss",
                    "misses": int(rag_misses),
                    "safe_action": "prefer_retrieval_debug_and_lower_confidence",
                    "source": "agentic.governed_improvement",
                },
            },
            "evidence": {
                "source_playbook": source_playbook,
                "window_seconds": window_seconds,
                "rag_misses": int(rag_misses),
            },
        })
    return candidates
