"""Local async queue for extrator jobs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from extrator.config import get_config

log = logging.getLogger(__name__)


class LocalJobQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task] = []
        self._processor: Callable[[str], None] | None = None

    def set_processor(self, processor: Callable[[str], None]) -> None:
        self._processor = processor

    async def start(self) -> None:
        cfg = get_config()
        self._queue = asyncio.Queue(maxsize=cfg.jobs.max_queued_jobs)
        for index in range(cfg.jobs.max_concurrent_jobs):
            self._workers.append(asyncio.create_task(self._worker(index)))

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def enqueue(self, job_id: str) -> None:
        if self._queue is None:
            raise RuntimeError("Job queue is not running")
        self._queue.put_nowait(job_id)

    async def _worker(self, index: int) -> None:
        if self._processor is None:
            raise RuntimeError("Job processor is not configured")
        assert self._queue is not None
        while True:
            job_id = await self._queue.get()
            try:
                await asyncio.to_thread(self._processor, job_id)
            except Exception:
                log.exception("Extrator job worker %s failed for job %s", index, job_id)
            finally:
                self._queue.task_done()


_queue = LocalJobQueue()


def get_queue() -> LocalJobQueue:
    return _queue
