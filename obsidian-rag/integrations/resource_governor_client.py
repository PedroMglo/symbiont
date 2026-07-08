"""Resource Governor client helpers for RAG background work."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from uuid import uuid4

from sharedai.system.resource_governor import ResourceGovernorClient
from sharedai.system.resource_governor.schemas import (
    LeaseDecision,
)


@dataclass(frozen=True)
class LeaseRequestPayload:
    """Server-authoritative lease payload.

    The Resource Governor service owns the current capability vocabulary. This
    adapter deliberately avoids client-side enum validation so RAG can use the
    active server contract even when the installed shared client package lags a
    newly added generic capability.
    """

    idempotency_key: str
    requester: str
    component: str
    lane: str
    lease_scope: str
    resource_class: str
    capability: str
    estimated_duration_seconds: int | None = None
    estimated_ram_mb: int | None = None
    estimated_vram_mb: int | None = None
    estimated_io_mb: int | None = None
    preemptible: bool = True
    quality_policy: str = "preserve"
    estimated_quality_impact: str = "low"
    request_id: str = ""
    session_id: str | None = None

    def model_dump(self, *, mode: str = "json") -> dict[str, Any]:
        del mode
        return {
            "idempotency_key": self.idempotency_key,
            "requester": self.requester,
            "component": self.component,
            "lane": self.lane,
            "lease_scope": self.lease_scope,
            "resource_class": self.resource_class,
            "capability": self.capability,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "estimated_ram_mb": self.estimated_ram_mb,
            "estimated_vram_mb": self.estimated_vram_mb,
            "estimated_io_mb": self.estimated_io_mb,
            "preemptible": self.preemptible,
            "quality_policy": self.quality_policy,
            "estimated_quality_impact": self.estimated_quality_impact,
            "request_id": self.request_id,
            "session_id": self.session_id,
        }


@dataclass
class LeaseHandle:
    client: ResourceGovernorClient
    request: LeaseRequestPayload
    decision: LeaseDecision

    @property
    def granted(self) -> bool:
        return self.decision.granted

    def heartbeat(self) -> None:
        if self.decision.lease_id:
            self.client.heartbeat(
                self.decision.lease_id,
                owner=self.request.requester,
                request_id=self.request.request_id,
            )

    def release(self) -> None:
        if self.decision.lease_id:
            self.client.release(self.decision.lease_id)

    def __enter__(self) -> "LeaseHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb
        self.release()


def request_lease(
    *,
    component: str,
    lane: str,
    lease_scope: str,
    resource_class: str,
    capability: str,
    request_id: str | None = None,
    session_id: str | None = None,
    estimated_duration_seconds: int | None = None,
    estimated_ram_mb: int | None = None,
    estimated_vram_mb: int | None = None,
    estimated_io_mb: int | None = None,
    preemptible: bool = True,
    quality_policy: str = "preserve",
    estimated_quality_impact: str = "low",
    idempotency_suffix: str | None = None,
) -> LeaseHandle:
    req_id = request_id or f"rag_{uuid4().hex[:16]}"
    suffix = idempotency_suffix or uuid4().hex
    request = LeaseRequestPayload(
        idempotency_key=f"obsidian-rag:{component}:{capability}:{req_id}:{suffix}",
        requester="obsidian-rag",
        component=component,
        lane=str(lane),
        lease_scope=str(lease_scope),
        resource_class=str(resource_class),
        capability=str(capability),
        estimated_duration_seconds=estimated_duration_seconds,
        estimated_ram_mb=estimated_ram_mb,
        estimated_vram_mb=estimated_vram_mb,
        estimated_io_mb=estimated_io_mb,
        preemptible=preemptible,
        quality_policy=quality_policy,
        estimated_quality_impact=estimated_quality_impact,
        request_id=req_id,
        session_id=session_id,
    )
    client = ResourceGovernorClient(
        base_url=os.environ.get("AI_RESOURCE_GOVERNOR_URL"),
        token=os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN"),
    )
    return LeaseHandle(client=client, request=request, decision=client.request_lease(request))


@contextmanager
def lease_context(**kwargs: Any) -> Iterator[LeaseHandle]:
    """Request a lease, heartbeat while held, and always release it."""

    lease = request_lease(**kwargs)
    stop_event = threading.Event()
    heartbeat_thread: threading.Thread | None = None

    def _heartbeat_loop() -> None:
        while not stop_event.wait(15.0):
            try:
                lease.heartbeat()
            except Exception:
                break

    if lease.granted and lease.decision.lease_id:
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            name=f"rag-lease-heartbeat-{kwargs.get('component', 'unknown')}",
            daemon=True,
        )
        heartbeat_thread.start()
    try:
        yield lease
    finally:
        stop_event.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        try:
            lease.release()
        except Exception:
            pass
