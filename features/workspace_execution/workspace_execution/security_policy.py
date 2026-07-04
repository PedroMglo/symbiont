"""Security policy helpers owned by workspace_execution."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

SECRET_KEY_MARKERS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "OPENAI",
    "CODEX",
    "AWS_",
    "GCP_",
    "AZURE_",
    "KUBECONFIG",
    "SSH_AUTH_SOCK",
    "DOCKER_HOST",
)

BASE_ENV_ALLOWLIST = {"PATH", "LANG", "LC_ALL"}
GENERATED_ENV_ALLOWLIST = {"PYTHONPATH"}
LOCAL_ENV_ALLOWLIST = {"PYTHONPATH", "PIP_INDEX_URL", "UV_INDEX_URL"}


def scrub_command_env(env: dict[str, str], *, generated_project: bool) -> tuple[dict[str, str], list[str]]:
    scrubbed: dict[str, str] = {}
    removed: list[str] = []
    allowed = BASE_ENV_ALLOWLIST | (GENERATED_ENV_ALLOWLIST if generated_project else LOCAL_ENV_ALLOWLIST)
    for key, value in env.items():
        normalized = str(key).strip()
        if not normalized:
            continue
        if is_secret_env_key(normalized):
            removed.append(normalized)
            continue
        if normalized in allowed:
            scrubbed[normalized] = str(value)
            continue
        if not generated_project and normalized.startswith("AI_LOCAL_"):
            scrubbed[normalized] = str(value)
            continue
        removed.append(normalized)
    return scrubbed, sorted(set(removed))


def is_secret_env_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_KEY_MARKERS)


def command_requires_vm_backed_sandbox(request: Any) -> bool:
    metadata = getattr(request, "metadata", {}) if isinstance(getattr(request, "metadata", {}), dict) else {}
    return bool(
        getattr(request, "requires_vm_backed_sandbox", False)
        or metadata.get("requires_vm_backed_sandbox")
        or metadata.get("must_use_vm_backed_sandbox")
        or metadata.get("generated_project_trust") == "untrusted"
    )


def normalize_workspace_relative_path(path: str) -> str:
    candidate = str(path or ".").replace("\\", "/")
    if "\x00" in candidate:
        raise ValueError("path cannot contain NUL bytes")
    if candidate.startswith(("/", "~")):
        raise ValueError("path must be relative")
    if len(candidate) >= 2 and candidate[1] == ":":
        raise ValueError("path must not use drive-qualified syntax")
    parts = PurePosixPath(candidate).parts
    if any(part == ".." for part in parts):
        raise ValueError("path must not contain parent directory segments")
    return "." if candidate in {"", "."} else candidate
