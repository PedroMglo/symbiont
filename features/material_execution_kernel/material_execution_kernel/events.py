"""Event helpers for material execution sessions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from material_execution_kernel.types import EventSource, EventStatus, LatencySource, MaterialEvent


def new_material_event(
    *,
    event_type: str,
    session_id: str,
    task_id: str,
    source: EventSource,
    phase: str | None = None,
    status: EventStatus = "progress",
    latency_source: LatencySource = "unknown",
    payload: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    duration_ms: int | None = None,
) -> MaterialEvent:
    now = datetime.now(UTC)
    return MaterialEvent(
        event_id=f"evt_{uuid4().hex}",
        event_type=event_type,
        session_id=session_id,
        task_id=task_id,
        source=source,
        created_at=now,
        phase=phase,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        last_progress_at=now,
        duration_ms=duration_ms,
        latency_source=latency_source,
        payload=payload or {},
    )


def events_to_jsonl(events: list[MaterialEvent]) -> str:
    return "".join(event.model_dump_json() + "\n" for event in events)


def event_to_ai_local_payload(event: MaterialEvent) -> dict[str, Any]:
    """Return the generic payload shape expected by orchestrator event ingestion."""

    data = event.model_dump(mode="json")
    return {
        "schema_version": "material_event.v3.2",
        "event_id": data["event_id"],
        "session_id": data["session_id"],
        "phase": data.get("phase"),
        "status": data.get("status"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "last_progress_at": data.get("last_progress_at"),
        "duration_ms": data.get("duration_ms"),
        "latency_source": data.get("latency_source"),
        "payload": data.get("payload") or {},
    }


def jsonl_to_dicts(jsonl: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in jsonl.splitlines():
        if line.strip():
            items.append(json.loads(line))
    return items
