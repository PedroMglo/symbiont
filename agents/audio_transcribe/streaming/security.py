"""Security: API key verification for the streaming sub-agent.

Shares the same API key as the main transcriber (from Docker secret or env).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.websockets import WebSocket


_DEFAULT_SECRET_PATH = Path("/run/secrets/audio_transcribe_api_key")


def _allow_unauthenticated_dev() -> bool:
    value = os.environ.get("AUDIO_TRANSCRIBE_SECURITY_ALLOW_UNAUTHENTICATED_DEV", "")
    return value.lower() in {"1", "true", "yes"}


def _get_api_key() -> str:
    """Load API key from Docker secret or environment."""
    env_key = os.environ.get("AUDIO_TRANSCRIBE_API_KEY", "").strip()
    if env_key:
        return env_key
    env_file = os.environ.get("AUDIO_TRANSCRIBE_API_KEY_FILE", "").strip()
    secret_paths = [Path(env_file)] if env_file else []
    secret_paths.append(_DEFAULT_SECRET_PATH)
    for path in secret_paths:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def _header_key(headers) -> str:
    key = headers.get("X-API-Key", "").strip()
    if key:
        return key
    auth = headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


async def verify_api_key(request: Request) -> None:
    """FastAPI dependency: verify API key."""
    expected = _get_api_key()
    if not expected:
        if _allow_unauthenticated_dev():
            return
        raise HTTPException(status_code=503, detail="Audio streaming API key is not configured")

    provided = _header_key(request.headers)
    if not provided:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid API key")


async def verify_websocket_api_key(websocket: WebSocket) -> bool:
    """Verify WebSocket API key before accept()."""
    expected = _get_api_key()
    if not expected:
        if _allow_unauthenticated_dev():
            return True
        await websocket.close(code=1008, reason="API key is not configured")
        return False

    provided = _header_key(websocket.headers)
    if not provided or not secrets.compare_digest(provided, expected):
        await websocket.close(code=1008, reason="Invalid API key")
        return False
    return True
