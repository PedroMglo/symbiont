"""Deterministic reducer for agentic task state.

The SQLite event ledger is the source of truth. AgentState snapshots are only
projections produced by replaying these events.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from orchestrator.agentic.contracts import (
    ActionResult,
    AgentAction,
    AgentDecision,
    AgentObservation,
    AgentState,
)


def canonical_json(value: Any) -> str:
    """Return canonical JSON used for reproducible hashes."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def state_hash(state: AgentState) -> str:
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def initial_state_from_task(task: Any) -> AgentState:
    """Build the initial state projection from an AgenticTask or task dict."""

    get = task.get if isinstance(task, dict) else lambda key, default=None: getattr(task, key, default)
    metadata = get("metadata", {}) or {}
    constraints = metadata.get("constraints") if isinstance(metadata.get("constraints"), dict) else {}
    facts = metadata.get("known_facts") if isinstance(metadata.get("known_facts"), list) else []
    assumptions = metadata.get("assumptions") if isinstance(metadata.get("assumptions"), list) else []
    risk_level = metadata.get("risk_level") if metadata.get("risk_level") in {"low", "medium", "high", "deny"} else "low"
    return AgentState(
        task_id=str(get("id")),
        trace_id=str(get("trace_id")),
        goal=str(get("goal")),
        known_facts=[str(item) for item in facts],
        assumptions=[str(item) for item in assumptions],
        constraints=constraints,
        risk_level=risk_level,
        status="planning",
        metadata={"source": get("source"), "mode": get("mode")},
    )


def reduce_state(previous: AgentState, event: dict[str, Any]) -> AgentState:
    """Apply one ledger event to a state projection."""

    previous = AgentState.model_validate(previous)
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}

    if event_type == "agent.state.initialized" and isinstance(payload.get("state"), dict):
        return AgentState.model_validate(payload["state"])

    if event_type == "agent.decision.recorded" and isinstance(payload.get("decision"), dict):
        decision = AgentDecision.model_validate(payload["decision"])
        return _apply_decision(previous, decision)

    if event_type == "agent.action.proposed" and isinstance(payload.get("action"), dict):
        action = _validate_action(payload["action"])
        return _add_pending_action(previous, action)

    if event_type == "agent.action.result_recorded" and isinstance(payload.get("action_result"), dict):
        result = ActionResult.model_validate(payload["action_result"])
        return _apply_action_result(previous, result)

    if event_type == "agent.observation.recorded" and isinstance(payload.get("observation"), dict):
        observation = AgentObservation.model_validate(payload["observation"])
        return previous.model_copy(update={"observations": [*previous.observations, observation]})

    return previous


def replay_events(initial_state: AgentState, events: list[dict[str, Any]]) -> AgentState:
    state = initial_state
    for event in events:
        state = reduce_state(state, event)
    return state


def _apply_decision(previous: AgentState, decision: AgentDecision) -> AgentState:
    facts = _append_unique(previous.known_facts, decision.new_facts)
    pending = list(previous.pending_actions)
    known_action_ids = {action.action_id for action in pending}
    known_action_ids.update(result.action_id for result in previous.completed_actions)
    for action in decision.proposed_actions:
        if action.action_id not in known_action_ids:
            pending.append(action)
            known_action_ids.add(action.action_id)

    metadata = dict(previous.metadata)
    if decision.questions_for_user:
        metadata["questions_for_user"] = decision.questions_for_user
    metadata["last_decision_confidence"] = decision.confidence
    if decision.raw_output_ref is not None:
        metadata["last_raw_output_ref"] = decision.raw_output_ref.model_dump(mode="json")

    status = _state_status_for_decision(decision, has_pending=bool(pending))
    risk_level = _highest_risk(previous.risk_level, [_action_risk_hint(action) for action in pending])
    return previous.model_copy(
        update={
            "known_facts": facts,
            "pending_actions": pending,
            "risk_level": risk_level,
            "status": status,
            "metadata": metadata,
        }
    )


def _apply_action_result(previous: AgentState, result: ActionResult) -> AgentState:
    pending = [action for action in previous.pending_actions if action.action_id != result.action_id]
    completed_ids = {item.action_id for item in previous.completed_actions}
    completed = list(previous.completed_actions)
    if result.action_id not in completed_ids:
        completed.append(result)
    observations = list(previous.observations)
    if result.observation:
        observations.append(
            AgentObservation(
                observation_id=f"obs:{result.action_id}",
                source=f"action:{result.action_type}",
                content=result.observation,
                metadata={"action_id": result.action_id, "status": result.status},
            )
        )
    if result.status in {"blocked", "denied", "failed"}:
        status = "blocked"
    elif pending:
        status = "executing"
    else:
        status = "planning"
    return previous.model_copy(
        update={
            "pending_actions": pending,
            "completed_actions": completed,
            "observations": observations,
            "status": status,
        }
    )


def _add_pending_action(previous: AgentState, action: AgentAction) -> AgentState:
    if action.action_id in {item.action_id for item in previous.pending_actions}:
        return previous
    if action.action_id in {item.action_id for item in previous.completed_actions}:
        return previous
    return previous.model_copy(update={"pending_actions": [*previous.pending_actions, action], "status": "executing"})


def _append_unique(existing: list[str], incoming: list[str]) -> list[str]:
    seen = set(existing)
    result = list(existing)
    for item in incoming:
        text = str(item)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _validate_action(data: dict[str, Any]) -> AgentAction:
    from pydantic import TypeAdapter

    return TypeAdapter(AgentAction).validate_python(data)


def _state_status_for_decision(decision: AgentDecision, *, has_pending: bool) -> str:
    if decision.status == "complete":
        return "complete"
    if decision.status == "failed":
        return "failed"
    if decision.status == "blocked":
        return "blocked"
    if decision.status == "waiting_for_user" or decision.questions_for_user:
        return "waiting_for_user"
    if decision.status == "needs_action" and has_pending:
        return "executing"
    return "planning"


def _action_risk_hint(action: AgentAction) -> str:
    if getattr(action, "type", "") == "shell_command":
        return "low" if getattr(action, "expected_effect", "read_only") == "read_only" else "high"
    if getattr(action, "type", "") == "api_call":
        return "low" if getattr(action, "expected_effect", "external") == "read_only" else "medium"
    return "low"


def _highest_risk(current: str, candidates: list[str]) -> str:
    order = {"low": 0, "medium": 1, "high": 2, "deny": 3}
    risk = current if current in order else "low"
    for candidate in candidates:
        if order.get(candidate, 0) > order[risk]:
            risk = candidate
    return risk
