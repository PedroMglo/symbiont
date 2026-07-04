"""Unified Audio Intelligence Platform — API Gateway.

Endpoints:
- WebSocket /ws/stream — Real-time audio streaming (mic input → partial transcripts)
- GET /stream/{job_id} — SSE streaming for batch job progress
- GET /stream/{job_id}/segments — Snapshot of completed segments
- GET /jobs/active — List active jobs/sessions
- GET /metrics — Platform observability metrics
- GET /health — Health check
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from starlette.responses import StreamingResponse

from streaming.config import get_config
from streaming.monitor import JobMonitor, StreamEvent
from streaming.observability.metrics import get_metrics_collector
from streaming.security import verify_api_key, verify_websocket_api_key
from streaming.types import RealtimeStreamConfig, RealtimeTranscriptEvent

logger = logging.getLogger(__name__)


class _RealtimeResultTracker:
    """Tracks final STT results that must be forwarded before closing."""

    def __init__(self) -> None:
        self._expected_segments: set[str] = set()
        self._final_segments: set[str] = set()
        self._all_seen = asyncio.Event()
        self._all_seen.set()

    def track_segment_submission(self, event: dict[str, Any]) -> None:
        if event.get("type") != "segment_submitted":
            return
        segment_key = self._segment_key(event)
        if not segment_key:
            return
        self._expected_segments.add(segment_key)
        if not self._expected_segments.issubset(self._final_segments):
            self._all_seen.clear()

    def track_result(self, result: dict[str, Any]) -> None:
        if result.get("type") != "final_transcript":
            return
        segment_key = self._segment_key(result)
        if segment_key:
            self._final_segments.add(segment_key)
        if self._expected_segments.issubset(self._final_segments):
            self._all_seen.set()

    async def wait_for_pending(self, timeout_seconds: float) -> bool:
        if self._expected_segments.issubset(self._final_segments):
            return True
        try:
            await asyncio.wait_for(self._all_seen.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return False
        return True

    @staticmethod
    def _segment_key(event: dict[str, Any]) -> str:
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            segment_id = metadata.get("segment_id")
            if segment_id:
                return str(segment_id)
        segment_id = event.get("segment_id")
        if segment_id:
            return str(segment_id)
        segment_index = event.get("segment_index")
        return str(segment_index) if segment_index is not None else ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start GPU worker and event bus on startup."""
    cfg = get_config()
    logging.basicConfig(
        level=getattr(logging, cfg.server.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Initialize event bus
    from streaming.event_bus.redis_streams import get_event_bus
    event_bus = get_event_bus()
    try:
        await event_bus.initialize()
        logger.info("Event bus connected (Redis Streams)")
    except Exception as e:
        logger.warning(f"Event bus initialization failed (non-critical): {e}")

    # Start GPU worker (if configured and model available)
    gpu_worker = None
    if cfg.gpu.max_workers > 0:
        try:
            from streaming.workers.gpu_pool import GPUWorker
            gpu_worker = GPUWorker(worker_id="gpu-worker-0")
            await gpu_worker.start()
        except Exception as e:
            logger.warning(f"GPU worker start failed (batch-only mode): {e}")
            gpu_worker = None

    app.state.gpu_worker = gpu_worker

    yield

    # Shutdown
    if gpu_worker:
        await gpu_worker.stop()
    await event_bus.close()
    logger.info("Unified Audio Platform shutting down")


app = FastAPI(
    title="Audio Intelligence Platform",
    version="2.0.0",
    description="Unified real-time + batch audio transcription",
    lifespan=lifespan,
)


# =============================================================================
# Health & Metrics
# =============================================================================


@app.get("/health")
async def health():
    """Health check."""
    collector = get_metrics_collector()
    return {
        "status": "ok",
        "service": "audio_intelligence_platform",
        "version": "2.0.0",
        "uptime_seconds": round(collector.get_uptime(), 0),
        "active_sessions": collector.metrics.active_sessions,
    }


@app.get("/metrics", dependencies=[Depends(verify_api_key)])
async def metrics():
    """Full platform metrics."""
    collector = get_metrics_collector()
    result = collector.metrics.to_dict()

    # Add GPU worker metrics if available
    if hasattr(app.state, "gpu_worker") and app.state.gpu_worker:
        result["gpu_worker"] = app.state.gpu_worker.metrics

    return result


# =============================================================================
# REAL-TIME: WebSocket Streaming
# =============================================================================


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    """Real-time audio streaming via WebSocket.

    Protocol:
    1. Client connects and sends config JSON: {"language": "pt", "sample_rate": 16000}
    2. Client sends binary PCM frames (16-bit LE mono, 16kHz)
    3. Server sends JSON transcript events back
    4. Client sends text "END" to close session

    Transcript events:
    - {"type": "session_started", "session_id": "..."}
    - {"type": "segment_submitted", "segment_index": N, "duration": X}
    - {"type": "partial_transcript", "text": "...", "final": false}
    - {"type": "final_transcript", "text": "...", "final": true}
    - {"type": "session_closed", "segments_total": N}
    """
    if not await verify_websocket_api_key(websocket):
        return
    await websocket.accept()
    collector = get_metrics_collector()

    # Create session
    from streaming.realtime.engine import StreamEngine, get_session_manager

    session_mgr = get_session_manager()

    try:
        # Wait for config message
        config_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        config = RealtimeStreamConfig.model_validate(json.loads(config_msg))
        language = config.language

        session = await session_mgr.create_session(language=language)
        session.sample_rate = config.sample_rate
        engine = StreamEngine(session)
        collector.record_session_start()

        await websocket.send_json(
            RealtimeTranscriptEvent(
                type="session_started",
                session_id=session.session_id,
                metadata={"sample_rate": config.sample_rate, "channels": config.channels},
            ).model_dump(exclude_none=True)
        )

        logger.info(f"Stream session started: {session.session_id} (lang={language})")

        # Subscribe to results for this session
        from streaming.event_bus.redis_streams import get_event_bus
        event_bus = get_event_bus()
        result_tracker = _RealtimeResultTracker()

        # Background task: listen for transcription results and forward to client
        result_task = asyncio.create_task(
            _forward_results(websocket, event_bus, session.session_id, on_result=result_tracker.track_result)
        )

        # Main loop: receive audio frames
        try:
            while True:
                message = await websocket.receive()

                if message.get("type") == "websocket.disconnect":
                    break

                if "bytes" in message:
                    # Binary frame: PCM audio data
                    frame_data = message["bytes"]
                    session.append_audio(frame_data)

                    # Process through VAD + segmentation
                    event = await engine.process_frame(frame_data)
                    if event:
                        result_tracker.track_segment_submission(event)
                        await websocket.send_json(
                            RealtimeTranscriptEvent.model_validate(event).model_dump(exclude_none=True)
                        )

                elif "text" in message:
                    text = message["text"]
                    if text.upper() == "END":
                        break
                    # Config update
                    try:
                        update = json.loads(text)
                        if "language" in update:
                            session.language = update["language"]
                    except json.JSONDecodeError:
                        pass

        except WebSocketDisconnect:
            pass

        # Flush remaining audio
        final_event = await engine.flush()
        if final_event:
            result_tracker.track_segment_submission(final_event)
            try:
                await websocket.send_json(final_event)
            except Exception:
                pass

        if not await result_tracker.wait_for_pending(get_config().realtime.final_result_timeout_seconds):
            await websocket.send_json(
                RealtimeTranscriptEvent(
                    type="stream_warning",
                    session_id=session.session_id,
                    message="Timed out waiting for final realtime transcript",
                ).model_dump(exclude_none=True)
            )

        # Close session
        result_task.cancel()
        await session_mgr.close_session(session.session_id)
        collector.record_session_end()

        try:
            await websocket.send_json(
                RealtimeTranscriptEvent(
                    type="session_closed",
                    session_id=session.session_id,
                    duration=round(session.total_audio_duration, 1),
                    metadata={"segments_total": session.segments_completed},
                ).model_dump(exclude_none=True)
            )
        except Exception:
            pass

        logger.info(
            f"Stream session closed: {session.session_id} "
            f"({session.segments_completed} segments, "
            f"{session.total_audio_duration:.1f}s audio)"
        )

    except asyncio.TimeoutError:
        await websocket.close(code=4001, reason="Config timeout")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        try:
            await websocket.close(code=4000, reason=str(e)[:100])
        except Exception:
            pass


async def _forward_results(
    websocket: WebSocket,
    event_bus,
    session_id: str,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Background task: forward transcription results to WebSocket client."""
    try:
        async for result in event_bus.subscribe_results(session_id):
            try:
                await websocket.send_json(result)
                if on_result is not None:
                    on_result(result)
                logger.debug(f"Forwarded result to {session_id[:8]}: {str(result)[:60]}")
            except Exception as e:
                logger.warning(f"WebSocket send failed for {session_id[:8]}: {e}")
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Result forwarding error for {session_id[:8]}: {e}")


# =============================================================================
# BATCH: SSE Streaming (job progress + segments)
# =============================================================================


@app.get("/stream/{job_id}", dependencies=[Depends(verify_api_key)])
async def stream_job(
    job_id: str,
    include_text: bool = Query(default=True, description="Include segment text in events"),
):
    """Stream batch job progress and partial results via SSE."""
    monitor = JobMonitor(job_id)

    if not monitor.job_exists():
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    async def event_generator() -> AsyncIterator[str]:
        async for event in monitor.watch(include_text=include_text):
            yield _format_sse(event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/stream/{job_id}/segments", dependencies=[Depends(verify_api_key)])
async def get_segments(job_id: str):
    """Get all completed segments for a job (snapshot)."""
    monitor = JobMonitor(job_id)
    if not monitor.job_exists():
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    segments = monitor.get_completed_segments()
    return {"job_id": job_id, "segments": segments, "count": len(segments)}


@app.get("/jobs/active", dependencies=[Depends(verify_api_key)])
async def list_active():
    """List active jobs and streaming sessions."""
    monitor = JobMonitor("")
    jobs = monitor.list_active_jobs()

    from streaming.realtime.engine import get_session_manager
    sessions = get_session_manager().list_sessions()

    return {
        "batch_jobs": jobs,
        "streaming_sessions": sessions,
        "total_active": len(jobs) + len(sessions),
    }


# =============================================================================
# Helpers
# =============================================================================


def _format_sse(event: StreamEvent) -> str:
    """Format a StreamEvent as SSE text."""
    data = json.dumps(event.data, ensure_ascii=False)
    return f"event: {event.event_type}\ndata: {data}\n\n"
