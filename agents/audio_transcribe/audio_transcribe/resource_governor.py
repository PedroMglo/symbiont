"""Resource Governor integration for audio transcription GPU work."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from sharedai.system.resource_governor import ResourceGovernorClient
from sharedai.system.resource_governor.schemas import LeaseDecision, LeaseRequest


@dataclass
class AudioGpuLease:
    client: ResourceGovernorClient
    request: LeaseRequest
    decision: LeaseDecision

    @property
    def granted(self) -> bool:
        return self.decision.granted

    async def release(self) -> None:
        if self.decision.lease_id:
            await asyncio.to_thread(self.client.release, self.decision.lease_id)


def is_resource_governor_configured() -> bool:
    """Return whether audio_transcribe should request external resource leases."""
    return bool(os.environ.get("AI_RESOURCE_GOVERNOR_URL", "").strip())


async def request_audio_gpu_lease(job_id: str, *, model_name: str, duration_seconds: float | None = None) -> AudioGpuLease:
    request = LeaseRequest(
        idempotency_key=f"audio_transcribe:gpu:{job_id}:{model_name}",
        requester="audio_transcribe",
        component="transcription_pipeline",
        lane="heavy_gpu",
        lease_scope="batch",
        resource_class="vram",
        capability="audio_transcribe_gpu",
        estimated_duration_seconds=int(duration_seconds or 600),
        estimated_ram_mb=1024,
        estimated_vram_mb=2048,
        preemptible=True,
        quality_policy="degrade_allowed",
        estimated_quality_impact="low",
        request_id=job_id,
        session_id=None,
    )
    client = ResourceGovernorClient(
        base_url=os.environ.get("AI_RESOURCE_GOVERNOR_URL"),
        token=os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN"),
    )
    decision = await asyncio.to_thread(client.request_lease, request)
    return AudioGpuLease(client=client, request=request, decision=decision)
