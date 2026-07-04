"""Async job queue with configurable concurrency control."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class JobQueue:
    """Simple async job queue with GPU concurrency control.

    Architecture:
    - One queue for incoming jobs
    - Semaphore controls max concurrent GPU transcriptions (default: 1)
    - CPU preprocessing can run concurrently with GPU work
    - Prepared for future Redis/Celery swap (interface-compatible)
    """

    def __init__(self, max_concurrent_jobs: int = 1, max_gpu_workers: int = 1):
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._max_concurrent = max_concurrent_jobs
        self._gpu_semaphore = asyncio.Semaphore(max_gpu_workers)
        self._job_semaphore = asyncio.Semaphore(max_concurrent_jobs)
        self._workers: list[asyncio.Task[Any]] = []
        self._processing: set[str] = set()
        self._running = False
        self._process_fn: Callable[[str], Coroutine[Any, Any, None]] | None = None

    @property
    def gpu_semaphore(self) -> asyncio.Semaphore:
        """Semaphore for GPU transcription concurrency control."""
        return self._gpu_semaphore

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    @property
    def is_running(self) -> bool:
        return self._running

    def stats(self) -> dict[str, Any]:
        """Return queue operational stats for health/metrics."""
        return {
            "backend": "memory",
            "pending": self._queue.qsize(),
            "processing": len(self._processing),
            "dead_letter": 0,
            "running": self._running,
        }

    def set_processor(self, fn: Callable[[str], Coroutine[Any, Any, None]]) -> None:
        """Set the async function that processes a job_id."""
        self._process_fn = fn

    async def enqueue(self, job_id: str) -> None:
        """Add a job to the queue."""
        await self._queue.put(job_id)
        logger.info(f"Job {job_id} enqueued (queue size: {self._queue.qsize()})")

    async def start(self, num_workers: int = 1) -> None:
        """Start queue workers."""
        if self._running:
            return
        self._running = True
        for i in range(num_workers):
            task = asyncio.create_task(self._worker(i))
            self._workers.append(task)
        logger.info(f"Job queue started with {num_workers} worker(s)")

    async def stop(self) -> None:
        """Stop all workers gracefully."""
        self._running = False
        # Put sentinel values to unblock workers
        for _ in self._workers:
            await self._queue.put("")
        for worker in self._workers:
            worker.cancel()
        self._workers.clear()
        logger.info("Job queue stopped")

    async def _worker(self, worker_id: int) -> None:
        """Worker loop: pull jobs from queue and process them."""
        logger.debug(f"Worker {worker_id} started")
        while self._running:
            try:
                job_id = await self._queue.get()
                if not job_id:  # Sentinel
                    break
                if self._process_fn is None:
                    logger.error("No processor function set")
                    self._queue.task_done()
                    continue
                async with self._job_semaphore:
                    self._processing.add(job_id)
                    try:
                        await self._process_fn(job_id)
                    except Exception as e:
                        logger.error(f"Worker {worker_id} error processing {job_id}: {e}")
                    finally:
                        self._processing.discard(job_id)
                        self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} unexpected error: {e}")


# Module singleton
_queue: JobQueue | None = None
_redis_queue = None  # RedisJobQueue | None


def get_queue():
    """Get or create the global job queue.

    Uses Redis-backed queue if AUDIO_TRANSCRIBE_REDIS_URL is set,
    otherwise falls back to in-memory asyncio queue.
    """
    global _queue, _redis_queue
    import os

    redis_url = os.environ.get("AUDIO_TRANSCRIBE_REDIS_URL", "")
    if redis_url and _redis_queue is None:
        try:
            from audio_transcribe.config import get_config
            from audio_transcribe.redis_queue import RedisJobQueue

            cfg = get_config()
            _redis_queue = RedisJobQueue(
                redis_url=redis_url,
                max_concurrent_jobs=cfg.jobs.max_concurrent_jobs,
                max_gpu_workers=cfg.performance.max_concurrent_gpu_transcriptions,
                retry_attempts=cfg.jobs.redis_retry_attempts,
                processing_timeout_seconds=cfg.jobs.redis_processing_timeout_seconds,
            )
            logger.info("Using Redis-backed job queue: %s", redis_url)
            return _redis_queue
        except ImportError:
            logger.warning("redis.asyncio not available — falling back to in-memory queue")
        except Exception as exc:
            logger.warning("Redis queue init failed (%s) — falling back to in-memory queue", exc)

    if _redis_queue is not None:
        return _redis_queue

    if _queue is None:
        from audio_transcribe.config import get_config

        cfg = get_config()
        _queue = JobQueue(
            max_concurrent_jobs=cfg.jobs.max_concurrent_jobs,
            max_gpu_workers=cfg.performance.max_concurrent_gpu_transcriptions,
        )
    return _queue


def reset_queue() -> None:
    """Reset queue singleton (for testing)."""
    global _queue, _redis_queue
    _queue = None
    _redis_queue = None
