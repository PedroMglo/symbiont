"""Redis Streams event bus for unified audio pipeline.

Streams:
- audio.stream.segment — real-time speech segments awaiting ASR
- audio.batch.chunk — batch file chunks awaiting ASR
- audio.transcription.partial — partial transcription results
- audio.transcription.final — finalized transcription results
- audio.session.closed — session lifecycle events

Architecture:
- Producers: stream engine, batch pipeline
- Consumers: GPU workers (consumer group)
- Result delivery: pub/sub back to session owner
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

from streaming.config import get_config

logger = logging.getLogger(__name__)

# Stream names
STREAM_REALTIME = "audio.stream.segment"
STREAM_BATCH = "audio.batch.chunk"
STREAM_PARTIAL = "audio.transcription.partial"
STREAM_FINAL = "audio.transcription.final"
STREAM_SESSION = "audio.session.closed"

# Consumer group
WORKER_GROUP = "gpu-workers"

_bus: "EventBus | None" = None


def get_event_bus() -> "EventBus":
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


class EventBus:
    """Redis Streams-based event bus for the audio pipeline."""

    def __init__(self):
        cfg = get_config()
        self._redis: aioredis.Redis | None = None
        self._redis_url = cfg.redis.url
        self._max_len = cfg.redis.max_stream_len
        self._initialized = False

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=False,  # Binary for audio data
                socket_connect_timeout=3,
                socket_keepalive=True,
            )
        return self._redis

    async def initialize(self) -> None:
        """Create consumer groups if they don't exist."""
        if self._initialized:
            return
        r = await self._get_redis()
        for stream in (STREAM_REALTIME, STREAM_BATCH):
            try:
                await r.xgroup_create(stream, WORKER_GROUP, id="0", mkstream=True)
            except aioredis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
        self._initialized = True
        logger.info("Event bus initialized (Redis Streams)")

    # =========================================================================
    # PRODUCERS
    # =========================================================================

    async def publish_segment(
        self,
        session_id: str,
        segment_id: str,
        audio_data: bytes,
        segment_index: int,
        duration: float,
        language: str = "auto",
        priority: str = "realtime",
    ) -> str:
        """Publish a speech segment for GPU transcription.

        Real-time segments go to STREAM_REALTIME (higher priority).
        Returns the stream entry ID.
        """
        r = await self._get_redis()
        stream = STREAM_REALTIME if priority == "realtime" else STREAM_BATCH

        entry = {
            b"session_id": session_id.encode(),
            b"segment_id": segment_id.encode(),
            b"audio": base64.b64encode(audio_data),
            b"index": str(segment_index).encode(),
            b"duration": str(duration).encode(),
            b"language": language.encode(),
            b"priority": priority.encode(),
            b"timestamp": str(time.time()).encode(),
        }

        entry_id = await r.xadd(stream, entry, maxlen=self._max_len)
        logger.debug(f"Published segment {segment_id} to {stream} [{entry_id}]")
        return entry_id.decode() if isinstance(entry_id, bytes) else entry_id

    async def publish_batch_chunk(
        self,
        job_id: str,
        chunk_index: int,
        audio_data: bytes,
        duration: float,
        start_time: float,
        language: str = "auto",
    ) -> str:
        """Publish a batch chunk for GPU transcription."""
        r = await self._get_redis()

        entry = {
            b"session_id": job_id.encode(),
            b"segment_id": f"{job_id}:{chunk_index}".encode(),
            b"audio": base64.b64encode(audio_data),
            b"index": str(chunk_index).encode(),
            b"duration": str(duration).encode(),
            b"start_time": str(start_time).encode(),
            b"language": language.encode(),
            b"priority": b"batch",
            b"timestamp": str(time.time()).encode(),
        }

        entry_id = await r.xadd(STREAM_BATCH, entry, maxlen=self._max_len)
        return entry_id.decode() if isinstance(entry_id, bytes) else entry_id

    async def publish_result(
        self,
        session_id: str,
        segment_id: str,
        text: str,
        is_final: bool,
        language: str = "",
        confidence: float = 0.0,
        start: float = 0.0,
        end: float = 0.0,
    ) -> None:
        """Publish a transcription result (partial or final)."""
        r = await self._get_redis()
        stream = STREAM_FINAL if is_final else STREAM_PARTIAL

        entry = {
            b"session_id": session_id.encode(),
            b"segment_id": segment_id.encode(),
            b"text": text.encode("utf-8"),
            b"final": b"1" if is_final else b"0",
            b"language": language.encode(),
            b"confidence": str(confidence).encode(),
            b"start": str(start).encode(),
            b"end": str(end).encode(),
            b"timestamp": str(time.time()).encode(),
        }

        await r.xadd(stream, entry, maxlen=self._max_len)

        # Also publish to session-specific channel for immediate delivery
        channel = f"transcription:{session_id}"
        await r.publish(channel, json.dumps({
            "type": "partial_transcript" if not is_final else "final_transcript",
            "session_id": session_id,
            "segment_id": segment_id,
            "text": text,
            "final": is_final,
            "language": language,
            "timestamp": time.time(),
        }).encode("utf-8"))

    # =========================================================================
    # CONSUMERS (for GPU workers)
    # =========================================================================

    async def consume_segments(
        self,
        worker_id: str,
        batch_size: int = 1,
        block_ms: int = 1000,
    ) -> list[dict[str, Any]]:
        """Consume segments from both realtime and batch streams.

        Real-time stream is read first (priority).
        Returns list of segment dicts ready for transcription.
        """
        r = await self._get_redis()
        segments: list[dict[str, Any]] = []

        # Priority 1: real-time segments
        try:
            entries = await r.xreadgroup(
                WORKER_GROUP, worker_id,
                {STREAM_REALTIME: ">"},
                count=batch_size,
                block=block_ms,
            )
            for stream_name, messages in entries:
                for msg_id, fields in messages:
                    segments.append(self._decode_segment(msg_id, fields, "realtime"))
                    await r.xack(STREAM_REALTIME, WORKER_GROUP, msg_id)
        except Exception as e:
            if "No such key" not in str(e):
                logger.debug(f"Realtime read error: {e}")

        # Priority 2: batch chunks (only if no realtime work)
        if not segments:
            try:
                entries = await r.xreadgroup(
                    WORKER_GROUP, worker_id,
                    {STREAM_BATCH: ">"},
                    count=batch_size,
                    block=block_ms,
                )
                for stream_name, messages in entries:
                    for msg_id, fields in messages:
                        segments.append(self._decode_segment(msg_id, fields, "batch"))
                        await r.xack(STREAM_BATCH, WORKER_GROUP, msg_id)
            except Exception as e:
                if "No such key" not in str(e):
                    logger.debug(f"Batch read error: {e}")

        return segments

    # =========================================================================
    # SUBSCRIBE (for result delivery to WebSocket clients)
    # =========================================================================

    async def subscribe_results(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to transcription results for a specific session.

        Uses get_message() polling loop instead of listen() for reliability.
        The listen() approach can silently drop messages after periods of inactivity.
        """
        _r = await self._get_redis()
        # Use a SEPARATE redis client for pub/sub to avoid shared connection issues
        pubsub_client = aioredis.from_url(
            self._redis_url,
            decode_responses=False,
            socket_connect_timeout=3,
            socket_keepalive=True,
        )
        pubsub = pubsub_client.pubsub()
        channel = f"transcription:{session_id}"

        await pubsub.subscribe(channel)
        logger.debug(f"Subscribed to {channel}")
        try:
            while True:
                # get_message with timeout avoids blocking the event loop
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=0.1,
                )
                if msg is not None and msg["type"] == "message":
                    try:
                        data = json.loads(msg["data"])
                        yield data
                    except Exception as e:
                        logger.warning(f"subscribe_results decode error: {e}")
                else:
                    # No message — yield control to event loop
                    await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"subscribe_results error: {e}")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await pubsub_client.aclose()

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _decode_segment(self, msg_id: Any, fields: dict, priority: str) -> dict[str, Any]:
        """Decode a raw Redis stream entry into a segment dict."""
        return {
            "msg_id": msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
            "session_id": fields.get(b"session_id", b"").decode(),
            "segment_id": fields.get(b"segment_id", b"").decode(),
            "audio": base64.b64decode(fields.get(b"audio", b"")),
            "index": int(fields.get(b"index", b"0")),
            "duration": float(fields.get(b"duration", b"0")),
            "language": fields.get(b"language", b"auto").decode(),
            "priority": priority,
            "timestamp": float(fields.get(b"timestamp", b"0")),
        }

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None
