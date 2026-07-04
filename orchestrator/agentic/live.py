"""Read-only live projection for background task/session observers."""

from __future__ import annotations

import time
from typing import Any

from orchestrator.agentic.models import TaskStatus

ACTIVE_STATUSES = {
    TaskStatus.QUEUED.value,
    TaskStatus.PLANNING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.WAITING_APPROVAL.value,
    TaskStatus.RECOVERING.value,
}
TERMINAL_STATUSES = {
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
}


def build_live_snapshot(
    tasks: list[dict[str, Any]],
    *,
    status_filter: str = "running,recent",
    limit: int = 200,
    recent_seconds: int = 900,
    now: float | None = None,
) -> dict[str, Any]:
    """Project task rows into a compact live dashboard snapshot.

    The snapshot is intentionally read-only and bounded. It does not fetch full
    timelines per task; callers should fetch a selected task timeline lazily.
    """

    generated_at = float(now if now is not None else time.time())
    filters = _parse_filter(status_filter)
    bounded_limit = max(1, min(int(limit or 200), 500))
    recent_cutoff = generated_at - max(0, int(recent_seconds or 0))
    selected_tasks = [
        _task_summary(task, generated_at=generated_at)
        for task in tasks
        if _include_task(task, filters=filters, recent_cutoff=recent_cutoff)
    ][:bounded_limit]

    sessions_by_id: dict[str, dict[str, Any]] = {}
    for task in selected_tasks:
        session_id = str(task.get("session_id") or "unknown session")
        session = sessions_by_id.setdefault(
            session_id,
            {
                "session_id": session_id,
                "status": "idle",
                "cwd": "",
                "model": "",
                "created_at": task.get("created_at"),
                "updated_at": task.get("updated_at"),
                "last_prompt_preview": "",
                "active_task_id": "",
                "root_task_id": "",
                "task_ids": [],
                "task_count": 0,
                "running_task_count": 0,
                "failed_task_count": 0,
            },
        )
        session["task_ids"].append(task["task_id"])
        session["task_count"] = int(session["task_count"]) + 1
        if task["status"] in ACTIVE_STATUSES:
            session["running_task_count"] = int(session["running_task_count"]) + 1
            session["active_task_id"] = session["active_task_id"] or task["task_id"]
        if task["status"] == TaskStatus.FAILED.value:
            session["failed_task_count"] = int(session["failed_task_count"]) + 1
        session["created_at"] = min(float(session["created_at"] or task["created_at"]), float(task["created_at"]))
        if float(task["updated_at"]) >= float(session["updated_at"] or 0):
            session["updated_at"] = task["updated_at"]
            session["last_prompt_preview"] = task["goal_preview"]
            session["cwd"] = task.get("cwd") or session["cwd"]
            session["model"] = task.get("model") or session["model"]
            if not session["active_task_id"]:
                session["active_task_id"] = task["task_id"]
        session["root_task_id"] = session["root_task_id"] or task["task_id"]

    sessions = sorted(sessions_by_id.values(), key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    for session in sessions:
        session["status"] = _session_status(session)

    status_counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "seq": int(generated_at * 1000),
        "server_time": generated_at,
        "filter": {
            "status": status_filter,
            "recent_seconds": recent_seconds,
            "limit": bounded_limit,
        },
        "counts": {
            "sessions": len(sessions),
            "tasks": len(selected_tasks),
            "running": sum(1 for task in selected_tasks if task["status"] in ACTIVE_STATUSES),
            "failed": sum(1 for task in selected_tasks if task["status"] == TaskStatus.FAILED.value),
            "recent": sum(1 for task in selected_tasks if float(task["updated_at"]) >= recent_cutoff),
            "status": status_counts,
        },
        "sessions": sessions,
        "tasks": selected_tasks,
    }


def _parse_filter(value: str) -> set[str]:
    filters = {item.strip().lower() for item in str(value or "").split(",") if item.strip()}
    return filters or {"running", "recent"}


def _include_task(task: dict[str, Any], *, filters: set[str], recent_cutoff: float) -> bool:
    if "all" in filters:
        return True
    status = str(task.get("status") or "")
    updated_at = _float(task.get("updated_at"))
    if "running" in filters and status in ACTIVE_STATUSES:
        return True
    if "failed" in filters and status == TaskStatus.FAILED.value:
        return True
    if "completed" in filters and status == TaskStatus.COMPLETED.value:
        return True
    if "cancelled" in filters and status == TaskStatus.CANCELLED.value:
        return True
    if "recent" in filters and updated_at >= recent_cutoff:
        return True
    return status in filters


def _task_summary(task: dict[str, Any], *, generated_at: float) -> dict[str, Any]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    goal = str(task.get("goal") or "")
    task_id = str(task.get("id") or task.get("task_id") or "")
    status = str(task.get("status") or "unknown")
    created_at = _float(task.get("created_at"))
    updated_at = _float(task.get("updated_at"))
    return {
        "task_id": task_id,
        "session_id": task.get("session_id") or "unknown session",
        "parent_task_id": metadata.get("parent_task_id"),
        "name": str(metadata.get("name") or metadata.get("task_name") or "main"),
        "trace_id": str(task.get("trace_id") or ""),
        "status": status,
        "active_phase": str(metadata.get("active_phase") or metadata.get("runner") or status),
        "goal_preview": _preview(goal, 160),
        "created_at": created_at,
        "updated_at": updated_at,
        "elapsed_seconds": max(0.0, (generated_at if status in ACTIVE_STATUSES else updated_at) - created_at),
        "cwd": str(metadata.get("client_cwd") or metadata.get("cwd") or ""),
        "model": str(metadata.get("model") or metadata.get("llm_model") or ""),
        "counts": {
            "files": int(metadata.get("file_count") or 0),
            "commands": int(metadata.get("command_count") or 0),
            "tools": int(metadata.get("tool_count") or 0),
            "llm_calls": int(metadata.get("llm_call_count") or 0),
        },
        "file_summary": metadata.get("file_summary") if isinstance(metadata.get("file_summary"), list) else [],
        "last_event_summary": str(metadata.get("last_event_summary") or ""),
        "terminal": status in TERMINAL_STATUSES,
    }


def _session_status(session: dict[str, Any]) -> str:
    if int(session.get("running_task_count") or 0) > 0:
        return "running"
    if int(session.get("failed_task_count") or 0) > 0:
        return "failed"
    return "idle"


def _preview(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)] + "…"


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
