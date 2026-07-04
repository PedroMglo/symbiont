"""Privacy & security — redaction of sensitive content before persistence.

Ensures no secrets, full prompts, or PII leak into logs/ClickHouse/JSONL.
"""

from __future__ import annotations

import re
from typing import Any

from orchestrator.observability.config import PrivacyConfig

# Patterns that look like secrets
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|auth|bearer)\s*[:=]\s*\S+"),
    re.compile(r"(?i)sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"(?i)ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"(?i)ghs_[a-zA-Z0-9]{36}"),
    re.compile(r"[a-f0-9]{40,64}"),  # hex tokens/hashes
    re.compile(r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+"),  # JWT
]

# Email pattern
_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Path pattern (home dirs)
_HOME_PATH_PATTERN = re.compile(r"/home/[a-zA-Z0-9_-]+")

# Env-like patterns
_ENV_VAR_PATTERN = re.compile(r"(?i)([A-Z_]{3,})=(\S+)")


class Redactor:
    """Applies privacy rules to event data before export."""

    def __init__(self, config: PrivacyConfig):
        self._cfg = config

    def redact_string(self, value: str) -> str:
        """Redact secrets and sensitive content from a string."""
        if not value:
            return value

        result = value

        if self._cfg.redact_secrets:
            for pattern in _SECRET_PATTERNS:
                result = pattern.sub("[REDACTED]", result)
            result = _ENV_VAR_PATTERN.sub(r"\1=[REDACTED]", result)

        if self._cfg.redact_paths:
            result = _HOME_PATH_PATTERN.sub("/home/[REDACTED]", result)

        return result

    def redact_error_message(self, msg: str | None, max_length: int = 500) -> str | None:
        """Truncate and redact error messages."""
        if not msg:
            return None
        msg = self.redact_string(msg)
        if len(msg) > max_length:
            msg = msg[:max_length] + "...[truncated]"
        return msg

    def redact_email(self, text: str) -> str:
        """Mask email addresses."""
        return _EMAIL_PATTERN.sub("[EMAIL]", text)

    def should_record_prompt(self) -> bool:
        """Whether full prompt recording is allowed."""
        return self._cfg.record_prompts

    def should_record_response(self) -> bool:
        """Whether full response recording is allowed."""
        return self._cfg.record_responses

    def make_preview(self, text: str, field: str) -> str | None:
        """Create a truncated preview if configured."""
        if field == "prompt":
            if not self._cfg.record_prompt_preview:
                return None
            chars = self._cfg.prompt_preview_chars
        elif field == "response":
            if not self._cfg.record_response_preview:
                return None
            chars = self._cfg.response_preview_chars
        else:
            return None

        if chars <= 0:
            return None
        preview = text[:chars]
        if len(text) > chars:
            preview += "..."
        return self.redact_string(preview)

    def redact_event_dict(self, d: dict[str, Any]) -> dict[str, Any]:
        """Apply redaction rules to a serialised event dict."""
        # Redact error messages
        if "error_message_safe" in d and d["error_message_safe"]:
            d["error_message_safe"] = self.redact_error_message(d["error_message_safe"])

        # Redact metadata_json
        if "metadata_json" in d and d["metadata_json"]:
            d["metadata_json"] = self.redact_string(d["metadata_json"])

        return d


_redactor: Redactor | None = None


def get_redactor() -> Redactor:
    """Get the global redactor instance (lazy init with defaults)."""
    global _redactor
    if _redactor is None:
        _redactor = Redactor(PrivacyConfig())
    return _redactor


def set_redactor(config: PrivacyConfig) -> None:
    """Set global redactor from config."""
    global _redactor
    _redactor = Redactor(config)
