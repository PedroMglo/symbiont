"""Normalized task event feed for terminal and cockpit consumers."""

from __future__ import annotations

import time
from typing import Any

TERMINAL_EVENT_LIMIT = 500


def build_task_event_feed(
    trace: dict[str, Any],
    *,
    command_runs: list[dict[str, Any]] | None = None,
    cursor: int = 0,
    limit: int = 200,
    now: float | None = None,
) -> dict[str, Any]:
    """Project ledger rows into a stable event feed.

    ``seq`` is intentionally a projection cursor, not a database primary key.
    It is stable for append-only task event reads because rows are ordered by
    timestamp/id and new rows only append to the end of the feed.
    """

    bounded_limit = max(1, min(limit, TERMINAL_EVENT_LIMIT))
    safe_cursor = max(0, int(cursor or 0))
    task = dict(trace.get("task") or {})
    task_id = str(task.get("id") or "")
    trace_id = str(task.get("trace_id") or "")
    command_by_id = {str(run.get("id") or ""): run for run in command_runs or []}
    projected: list[dict[str, Any]] = []
    seq = 0

    for row in sorted(trace.get("events") or [], key=_event_sort_key):
        seq += 1
        event = _normalize_event(seq, row, task=task, command_by_id=command_by_id)
        projected.append(event)
        run_id = str((row.get("payload") or {}).get("run_id") or "")
        command = command_by_id.get(run_id)
        if command:
            for file_item in _command_diff_files(command):
                if not isinstance(file_item, dict) or not file_item.get("path"):
                    continue
                seq += 1
                projected.append(_file_event(seq, row, command, file_item, task=task))
            for artifact in _command_artifacts(command):
                if not isinstance(artifact, dict) or not artifact.get("path"):
                    continue
                seq += 1
                projected.append(_artifact_event(seq, row, command, artifact, task=task))

    events = [event for event in projected if int(event["seq"]) > safe_cursor][:bounded_limit]
    latest_seq = int(projected[-1]["seq"]) if projected else safe_cursor
    return {
        "task": {
            "id": task_id,
            "trace_id": trace_id,
            "status": task.get("status") or "unknown",
            "terminal": str(task.get("status") or "") in {"completed", "failed", "cancelled"},
        },
        "cursor": safe_cursor,
        "next_cursor": int(events[-1]["seq"]) if events else latest_seq,
        "latest_seq": latest_seq,
        "events": events,
        "count": len(events),
        "now": float(now if now is not None else time.time()),
    }


def sse_encode_event(event: dict[str, Any]) -> str:
    import json

    seq = int(event.get("seq") or 0)
    kind = str(event.get("kind") or "message")
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {seq}\nevent: {kind}\ndata: {payload}\n\n"


