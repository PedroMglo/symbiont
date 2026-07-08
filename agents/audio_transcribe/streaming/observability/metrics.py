"""Observability metrics for the unified audio platform.

Tracks:
- Stream latency (audio → text)
- GPU utilization and RTF
- Chunk throughput
- VAD activation rate
- Session duration and count
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlatformMetrics:
    """Aggregated platform metrics."""

    # Sessions
    active_sessions: int = 0
    total_sessions: int = 0

    # Processing
    segments_processed: int = 0
    total_audio_seconds: float = 0.0
    total_processing_seconds: float = 0.0

    # Latency (real-time)
    latency_samples: list[float] = field(default_factory=list)
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0

    # GPU
    gpu_busy_seconds: float = 0.0
    avg_rtf: float = 0.0

    # VAD
    vad_activations: int = 0
    vad_total_frames: int = 0
    vad_activation_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": {
                "active": self.active_sessions,
                "total": self.total_sessions,
            },
            "processing": {
                "segments_processed": self.segments_processed,
                "total_audio_seconds": round(self.total_audio_seconds, 1),
                "total_processing_seconds": round(self.total_processing_seconds, 1),
                "avg_rtf": round(self.avg_rtf, 3),
            },
            "latency": {
                "avg_ms": round(self.avg_latency_ms, 1),
                "p95_ms": round(self.p95_latency_ms, 1),
                "samples": len(self.latency_samples),
            },
            "gpu": {
                "busy_seconds": round(self.gpu_busy_seconds, 1),
            },
            "vad": {
                "activations": self.vad_activations,
                "total_frames": self.vad_total_frames,
                "activation_rate": round(self.vad_activation_rate, 3),
            },
        }


class MetricsCollector:
    """Singleton metrics collector."""

    def __init__(self):
        self._metrics = PlatformMetrics()
        self._start_time = time.time()

    @property
    def metrics(self) -> PlatformMetrics:
        return self._metrics

    def record_session_start(self) -> None:
        self._metrics.total_sessions += 1
        self._metrics.active_sessions += 1

    def record_session_end(self) -> None:
        self._metrics.active_sessions = max(0, self._metrics.active_sessions - 1)

    def record_segment_processed(self, audio_seconds: float, processing_seconds: float) -> None:
        self._metrics.segments_processed += 1
        self._metrics.total_audio_seconds += audio_seconds
        self._metrics.total_processing_seconds += processing_seconds
        self._metrics.gpu_busy_seconds += processing_seconds

        if self._metrics.total_audio_seconds > 0:
            self._metrics.avg_rtf = (
                self._metrics.total_processing_seconds / self._metrics.total_audio_seconds
            )

    def record_latency(self, latency_ms: float) -> None:
        self._metrics.latency_samples.append(latency_ms)
        # Keep last 1000 samples
        if len(self._metrics.latency_samples) > 1000:
            self._metrics.latency_samples = self._metrics.latency_samples[-1000:]

        # Recalculate stats
        samples = self._metrics.latency_samples
        self._metrics.avg_latency_ms = sum(samples) / len(samples)
        sorted_samples = sorted(samples)
        p95_idx = int(len(sorted_samples) * 0.95)
        self._metrics.p95_latency_ms = sorted_samples[min(p95_idx, len(sorted_samples) - 1)]

    def record_vad_frame(self, is_speech: bool) -> None:
        self._metrics.vad_total_frames += 1
        if is_speech:
            self._metrics.vad_activations += 1
        if self._metrics.vad_total_frames > 0:
            self._metrics.vad_activation_rate = (
                self._metrics.vad_activations / self._metrics.vad_total_frames
            )

    def get_uptime(self) -> float:
        return time.time() - self._start_time


# Singleton
_collector: MetricsCollector | None = None


def get_metrics_collector() -> MetricsCollector:
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector
