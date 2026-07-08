"""Safe restore execution checks for managed local storage volumes."""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from pathlib import Path
from typing import Any

CATALOG_RELATIVE_PATH = Path("config/docker/volumes-catalog.toml")
STORAGE_ENV_RELATIVE_PATH = Path(".env.storage.generated")
REPORT_RELATIVE_JSON = Path("docs/generated/restore-execution-report.json")
REPORT_RELATIVE_MD = Path("docs/generated/restore-execution-report.md")
DEFAULT_PROJECT_SCRATCH_ROOT = Path(".local/data/storage_guardian/scratch/project")
RESTORE_TEST_RELATIVE_ROOT = Path("restore-test")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RestoreExecutionError(ValueError):
    """Raised when a restore execution request is unsafe or unsupported."""


def _safe_component(value: str, label: str) -> str:
    raw = (value or "").strip()
    if not _SAFE_COMPONENT_RE.fullmatch(raw):
        raise RestoreExecutionError(f"invalid {label}")
    return raw


def _declared_volume_path(raw: str) -> Path:
    value = (raw or "").strip()
    if not value or "\x00" in value:
        raise RestoreExecutionError("declared volume path is invalid")
    try:
        return Path(value).expanduser().resolve()
    except OSError as exc:
        raise RestoreExecutionError("declared volume path cannot be resolved") from exc


def _safe_child_path(root: Path, *parts: str) -> Path:
    root_text = os.path.realpath(os.path.abspath(os.fspath(root)))
    candidate_text = os.path.realpath(os.path.join(root_text, *parts))
    try:
        if os.path.commonpath([root_text, candidate_text]) != root_text:
            raise RestoreExecutionError("restore destination escaped restore scratch root")
    except ValueError as exc:
        raise RestoreExecutionError("restore destination escaped restore scratch root") from exc
    return Path(candidate_text)


def _restore_test_root(workspace: Path) -> Path:
    raw = os.environ.get("AI_LOCAL_PROJECT_SCRATCH_ROOT")
    scratch_root = Path(raw).expanduser().resolve() if raw else (workspace / DEFAULT_PROJECT_SCRATCH_ROOT).resolve()
    return scratch_root / RESTORE_TEST_RELATIVE_ROOT


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"')
    return env


def _directory_size(path: Path) -> int | None:
    if not path.exists():
        return None
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return None
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        return None
    return total


def _write_report(root: Path, payload: dict[str, Any]) -> None:
    json_path = root / REPORT_RELATIVE_JSON
    md_path = root / REPORT_RELATIVE_MD
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Restore Execution Report",
        "",
        f"Generated at: `{payload['generated_at']}`",
        f"Status: `{payload['status']}`",
        f"Volume: `{payload['volume']}`",
        f"Destination: `{payload.get('destination') or '-'}`",
        "",
        "## Checks",
        "",
        "| Check | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for check in payload.get("checks") or []:
        lines.append(f"| `{check['name']}` | `{check['status']}` | {check.get('detail') or ''} |")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")


def execute_restore_test(
    volume: str,
    *,
    root: Path,
    requested_by: str = "storage_guardian.api",
    now: float | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    workspace = root.resolve()
    safe_volume = _safe_component(volume, "volume")
    catalog_path = workspace / CATALOG_RELATIVE_PATH
    if not catalog_path.exists():
        raise RestoreExecutionError(f"volume catalog not found: {catalog_path}")
    catalog = tomllib.loads(catalog_path.read_text(encoding="utf-8"))
    policy = (catalog.get("volumes") or {}).get(safe_volume)
    if not isinstance(policy, dict):
        raise KeyError(safe_volume)
    env_key = policy.get("env_path")
    if not env_key:
        raise RestoreExecutionError("restore execution v1 supports only filesystem env_path volumes")
    env = _read_env(workspace / STORAGE_ENV_RELATIVE_PATH)
    source_raw = env.get(str(env_key))
    if not source_raw:
        raise RestoreExecutionError(f"volume {safe_volume} is not declared in .env.storage.generated")

    current_time = time.time() if now is None else now
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(current_time))
    source = _declared_volume_path(source_raw)
    tmp_root = _restore_test_root(workspace)
    destination = _safe_child_path(tmp_root, safe_volume, timestamp)
    if destination == source or source in destination.parents:
        raise RestoreExecutionError("restore destination overlaps active source volume")
    if destination.exists():
        raise RestoreExecutionError(f"restore destination already exists: {destination}")

    destination.mkdir(parents=True, exist_ok=False)
    source_exists = source.exists()
    source_readable = os.access(source, os.R_OK) if source_exists else False
    manifest_path = destination / "restore-manifest.json"
    size_bytes = _directory_size(source)
    manifest = {
        "volume": safe_volume,
        "source": str(source),
        "destination": str(destination),
        "source_exists": source_exists,
        "source_readable": source_readable,
        "size_bytes": size_bytes,
        "backup_method": "filesystem-path",
        "active_volume_overwrite": False,
        "requested_by": requested_by,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checks = [
        {"name": "source_exists", "status": "pass" if source_exists else "fail", "detail": str(source)},
        {"name": "source_readable", "status": "pass" if source_readable else "fail", "detail": str(source)},
        {"name": "destination_temp", "status": "pass", "detail": str(destination)},
        {"name": "manifest_written", "status": "pass" if manifest_path.exists() else "fail", "detail": str(manifest_path)},
        {"name": "active_volume_overwrite", "status": "pass", "detail": "false"},
        {"name": "size_bytes", "status": "pass" if size_bytes is not None else "unknown", "detail": str(size_bytes)},
    ]
    status = "fail" if any(check["status"] == "fail" for check in checks) else "pass"
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(current_time)),
        "status": status,
        "volume": safe_volume,
        "source": str(source),
        "destination": str(destination),
        "manifest": str(manifest_path),
        "requested_by": requested_by,
        "checks": checks,
        "immutability": {"active_volume_overwrite": False},
    }
    if write_report:
        _write_report(workspace, payload)
    return payload
