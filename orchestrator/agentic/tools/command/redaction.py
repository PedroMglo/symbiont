"""Output redaction for command observations."""

from __future__ import annotations

import re

from orchestrator.agentic.tools.command.mounts import redact_real_paths
from orchestrator.agentic.tools.command.schemas import CommandContext

SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[a-z0-9._~+/-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


def redact_text(text: str, *, context: CommandContext | None = None) -> str:
    redacted = text or ""
    if context is not None:
        redacted = redact_real_paths(redacted, context)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: _secret_replacement(match.group(0)), redacted)
    return redacted


def truncate_text(text: str, *, max_bytes: int) -> tuple[str, bool]:
    encoded = (text or "").encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text or "", False
    truncated = encoded[:max(0, max_bytes)].decode("utf-8", errors="replace")
    return truncated + "\n[agentic-command-output-truncated]", True


def redact_and_truncate(
    text: str,
    *,
    context: CommandContext | None = None,
    max_bytes: int,
) -> tuple[str, bool]:
    return truncate_text(redact_text(text, context=context), max_bytes=max_bytes)


def _secret_replacement(value: str) -> str:
    if ":" in value:
        return value.split(":", 1)[0] + ": [REDACTED]"
    if "=" in value:
        return value.split("=", 1)[0] + "=[REDACTED]"
    return "[REDACTED]"
