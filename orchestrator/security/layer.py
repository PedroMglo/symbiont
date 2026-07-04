"""Unified security layer — single entry point for all v1.3 security checks.

Composes injection scanning, secrets detection, agent sandboxing,
rate limiting, and audit trail behind feature flags.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from orchestrator.security.audit import AuditTrail
from orchestrator.security.injection import InjectionScanner, ScanResult
from orchestrator.security.rate_limiter import AgentRateLimiter
from orchestrator.security.secrets import SecretsScanner

if TYPE_CHECKING:
    from orchestrator.config import SecurityConfig

log = logging.getLogger(__name__)


class SecurityLayer:
    """Unified security layer — orchestrates all v1.3 security modules."""

    def __init__(self, cfg: "SecurityConfig"):
        self._cfg = cfg
        self._injection: InjectionScanner | None = None
        self._secrets: SecretsScanner | None = None
        self._rate_limiter: AgentRateLimiter | None = None
        self._audit: AuditTrail | None = None

        if cfg.injection_scanning:
            self._injection = InjectionScanner(block_threshold=cfg.injection_block_threshold)
        if cfg.secrets_scanning:
            self._secrets = SecretsScanner()
        if cfg.rate_limiting:
            self._rate_limiter = AgentRateLimiter(
                calls_per_minute=cfg.rate_limit_calls_per_minute,
            )
        if cfg.audit_trail:
            self._audit = AuditTrail()

    @property
    def injection_enabled(self) -> bool:
        return self._injection is not None

    @property
    def secrets_enabled(self) -> bool:
        return self._secrets is not None

    @property
    def rate_limiting_enabled(self) -> bool:
        return self._rate_limiter is not None

    @property
    def audit(self) -> AuditTrail | None:
        return self._audit

    def scan_input(self, text: str) -> ScanResult | None:
        """Scan user input for injection attempts. Returns None if disabled."""
        if self._injection is None:
            return None
        return self._injection.scan_input(text)

    def scan_context_block(self, text: str) -> ScanResult | None:
        """Scan a context block for injection attempts."""
        if self._injection is None:
            return None
        return self._injection.scan_context(text)

    def scan_output(self, text: str) -> ScanResult | None:
        """Scan LLM output for injection markers."""
        if self._injection is None:
            return None
        return self._injection.scan_output(text)

    def redact_secrets(self, text: str) -> tuple[str, bool]:
        """Redact secrets from text. Returns (text, had_secrets)."""
        if self._secrets is None:
            return text, False
        if not self._secrets.has_secrets(text):
            return text, False
        return self._secrets.redact(text), True

    def check_rate_limit(self, agent_name: str, tokens: int = 0) -> bool:
        """Check if agent is within rate limits. Returns True if allowed."""
        if self._rate_limiter is None:
            return True
        return self._rate_limiter.acquire(agent_name, tokens)

    def log_security_event(
        self,
        *,
        event_type: str,
        request_id: str,
        agent_name: str | None = None,
        session_id: str | None = None,
        **detail: Any,
    ) -> None:
        """Record a security event to the audit trail."""
        if self._audit is None:
            return
        self._audit.log_security_event(
            event_type=event_type,
            request_id=request_id,
            agent_name=agent_name,
            session_id=session_id,
            **detail,
        )

    def log_llm_call(
        self,
        *,
        request_id: str,
        agent_name: str | None = None,
        session_id: str | None = None,
        model: str = "",
        tokens: int = 0,
        **extra: Any,
    ) -> None:
        """Record an LLM call to the audit trail."""
        if self._audit is None:
            return
        self._audit.log_llm_call(
            request_id=request_id,
            agent_name=agent_name,
            session_id=session_id,
            model=model,
            tokens=tokens,
            **extra,
        )
