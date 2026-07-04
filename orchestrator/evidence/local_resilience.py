"""Read-only local resilience evidence for the agentic cockpit."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.evidence.docker_shield import _workspace_root

DEFAULT_STALE_SECONDS = 86400
STALE_SECONDS_ENV = "ORC_AGENTIC_LOCAL_RESILIENCE_STALE_SECONDS"

REPORTS: dict[str, dict[str, str]] = {
    "resilience": {
        "path": "docs/generated/resilience-report.json",
        "refresh_command": "make resilience-test",
    },
    "slo": {
        "path": "docs/generated/slo-report.json",
        "refresh_command": "make infra",
    },
    "restore": {
        "path": "docs/generated/restore-test.json",
        "refresh_command": "make infra",
    },
    "chaos": {
        "path": "docs/generated/chaos-local.json",
        "refresh_command": "make chaos-local",
    },
}
RESTORE_EXECUTION_RELATIVE_PATH = Path("docs/generated/restore-execution-report.json")
DAILY_REPORT_RELATIVE_PATH = Path("docs/generated/daily-report.json")
SLO_TRENDS_RELATIVE_PATH = Path("docs/generated/slo-trends.json")


def local_resilience_report_path(report_name: str) -> Path:
    return _workspace_root() / REPORTS[report_name]["path"]


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


def _links(report_name: str) -> dict[str, str]:
    info = REPORTS[report_name]
    return {
        "cockpit": "/agentic/cockpit/resilience",
        "evidence": f"/agentic/evidence/local-resilience/{report_name}",
        "source": info["path"],
        "refresh_command": info["refresh_command"],
    }


def _missing_summary(report_name: str, path: Path) -> dict[str, Any]:
    return {
        "name": report_name,
        "status": "missing",
        "generated_at": None,
        "age_seconds": None,
        "is_stale": True,
        "summary": {
            "message": f"Local resilience report {report_name!r} is missing. Run {REPORTS[report_name]['refresh_command']}.",
            "source": str(path),
        },
        "scenarios": [],
        "slos": [],
        "action_items": [
            {"control": f"local_resilience:{report_name}", "action": f"run {REPORTS[report_name]['refresh_command']}"},
        ],
        "links": _links(report_name),
        "source": REPORTS[report_name]["path"],
    }


def _invalid_summary(report_name: str, path: Path, error: str) -> dict[str, Any]:
    return {
        "name": report_name,
        "status": "invalid",
        "generated_at": None,
        "age_seconds": None,
        "is_stale": True,
        "summary": {
            "message": f"Local resilience report {report_name!r} is invalid. Regenerate it.",
            "error": error,
            "source": str(path),
        },
        "scenarios": [],
        "slos": [],
        "action_items": [
            {"control": f"local_resilience:{report_name}", "action": f"regenerate with {REPORTS[report_name]['refresh_command']}"},
        ],
        "links": _links(report_name),
        "source": REPORTS[report_name]["path"],
    }


def _payload_action_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for scenario in payload.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        for action in scenario.get("actions") or []:
            items.append({"control": scenario.get("name") or "scenario", "action": action})
    return items


def _optional_json(relative_path: Path) -> dict[str, Any] | None:
    path = _workspace_root() / relative_path
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def local_resilience_report_summary(
    report_name: str,
    *,
    path: Path | None = None,
    now: float | None = None,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    if report_name not in REPORTS:
        raise KeyError(report_name)
    report_path = path or local_resilience_report_path(report_name)
    current_time = time.time() if now is None else now
    stale_after = stale_seconds or _stale_seconds()
    if not report_path.exists():
        return _missing_summary(report_name, report_path)
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _invalid_summary(report_name, report_path, str(exc))
    if not isinstance(payload, dict):
        return _invalid_summary(report_name, report_path, "top-level JSON value is not an object")

    age = _age_seconds(payload, report_path, current_time)
    scenarios = payload.get("scenarios") if isinstance(payload.get("scenarios"), list) else []
    slos = payload.get("slos") if isinstance(payload.get("slos"), list) else []
    simulations = payload.get("simulations") if isinstance(payload.get("simulations"), list) else []
    summary = {
        "command": payload.get("command"),
        "mode": payload.get("mode"),
        "scenarios": len(scenarios),
        "slos": len(slos),
        "simulations": len(simulations),
        "warnings": len([item for item in [*scenarios, *slos] if isinstance(item, dict) and item.get("status") == "warn"]),
        "failures": len([item for item in [*scenarios, *slos] if isinstance(item, dict) and item.get("status") == "fail"]),
    }
    return {
        "name": report_name,
        "status": str(payload.get("status") or "unknown"),
        "generated_at": payload.get("generated_at"),
        "age_seconds": age,
        "is_stale": age is None or age > stale_after,
        "summary": summary,
        "scenarios": scenarios,
        "slos": slos,
        "action_items": _payload_action_items(payload),
        "links": _links(report_name),
        "source": REPORTS[report_name]["path"],
    }


def local_resilience_summary(
    *,
    now: float | None = None,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    reports = {
        name: local_resilience_report_summary(name, now=now, stale_seconds=stale_seconds)
        for name in REPORTS
    }
    order = {"pass": 0, "unknown": 1, "warn": 2, "invalid": 3, "missing": 3, "fail": 4}
    status = max((str(item.get("status") or "unknown") for item in reports.values()), key=lambda item: order.get(item, 1))
    return {
        "status": status,
        "reports": reports,
        "summary": {
            "reports": len(reports),
            "stale": len([item for item in reports.values() if item.get("is_stale")]),
            "missing": len([item for item in reports.values() if item.get("status") == "missing"]),
            "warnings": len([item for item in reports.values() if item.get("status") == "warn"]),
            "failures": len([item for item in reports.values() if item.get("status") == "fail"]),
        },
        "action_items": [
            action
            for report in reports.values()
            for action in report.get("action_items", [])
        ],
        "history": {
            "daily_report": _optional_json(DAILY_REPORT_RELATIVE_PATH),
            "slo_trends": _optional_json(SLO_TRENDS_RELATIVE_PATH),
        },
        "links": {
            "cockpit": "/agentic/cockpit/resilience",
            "evidence": "/agentic/evidence/local-resilience/{report_name}",
        },
    }


def local_resilience_evidence(
    report_name: str,
    *,
    path: Path | None = None,
    now: float | None = None,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    report_path = path or local_resilience_report_path(report_name)
    summary = local_resilience_report_summary(
        report_name,
        path=report_path,
        now=now,
        stale_seconds=stale_seconds,
    )
    payload: dict[str, Any] | None = None
    if report_path.exists():
        try:
            loaded = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            payload = None
    execution_report = None
    if report_name == "restore":
        execution_path = _workspace_root() / RESTORE_EXECUTION_RELATIVE_PATH
        if execution_path.exists():
            try:
                loaded_execution = json.loads(execution_path.read_text(encoding="utf-8"))
                if isinstance(loaded_execution, dict):
                    execution_report = loaded_execution
            except (OSError, json.JSONDecodeError):
                execution_report = None
    return {
        "read_only": True,
        "generated_at": time.time() if now is None else now,
        "source": REPORTS[report_name]["path"],
        "immutability": {"payload_editing_allowed": False},
        "summary": summary,
        "report": payload or summary,
        "execution_report": execution_report,
        "links": _links(report_name),
    }
