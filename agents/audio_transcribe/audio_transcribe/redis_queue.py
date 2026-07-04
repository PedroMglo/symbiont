"""Redis-backed job queue — drop-in replacement for asyncio.Queue-based JobQueue.

Uses redis.asyncio for non-blocking operations. Falls back to the in-memory
asyncio queue if Redis is unavailable (graceful degradation).

Config: Set AUDIO_TRANSCRIBE_REDIS_URL env var or redis_url in [jobs] config.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Redis key constants (configurable via constructor)
_DEFAULT_QUEUE_KEY = "audio_transcribe:jobs"
_DEFAULT_PROCESSING_KEY = "audio_transcribe:processing"
_DEFAULT_RESULTS_KEY = "audio_transcribe:results"
_DEFAULT_DEAD_LETTER_KEY = "audio_transcribe:dead_letter"


class RedisJobQueue:
    """Redis-backed async job queue with GPU concurrency control.

    Interface-compatible with JobQueue (same methods: enqueue, start, stop,
    set_processor, pending_count, is_running, gpu_semaphore).

    Architecture:
    - Redis LIST as the job queue (LPUSH/BRPOP)
    - Redis HASH for job status tracking
    - Local asyncio.Semaphore for GPU concurrency (single-node)
    - Automatic reconnection on Redis failure
    """

    def __init__(
        self,
        redis_url: str,
        *,
        max_concurrent_jobs: int,
        max_gpu_workers: int,
        queue_key: str = _DEFAULT_QUEUE_KEY,
        processing_key: str = _DEFAULT_PROCESSING_KEY,
        results_key: str = _DEFAULT_RESULTS_KEY,
        dead_letter_key: str = _DEFAULT_DEAD_LETTER_KEY,
        retry_attempts: int = 2,
        processing_timeout_seconds: int = 3600,
    ):
        self._redis_url = redis_url
        self._queue_key = queue_key
        self._processing_key = processing_key
        self._results_key = results_key
        self._dead_letter_key = dead_letter_key
        self._retry_attempts = max(0, retry_attempts)
        self._processing_timeout_seconds = max(1, processing_timeout_seconds)
        self._max_concurrent = max_concurrent_jobs
        self._gpu_semaphore = asyncio.Semaphore(max_gpu_workers)
        self._job_semaphore = asyncio.Semaphore(max_concurrent_jobs)
        self._workers: list[asyncio.Task[Any]] = []
        self._running = False
        self._process_fn: Callable[[str], Coroutine[Any, Any, None]] | None = None
        self._redis: Any = None  # redis.asyncio.Redis instance

    @property
    def gpu_semaphore(self) -> asyncio.Semaphore:
        """Semaphore for GPU transcription concurrency control."""
        return self._gpu_semaphore

    @property
    def pending_count(self) -> int:
        """Approximate queue length (sync — may be stale)."""
        # For async-accurate count, use pending_count_async()
        return 0  # Cannot do sync Redis call; use pending_count_async

    @property
    def is_running(self) -> bool:
        return self._running

    async def pending_count_async(self) -> int:
        """Get exact pending job count from Redis."""
        if self._redis is None:
            return 0
        try:
            return await self._redis.llen(self._queue_key)
        except Exception:
            return 0

    async def stats_async(self) -> dict[str, Any]:
        """Return queue operational stats without mutating queue state."""
        if self._redis is None:
            return {
                "backend": "redis",
                "connected": False,
                "pending": 0,
                "processing": 0,
                "dead_letter": 0,
                "running": self._running,
            }
        try:
            pending, processing, dead_letter = await asyncio.gather(
                self._redis.llen(self._queue_key),
                self._redis.hlen(self._processing_key),
                self._redis.llen(self._dead_letter_key),
            )
            return {
                "backend": "redis",
                "connected": True,
                "pending": int(pending),
                "processing": int(processing),
                "dead_letter": int(dead_letter),
                "running": self._running,
            }
        except Exception as exc:
            logger.warning("RedisJobQueue stats unavailable: %s", exc)
            return {
                "backend": "redis",
                "connected": False,
                "pending": 0,
                "processing": 0,
                "dead_letter": 0,
                "running": self._running,
            }

    async def _connect(self) -> None:
        """Establish Redis connection."""
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=10,
                socket_keepalive=True,
                health_check_interval=30,
            )
            await self._redis.ping()
            await self._ensure_key_types()
            logger.info("RedisJobQueue connected to %s", self._redis_url)
        except Exception as exc:
            logger.error("RedisJobQueue: failed to connect to Redis: %s", exc)
            self._redis = None
            raise

    async def _ensure_key_types(self) -> None:
        if self._redis is None:
            return
        expected = {
            self._queue_key: "list",
            self._processing_key: "hash",
            self._results_key: "hash",
            self._dead_letter_key: "list",
        }
        for key, expected_type in expected.items():
            try:
                observed = await self._redis.type(key)
            except Exception as exc:
                logger.warning("RedisJobQueue could not inspect key type for %s: %s", key, exc)
                continue
            observed_type = observed.decode("utf-8") if isinstance(observed, bytes) else str(observed)
            if observed_type in {"none", expected_type}:
                continue
            backup_key = f"{key}:legacy:{int(time.time())}"
            try:
                await self._redis.rename(key, backup_key)
                logger.warning(
                    "RedisJobQueue preserved incompatible key %s type=%s expected=%s as %s",
                    key,
                    observed_type,
                    expected_type,
                    backup_key,
                )
            except Exception as exc:
                logger.error(
                    "RedisJobQueue could not preserve incompatible key %s type=%s expected=%s: %s",
                    key,
                    observed_type,
                    expected_type,
                    exc,
                )
                raise

    def set_processor(self, fn: Callable[[str], Coroutine[Any, Any, None]]) -> None:
        """Set the async function that processes a job_id."""
        self._process_fn = fn

    async def enqueue(self, job_id: str) -> None:
        """Add a job to the Redis queue."""
        if self._redis is None:
            await self._connect()
        try:
            payload = json.dumps({"job_id": job_id, "enqueued_at": time.time(), "attempts": 0})
            await self._redis.lpush(self._queue_key, payload)
            logger.info("Job %s enqueued to Redis (key=%s)", job_id, self._queue_key)
        except Exception as exc:
            logger.error("RedisJobQueue: enqueue failed: %s", exc)
            raise

    async def start(self, num_workers: int = 1) -> None:
        """Start queue workers that consume from Redis."""
        if self._running:
            return
        if self._redis is None:
            await self._connect()
        self._running = True
        for i in range(num_workers):
            task = asyncio.create_task(self._worker(i))
            self._workers.append(task)
        logger.info("RedisJobQueue started with %d worker(s)", num_workers)

    async def stop(self) -> None:
        """Stop all workers gracefully."""
        self._running = False
        for worker in self._workers:
            worker.cancel()
        self._workers.clear()
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        logger.info("RedisJobQueue stopped")

    async def _worker(self, worker_id: int) -> None:
        """Worker loop: BRPOP from Redis and process jobs."""
        logger.debug("Redis worker %d started", worker_id)
        while self._running:
            try:
                if self._redis is None:
                    await self._connect()
                # BRPOP blocks until an item is available (timeout 5s to check _running)
                try:
                    result = await self._redis.brpop(self._queue_key, timeout=5)
                except Exception as exc:
                    if exc.__class__.__name__ == "TimeoutError" or "Timeout reading from redis" in str(exc):
                        logger.debug("Redis worker %d BRPOP timed out waiting for jobs", worker_id)
                        continue
                    raise
                if result is None:
                    continue  # Timeout — loop back to check _running
                _, payload = result
                data = json.loads(payload)
                job_id = data["job_id"]
                attempts = int(data.get("attempts") or 0)

                if self._process_fn is None:
                    logger.error("No processor function set")
                    await self._dead_letter(job_id, data, "No processor function set")
                    continue

                # Track processing state
                await self._redis.hset(
                    self._processing_key, job_id,
                    json.dumps({"started_at": time.time(), "worker": worker_id, "payload": data}),
                )

                async with self._job_semaphore:
                    try:
                        await self._process_fn(job_id)
                        # Mark completed
                        await self._redis.hdel(self._processing_key, job_id)
                        await self._redis.hset(
                            self._results_key, job_id,
                            json.dumps({"status": "completed", "finished_at": time.time()}),
                        )
                    except Exception as e:
                        logger.error("Redis worker %d error processing %s: %s", worker_id, job_id, e)
                        await self._redis.hdel(self._processing_key, job_id)
                        await self._retry_or_dead_letter(job_id, data, attempts, str(e)[:200])
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Redis worker %d unexpected error: %s", worker_id, e)
                await asyncio.sleep(2)  # Backoff before reconnect attempt
                self._redis = None  # Force reconnect

    async def get_job_status(self, job_id: str) -> str | None:
        """Check job status from Redis. Returns 'pending', 'processing', 'completed', or 'failed'."""
        if self._redis is None:
            return None
        try:
            # Check results first
            result = await self._redis.hget(self._results_key, job_id)
            if result:
                data = json.loads(result)
                return data.get("status")
            # Check processing
            if await self._redis.hexists(self._processing_key, job_id):
                return "processing"
            return "pending"
        except Exception:
            return None

    async def recover_stale_processing(self, timeout_seconds: int | None = None) -> dict[str, int]:
        """Requeue or dead-letter jobs stuck in the processing hash."""
        if self._redis is None:
            await self._connect()
        timeout = timeout_seconds or self._processing_timeout_seconds
        now = time.time()
        recovered = 0
        dead_lettered = 0
        try:
            entries = await self._redis.hgetall(self._processing_key)
        except Exception as exc:
            logger.warning("RedisJobQueue processing recovery failed: %s", exc)
            return {"recovered": 0, "dead_lettered": 0}

        for job_id, raw in entries.items():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await self._redis.hdel(self._processing_key, job_id)
                dead_lettered += 1
                continue
            started_at = float(data.get("started_at") or 0)
            if now - started_at < timeout:
                continue
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {"job_id": job_id, "attempts": 0}
            attempts = int(payload.get("attempts") or 0)
            await self._redis.hdel(self._processing_key, job_id)
            if attempts < self._retry_attempts:
                payload["attempts"] = attempts + 1
                payload["recovered_at"] = now
                await self._redis.lpush(self._queue_key, json.dumps(payload))
                recovered += 1
            else:
                await self._dead_letter(str(job_id), payload, "processing timeout")
                dead_lettered += 1
        return {"recovered": recovered, "dead_lettered": dead_lettered}

    async def _retry_or_dead_letter(self, job_id: str, data: dict[str, Any], attempts: int, error: str) -> None:
        if attempts < self._retry_attempts:
            data["attempts"] = attempts + 1
            data["last_error"] = error
            data["retried_at"] = time.time()
            await self._redis.lpush(self._queue_key, json.dumps(data))
            await self._redis.hset(
                self._results_key,
                job_id,
                json.dumps({"status": "retrying", "error": error, "attempts": attempts + 1}),
            )
            return
        await self._dead_letter(job_id, data, error)

    async def _dead_letter(self, job_id: str, data: dict[str, Any], error: str) -> None:
        entry = {
            "job_id": job_id,
            "payload": data,
            "error": error,
            "dead_lettered_at": time.time(),
        }
        await self._redis.lpush(self._dead_letter_key, json.dumps(entry))
        await self._redis.hset(
            self._results_key,
            job_id,
            json.dumps({"status": "dead_lettered", "error": error, "finished_at": time.time()}),
        )