def sse_encode_control(name: str, payload: dict[str, Any]) -> str:
    import json

    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _normalize_event(
    seq: int,
    row: dict[str, Any],
    *,
    task: dict[str, Any],
    command_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    raw_kind = str(row.get("event_type") or "event")
    task_id = str(row.get("task_id") or task.get("id") or "")
    trace_id = str(row.get("trace_id") or task.get("trace_id") or "")
    run_id = str(payload.get("run_id") or "")
    command = command_by_id.get(run_id)
    kind = _kind(raw_kind)
    title = _title(raw_kind, payload, command)
    refs = _refs(payload, command)
    event_payload = _safe_payload(payload)
    if raw_kind.startswith("ai_local.material."):
        event_payload.update(_safe_material_payload(_inner_material_payload(payload)))
    if command:
        event_payload.update({
            "command": command.get("command"),
            "cwd": command.get("cwd"),
            "exit_code": command.get("exit_code"),
        })
    return {
        "seq": seq,
        "ts": _float(row.get("timestamp")) or time.time(),
        "session_id": task.get("session_id"),
        "trace_id": trace_id,
        "task_group_id": "main",
        "task_id": task_id or "main",
        "parent_task_id": None,
        "kind": kind,
        "raw_kind": raw_kind,
        "actor": row.get("actor") or "symbiont",
        "title": title,
        "summary": _summary(raw_kind, payload, command),
        "status": _status(raw_kind, task, command),
        "duration_ms": command.get("duration_ms") if command else payload.get("duration_ms"),
        "payload": event_payload,
        "refs": refs,
    }


def _file_event(
    seq: int,
    row: dict[str, Any],
    command: dict[str, Any],
    file_item: dict[str, Any],
    *,
    task: dict[str, Any],
) -> dict[str, Any]:
    path = str(file_item.get("path") or "")
    return {
        "seq": seq,
        "ts": _float(row.get("timestamp")) or time.time(),
        "session_id": task.get("session_id"),
        "trace_id": task.get("trace_id"),
        "task_group_id": "main",
        "task_id": task.get("id") or "main",
        "parent_task_id": None,
        "kind": "file.diff" if file_item.get("patch") or file_item.get("patch_ref") else "file.changed",
        "raw_kind": "file.diff",
        "actor": "agentic.command",
        "title": path,
        "summary": f"{file_item.get('status') or 'changed'} {path}",
        "status": file_item.get("status") or "changed",
        "duration_ms": command.get("duration_ms"),
        "payload": {
            "path": path,
            "status": file_item.get("status") or "changed",
            "additions": file_item.get("additions"),
            "deletions": file_item.get("deletions"),
            "patch": file_item.get("patch"),
            "patch_ref": file_item.get("patch_ref"),
            "binary": bool(file_item.get("binary")),
            "run_id": command.get("id"),
        },
        "refs": {
            "diff_ref": _command_output(command).get("diff_ref") or file_item.get("patch_ref"),
            "stdout_ref": _command_output(command).get("stdout_ref"),
            "stderr_ref": _command_output(command).get("stderr_ref"),
        },
    }


def _artifact_event(
    seq: int,
    row: dict[str, Any],
    command: dict[str, Any],
    artifact: dict[str, Any],
    *,
    task: dict[str, Any],
) -> dict[str, Any]:
    path = str(artifact.get("path") or "")
    return {
        "seq": seq,
        "ts": _float(row.get("timestamp")) or time.time(),
        "session_id": task.get("session_id"),
        "trace_id": task.get("trace_id"),
        "task_group_id": "main",
        "task_id": task.get("id") or "main",
        "parent_task_id": None,
        "kind": "artifact.created",
        "raw_kind": "artifact.created",
        "actor": "agentic.command",
        "title": path,
        "summary": f"artifact {path}",
        "status": "created",
        "duration_ms": command.get("duration_ms"),
        "payload": {
            "path": path,
            "artifact_id": artifact.get("artifact_id"),
            "sha256": artifact.get("sha256"),
            "size_bytes": artifact.get("size_bytes"),
            "run_id": command.get("id"),
        },
        "refs": {
            "diff_ref": _command_output(command).get("diff_ref"),
            "stdout_ref": _command_output(command).get("stdout_ref"),
            "stderr_ref": _command_output(command).get("stderr_ref"),
        },
    }


def _kind(raw_kind: str) -> str:
    if raw_kind.startswith("ai_local.material."):
        return raw_kind.removeprefix("ai_local.")
    if raw_kind.startswith("task."):
        return "task.finished" if raw_kind in {"task.completed", "task.failed", "task.cancelled"} else "task.updated"
    if raw_kind.startswith("run."):
        return "task.updated"
    if raw_kind.startswith("command."):
        return "command.finished"
    if raw_kind.startswith("approval."):
        return "approval.requested" if raw_kind == "approval.created" else "approval.resolved"
    if raw_kind.startswith("agent.message"):
        return "agent.message"
    if raw_kind.startswith("agent.decision"):
        return "agent.decision"
    if raw_kind.startswith("agent.raw_output"):
        return "llm.response.done"
    return raw_kind


def _title(raw_kind: str, payload: dict[str, Any], command: dict[str, Any] | None) -> str:
    if command:
        return str(command.get("command") or payload.get("run_id") or raw_kind)
    if raw_kind.startswith("ai_local.material."):
        material = _inner_material_payload(payload)
        issue = material.get("issue") if isinstance(material.get("issue"), dict) else {}
        artifact = material.get("artifact") if isinstance(material.get("artifact"), dict) else {}
        artifacts = material.get("artifacts") if isinstance(material.get("artifacts"), list) else []
        first_artifact = next((item for item in artifacts if isinstance(item, dict)), {})
        return str(
            material.get("action_id")
            or material.get("phase")
            or material.get("session_id")
            or material.get("validation_profile")
            or issue.get("code")
            or artifact.get("path")
            or first_artifact.get("path")
            or raw_kind
        )
    return str(payload.get("action") or payload.get("entrypoint") or payload.get("status") or payload.get("run_id") or raw_kind)


def _summary(raw_kind: str, payload: dict[str, Any], command: dict[str, Any] | None) -> str:
    if command:
        exit_code = command.get("exit_code")
        status = command.get("status") or raw_kind.removeprefix("command.")
        suffix = f" exit={exit_code}" if exit_code is not None else ""
        return f"{status} {_preview(str(command.get('command') or ''), 120)}{suffix}".strip()
    if raw_kind.startswith("ai_local.material."):
        material = _inner_material_payload(payload)
        issue = material.get("issue") if isinstance(material.get("issue"), dict) else {}
        if issue.get("code"):
            return f"issue={issue.get('code')}"
        if material.get("phase"):
            latency = f" latency_source={material.get('latency_source')}" if material.get("latency_source") else ""
            return f"phase={material.get('phase')} status={material.get('status')}{latency}"
        if material.get("validation_profile"):
            return f"validation_profile={material.get('validation_profile')} status={material.get('status')}"
        if material.get("status"):
            return f"status={material.get('status')}"
    for key in ("reason", "status", "decision", "action", "run_id", "worker_id", "error"):
        value = payload.get(key)
        if value:
            return f"{key}={_preview(str(value), 120)}"
    return _preview(" ".join(f"{key}={value}" for key, value in sorted(payload.items())[:3]), 160) or raw_kind


def _status(raw_kind: str, task: dict[str, Any], command: dict[str, Any] | None) -> str:
    if command:
        return str(command.get("status") or "unknown")
    if raw_kind.endswith(".started") or raw_kind in {"task.claimed", "task.running", "task.planning"}:
        return "running"
    if raw_kind.endswith(".failed"):
        return "failed"
    if raw_kind.endswith(".completed") or raw_kind.endswith(".approved") or raw_kind.endswith(".passed") or raw_kind.endswith(".created"):
        return "completed"
    return str(task.get("status") or "info")


def _refs(payload: dict[str, Any], command: dict[str, Any] | None) -> dict[str, Any]:
    command_output = _command_output(command or {})
    material = _inner_material_payload(payload)
    return {
        "diff_ref": command_output.get("diff_ref") or payload.get("diff_ref"),
        "stdout_ref": command_output.get("stdout_ref") or payload.get("stdout_ref") or material.get("stdout_ref"),
        "stderr_ref": command_output.get("stderr_ref") or payload.get("stderr_ref") or material.get("stderr_ref"),
        "prompt_ref": payload.get("prompt_ref"),
    }


def _command_output(command: dict[str, Any]) -> dict[str, Any]:
    output = command.get("output")
    if isinstance(output, dict):
        return output
    metadata = command.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "diff_ref": metadata.get("diff_ref") or metadata.get("workspace_execution_diff_ref"),
        "stdout_ref": metadata.get("stdout_ref"),
        "stderr_ref": metadata.get("stderr_ref"),
    }


