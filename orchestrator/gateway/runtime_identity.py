"""Runtime/build identity helpers for live verification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

GENERATED_ENV_FILES = (
    ".env.storage.generated",
    ".env.llm.generated",
    ".env.services.generated",
    ".env.docker.resources.generated",
)
SECRET_MARKERS = ("SECRET", "TOKEN", "KEY", "PASSWORD", "PASSWD", "CREDENTIAL")
ALLOWED_CONTAINER_ENV_REMAPS = {
    "SYMBIONT_DATA_DIR": ("/app/data",),
}


def project_root() -> Path:
    raw = os.environ.get("AI_LOCAL_PROJECT_ROOT")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2]


def generated_env_root() -> Path:
    candidates = [
        os.environ.get("AI_LOCAL_GENERATED_ENV_ROOT"),
        os.environ.get("AI_LOCAL_HOST_PROJECT_ROOT"),
        "/project",
        os.environ.get("AI_LOCAL_PROJECT_ROOT"),
        str(project_root()),
    ]
    for raw in candidates:
        if not raw:
            continue
        root = Path(raw)
        if any((root / name).exists() for name in GENERATED_ENV_FILES):
            return root
    return project_root()


def image_info() -> dict[str, Any]:
    return {
        "service": os.environ.get("AI_LOCAL_SERVICE_NAME", "symbiont"),
        "image_digest": os.environ.get("AI_LOCAL_IMAGE_DIGEST") or os.environ.get("IMAGE_DIGEST") or "unknown",
        "build_timestamp": os.environ.get("AI_LOCAL_BUILD_TIMESTAMP") or os.environ.get("BUILD_TIMESTAMP") or "unknown",
        "git_commit": os.environ.get("AI_LOCAL_GIT_COMMIT") or _git_commit() or "unknown",
        "code_source": code_source(),
    }


def runtime_info() -> dict[str, Any]:
    payload = image_info()
    payload.update(
        {
            "config_mode": os.environ.get("AI_LOCAL_MODE") or os.environ.get("ORC_CONFIG_MODE") or _settings_mode(),
            "python_executable": os.environ.get("PYTHON_EXECUTABLE") or "",
            "project_root": str(project_root()),
        }
    )
    payload.update(config_effective_hashes())
    return payload


def config_effective() -> dict[str, Any]:
    return {
        "service": os.environ.get("AI_LOCAL_SERVICE_NAME", "symbiont"),
        **config_effective_hashes(include_keys=True),
    }


def config_effective_hashes(*, include_keys: bool = False) -> dict[str, Any]:
    env_root = generated_env_root()
    generated = read_generated_env(env_root)
    effective = {
        key: os.environ.get(key, "")
        for key in generated
        if key in os.environ and not _is_secret_key(key)
    }
    comparable = {
        key: generated[key]
        for key in effective
        if not _is_secret_key(key)
    }
    normalized_remaps: list[str] = []
    for key, allowed_effective_values in ALLOWED_CONTAINER_ENV_REMAPS.items():
        if key in comparable and key in effective and effective[key] in allowed_effective_values:
            if comparable[key] != effective[key]:
                comparable[key] = effective[key]
                normalized_remaps.append(key)
    generated_hash = hash_mapping(comparable) if comparable else None
    effective_hash = hash_mapping(effective) if effective else None
    payload: dict[str, Any] = {
        "generated_env_hash": generated_hash,
        "effective_env_hash": effective_hash,
        "runtime_config_drift": bool(generated_hash and effective_hash and generated_hash != effective_hash),
        "generated_env_root": str(env_root),
        "generated_env_files": [name for name in GENERATED_ENV_FILES if (env_root / name).exists()],
    }
    if include_keys:
        payload["compared_keys"] = sorted(comparable)
        payload["normalized_container_remaps"] = sorted(normalized_remaps)
    return payload


def read_generated_env(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in GENERATED_ENV_FILES:
        path = root / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or _is_secret_key(key):
                continue
            values[key] = value.strip().strip('"').strip("'")
    return values


def hash_mapping(values: dict[str, str]) -> str:
    normalized = json.dumps(dict(sorted(values.items())), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def code_source() -> str:
    explicit = os.environ.get("AI_LOCAL_CODE_SOURCE")
    if explicit in {"image", "bind_mount"}:
        return explicit
    root = project_root()
    return "bind_mount" if (root / ".git").exists() else "image"


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root(),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def _settings_mode() -> str:
    try:
        from orchestrator.config import get_settings

        return str(getattr(get_settings(), "mode", "") or "unknown")
    except Exception:
        return "unknown"
