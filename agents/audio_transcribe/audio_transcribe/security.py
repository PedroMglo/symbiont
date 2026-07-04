"""Security utilities: path validation, API key, filename sanitization."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from audio_transcribe.config import get_config
from audio_transcribe.errors import PathSecurityError, UnsupportedMediaError

_DEFAULT_SECRET_PATH = Path("/run/secrets/audio_transcribe_api_key")


def get_api_key() -> Optional[str]:
    """Get the configured API key from env or secret file."""
    cfg = get_config()
    env_name = cfg.security.api_key_env
    key = os.environ.get(env_name, "").strip()
    if key:
        return key

    secret_env = os.environ.get(cfg.security.api_key_file_env, "").strip()
    secret_paths = [Path(secret_env)] if secret_env else []
    secret_paths.append(_DEFAULT_SECRET_PATH)
    for path in secret_paths:
        try:
            if path.is_file():
                file_key = path.read_text(encoding="utf-8").strip()
                if file_key:
                    return file_key
        except OSError:
            continue
    return None


def _provided_api_key(request: Request) -> str:
    header_key = request.headers.get("X-API-Key", "").strip()
    if header_key:
        return header_key
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


async def verify_api_key(request: Request) -> None:
    """Verify API key. Missing service secret is fail-closed by default."""
    cfg = get_config()
    expected_key = get_api_key()
    if expected_key is None:
        if cfg.security.allow_unauthenticated_dev:
            return
        raise HTTPException(status_code=503, detail="Audio transcribe API key is not configured")

    provided_key = _provided_api_key(request)
    if not provided_key:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not secrets.compare_digest(provided_key, expected_key):
        raise HTTPException(status_code=403, detail="Invalid API key")


def validate_path_within_directory(file_path: str, allowed_dir: str) -> Path:
    """Validate that file_path resolves within allowed_dir. Blocks traversal."""
    if "\x00" in file_path:
        raise PathSecurityError(message="Invalid path", detail="Path contains a null byte")
    allowed = _canonical_directory(allowed_dir)
    target = _resolve_under_directory(file_path, allowed)
    if target is None:
        raise PathSecurityError(
            message="Path not within allowed directory",
            detail=f"Target must be within {allowed_dir}",
        )
    return target


def validate_input_path(file_path: str) -> Path:
    """Validate an input file path is safe and within allowed directories.

    Allows paths within:
    - The configured input_dir (default: /data/input)
    - Additional allowed directories from AUDIO_TRANSCRIBE_ALLOWED_DIRS env var
      (comma-separated, e.g. "/host_home,/mnt/media")
    """
    cfg = get_config()
    if "\x00" in file_path:
        raise PathSecurityError(message="Invalid input path", detail="Path contains a null byte")

    # Build list of allowed directories
    allowed_dirs = [_canonical_directory(cfg.paths.input_dir)]
    extra_dirs = os.environ.get("AUDIO_TRANSCRIBE_ALLOWED_DIRS", "").strip()
    if extra_dirs:
        allowed_dirs.extend(_canonical_directory(d.strip()) for d in extra_dirs.split(",") if d.strip())

    # Check if path is within any allowed directory
    resolved: Path | None = None
    for allowed_dir in allowed_dirs:
        candidate = _resolve_under_directory(file_path, allowed_dir)
        if candidate is not None:
            resolved = candidate
            break

    if resolved is None:
        raise PathSecurityError(
            message="Path not within allowed directory",
            detail="Target must be within one of the configured input directories",
        )

    if not resolved.exists():
        raise PathSecurityError(
            message="Input file does not exist",
            detail="File not found at validated path",
        )

    validate_extension(resolved.name)
    return resolved


def validate_output_path(file_path: str) -> Path:
    """Validate a path is within output_dir."""
    cfg = get_config()
    return validate_path_within_directory(file_path, cfg.paths.output_dir)


def validate_extension(filename: str) -> None:
    """Validate file extension against allowed list."""
    cfg = get_config()
    ext = Path(filename).suffix.lstrip(".").lower()
    if ext not in cfg.security.allowed_input_extensions:
        raise UnsupportedMediaError(
            message=f"Unsupported file extension: .{ext}",
            detail=f"Allowed: {cfg.security.allowed_input_extensions}",
        )


def _canonical_directory(value: str) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.path.expanduser(value))))


def _resolve_under_directory(file_path: str, allowed_dir: Path) -> Path | None:
    raw = file_path.strip()
    if not raw or "\x00" in raw:
        return None
    expanded = os.path.expanduser(raw)
    allowed_text = os.fspath(allowed_dir)
    if os.path.isabs(expanded):
        candidate_text = os.path.realpath(expanded)
    else:
        candidate_text = os.path.realpath(os.path.join(allowed_text, expanded))
    try:
        if os.path.commonpath([allowed_text, candidate_text]) != allowed_text:
            return None
    except ValueError:
        return None
    return Path(candidate_text)


def sanitize_filename(filename: str) -> str:
    """Sanitize a filename to prevent injection/traversal."""
    # Remove path separators
    name = Path(filename).name
    # Remove null bytes
    name = name.replace("\x00", "")
    # Replace suspicious characters, keep alphanumeric, dots, hyphens, underscores
    name = re.sub(r"[^\w\-.]", "_", name)
    # Remove leading dots (hidden files)
    name = name.lstrip(".")
    # Truncate to reasonable length
    if len(name) > 200:
        stem = Path(name).stem[:180]
        suffix = Path(name).suffix[:20]
        name = stem + suffix
    # Fallback if empty
    if not name:
        name = "unnamed_file"
    return name


def validate_upload_size(size_bytes: int) -> None:
    """Validate upload file size against configured maximum."""
    cfg = get_config()
    max_bytes = cfg.security.max_upload_size_mb * 1024 * 1024
    if size_bytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum: {cfg.security.max_upload_size_mb}MB",
        )
