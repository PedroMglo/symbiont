"""Canonical RAG admin reprocess workflow and direct executor."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from threading import Event
from typing import Any, Callable, Protocol

VALID_REPROCESS_TARGETS = frozenset({"local", "sources", "graph", "cag", "all"})
ProgressCallback = Callable[[dict[str, Any]], None]
RESOURCE_VISIBLE_STATUSES = frozenset({
    "paused_resource_pressure",
    "deferred_resource_pressure",
    "retry_scheduled",
    "failed_resource_pressure",
    "cancelled",
})
TERMINAL_CHILD_STATUSES = frozenset({
    "completed",
    "failed",
    "canceled",
    "cancelled",
    "failed_resource_pressure",
})


class _SyncModule(Protocol):
    def sync_local(
        self,
        *,
        vault_filter: str | None = None,
        force: bool = False,
        cancel_event: Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None: ...

    def sync_requested_sources(
        self,
        sources: list[dict[str, str]],
        *,
        force: bool = False,
        cancel_event: Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None: ...

    def sync_graphify(
        self,
        *,
        force: bool = False,
        cancel_event: Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None: ...

    def generate_cag_packs(self) -> None: ...

    def sync_all(
        self,
        *,
        vault_filter: str | None = None,
        force: bool = False,
        cancel_event: Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None: ...


class ReprocessCancelled(RuntimeError):
    """Raised when an admin reprocess job is canceled cooperatively."""


def _raise_if_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ReprocessCancelled("Admin reprocess job canceled")


def _load_sync_module() -> _SyncModule:
    from pipeline import sync

    return sync


def _normalize_origin(origin: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(origin or {})
    payload.setdefault("kind", "unknown")
    payload.setdefault("name", None)
    payload.setdefault("metadata", {})
    if not payload.get("name"):
        payload["name"] = (
            payload.get("agent")
            or payload.get("feature")
            or payload.get("service")
            or payload.get("user")
            or payload.get("machine")
        )
    return payload


class ReprocessProgressTracker:
    """Persist child progress for direct and Temporal-backed admin jobs."""

    def __init__(self, job_id: str | None, *, origin: dict[str, Any] | None = None) -> None:
        self.job_id = job_id
        self.origin = _normalize_origin(origin)
        self._lock = threading.RLock()
        self._children: dict[str, dict[str, Any]] = {}

    def __call__(self, event: dict[str, Any]) -> None:
        child_id = str(event.get("child_id") or "")
        if not child_id:
            return
        with self._lock:
            current = dict(self._children.get(child_id, {}))
            event_name = str(event.get("event") or "")
            explicit_status = str(event.get("status") or event.get("resource_state") or "")
            status = {
                "child_started": "running",
                "child_completed": "completed",
                "child_failed": "failed",
                "child_paused": "paused_resource_pressure",
                "child_deferred": "retry_scheduled",
            }.get(event_name, explicit_status or str(current.get("status") or "running"))
            if event_name == "child_deferred" and explicit_status in {"deferred_resource_pressure", "retry_scheduled"}:
                status = "retry_scheduled"
            elif explicit_status in RESOURCE_VISIBLE_STATUSES:
                status = explicit_status
            current.update({k: v for k, v in event.items() if k != "event"})
            current["child_id"] = child_id
            current["status"] = status
            if status in RESOURCE_VISIBLE_STATUSES:
                current["resource_state"] = status
            else:
                current.setdefault("resource_state", status)
            current.setdefault("started_at", time.time())
            if status in TERMINAL_CHILD_STATUSES:
                current.setdefault("finished_at", time.time())
            self._children[child_id] = current
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            children = sorted(self._children.values(), key=lambda item: str(item.get("child_id") or ""))
            result = {
                "origin": self.origin,
                "children": children,
                "children_total": len(children),
                "children_completed": sum(1 for child in children if child.get("status") == "completed"),
                "children_failed": sum(
                    1 for child in children
                    if child.get("status") in {"failed", "failed_resource_pressure"}
                ),
            }
            retry_at = _next_retry_at(children)
            if retry_at is not None:
                result["retry_at"] = retry_at
            return result

    def _persist(self) -> None:
        if not self.job_id:
            return
        try:
            from rag_config import settings
            from workflows.job_store import default_admin_job_store

            store = default_admin_job_store(settings)
            job = dict(store.get(self.job_id) or {"job_id": self.job_id})
            result = dict(job.get("result") or {})
            result.update(self.snapshot())
            result["children_total"] = len(result.get("children") or [])
            result["children_completed"] = sum(1 for child in result["children"] if child.get("status") == "completed")
            result["children_failed"] = sum(
                1 for child in result["children"]
                if child.get("status") in {"failed", "failed_resource_pressure"}
            )
            job["result"] = result
            job["origin"] = self.origin
            aggregate_status = _aggregate_parent_status(result.get("children") or [])
            if aggregate_status:
                job["status"] = aggregate_status
            retry_at = result.get("retry_at")
            if retry_at is not None:
                job["retry_at"] = retry_at
            else:
                job.pop("retry_at", None)
            job["updated_at"] = time.time()
            store.upsert(self.job_id, job)
        except Exception:
            return


def _next_retry_at(children: list[dict[str, Any]]) -> float | None:
    values: list[float] = []
    for child in children:
        if str(child.get("status") or "") not in {"retry_scheduled", "deferred_resource_pressure"}:
            continue
        try:
            retry_at = float(child.get("retry_at") or 0.0)
        except (TypeError, ValueError):
            continue
        if retry_at > 0:
            values.append(retry_at)
    return min(values) if values else None


def _aggregate_parent_status(children: list[dict[str, Any]]) -> str | None:
    statuses = {str(child.get("status") or "") for child in children}
    if "failed_resource_pressure" in statuses:
        return "failed_resource_pressure"
    if "cancelled" in statuses or "canceled" in statuses:
        return "cancelled"
    if "retry_scheduled" in statuses or "deferred_resource_pressure" in statuses:
        return "retry_scheduled"
    if "paused_resource_pressure" in statuses:
        return "paused_resource_pressure"
    if "running" in statuses:
        return "running"
    return None


def execute_reprocess_target(
    target: str,
    *,
    force: bool = False,
    vault: str | None = None,
    sources: list[dict[str, Any]] | None = None,
    origin: dict[str, Any] | None = None,
    job_id: str | None = None,
    sync_module: _SyncModule | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Run one RAG reprocess target through its owning pipeline function."""
    normalized_target = str(target).strip().lower()
    if normalized_target not in VALID_REPROCESS_TARGETS:
        raise ValueError(f"Unsupported reprocess target: {target}")

    registered_sources: list[dict[str, str]] = []
    if sources:
        from pipeline.adhoc_sources import register_requested_sources

        registered_sources = register_requested_sources(sources)

    sync = sync_module or _load_sync_module()
    tracker = ReprocessProgressTracker(job_id, origin=origin)
    _raise_if_cancelled(cancel_event)
    if normalized_target == "local":
        sync.sync_local(
            vault_filter=vault,
            force=force,
            cancel_event=cancel_event,
            progress_callback=tracker,
        )
    elif normalized_target == "sources":
        sync.sync_requested_sources(
            registered_sources,
            force=force,
            cancel_event=cancel_event,
            progress_callback=tracker,
        )
    elif normalized_target == "graph":
        sync.sync_graphify(force=force, cancel_event=cancel_event, progress_callback=tracker)
    elif normalized_target == "cag":
        tracker(
            {
                "event": "child_started",
                "child_id": "cag-packs",
                "phase": "cag",
                "source": {"name": "cag-packs", "source_type": "cag"},
                "started_at": time.time(),
            }
        )
        sync.generate_cag_packs()
        tracker(
            {
                "event": "child_completed",
                "child_id": "cag-packs",
                "phase": "cag",
                "source": {"name": "cag-packs", "source_type": "cag"},
                "finished_at": time.time(),
                "result": {},
            }
        )
    elif normalized_target == "all":
        sync.sync_all(
            vault_filter=vault,
            force=force,
            cancel_event=cancel_event,
            progress_callback=tracker,
        )
    _raise_if_cancelled(cancel_event)

    return {
        "target": normalized_target,
        "force": force,
        "vault": vault,
        "sources": registered_sources,
        **tracker.snapshot(),
    }


try:
    from temporalio import activity, workflow
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    activity = None
    workflow = None
    run_reprocess_activity = None
    RagReprocessWorkflow = None
else:

    @activity.defn(name="rag.reprocess")
    def run_reprocess_activity(payload: dict[str, Any]) -> dict[str, Any]:
        return execute_reprocess_target(
            str(payload.get("target", "all")),
            force=bool(payload.get("force", False)),
            vault=payload.get("vault"),
            sources=payload.get("sources") if isinstance(payload.get("sources"), list) else None,
            origin=payload.get("origin") if isinstance(payload.get("origin"), dict) else None,
            job_id=str(payload.get("job_id") or "") or None,
        )

    @workflow.defn(name="RagReprocessWorkflow")
    class RagReprocessWorkflow:
        @workflow.run
        async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
            timeout_seconds = int(payload.get("timeout_seconds") or 7200)
            return await workflow.execute_activity(
                run_reprocess_activity,
                payload,
                start_to_close_timeout=timedelta(seconds=timeout_seconds),
            )
