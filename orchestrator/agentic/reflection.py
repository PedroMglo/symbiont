"""Read-only reflection projection for the agentic cockpit."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

SCHEMA_VERSION = "ai-local.agentic-reflection.v1"

_ROUTE_EVENT_TYPES = {
    "route_decision",
    "routing.decision",
    "capability.route_selected",
    "capability.selected",
    "escalation.route_planned",
}
_QUALITY_EVENT_TYPES = {
    "answer.quality",
    "quality_critic.reviewed",
    "quality.critic.reviewed",
    "agent.critique.recorded",
    "agent.validation.recorded",
}
_POLICY_EVENT_TYPES = {
    "policy.checked",
    "agent.action.policy_checked",
    "agent.parallel_agent.policy_checked",
}
_PRESSURE_EVENT_TYPES = {
    "resource.pressure",
    "resource_governor.pressure",
    "agentic.resource_pressure",
    "event_loop.resource_pressure",
}
_BLOCKING_POLICY_DECISIONS = {"block", "blocked", "deny", "denied", "wait_for_approval", "approval_required"}
_SUCCESS_TOOL_STATUSES = {"allow", "allowed", "completed", "success", "succeeded"}
_FAILED_TOOL_STATUSES = {"failed", "error", "blocked", "deny", "denied", "timeout"}
_PRESSURE_LEASE_DECISIONS = {"deferred", "defer", "denied", "deny", "expired", "throttled"}


def build_agentic_reflection(store: Any, *, limit: int = 100) -> dict[str, Any]:
    """Build a bounded, read-only reflection view from persisted audit data."""

    limit = max(1, min(int(limit), 500))
    tasks = store.list_tasks(limit=limit)
    events = store.list_events(limit=min(limit * 5, 1000))
    traces = [_trace for task in tasks if (_trace := store.trace(str(task["id"])))]
    ai_events = store.list_ai_local_events(limit=min(limit * 5, 1000))
    leases = store.list_resource_leases(limit=limit)

    route_events = [event for event in events if str(event.get("event_type") or "") in _ROUTE_EVENT_TYPES]
    quality_events = [event for event in events if str(event.get("event_type") or "") in _QUALITY_EVENT_TYPES]
    policy_events = [event for event in events if str(event.get("event_type") or "") in _POLICY_EVENT_TYPES]
    pressure_events = [event for event in events if str(event.get("event_type") or "") in _PRESSURE_EVENT_TYPES]
    ledger_rag_misses = [event for event in events if str(event.get("event_type") or "") in {"rag.miss", "rag.query.miss"}]
    normalized_rag_misses = [event for event in ai_events if _ai_event_type(event) in {"rag.miss", "rag.query.miss"}]
    tool_calls = [call for trace in traces for call in trace.get("tool_calls", [])]

    route_quality = _route_quality(route_events)
    answer_quality = _answer_quality(quality_events)
    tool_success = _tool_success(tool_calls)
    policy_blocks = _policy_blocks(policy_events)
    resource_pressure = _resource_pressure(leases, pressure_events)
    rag_misses = _rag_misses(ledger_rag_misses, normalized_rag_misses)
    owner_explanations = _owner_explanations(events, limit=limit)
    learning_signals = _learning_signals(
        route_quality=route_quality,
        answer_quality=answer_quality,
        tool_success=tool_success,
        policy_blocks=policy_blocks,
        resource_pressure=resource_pressure,
        rag_misses=rag_misses,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "generated_at": time.time(),
        "window": {
            "limit": limit,
            "tasks": len(tasks),
            "events": len(events),
            "ai_local_events": len(ai_events),
            "resource_leases": len(leases),
        },
        "evaluation": {
            "route_quality": route_quality,
            "answer_quality": answer_quality,
            "tool_success": tool_success,
            "policy_blocks": policy_blocks,
            "resource_pressure": resource_pressure,
            "rag_misses": rag_misses,
        },
        "learning_loop": {
            "mode": "audit_only",
            "writes_allowed": False,
            "raw_llm_text_allowed": False,
            "candidate_signals": learning_signals,
        },
        "owner_explanations": owner_explanations,
    }


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _evidence_ref(event: dict[str, Any], *, prefix: str = "event") -> str:
    event_id = event.get("id") or event.get("event_id")
    return f"{prefix}:{event_id}" if event_id else prefix


def _ai_event_type(event: dict[str, Any]) -> str:
    if event.get("event_type"):
        return str(event["event_type"])
    payload = event.get("event")
    if isinstance(payload, dict):
        return str(payload.get("type") or "")
    return ""


def _ai_event_ref(event: dict[str, Any]) -> str:
    payload = event.get("event")
    event_id = event.get("event_id") or event.get("id")
    if isinstance(payload, dict):
        event_id = event_id or payload.get("event_id")
    return f"ai_local_event:{event_id}" if event_id else "ai_local_event"


def _ai_event_id(event: dict[str, Any]) -> Any:
    payload = event.get("event")
    if isinstance(payload, dict) and payload.get("event_id"):
        return payload.get("event_id")
    return event.get("event_id") or event.get("id")


def _route_owner(payload: dict[str, Any]) -> str:
    route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
    capability = payload.get("capability") if isinstance(payload.get("capability"), dict) else {}
    candidates = (
        payload.get("owner"),
        payload.get("owner_family"),
        payload.get("service_name"),
        payload.get("selected_owner"),
        route.get("owner"),
        route.get("domain"),
        capability.get("owner"),
        capability.get("service_name"),
    )
    return next((str(candidate) for candidate in candidates if candidate), "")


def _route_capability(payload: dict[str, Any]) -> str:
    route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
    capability = payload.get("capability") if isinstance(payload.get("capability"), dict) else {}
    candidates = (
        payload.get("capability_id"),
        payload.get("selected_capability"),
        route.get("capability_id"),
        capability.get("capability_id"),
    )
    return next((str(candidate) for candidate in candidates if candidate), "")


def _route_quality(events: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(events)
    resolved = []
    for event in events:
        payload = _payload(event)
        owner = _route_owner(payload)
        capability_id = _route_capability(payload)
        if owner or capability_id:
            resolved.append(
                {
                    "event_id": event.get("id"),
                    "task_id": event.get("task_id"),
                    "owner": owner or None,
                    "capability_id": capability_id or None,
                    "evidence_ref": _evidence_ref(event),
                }
            )
    return {
        "status": "measured" if total else "insufficient_evidence",
        "total": total,
        "resolved_owner_count": len(resolved),
        "unresolved_count": max(0, total - len(resolved)),
        "resolved_ratio": round(len(resolved) / total, 4) if total else None,
        "recent": resolved[:10],
    }


def _answer_quality(events: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    scores: list[float] = []
    evidence: list[dict[str, Any]] = []
    for event in events:
        payload = _payload(event)
        status = str(payload.get("review_status") or payload.get("status") or payload.get("verdict") or "reported")
        statuses[status] += 1
        score = payload.get("groundedness_score", payload.get("score"))
        if isinstance(score, int | float):
            scores.append(float(score))
        evidence.append(
            {
                "event_id": event.get("id"),
                "task_id": event.get("task_id"),
                "status": status,
                "evidence_ref": _evidence_ref(event),
            }
        )
    return {
        "status": "measured" if events else "insufficient_evidence",
        "total": len(events),
        "status_counts": dict(statuses),
        "average_score": round(sum(scores) / len(scores), 4) if scores else None,
        "recent": evidence[:10],
    }


def _tool_success(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(call.get("status") or "unknown") for call in tool_calls)
    succeeded = sum(count for status, count in statuses.items() if status in _SUCCESS_TOOL_STATUSES)
    failed = sum(count for status, count in statuses.items() if status in _FAILED_TOOL_STATUSES)
    total = len(tool_calls)
    return {
        "status": "measured" if total else "insufficient_evidence",
        "total": total,
        "succeeded": succeeded,
        "failed_or_blocked": failed,
        "success_ratio": round(succeeded / total, 4) if total else None,
        "status_counts": dict(statuses),
        "recent": [
            {
                "tool_call_id": call.get("id"),
                "task_id": call.get("task_id"),
                "tool_name": call.get("tool_name"),
                "status": call.get("status"),
                "evidence_ref": f"tool_call:{call.get('id')}" if call.get("id") else "tool_call",
            }
            for call in tool_calls[:10]
        ],
    }


def _policy_decision(payload: dict[str, Any]) -> str:
    decision = payload.get("decision")
    if isinstance(decision, dict):
        return str(decision.get("action") or decision.get("decision") or decision.get("status") or "")
    return str(decision or payload.get("action") or payload.get("status") or "")


def _policy_blocks(events: list[dict[str, Any]]) -> dict[str, Any]:
    blocked = []
    for event in events:
        payload = _payload(event)
        decision = _policy_decision(payload).lower()
        requires_approval = bool(payload.get("requires_approval"))
        if decision in _BLOCKING_POLICY_DECISIONS or requires_approval:
            blocked.append(
                {
                    "event_id": event.get("id"),
                    "task_id": event.get("task_id"),
                    "decision": decision or "approval_required",
                    "evidence_ref": _evidence_ref(event),
                }
            )
    return {
        "status": "measured" if events else "insufficient_evidence",
        "total_policy_checks": len(events),
        "blocked": len(blocked),
        "block_ratio": round(len(blocked) / len(events), 4) if events else None,
        "recent": blocked[:10],
    }


def _resource_pressure(leases: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    pressured_leases = [
        lease
        for lease in leases
        if str(lease.get("decision") or "").lower() in _PRESSURE_LEASE_DECISIONS
        or str(lease.get("status") or "").lower() in _PRESSURE_LEASE_DECISIONS
    ]
    recent = [
        {
            "lease_id": lease.get("lease_id") or lease.get("id"),
            "task_id": lease.get("task_id"),
            "capability": lease.get("capability"),
            "decision": lease.get("decision"),
            "status": lease.get("status"),
            "evidence_ref": f"resource_lease:{lease.get('id')}" if lease.get("id") else "resource_lease",
        }
        for lease in pressured_leases[:10]
    ]
    recent.extend(
        {
            "event_id": event.get("id"),
            "task_id": event.get("task_id"),
            "event_type": event.get("event_type"),
            "evidence_ref": _evidence_ref(event),
        }
        for event in events[:10]
    )
    total = len(leases) + len(events)
    return {
        "status": "measured" if total else "insufficient_evidence",
        "resource_leases": len(leases),
        "pressure_events": len(events),
        "pressure_count": len(pressured_leases) + len(events),
        "recent": recent[:10],
    }


def _rag_misses(ledger_events: list[dict[str, Any]], normalized_events: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(ledger_events) + len(normalized_events)
    return {
        "status": "measured" if total else "insufficient_evidence",
        "total": total,
        "ledger_events": len(ledger_events),
        "normalized_ai_local_events": len(normalized_events),
        "recent": [
            {
                "event_id": _ai_event_id(event) if event in normalized_events else event.get("id") or event.get("event_id"),
                "task_id": event.get("task_id"),
                "producer": event.get("producer"),
                "evidence_ref": _ai_event_ref(event) if event in normalized_events else _evidence_ref(event),
            }
            for event in [*ledger_events, *normalized_events][:10]
        ],
    }


def _owner_explanations(events: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    explanations = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        payload = _payload(event)
        if event_type == "agent.action.boundary_checked":
            explanations.append(
                {
                    "source": event_type,
                    "event_id": event.get("id"),
                    "task_id": event.get("task_id"),
                    "owner": payload.get("owner") or None,
                    "capability_id": payload.get("capability_id") or None,
                    "policy_action": payload.get("policy_action") or None,
                    "decision": payload.get("decision") or None,
                    "reason": payload.get("reason") or "runtime boundary check",
                    "evidence_ref": _evidence_ref(event),
                }
            )
        elif event_type in _ROUTE_EVENT_TYPES:
            owner = _route_owner(payload)
            capability_id = _route_capability(payload)
            if owner or capability_id:
                explanations.append(
                    {
                        "source": event_type,
                        "event_id": event.get("id"),
                        "task_id": event.get("task_id"),
                        "owner": owner or None,
                        "capability_id": capability_id or None,
                        "policy_action": payload.get("policy_action") or None,
                        "decision": payload.get("decision") or payload.get("status") or None,
                        "reason": payload.get("reason") or "route/capability selection event",
                        "evidence_ref": _evidence_ref(event),
                    }
                )
    return explanations[:limit]


def _learning_signals(
    *,
    route_quality: dict[str, Any],
    answer_quality: dict[str, Any],
    tool_success: dict[str, Any],
    policy_blocks: dict[str, Any],
    resource_pressure: dict[str, Any],
    rag_misses: dict[str, Any],
) -> list[dict[str, Any]]:
    signals = []
    if route_quality.get("unresolved_count"):
        signals.append({"kind": "route_owner_missing", "count": route_quality["unresolved_count"], "evidence": route_quality["recent"]})
    if answer_quality.get("status") == "insufficient_evidence":
        signals.append({"kind": "answer_quality_missing", "count": 0, "evidence": []})
    if tool_success.get("failed_or_blocked"):
        signals.append({"kind": "tool_failures", "count": tool_success["failed_or_blocked"], "evidence": tool_success["recent"]})
    if policy_blocks.get("blocked"):
        signals.append({"kind": "policy_blocks", "count": policy_blocks["blocked"], "evidence": policy_blocks["recent"]})
    if resource_pressure.get("pressure_count"):
        signals.append({"kind": "resource_pressure", "count": resource_pressure["pressure_count"], "evidence": resource_pressure["recent"]})
    if rag_misses.get("total"):
        signals.append({"kind": "rag_misses", "count": rag_misses["total"], "evidence": rag_misses["recent"]})
    return signals
