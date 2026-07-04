"""Read-only Docker shield evidence for the agentic cockpit."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.config import PROJECT_ROOT

DEFAULT_REPORT_RELATIVE_PATH = Path("docs/generated/docker-shield-report.json")
DEFAULT_STALE_SECONDS = 86400
STALE_SECONDS_ENV = "ORC_AGENTIC_DOCKER_SHIELD_STALE_SECONDS"


def _workspace_root() -> Path:
    for key in (
        "AI_LOCAL_PROJECT_ROOT",
        "AI_LOCAL_ROOT",
        "PROJECT_ROOT",
        "ORC_LIFECYCLE_PROJECT_DIR",
        "AI_LOCAL_HOST_PROJECT_ROOT",
        "ORC_LIFECYCLE_COMPOSE_PROJECT_DIR",
    ):
        raw = os.environ.get(key)
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if (candidate / "compose.yml").exists() or (candidate / "docs" / "generated").exists():
            return candidate.resolve()
    for candidate in (Path("/project"), Path("/workspace/ai-local")):
        if (candidate / "compose.yml").exists() or (candidate / "docs" / "generated").exists():
            return candidate.resolve()
    return PROJECT_ROOT.parent.resolve()


def docker_shield_report_path() -> Path:
    return _workspace_root() / DEFAULT_REPORT_RELATIVE_PATH


def _stale_seconds() -> int:
    raw = os.environ.get(STALE_SECONDS_ENV)
    if not raw:
        return DEFAULT_STALE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_STALE_SECONDS
    return value if value > 0 else DEFAULT_STALE_SECONDS


def _parse_generated_at(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _age_seconds(payload: dict[str, Any], path: Path, now: float) -> int | None:
    generated_ts = _parse_generated_at(payload.get("generated_at"))
    if generated_ts is None:
        try:
            generated_ts = path.stat().st_mtime
        except OSError:
            return None
    return max(0, int(now - generated_ts))


def _links() -> dict[str, str]:
    return {
        "cockpit": "/agentic/cockpit/docker",
        "evidence": "/agentic/evidence/docker-shield",
        "source": str(DEFAULT_REPORT_RELATIVE_PATH),
        "refresh_command": "make docker-shield-report",
    }


def _missing_summary(path: Path) -> dict[str, Any]:
    return {
        "status": "missing",
        "score": None,
        "generated_at": None,
        "age_seconds": None,
        "is_stale": True,
        "summary": {
            "message": "Docker shield report is missing. Run make docker-shield-report.",
            "source": str(path),
        },
        "controls": [],
        "action_items": [
            {"control": "docker_shield", "action": "run make docker-shield-report"},
        ],
        "links": _links(),
        "source": str(DEFAULT_REPORT_RELATIVE_PATH),
    }


def _invalid_summary(path: Path, error: str) -> dict[str, Any]:
    return {
        "status": "invalid",
        "score": None,
        "generated_at": None,
        "age_seconds": None,
        "is_stale": True,
        "summary": {
            "message": "Docker shield report is invalid. Regenerate it with make docker-shield-report.",
            "error": error,
            "source": str(path),
        },
        "controls": [],
        "action_items": [
            {"control": "docker_shield", "action": "regenerate with make docker-shield-report"},
        ],
        "links": _links(),
        "source": str(DEFAULT_REPORT_RELATIVE_PATH),
    }


def docker_shield_summary(
    *,
    path: Path | None = None,
    now: float | None = None,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    report_path = path or docker_shield_report_path()
    current_time = time.time() if now is None else now
    stale_after = stale_seconds or _stale_seconds()
    if not report_path.exists():
        return _missing_summary(report_path)
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _invalid_summary(report_path, str(exc))
    if not isinstance(payload, dict):
        return _invalid_summary(report_path, "top-level JSON value is not an object")

    age = _age_seconds(payload, report_path, current_time)
    is_stale = age is None or age > stale_after
    return {
        "status": str(payload.get("status") or "unknown"),
        "score": payload.get("score"),
        "generated_at": payload.get("generated_at"),
        "age_seconds": age,
        "is_stale": is_stale,
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        "controls": payload.get("controls") if isinstance(payload.get("controls"), list) else [],
        "action_items": payload.get("action_items") if isinstance(payload.get("action_items"), list) else [],
        "links": _links(),
        "source": str(DEFAULT_REPORT_RELATIVE_PATH),
    }


def docker_shield_evidence(
    *,
    path: Path | None = None,
    now: float | None = None,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    report_path = path or docker_shield_report_path()
    summary = docker_shield_summary(path=report_path, now=now, stale_seconds=stale_seconds)
    payload: dict[str, Any] | None = None
    if report_path.exists():
        try:
            loaded = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            payload = None
    return {
        "read_only": True,
        "generated_at": time.time() if now is None else now,
        "source": str(DEFAULT_REPORT_RELATIVE_PATH),
        "immutability": {"payload_editing_allowed": False},
        "summary": summary,
        "report": payload or summary,
        "links": _links(),
    }
