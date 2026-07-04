"""Durable local index for RAG admin workflow jobs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any


class AdminJobStore:
    """Persist API job status so Temporal submissions survive API restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def load_all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            if not isinstance(payload, dict):
                return {}
            jobs = payload.get("jobs", payload)
            if not isinstance(jobs, dict):
                return {}
            return {
                str(job_id): dict(job)
                for job_id, job in jobs.items()
                if isinstance(job, dict)
            }

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.load_all().get(job_id)

    def upsert(self, job_id: str, job: dict[str, Any]) -> None:
        with self._lock:
            jobs = self.load_all()
            jobs[str(job_id)] = dict(job)
            self._write_all(jobs)

    def _write_all(self, jobs: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps({"jobs": jobs}, sort_keys=True, separators=(",", ":"), default=str)
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self.path)


def default_admin_job_store(settings_obj: Any) -> AdminJobStore:
    return AdminJobStore(Path(settings_obj.workflows.job_store_path))
