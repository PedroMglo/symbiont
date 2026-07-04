"""Temporal client adapter for RAG reprocess jobs."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from rag_config import settings


def _temporal_unavailable_error() -> RuntimeError:
    return RuntimeError(
        "RAG workflows backend is 'temporal' but temporalio is not installed. "
        "Install obsidian-rag with the temporal extra and run the Temporal worker."
    )


async def _start_reprocess_workflow_async(
    job_id: str,
    payload: dict[str, Any],
    *,
    settings_obj: Any,
) -> dict[str, Any]:
    try:
        from temporalio.client import Client
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise _temporal_unavailable_error() from exc

    from workflows.reprocess import RagReprocessWorkflow

    if RagReprocessWorkflow is None:  # pragma: no cover - defensive guard
        raise _temporal_unavailable_error()

    cfg = settings_obj.workflows
    workflow_id = f"rag-reprocess-{job_id}"
    payload = dict(payload)
    payload.setdefault("timeout_seconds", cfg.temporal_workflow_timeout_seconds)

    client = await Client.connect(
        cfg.temporal_address,
        namespace=cfg.temporal_namespace,
    )
    handle = await client.start_workflow(
        RagReprocessWorkflow.run,
        payload,
        id=workflow_id,
        task_queue=cfg.temporal_task_queue,
        execution_timeout=timedelta(seconds=cfg.temporal_workflow_timeout_seconds),
    )
    run_id = getattr(handle, "result_run_id", "") or getattr(handle, "first_execution_run_id", "")
    return {
        "backend": "temporal",
        "workflow_id": handle.id,
        "run_id": run_id,
        "task_queue": cfg.temporal_task_queue,
        "namespace": cfg.temporal_namespace,
        "temporal_address": cfg.temporal_address,
    }


def start_reprocess_workflow(
    job_id: str,
    payload: dict[str, Any],
    *,
    settings_obj: Any = settings,
) -> dict[str, Any]:
    """Submit a RAG reprocess workflow and return Temporal tracking data."""
    return asyncio.run(_start_reprocess_workflow_async(job_id, payload, settings_obj=settings_obj))


def _status_name(raw_status: Any) -> str:
    name = getattr(raw_status, "name", "")
    if name:
        return str(name).lower()
    return str(raw_status).rsplit(".", 1)[-1].strip().lower()


def _map_temporal_status(raw_status: Any) -> str:
    status = _status_name(raw_status)
    if status == "completed":
        return "completed"
    if status in {"failed", "canceled", "terminated", "timed_out"}:
        return "failed"
    if status == "continued_as_new":
        return "submitted"
    return "running"


async def _describe_reprocess_workflow_async(
    workflow_id: str,
    run_id: str = "",
    *,
    settings_obj: Any,
) -> dict[str, Any]:
    try:
        from temporalio.client import Client
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise _temporal_unavailable_error() from exc

    cfg = settings_obj.workflows
    client = await Client.connect(
        cfg.temporal_address,
        namespace=cfg.temporal_namespace,
    )
    handle = client.get_workflow_handle(workflow_id, run_id=run_id or None)
    description = await handle.describe()
    temporal_status = _status_name(getattr(description, "status", "unknown"))
    return {
        "status": _map_temporal_status(getattr(description, "status", "unknown")),
        "temporal_status": temporal_status,
    }


def describe_reprocess_workflow(
    workflow_id: str,
    run_id: str = "",
    *,
    settings_obj: Any = settings,
) -> dict[str, Any]:
    """Return a non-blocking status summary for a submitted RAG workflow."""
    return asyncio.run(
        _describe_reprocess_workflow_async(workflow_id, run_id=run_id, settings_obj=settings_obj)
    )
