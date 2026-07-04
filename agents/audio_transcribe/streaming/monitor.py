"""Job monitoring engine — watches checkpoints and job state for streaming.

Filesystem-based monitoring that reads the transcriber's output structure:
- /temp/audio-streaming/output/{job_id}/metadata/job.json — job state
- /temp/audio-streaming/output/{job_id}/checkpoints/segment_XXXX.json — completed segments
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# Output directory (shared volume with main transcriber)
_OUTPUT_DIR = os.environ.get("AUDIO_OUTPUT_DIR", "/temp/audio-streaming/output")


@dataclass
class StreamEvent:
    """A single SSE event to emit."""
    event_type: str  # status, progress, segment, complete, error
    data: dict[str, Any]


@dataclass
class JobState:
    """Tracked state of a job for change detection."""
    status: str = ""
    stage: str = ""
    progress: float = 0.0
    segments_emitted: int = 0
    total_duration: float = 0.0


class JobMonitor:
    """Monitors a transcription job and yields streaming events."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._job_dir = Path(_OUTPUT_DIR) / job_id if job_id else Path(_OUTPUT_DIR)
        self._checkpoint_dir = self._job_dir / "checkpoints"
        self._metadata_dir = self._job_dir / "metadata"

    def job_exists(self) -> bool:
        """Check if the job output directory exists."""
        return self._job_dir.exists() and (self._metadata_dir / "job.json").exists()

    def _read_job_state(self) -> dict[str, Any] | None:
        """Read current job state from metadata/job.json."""
        job_file = self._metadata_dir / "job.json"
        if not job_file.exists():
            return None
        try:
            return json.loads(job_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"Error reading job state: {e}")
            return None

    def _read_checkpoints(self) -> list[dict[str, Any]]:
        """Read all completed checkpoints, sorted by index."""
        if not self._checkpoint_dir.exists():
            return []
        checkpoints = []
        for f in sorted(self._checkpoint_dir.glob("segment_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("status") == "completed":
                    checkpoints.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        return checkpoints

    def get_completed_segments(self) -> list[dict[str, Any]]:
        """Get all completed segments (snapshot, non-streaming)."""
        checkpoints = self._read_checkpoints()
        return [
            {
                "index": cp.get("index", 0),
                "start": cp.get("start", 0.0),
                "end": cp.get("end", 0.0),
                "text": cp.get("transcript_text", ""),
            }
            for cp in checkpoints
        ]

    def list_active_jobs(self) -> list[dict[str, Any]]:
        """List all active (running/queued) jobs."""
        output_root = Path(_OUTPUT_DIR)
        if not output_root.exists():
            return []
        active = []
        for job_dir in output_root.iterdir():
            if not job_dir.is_dir():
                continue
            job_file = job_dir / "metadata" / "job.json"
            if not job_file.exists():
                continue
            try:
                data = json.loads(job_file.read_text(encoding="utf-8"))
                status = data.get("status", "")
                if status in ("queued", "running"):
                    active.append({
                        "job_id": data.get("job_id", job_dir.name),
                        "status": status,
                        "stage": data.get("stage", ""),
                        "progress": data.get("progress", 0),
                        "input_filename": data.get("input_filename", ""),
                    })
            except (json.JSONDecodeError, OSError):
                continue
        return active

    async def watch(
        self,
        *,
        include_text: bool = True,
        poll_interval: float = 0.5,
        timeout: float = 3600.0,
    ) -> AsyncIterator[StreamEvent]:
        """Watch a job and yield events as state changes.

        Polls filesystem for changes at `poll_interval` frequency.
        Terminates on job completion, failure, or timeout.
        """
        state = JobState()
        elapsed = 0.0

        # Initial event
        yield StreamEvent("status", {"stage": "waiting", "status": "monitoring", "job_id": self.job_id})

        while elapsed < timeout:
            job_data = self._read_job_state()
            if job_data is None:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                continue

            current_status = job_data.get("status", "")
            current_stage = job_data.get("stage", "")
            current_progress = job_data.get("progress", 0.0) or 0.0
            total_duration = job_data.get("total_duration", 0.0) or 0.0

            # Emit stage change
            if current_stage != state.stage:
                state.stage = current_stage
                state.status = current_status
                yield StreamEvent("status", {
                    "stage": current_stage,
                    "status": current_status,
                    "job_id": self.job_id,
                })

            # Emit progress (every 5% or significant change)
            if current_progress - state.progress >= 5.0:
                state.progress = current_progress
                state.total_duration = total_duration
                yield StreamEvent("progress", {
                    "progress": round(current_progress, 1),
                    "total_duration": total_duration,
                    "job_id": self.job_id,
                })

            # Emit new segments
            checkpoints = self._read_checkpoints()
            while state.segments_emitted < len(checkpoints):
                cp = checkpoints[state.segments_emitted]
                event_data: dict[str, Any] = {
                    "index": cp.get("index", state.segments_emitted),
                    "start": cp.get("start", 0.0),
                    "end": cp.get("end", 0.0),
                    "job_id": self.job_id,
                }
                if include_text:
                    event_data["text"] = cp.get("transcript_text", "")
                yield StreamEvent("segment", event_data)
                state.segments_emitted += 1

            # Terminal states
            if current_status == "completed":
                # Read final metrics if available
                metrics_file = self._metadata_dir / "metrics.json"
                metrics = {}
                if metrics_file.exists():
                    try:
                        metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        pass

                yield StreamEvent("complete", {
                    "job_id": self.job_id,
                    "segments_total": state.segments_emitted,
                    "total_duration": total_duration,
                    "rtf": metrics.get("realtime_factor", 0),
                    "processing_time": metrics.get("total_processing_time", 0),
                })
                return

            elif current_status in ("failed", "cancelled"):
                yield StreamEvent("error", {
                    "job_id": self.job_id,
                    "status": current_status,
                    "error": job_data.get("error", "Unknown error"),
                    "stage": current_stage,
                })
                return

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        # Timeout
        yield StreamEvent("error", {
            "job_id": self.job_id,
            "status": "timeout",
            "error": f"Monitoring timed out after {timeout}s",
        })
