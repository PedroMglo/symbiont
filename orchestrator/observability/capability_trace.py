"""Metadata-only capability trace for local agentic requests.

The trace is intentionally operational: it records routing/provider/storage/LLM
metadata, not prompts, file contents or final answers.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

_REDACTED_KEYS = {
    "content",
    "history",
    "messages",
    "original_query",
    "payload",
    "prompt",
    "query",
    "response",
    "text",
}
_SECRET_KEY_FRAGMENTS = ("secret", "password", "api_key")
_TOKEN_SECRET_KEY_FRAGMENTS = ("auth_token", "access_token", "refresh_token", "bearer_token", "session_token")


def capability_trace_path() -> Path:
    raw = (
        os.environ.get("ORC_CAPABILITY_TRACE_PATH")
        or os.environ.get("AI_LOCAL_CAPABILITY_TRACE_PATH")
        or str(Path(tempfile.gettempdir()) / "ai-local" / "capability_trace.jsonl")
    )
    return Path(raw).expanduser()


def capability_trace_enabled() -> bool:
    raw = os.environ.get("ORC_CAPABILITY_TRACE_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def emit_capability_event(event: str, **fields: Any) -> None:
    """Append one sanitized JSONL event, failing open on trace errors."""

    if not capability_trace_enabled():
        return
    payload: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
    }
    try:
        from orchestrator.agentic.context import get_agentic_context

        ctx = get_agentic_context()
    except Exception:
        ctx = None
    if ctx is not None:
        payload.update({
            "task_id": ctx.task_id,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "session_id": ctx.session_id,
            "mode": ctx.mode,
        })
    payload.update({key: _sanitize_value(key, value) for key, value in fields.items()})
    try:
        path = capability_trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        return


def _sanitize_value(key: str, value: Any, *, depth: int = 0) -> Any:
    lowered = key.lower()
    if lowered in _REDACTED_KEYS or any(token in lowered for token in _SECRET_KEY_FRAGMENTS):
        return "<redacted>"
    if any(token in lowered for token in _TOKEN_SECRET_KEY_FRAGMENTS):
        return "<redacted>"
    if "token" in lowered and not _is_numeric_token_metric(lowered, value):
        return "<redacted>"
    if depth >= 4:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 500:
            return value[:497] + "..."
        return value
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(key, item, depth=depth + 1) for item in list(value)[:25]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (inner_key, inner_value) in enumerate(value.items()):
            if index >= 50:
                result["<truncated>"] = True
                break
            result[str(inner_key)] = _sanitize_value(str(inner_key), inner_value, depth=depth + 1)
        return result
    return str(value)[:500]


def _is_numeric_token_metric(key: str, value: Any) -> bool:
    if not isinstance(value, (int, float)):
        return False
    return key.endswith("_tokens") or key.endswith("_token_count") or key.endswith("_tokens_estimate")
