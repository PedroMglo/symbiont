"""Security utilities for extrator path, upload, and API validation."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from fastapi import HTTPException, Request

from extrator.config import get_config
from sharedai.servicekit.auth import read_secret_file, verify_service_token


class PathSecurityError(RuntimeError):
    """Raised when a path is outside configured safety boundaries."""


def get_api_key() -> str | None:
    cfg = get_config()
    key = os.environ.get(cfg.security.api_key_env, "").strip()
    if not key:
        key = read_secret_file(os.environ.get(f"{cfg.security.api_key_env}_FILE", ""))
    if not key:
        key = os.environ.get("API_KEY", "").strip()
    if not key:
        key = read_secret_file(os.environ.get("API_KEY_FILE", ""))
    if not key:
        key = os.environ.get("INTERNAL_API_KEY", "").strip()
    if not key:
        key = read_secret_file(os.environ.get("INTERNAL_API_KEY_FILE", ""))
    if not key:
        key = read_secret_file("/run/secrets/internal_api_key")
    return key if key else None


async def verify_api_key(request: Request) -> None:
    verify_service_token(
        service_name="Extrator",
        configured_key=get_api_key() or "",
        authorization=request.headers.get("Authorization"),
        x_api_key=request.headers.get("X-API-Key"),
    )


def sanitize_filename(filename: str) -> str:
    name = Path(filename).name.replace("\x00", "")
    name = re.sub(r"[^\w\-.]", "_", name).lstrip(".")
    if len(name) > 200:
        suffix = Path(name).suffix[:20]
        name = Path(name).stem[:180] + suffix
    if not name:
        raise PathSecurityError("Empty filename after sanitization")
    return name


def _resolved_allowed_roots() -> list[Path]:
    return [Path(root).expanduser().resolve() for root in get_config().security.allowed_roots]


def validate_within_allowed_roots(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    for root in _resolved_allowed_roots():
        try:
            if target == root or target.is_relative_to(root):
                return target
        except OSError:
            continue
    raise PathSecurityError(f"Path is outside allowed roots: {target}")


def validate_output_path(path: str | Path, root: str | Path) -> Path:
    root_path = Path(root).expanduser().resolve()
    target = Path(path).expanduser().resolve()
    if target == root_path or target.is_relative_to(root_path):
        return target
    raise PathSecurityError(f"Output path is outside configured root: {target}")


def extension_for(path: str | Path) -> str:
    name = Path(path).name.lower()
    for ext in ("jsonl.gz", "ndjson.gz", "csv.gz", "tsv.gz"):
        if name.endswith(f".{ext}"):
            return ext
    return Path(path).suffix.lower().lstrip(".")


def is_skipped(path: str | Path) -> tuple[bool, str]:
    cfg = get_config()
    p = Path(path)
    parts = set(p.parts)
    for pattern in cfg.security.skip_patterns:
        if pattern in parts or fnmatch.fnmatch(p.name, pattern):
            return True, pattern
    ext = extension_for(p)
    if ext in cfg.security.denied_extensions:
        return True, f"denied_extension:{ext}"
    return False, ""


def validate_extension(path: str | Path, *, for_extraction: bool) -> str:
    cfg = get_config()
    ext = extension_for(path)
    allowed = cfg.formats.extract_input_extensions if for_extraction else cfg.security.allowed_extensions
    if ext not in allowed:
        raise PathSecurityError(f"Unsupported extension: .{ext}")
    if ext in cfg.security.denied_extensions:
        raise PathSecurityError(f"Denied extension: .{ext}")
    return ext


def validate_file_size(path: str | Path) -> None:
    cfg = get_config()
    size = Path(path).stat().st_size
    if size > cfg.security.max_file_size_bytes:
        raise PathSecurityError(f"File exceeds configured size limit: {path}")


def validate_upload_size(size_bytes: int) -> None:
    cfg = get_config()
    if size_bytes > cfg.security.max_upload_size_bytes:
        raise HTTPException(status_code=413, detail="Upload exceeds configured size limit")


def validate_input_path(path: str | Path, *, for_extraction: bool) -> Path:
    target = validate_within_allowed_roots(path)
    if not target.exists():
        raise PathSecurityError(f"Input path does not exist: {target}")
    skipped, reason = is_skipped(target)
    if skipped:
        raise PathSecurityError(f"Input path is skipped by policy: {reason}")
    if target.is_file():
        validate_extension(target, for_extraction=for_extraction)
        validate_file_size(target)
    return target


def detect_mime(path: str | Path) -> str:
    try:
        import magic

        return str(magic.from_file(str(path), mime=True))
    except Exception:
        return ""
