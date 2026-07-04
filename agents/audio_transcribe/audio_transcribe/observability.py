"""Observability: structured logging, metrics, stage timings."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from audio_transcribe.storage import get_job_dir
from audio_transcribe.types import ProcessingMetrics, StageTiming

logger = logging.getLogger(__name__)


class JobObserver:
    """Tracks processing metrics and stage timings for a single job."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._metrics = ProcessingMetrics()
        self._stage_timings: list[StageTiming] = []
        self._current_stage: Optional[str] = None
        self._stage_start: Optional[float] = None
        self._job_start: Optional[float] = None
        self._log_handler: Optional[logging.FileHandler] = None

    @property
    def metrics(self) -> ProcessingMetrics:
        return self._metrics

    def start_job(self) -> None:
        """Mark job processing start."""
        self._job_start = time.time()
        self._setup_job_logger()

    def end_job(self) -> None:
        """Mark job processing end. Compute final metrics."""
        if self._job_start:
            self._metrics.total_processing_seconds = time.time() - self._job_start
            if self._metrics.audio_duration_seconds > 0:
                self._metrics.realtime_factor = (
                    self._metrics.total_processing_seconds / self._metrics.audio_duration_seconds
                )

        self._metrics.stage_timings = self._stage_timings
        self._teardown_job_logger()

    def start_stage(self, stage: str) -> None:
        """Mark start of a processing stage."""
        # End previous stage if any
        if self._current_stage:
            self.end_stage()

        self._current_stage = stage
        self._stage_start = time.time()
        self._stage_timings.append(StageTiming(
            stage=stage,
            started_at=datetime.now(timezone.utc).isoformat(),
        ))
        self._log(f"Stage started: {stage}")

    def end_stage(self) -> None:
        """Mark end of current stage."""
        if not self._current_stage or not self._stage_start:
            return

        elapsed = time.time() - self._stage_start
        # Update the last timing entry
        if self._stage_timings:
            self._stage_timings[-1].completed_at = datetime.now(timezone.utc).isoformat()
            self._stage_timings[-1].duration_seconds = elapsed

        self._log(f"Stage completed: {self._current_stage} ({elapsed:.2f}s)")
        self._current_stage = None
        self._stage_start = None

    def set_audio_duration(self, duration: float) -> None:
        self._metrics.audio_duration_seconds = duration

    def set_model_info(self, model: str, device: str, compute_type: str, batch_size: int) -> None:
        self._metrics.model = model
        self._metrics.device = device
        self._metrics.compute_type = compute_type
        self._metrics.batch_size = batch_size

    def set_segments_count(self, count: int) -> None:
        self._metrics.segments_count = count

    def set_speakers_count(self, count: int) -> None:
        self._metrics.speakers_count = count

    def increment_retries(self) -> None:
        self._metrics.retries += 1

    def record_output_size(self, name: str, size_bytes: int) -> None:
        self._metrics.output_sizes[name] = size_bytes

    def _setup_job_logger(self) -> None:
        """Set up per-job log file."""
        log_path = get_job_dir(self.job_id) / "logs" / "processing.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handler = logging.FileHandler(log_path, encoding="utf-8")
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logging.getLogger("audio_transcribe").addHandler(self._log_handler)

    def _teardown_job_logger(self) -> None:
        """Remove per-job log handler."""
        if self._log_handler:
            logging.getLogger("audio_transcribe").removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None

    def _log(self, message: str) -> None:
        logger.info(f"[{self.job_id[:8]}] {message}")
