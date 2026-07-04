"""Admission Controller — decides whether to accept, degrade, queue, or reject LLM requests.

Provides per-backend semaphores, per-user rate limiting, token estimation,
and automatic model downgrade when the primary backend is saturated.

All thresholds and policies are configurable via [admission] in
config/orc/admission.toml — no hardcoded defaults.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.config import AdmissionConfig

log = logging.getLogger(__name__)


class AdmissionDecision(str, Enum):
    """Possible outcomes of the admission check."""

    ACCEPT = "accept"
    DOWNGRADE = "downgrade"
    QUEUE = "queue"
    REJECT = "reject"


@dataclass
class AdmissionResult:
    """Result of an admission evaluation."""

    decision: AdmissionDecision
    backend: str  # recommended backend name
    model: str  # recommended model
    reason: str = ""
    wait_seconds: float = 0.0  # suggested retry-after when REJECT/QUEUE


@dataclass
class _UserBucket:
    """Per-user token bucket for rate limiting."""

    tokens: float
    last_refill: float
    requests_in_window: int = 0
    window_start: float = 0.0


class AdmissionController:
    """Evaluates incoming requests and decides routing/rejection.

    Responsibilities:
    - Per-backend concurrency tracking (semaphores)
    - Per-user/session rate limiting (token bucket)
    - Estimated token budget check
    - Automatic downgrade to lighter model when primary is saturated
    - Clear rejection (429) when the system is overloaded

    Config comes from [admission] section in config/orc/admission.toml.
    """

    def __init__(self, cfg: "AdmissionConfig") -> None:
        self._cfg = cfg

        # Per-backend semaphores (limit concurrent requests per backend)
        self._backend_semaphores: dict[str, asyncio.Semaphore] = {}
        for name, limit in cfg.backend_concurrency.items():
            self._backend_semaphores[name] = asyncio.Semaphore(limit)

        # Global semaphore (total system capacity)
        self._global_semaphore = asyncio.Semaphore(cfg.max_concurrent_global)

        # Per-user tracking
        self._user_buckets: dict[str, _UserBucket] = {}

        # Backend load tracking (in-flight count per backend)
        self._inflight: dict[str, int] = {name: 0 for name in cfg.backend_concurrency}

    def evaluate(
        self,
        *,
        user_id: str,
        estimated_tokens_in: int,
        estimated_tokens_out: int,
        preferred_backend: str,
        preferred_model: str,
        task_complexity: str,
    ) -> AdmissionResult:
        """Synchronous evaluation — can be called from any context.

        Args:
            user_id: Identifier for rate limiting (session_id or API key).
            estimated_tokens_in: Approximate prompt tokens.
            estimated_tokens_out: Requested max_tokens.
            preferred_backend: Backend the router would normally select.
            preferred_model: Model the router would normally select.
            task_complexity: "low", "medium", or "high".
        """
        cfg = self._cfg

        # 1. Check user rate limit
        if not self._check_user_rate(user_id):
            return AdmissionResult(
                decision=AdmissionDecision.REJECT,
                backend=preferred_backend,
                model=preferred_model,
                reason=f"Rate limit exceeded for user {user_id}",
                wait_seconds=cfg.rate_limit_window_seconds,
            )

        # 2. Check total token budget
        total_tokens = estimated_tokens_in + estimated_tokens_out
        if total_tokens > cfg.max_tokens_per_request:
            return AdmissionResult(
                decision=AdmissionDecision.REJECT,
                backend=preferred_backend,
                model=preferred_model,
                reason=f"Token budget {total_tokens} exceeds limit {cfg.max_tokens_per_request}",
            )

        # 3. Check global capacity
        if self._global_semaphore.locked():
            # System at capacity — can we downgrade?
            if task_complexity == "low" and cfg.downgrade_model:
                downgrade_backend = cfg.downgrade_backend or preferred_backend
                return AdmissionResult(
                    decision=AdmissionDecision.DOWNGRADE,
                    backend=downgrade_backend,
                    model=cfg.downgrade_model,
                    reason="System at capacity — downgrading to lighter model",
                )
            return AdmissionResult(
                decision=AdmissionDecision.REJECT,
                backend=preferred_backend,
                model=preferred_model,
                reason="System at full capacity",
                wait_seconds=cfg.reject_retry_after_seconds,
            )

        # 4. Check per-backend capacity
        backend_sem = self._backend_semaphores.get(preferred_backend)
        if backend_sem and backend_sem.locked():
            # Preferred backend full — try downgrade for simple tasks
            if task_complexity in ("low", "medium") and cfg.downgrade_model:
                downgrade_backend = cfg.downgrade_backend or preferred_backend
                downgrade_sem = self._backend_semaphores.get(downgrade_backend)
                if downgrade_sem is None or not downgrade_sem.locked():
                    return AdmissionResult(
                        decision=AdmissionDecision.DOWNGRADE,
                        backend=downgrade_backend,
                        model=cfg.downgrade_model,
                        reason=f"Backend {preferred_backend} saturated — downgrading",
                    )
            # Complex task + backend full → queue or reject
            if cfg.queue_enabled:
                return AdmissionResult(
                    decision=AdmissionDecision.QUEUE,
                    backend=preferred_backend,
                    model=preferred_model,
                    reason=f"Backend {preferred_backend} busy — queuing",
                    wait_seconds=cfg.queue_timeout_seconds,
                )
            return AdmissionResult(
                decision=AdmissionDecision.REJECT,
                backend=preferred_backend,
                model=preferred_model,
                reason=f"Backend {preferred_backend} saturated",
                wait_seconds=cfg.reject_retry_after_seconds,
            )

        # 5. Accept
        return AdmissionResult(
            decision=AdmissionDecision.ACCEPT,
            backend=preferred_backend,
            model=preferred_model,
        )

    async def acquire(self, backend: str) -> None:
        """Acquire a slot for the given backend (blocks until available)."""
        await self._global_semaphore.acquire()
        sem = self._backend_semaphores.get(backend)
        if sem:
            await sem.acquire()
        self._inflight[backend] = self._inflight.get(backend, 0) + 1

    def release(self, backend: str) -> None:
        """Release a slot after request completion."""
        sem = self._backend_semaphores.get(backend)
        if sem:
            sem.release()
        self._global_semaphore.release()
        self._inflight[backend] = max(0, self._inflight.get(backend, 0) - 1)

    def get_inflight(self, backend: str) -> int:
        """Current in-flight requests for a backend."""
        return self._inflight.get(backend, 0)

    def get_load_summary(self) -> dict[str, int]:
        """Return {backend_name: inflight_count} for all backends."""
        return dict(self._inflight)

    # ------------------------------------------------------------------
    # Internal rate limiting
    # ------------------------------------------------------------------

    def _check_user_rate(self, user_id: str) -> bool:
        """Token-bucket rate limiter per user. Returns True if allowed."""
        cfg = self._cfg
        now = time.monotonic()

        bucket = self._user_buckets.get(user_id)
        if bucket is None:
            bucket = _UserBucket(
                tokens=cfg.rate_limit_requests_per_window,
                last_refill=now,
                requests_in_window=0,
                window_start=now,
            )
            self._user_buckets[user_id] = bucket

        # Refill tokens based on elapsed time
        elapsed = now - bucket.last_refill
        refill_rate = cfg.rate_limit_requests_per_window / cfg.rate_limit_window_seconds
        bucket.tokens = min(
            cfg.rate_limit_requests_per_window,
            bucket.tokens + elapsed * refill_rate,
        )
        bucket.last_refill = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_controller: AdmissionController | None = None


def get_admission_controller() -> AdmissionController | None:
    """Return the singleton AdmissionController (None if not configured)."""
    return _controller


def init_admission_controller(cfg: "AdmissionConfig") -> AdmissionController:
    """Initialize the singleton. Called once at startup."""
    global _controller
    _controller = AdmissionController(cfg)
    log.info(
        "AdmissionController initialized: global_cap=%d, backends=%s",
        cfg.max_concurrent_global,
        list(cfg.backend_concurrency.keys()),
    )
    return _controller
