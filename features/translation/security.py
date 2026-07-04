"""Security helpers for the translation feature."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from sharedai.servicekit.auth import read_secret_file


_DEFAULT_INTERNAL_SECRET_FILE = "/run/secrets/internal_api_key"
_SERVICE_KEY_FILE_ENVS = (
    "TRANSLATION_INTERNAL_API_KEY_FILE",
    "TRANSLATION_API_KEY_FILE",
    "API_KEY_FILE",
    "INTERNAL_API_KEY_FILE",
)
_SERVICE_KEY_ENVS = (
    "TRANSLATION_INTERNAL_API_KEY",
    "TRANSLATION_API_KEY",
    "API_KEY",
    "INTERNAL_API_KEY",
)
_REDACTED = "<redacted>"
_REDACTED_TEXT = "<redacted_text>"
_SENSITIVE_TEXT_KEYS = {
    "content",
    "document",
    "message",
    "messages",
    "normalized",
    "normalized_query",
    "original",
    "original_query",
    "prompt",
    "query",
    "response",
    "text",
    "translated",
    "working_query",
}
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "password",
    "passwd",
    "secret",
    "set-cookie",
    "token",
    "x-api-key",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization|x-api-key|api[_-]?key|token|secret|password)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)
_FILTER_NAME = "translation-redaction-filter"
_LOGGERS = (
    "app",
    "cache",
    "config",
    "glossary",
    "language_detector",
    "normalizer",
    "protected_spans",
    "ptpt_linter",
    "security",
    "spellcheck",
    "translator",
)


def get_translation_api_key() -> str:
    """Resolve the internal service token from the integrated secret surface."""

    for env_name in _SERVICE_KEY_FILE_ENVS:
        key = read_secret_file(os.environ.get(env_name, ""))
        if key:
            return key
    for env_name in _SERVICE_KEY_ENVS:
        key = os.environ.get(env_name, "").strip()
        if key:
            return key
    return read_secret_file(_DEFAULT_INTERNAL_SECRET_FILE)


def redact_sensitive_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_replace_secret_match, redacted)
    return redacted


def redact_log_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _redact_for_key(str(key), item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_log_value(item) for item in value)
    if isinstance(value, list):
        return [redact_log_value(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def install_translation_log_redaction() -> None:
    log_filter = _RedactingLogFilter()
    for logger_name in _LOGGERS:
        logger = logging.getLogger(logger_name)
        if not any(getattr(item, "name", "") == _FILTER_NAME for item in logger.filters):
            logger.addFilter(log_filter)


class _RedactingLogFilter(logging.Filter):
    name = _FILTER_NAME

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_value(record.msg)
        if isinstance(record.args, Mapping | tuple):
            record.args = redact_log_value(record.args)
        return True


def _redact_for_key(key: str, value: Any) -> Any:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SECRET_KEYS:
        return _REDACTED
    if normalized in _SENSITIVE_TEXT_KEYS:
        return _redact_text_value(value)
    return redact_log_value(value)


def _redact_text_value(value: Any) -> Any:
    if isinstance(value, str):
        return _REDACTED_TEXT
    if isinstance(value, Mapping):
        return {
            str(key): _redact_text_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_redact_text_value(item) for item in value]
    return _REDACTED_TEXT


def _replace_secret_match(match: re.Match[str]) -> str:
    text = match.group(0)
    if text.lower().startswith("bearer "):
        return "Bearer " + _REDACTED
    if len(match.groups()) >= 2:
        return f"{match.group(1)}={_REDACTED}"
    return _REDACTED
