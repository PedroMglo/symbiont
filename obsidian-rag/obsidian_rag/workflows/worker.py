"""Temporal worker for RAG-owned workflows."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from obsidian_rag.config import settings
from obsidian_rag.workflows.temporal_client import _temporal_unavailable_error


async def run_worker() -> None:
    try:
        from temporalio.client import Client
        from temporalio.worker import Worker
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise _temporal_unavailable_error() from exc

    from obsidian_rag.workflows.reprocess import RagReprocessWorkflow, run_reprocess_activity

    if RagReprocessWorkflow is None or run_reprocess_activity is None:  # pragma: no cover - defensive guard
        raise _temporal_unavailable_error()

    cfg = settings.workflows
    client = await Client.connect(
        cfg.temporal_address,
        namespace=cfg.temporal_namespace,
    )
    activity_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-temporal-activity")
    try:
        worker = Worker(
            client,
            task_queue=cfg.temporal_task_queue,
            workflows=[RagReprocessWorkflow],
            activities=[run_reprocess_activity],
            activity_executor=activity_executor,
        )
        await worker.run()
    finally:
        activity_executor.shutdown(wait=False, cancel_futures=True)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
