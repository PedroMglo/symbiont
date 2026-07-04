"""In-memory activity registry with TTL and idempotency."""

from __future__ import annotations

from threading import RLock

from orchestrator.resource_governor.schemas import ActivityRecord, ActivityRequest, utc_now


class ActivityRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._activities: dict[str, ActivityRecord] = {}
        self._idempotency: dict[str, str] = {}

    def active_records(self) -> list[ActivityRecord]:
        now = utc_now()
        with self._lock:
            return [
                record
                for record in self._activities.values()
                if record.released_at is None and record.expires_at > now
            ]

    def create_or_refresh(self, request: ActivityRequest) -> ActivityRecord:
        now = utc_now()
        with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id:
                existing = self._activities.get(existing_id)
                if existing and existing.released_at is None and existing.expires_at > now:
                    existing.heartbeat()
                    return existing
                self._idempotency.pop(request.idempotency_key, None)

            record = ActivityRecord.from_request(request)
            self._activities[record.activity_id] = record
            self._idempotency[request.idempotency_key] = record.activity_id
            return record

    def heartbeat(self, activity_id: str) -> ActivityRecord | None:
        with self._lock:
            record = self._activities.get(activity_id)
            if record is None or record.released_at is not None:
                return None
            if record.expires_at <= utc_now():
                return None
            record.heartbeat()
            return record

    def release(self, activity_id: str) -> bool:
        with self._lock:
            record = self._activities.get(activity_id)
            if record is None or record.released_at is not None:
                return False
            record.released_at = utc_now()
            return True

    def expire(self) -> int:
        now = utc_now()
        expired = 0
        with self._lock:
            for record in self._activities.values():
                if record.released_at is None and record.expires_at <= now:
                    record.released_at = now
                    expired += 1
        return expired
