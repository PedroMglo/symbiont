"""Input and output sanitisation — security boundary enforcement.

All external input (API queries, history, session IDs, model names) passes
through these validators before reaching the Engine or LLM.  Provider
output is sanitised before injection into LLM prompts.
"""

from __future__ import annotations

import re
import uuid

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Allow only printable text + common whitespace (newline, tab)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Model names: alphanumeric, colon, dot, dash, underscore, slash
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9.:/_-]+$")

# Allowed roles in conversation history
_VALID_ROLES = frozenset({"user", "assistant", "system"})

# Hard limits
MAX_QUERY_LENGTH = 65_536
MAX_HISTORY_MESSAGES = 50
MAX_HISTORY_CONTENT_LENGTH = 8192
MAX_CONTEXT_BLOCK_LENGTH = 32_000
MAX_MODEL_NAME_LENGTH = 128


# ---------------------------------------------------------------------------
# Query sanitisation
# ---------------------------------------------------------------------------

def sanitize_text(text: str) -> str:
    """Strip control characters (except newline and tab) from text."""
    return _CONTROL_CHAR_RE.sub("", text)


def sanitize_query(query: str) -> str:
    """Sanitise and validate a user query.

    - Strips control characters
    - Enforces max length
    - Strips leading/trailing whitespace
    """
    clean = sanitize_text(query).strip()
    return clean[:MAX_QUERY_LENGTH]


# ---------------------------------------------------------------------------
# History validation
# ---------------------------------------------------------------------------

def validate_history(history: list[dict] | None) -> list[dict] | None:
    """Validate and sanitise conversation history.

    Ensures:
    - Each entry has only ``role`` and ``content`` keys
    - ``role`` is one of user/assistant/system
    - ``content`` is a non-empty string within size limits
    - Total messages are capped
    """
    if history is None:
        return None

    validated: list[dict] = []
    for entry in history[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if role not in _VALID_ROLES:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        validated.append({
            "role": role,
            "content": sanitize_text(content)[:MAX_HISTORY_CONTENT_LENGTH],
        })

    return validated if validated else None


# ---------------------------------------------------------------------------
# Session ID validation
# ---------------------------------------------------------------------------

def validate_session_id(session_id: str | None) -> str | None:
    """Validate that session_id is a proper UUID4 string.

    Returns the normalised UUID string, or None if invalid/absent.
    """
    if session_id is None:
        return None
    try:
        parsed = uuid.UUID(session_id, version=4)
        return str(parsed)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Model name validation
# ---------------------------------------------------------------------------

def validate_model_name(model: str | None) -> str | None:
    """Validate model name format.

    Allows alphanumeric, colon (tag), dot, dash, underscore, slash.
    Returns None if invalid.
    """
    if model is None:
        return None
    model = model.strip()
    if not model or len(model) > MAX_MODEL_NAME_LENGTH:
        return None
    if not _MODEL_NAME_RE.match(model):
        return None
    return model


# ---------------------------------------------------------------------------
# Context sanitisation
# ---------------------------------------------------------------------------

def sanitize_context(text: str) -> str:
    """Sanitise context block content before LLM prompt injection.

    - Strips control characters
    - Truncates to MAX_CONTEXT_BLOCK_LENGTH
    """
    clean = sanitize_text(text)
    return clean[:MAX_CONTEXT_BLOCK_LENGTH]


# ---------------------------------------------------------------------------
# Inline model prefix extraction
# ---------------------------------------------------------------------------

def extract_model_prefix(query: str) -> tuple[str | None, str]:
    """Detect a model alias prefix in the query text.

    If the query starts with a known model alias or routing key followed by
    a space, returns (alias, remaining_query). Otherwise returns (None, query).

    Examples:
        "gemma3 explain DNS" → ("gemma3", "explain DNS")
        "deep what is X?"   → ("deep", "what is X?")
        "hello world"       → (None, "hello world")
    """

    # Only check the first word
    parts = query.split(None, 1)
    if len(parts) < 2:
        return None, query

    first_word = parts[0].lower()

    # Check routing profiles
    profiles = ["fast", "default", "code", "deep"]

    if first_word in profiles:
        return first_word, parts[1]

    return None, query
