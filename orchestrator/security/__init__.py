"""Security package — subprocess safety + v1.3 defense-in-depth modules.

Public API:
- safe_run(): Whitelisted subprocess execution (existing)
- SecurityLayer: Unified facade for all security checks (v1.3)
- InjectionScanner: Prompt injection detection
- SecretsScanner: Secrets detection and redaction
- AgentRateLimiter: Per-agent rate limiting
- AuditTrail: Immutable audit log
"""

from __future__ import annotations

import logging
import subprocess

from orchestrator.config import get_settings

log = logging.getLogger(__name__)


def safe_run(cmd: list[str], *, timeout: int | None = None, cwd: str | None = None) -> str:
    """Run a whitelisted command and return its stdout.

    Args:
        cmd: Command and arguments as a list (never a string — no shell expansion).
        timeout: Override timeout in seconds. Defaults to ``security.max_command_timeout``.
        cwd: Optional working directory for the subprocess.

    Returns:
        stdout stripped of surrounding whitespace, or ``""`` on any failure.
    """
    cfg = get_settings().security
    if not cmd:
        return ""
    if cmd[0] not in cfg.allowed_commands:
        log.debug("safe_run: command %r not in allowed_commands, skipping", cmd[0])
        return ""

    effective_timeout = min(timeout, cfg.max_command_timeout) if timeout is not None else cfg.max_command_timeout

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            shell=False,  # explicit — never allow shell expansion
            cwd=cwd,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.debug("safe_run: %s timed out after %ds", cmd, effective_timeout)
        return ""
    except (FileNotFoundError, OSError) as exc:
        log.debug("safe_run: %s failed: %s", cmd, exc)
        return ""
