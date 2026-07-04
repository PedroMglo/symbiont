"""Agentic deliberation summary helpers.

This module owns the read-side contract that turns blackboard messages in the
agentic ledger into a compact runtime result. It does not execute agents or
interpret feature-specific semantics.
"""

from __future__ import annotations

from typing import Any


def summarize_agentic_deliberation(store: Any, task_id: str, *, limit: int = 500) -> dict[str, Any]:
    """Return a stable summary of deliberation messages for one task."""
    messages = store.list_agent_messages(task_id=task_id, limit=limit)
    questions = [message for message in messages if message.get("message_type") == "question"]
    answers = [message for message in messages if message.get("message_type") == "answer"]
    critiques = [message for message in messages if message.get("message_type") == "critique"]
    validations = [message for message in messages if message.get("message_type") == "validation"]
    consensus_messages = [message for message in messages if message.get("message_type") == "consensus"]
    if not any((questions, answers, critiques, validations, consensus_messages)):
        return {"available": False}

    consensus_payloads = [
        payload
        for payload in (_message_consensus_payload(item) for item in consensus_messages)
        if payload
    ]
    deliberation_payloads = [
        payload
        for payload in consensus_payloads
        if (payload.get("metadata") or {}).get("decider") == "agentic.runner.deliberation"
    ]
    latest_consensus = (deliberation_payloads or consensus_payloads or [{}])[0]
    latest_metadata = latest_consensus.get("metadata") if isinstance(latest_consensus.get("metadata"), dict) else {}
    status = str(latest_consensus.get("status") or "")
    return {
        "available": True,
        "status": status,
        "requires_attention": status in {"contested", "needs_more_evidence", "failed"},
        "questions_count": len(questions),
        "answers_count": len(answers),
        "critiques_count": len(critiques),
        "validations_count": len(validations),
        "consensus_count": len(consensus_messages),
        "latest_consensus": {
            "status": status,
            "summary": str(latest_consensus.get("summary") or "")[:2000],
            "confidence": _safe_float(latest_consensus.get("confidence")),
            "agreed_facts": _string_list(latest_consensus.get("agreed_facts")),
            "contested_facts": _string_list(latest_consensus.get("contested_facts")),
            "contradictions": _string_list(latest_metadata.get("contradictions")),
            "reason": str(latest_metadata.get("reason") or ""),
            "score": latest_metadata.get("score"),
            "decider": str(latest_metadata.get("decider") or ""),
        },
    }


def deliberation_to_synthesis_text(deliberation: dict[str, Any]) -> str:
    """Render a deliberation summary as bounded evidence for final synthesis."""
    if not isinstance(deliberation, dict) or not deliberation.get("available"):
        return ""
    consensus = deliberation.get("latest_consensus")
    if not isinstance(consensus, dict):
        return ""

    lines = [
        f"Consensus status: {consensus.get('status') or deliberation.get('status') or 'unknown'}",
        f"Requires attention: {'yes' if deliberation.get('requires_attention') else 'no'}",
    ]
    summary = str(consensus.get("summary") or "").strip()
    if summary:
        lines.append(f"Summary: {summary[:2000]}")
    _append_list(lines, "Validated facts", consensus.get("agreed_facts"))
    _append_list(lines, "Contested facts", consensus.get("contested_facts"))
    _append_list(lines, "Contradictions", consensus.get("contradictions"))
    reason = str(consensus.get("reason") or "").strip()
    if reason:
        lines.append(f"Consensus reason: {reason[:500]}")
    confidence = consensus.get("confidence")
    if confidence is not None:
        lines.append(f"Confidence: {_safe_float(confidence):.2f}")
    return "\n".join(lines).strip()


def _append_list(lines: list[str], label: str, value: Any) -> None:
    items = _string_list(value)
    if not items:
        return
    lines.append(f"{label}:")
    lines.extend(f"- {item}" for item in items[:8])


def _message_consensus_payload(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") if isinstance(message, dict) else None
    if not isinstance(payload, dict):
        return {}
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    nested = metadata.get("payload")
    if isinstance(nested, dict):
        return nested
    nested = metadata.get("consensus")
    if isinstance(nested, dict):
        return nested
    if payload.get("kind") == "consensus":
        return {
            "status": metadata.get("status") or "",
            "summary": payload.get("content") or "",
            "confidence": metadata.get("confidence") or 0.0,
            "agreed_facts": metadata.get("agreed_facts") or [],
            "contested_facts": metadata.get("contested_facts") or [],
            "metadata": metadata,
        }
    return {}


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    return [str(item).strip()[:1000] for item in items if str(item).strip()]
