"""In-memory lease registry with TTL, heartbeat and idempotency."""

from __future__ import annotations

from threading import RLock

from orchestrator.resource_governor.schemas import LeaseDecision, LeaseRecord, LeaseRequest, utc_now


class LeaseRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._leases: dict[str, LeaseRecord] = {}
        self._idempotency: dict[str, str] = {}

    def active_records(self) -> list[LeaseRecord]:
        now = utc_now()
        with self._lock:
            return [
                record
                for record in self._leases.values()
                if record.released_at is None and record.expires_at > now
            ]

    def create_or_get(self, request: LeaseRequest, decision: LeaseDecision) -> LeaseDecision:
        now = utc_now()
        with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id:
                existing = self._leases.get(existing_id)
                if existing and existing.released_at is None and existing.expires_at > now:
                    return existing.decision
                self._idempotency.pop(request.idempotency_key, None)

            if not decision.granted:
                return decision

            record = LeaseRecord.from_request(request, decision)
            self._leases[record.lease_id] = record
            self._idempotency[request.idempotency_key] = record.lease_id
            return record.decision

    def heartbeat(self, lease_id: str) -> LeaseRecord | None:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None or record.released_at is not None:
                return None
            if record.expires_at <= utc_now():
                return None
            record.heartbeat()
            return record

    def release(self, lease_id: str) -> bool:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None or record.released_at is not None:
                return False
            record.released_at = utc_now()
            return True

    def expire(self) -> int:
        now = utc_now()
        expired = 0
        with self._lock:
            for record in self._leases.values():
                if record.released_at is None and record.expires_at <= now:
                    record.released_at = now
                    expired += 1
        return expired