def _command_diff_files(command: dict[str, Any]) -> list[dict[str, Any]]:
    items = command.get("diff_files")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    metadata = command.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    raw = metadata.get("workspace_execution_diff_files") or metadata.get("diff_files") or []
    return [item for item in raw if isinstance(item, dict)]


def _command_artifacts(command: dict[str, Any]) -> list[dict[str, Any]]:
    items = command.get("artifacts")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    metadata = command.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    raw = metadata.get("workspace_execution_artifacts") or metadata.get("artifacts") or []
    return [item for item in raw if isinstance(item, dict)]


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "run_id",
        "session_id",
        "action",
        "risk_level",
        "policy_decision",
        "exit_code",
        "approval_id",
        "output_truncated",
        "stdout_ref",
        "stderr_ref",
        "diff_ref",
        "worker_id",
        "previous_status",
        "status",
        "entrypoint",
        "graph_run_id",
        "state_hash",
    }
    return {key: value for key, value in payload.items() if key in allowed}


def _inner_material_payload(payload: dict[str, Any]) -> dict[str, Any]:
    material = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    return material


def _safe_material_payload(payload: dict[str, Any]) -> dict[str, Any]:
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    return {
        "event_id": payload.get("event_id"),
        "session_id": payload.get("session_id"),
        "phase": payload.get("phase"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "last_progress_at": payload.get("last_progress_at"),
        "duration_ms": payload.get("duration_ms"),
        "latency_source": payload.get("latency_source"),
        "vm_session_id": payload.get("vm_session_id"),
        "action_id": payload.get("action_id"),
        "validation_profile": payload.get("validation_profile"),
        "command_run_id": payload.get("command_run_id"),
        "status": payload.get("status"),
        "issue": {
            "code": issue.get("code"),
            "gate": issue.get("gate"),
            "owner": issue.get("owner"),
            "severity": issue.get("severity"),
            "path": issue.get("path"),
        } if issue else None,
        "artifact": {
            "path": artifact.get("path"),
            "status": artifact.get("status"),
            "sha256": artifact.get("sha256"),
        } if artifact else None,
        "artifacts": [
            {
                "path": item.get("path"),
                "artifact_id": item.get("artifact_id"),
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes"),
            }
            for item in artifacts
            if isinstance(item, dict)
        ],
    }


def _event_sort_key(row: dict[str, Any]) -> tuple[float, str]:
    return (_float(row.get("timestamp")) or 0.0, str(row.get("id") or ""))


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _preview(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
