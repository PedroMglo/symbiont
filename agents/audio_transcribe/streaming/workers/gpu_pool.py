"""Unified GPU Worker — consumes segments from event bus and transcribes.

Single worker = single GPU model instance.
Processes both real-time and batch segments with priority scheduling.
Real-time segments are ALWAYS processed first.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import wave
from typing import TYPE_CHECKING, Any

from streaming.config import get_config
from streaming.event_bus.redis_streams import get_event_bus

if TYPE_CHECKING:
    from streaming.event_bus.redis_streams import EventBus

logger = logging.getLogger(__name__)

_transcriber: "WhisperTranscriber | None" = None


class WhisperTranscriber:
    """faster-whisper transcription engine (GPU optimized).

    Keeps model loaded in VRAM — no reload per segment.
    Supports both short segments (real-time, 1-30s) and long chunks (batch, 30-60s).
    """

    def __init__(self, model_name: str, device: str, compute_type: str):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._loading = False
        self._ready = False

    def _ensure_model(self) -> None:
        """Load model on first use (lazy init to avoid blocking startup)."""
        if self._ready:
            return
        self._load_model()

    def _load_model(self) -> None:
        """Load faster-whisper model into GPU."""
        from faster_whisper import WhisperModel

        logger.info(
            f"Loading Whisper model: {self.model_name} "
            f"(device={self.device}, compute={self.compute_type})"
        )
        start = time.time()
        self._model = WhisperModel(
            self.model_name,
            device=self.device if self.device != "auto" else "cuda",
            compute_type=self.compute_type,
            cpu_threads=4,
        )
        elapsed = time.time() - start
        self._ready = True
        logger.info(f"Model loaded in {elapsed:.1f}s")

    def transcribe_segment(
        self,
        audio_data: bytes,
        language: str = "auto",
        sample_rate: int = 16000,
    ) -> dict[str, Any]:
        """Transcribe a PCM audio segment.

        Args:
            audio_data: Raw PCM 16-bit LE mono audio bytes
            language: Language code or 'auto'
            sample_rate: Audio sample rate (default 16kHz)

        Returns:
            Dict with text, language, segments, confidence, timing
        """
        if self._model is None:
            self._ensure_model()
        if self._model is None:
            raise RuntimeError("Model not loaded")

        # Write PCM to temporary WAV file (faster-whisper needs file or numpy)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            self._write_wav(tmp.name, audio_data, sample_rate)

            start = time.time()

            # Transcribe
            kwargs: dict[str, Any] = {
                "beam_size": 5,
                "vad_filter": False,  # Already VAD-processed
                "without_timestamps": False,
            }
            if language and language != "auto":
                kwargs["language"] = language

            segments_iter, info = self._model.transcribe(tmp.name, **kwargs)
            segments = list(segments_iter)

            elapsed = time.time() - start
            audio_duration = len(audio_data) / (sample_rate * 2)
            rtf = elapsed / audio_duration if audio_duration > 0 else 0

        # Build result
        full_text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        segment_list = [
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "confidence": seg.avg_logprob,
            }
            for seg in segments
            if seg.text.strip()
        ]

        return {
            "text": full_text,
            "language": info.language,
            "language_probability": info.language_probability,
            "segments": segment_list,
            "duration": audio_duration,
            "processing_time": elapsed,
            "rtf": rtf,
        }

    @staticmethod
    def _write_wav(path: str, pcm_data: bytes, sample_rate: int) -> None:
        """Write raw PCM bytes to a WAV file."""
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)


def get_transcriber() -> WhisperTranscriber:
    """Get or create the singleton transcriber."""
    global _transcriber
    if _transcriber is None:
        cfg = get_config()
        _transcriber = WhisperTranscriber(
            model_name=cfg.gpu.model_name,
            device=cfg.gpu.device,
            compute_type=cfg.gpu.compute_type,
        )
    return _transcriber


class GPUWorker:
    """Async GPU worker — continuously consumes from event bus.

    Runs as a background task. Processes segments in priority order:
    1. Real-time segments (STREAM_REALTIME)
    2. Batch chunks (STREAM_BATCH)
    """

    def __init__(self, worker_id: str = "gpu-worker-0"):
        self.worker_id = worker_id
        self._running = False
        self._task: asyncio.Task | None = None
        self._segments_processed = 0
        self._total_audio_seconds = 0.0
        self._total_processing_seconds = 0.0

    async def start(self) -> None:
        """Start the worker loop."""
        self._running = True
        event_bus = get_event_bus()
        await event_bus.initialize()
        self._task = asyncio.create_task(self._run())
        logger.info(f"GPU Worker {self.worker_id} started")

    @staticmethod
    def _load_transcriber() -> "WhisperTranscriber":
        """Load transcriber (with model) — runs in thread."""
        t = get_transcriber()
        t._ensure_model()
        return t

    async def stop(self) -> None:
        """Stop the worker loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"GPU Worker {self.worker_id} stopped ({self._segments_processed} segments)")

    async def _run(self) -> None:
        """Main worker loop."""
        event_bus = get_event_bus()

        # Load model in thread to avoid blocking event loop (downloads ~1.5GB)
        loop = asyncio.get_event_loop()
        transcriber = await loop.run_in_executor(None, self._load_transcriber)
        logger.info(f"GPU Worker {self.worker_id} model ready")

        while self._running:
            try:
                # Consume segments (blocks up to 1s if none available)
                segments = await event_bus.consume_segments(
                    worker_id=self.worker_id,
                    batch_size=1,
                    block_ms=1000,
                )

                for segment in segments:
                    await self._process_segment(segment, transcriber, event_bus)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _process_segment(
        self,
        segment: dict[str, Any],
        transcriber: WhisperTranscriber,
        event_bus: "EventBus",
    ) -> None:
        """Process a single segment: transcribe and publish result."""
        session_id = segment["session_id"]
        segment_id = segment["segment_id"]
        audio_data = segment["audio"]
        language = segment["language"]
        priority = segment["priority"]

        logger.debug(
            f"Processing {priority} segment {segment_id} "
            f"({segment['duration']:.1f}s)"
        )

        try:
            # Run transcription (CPU-bound, run in thread to not block event loop)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                transcriber.transcribe_segment,
                audio_data,
                language,
                16000,
            )

            # Publish result
            await event_bus.publish_result(
                session_id=session_id,
                segment_id=segment_id,
                text=result["text"],
                is_final=True,
                language=result["language"],
                confidence=result.get("language_probability", 0),
            )

            # Update metrics
            self._segments_processed += 1
            self._total_audio_seconds += result["duration"]
            self._total_processing_seconds += result["processing_time"]

            logger.info(
                f"Transcribed {segment_id}: "
                f"\"{result['text'][:60]}...\" "
                f"(RTF={result['rtf']:.2f}, {priority})"
            )

        except Exception as e:
            logger.error(f"Transcription failed for {segment_id}: {e}")
            await event_bus.publish_error(
                session_id=session_id,
                segment_id=segment_id,
                code="transcription_failed",
                message=str(e)[:200],
            )

    @property
    def metrics(self) -> dict[str, Any]:
        avg_rtf = (
            self._total_processing_seconds / self._total_audio_seconds
            if self._total_audio_seconds > 0 else 0
        )
        return {
            "worker_id": self.worker_id,
            "segments_processed": self._segments_processed,
            "total_audio_seconds": round(self._total_audio_seconds, 1),
            "total_processing_seconds": round(self._total_processing_seconds, 1),
            "avg_rtf": round(avg_rtf, 3),
            "running": self._running,
        }
