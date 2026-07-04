"""LLM Router — selects backend and model; implements LLMClient protocol.

The Engine depends only on this interface. It has no knowledge of Ollama,
vLLM, llama.cpp, LM Studio, or any other concrete backend.

Selection order (capability_first strategy):
  1. Healthy backends that have the requested model, sorted by priority.
  2. If require_local, skip backends with privacy_level != "local" | "lan".
  3. If no direct match and fallback_enabled, resolve via model profiles.
  4. If still nothing, raise LLMRoutingError with available model suggestions.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

import httpx
from sharedai.llm.utils import mask_url

from context_governor import ContextGovernorBlocked, govern_messages_for_llm_call
from orchestrator.llm.openai_compat import OpenAICompatibleLLMClient
from orchestrator.observability.capability_trace import emit_capability_event

if TYPE_CHECKING:
    from orchestrator.config import (
        BackendConfig,
        InferenceProfileConfig,
        LatencyRoutingConfig,
        LLMConfig,
        ModelProfileConfig,
    )
    from orchestrator.llm.base import BatchRequest, BatchResult
    from orchestrator.llm.openai_compat import InstrumentedStreamResult
    from orchestrator.observability.models import LLMChatResult

log = logging.getLogger(__name__)

# Used only when the router is instantiated without inference profiles, or
# when a low-level backend client is called directly outside the router.
_FALLBACK_MAX_TOKENS = 4096
_OLLAMA_PS_CACHE: dict[str, tuple[float, dict]] = {}


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------

class LLMRoutingError(RuntimeError):
    """Raised when no backend can serve the requested model."""


# ---------------------------------------------------------------------------
# Internal health state
# ---------------------------------------------------------------------------

@dataclass
class BackendHealth:
    backend_name: str
    status: str  # healthy | unavailable | disabled | unknown
    models_detected: list[str] = field(default_factory=list)
    last_error: str | None = None
    latency_ms: float | None = None
    checked_at: float = 0.0  # time.monotonic()


# ---------------------------------------------------------------------------
# LLMRouter
# ---------------------------------------------------------------------------

class LLMRouter:
    """Routes LLM requests across multiple OpenAI-compatible backends.

    Implements the LLMClient protocol so the Engine can use it transparently.
    Also exposes ``health_report()`` for CLI and API health endpoints.
    """

    def __init__(
        self,
        config: "LLMConfig",
        *,
        latency_routing: "LatencyRoutingConfig | None" = None,
        inference_profiles: Mapping[str, "InferenceProfileConfig"] | None = None,
    ) -> None:
        self._cfg = config
        self._latency_routing = latency_routing
        self._inference_profiles = inference_profiles or {}
        # Only instantiate clients for enabled backends
        self._clients: dict[str, tuple["BackendConfig", OpenAICompatibleLLMClient]] = {}
        for backend in config.backends:
            if backend.enabled:
                self._clients[backend.name] = (backend, OpenAICompatibleLLMClient(backend))
        # Per-backend health cache
        self._health_cache: dict[str, BackendHealth] = {}
        # Per-backend call stats
        self._call_count: dict[str, int] = {}
        self._total_latency_ms: dict[str, float] = {}
        # Per-model latency tracking (for latency-aware routing)
        self._model_latencies: dict[str, list[float]] = {}  # model → last N latencies
        # Max latency samples retained per model — from config when provided
        self._model_latency_max_samples: int = (
            latency_routing.max_latency_samples if latency_routing else 50
        )
        # Last backend used (for observability)
        self._last_backend_used: str = ""

    def _profile_key_for_model(self, requested_model: str, resolved_model: str) -> str | None:
        for key in (requested_model, resolved_model):
            if key in self._inference_profiles:
                return key

        for profile in self._cfg.model_profiles:
            if not profile.enabled or profile.alias not in self._inference_profiles:
                continue
            if (
                requested_model == profile.alias
                or resolved_model == profile.alias
                or requested_model in profile.preferred_models
                or resolved_model in profile.preferred_models
                or requested_model == profile.fallback_model
                or resolved_model == profile.fallback_model
            ):
                return profile.alias

        if "default" in self._inference_profiles:
            return "default"
        return None

    def _resolve_max_tokens(
        self,
        requested_model: str,
        resolved_model: str,
        max_tokens: int | None,
    ) -> int:
        if max_tokens is not None:
            return max_tokens
        profile_key = self._profile_key_for_model(requested_model, resolved_model)
        if profile_key is None:
            return _FALLBACK_MAX_TOKENS
        return self._inference_profiles[profile_key].num_predict

    def _resolve_timeout(
        self,
        backend: "BackendConfig",
        timeout: float | None,
        *,
        stream: bool = False,
    ) -> float:
        if timeout is not None:
            return timeout
        return float(backend.stream_timeout if stream else backend.request_timeout)

    def _resolve_num_ctx(
        self,
        requested_model: str,
        resolved_model: str,
        num_ctx: int | None,
    ) -> int | None:
        if num_ctx is not None:
            return num_ctx
        profile_key = self._profile_key_for_model(requested_model, resolved_model)
        if profile_key is None:
            return None
        return self._inference_profiles[profile_key].num_ctx

    def _govern_messages(
        self,
        messages: list[dict],
        *,
        requested_model: str,
        resolved_model: str,
        backend_name: str,
        max_tokens: int | None,
        num_ctx: int | None = None,
        phase: str,
        stream: bool,
    ) -> list[dict]:
        try:
            package = govern_messages_for_llm_call(
                messages,
                model=resolved_model,
                phase=phase,
                reserved_response_tokens=max_tokens,
                context_window_tokens=self._resolve_num_ctx(requested_model, resolved_model, num_ctx),
            )
        except ContextGovernorBlocked as exc:
            emit_capability_event(
                "context_governor",
                backend=backend_name,
                requested_model=requested_model,
                resolved_model=resolved_model,
                phase=phase,
                stream=stream,
                decision="block",
                error=str(exc),
            )
            raise LLMRoutingError(str(exc)) from exc

        emit_capability_event(
            "context_governor",
            backend=backend_name,
            requested_model=requested_model,
            resolved_model=resolved_model,
            stream=stream,
            **package.to_event_fields(),
        )
        return package.messages

    # ------------------------------------------------------------------
    # Per-model latency tracking
    # ------------------------------------------------------------------

    def _record_model_latency(self, model: str, latency_ms: float) -> None:
        """Record a latency sample for a model."""
        if model not in self._model_latencies:
            self._model_latencies[model] = []
        samples = self._model_latencies[model]
        samples.append(latency_ms)
        # Keep only last N samples
        if len(samples) > self._model_latency_max_samples:
            self._model_latencies[model] = samples[-self._model_latency_max_samples:]

    def get_model_avg_latency(self, model: str) -> float | None:
        """Get average latency for a model from recent samples."""
        samples = self._model_latencies.get(model)
        if not samples:
            return None
        return sum(samples) / len(samples)

    def get_model_p95_latency(self, model: str) -> float | None:
        """Get p95 latency for a model from recent samples."""
        samples = self._model_latencies.get(model)
        if not samples or len(samples) < 3:
            return None
        sorted_samples = sorted(samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    # ------------------------------------------------------------------
    # Health management
    # ------------------------------------------------------------------

    def _get_health(self, name: str) -> BackendHealth:
        """Return cached health or refresh if TTL expired."""
        cached = self._health_cache.get(name)
        ttl = float(self._cfg.health_cache_seconds)
        if cached and (time.monotonic() - cached.checked_at < ttl):
            return cached
        return self._refresh_health(name)

    def _refresh_health(self, name: str) -> BackendHealth:
        """Call backend health probe and update cache."""
        _, client = self._clients[name]
        t0 = time.monotonic()
        try:
            ok = client._probe_health()
            latency_ms = (time.monotonic() - t0) * 1000.0
            models_detected = client.list_models() if ok else []
            status = "healthy" if ok else "unavailable"
            h = BackendHealth(
                backend_name=name,
                status=status,
                models_detected=models_detected,
                last_error=None,
                latency_ms=round(latency_ms, 1),
                checked_at=time.monotonic(),
            )
        except Exception as exc:
            h = BackendHealth(
                backend_name=name,
                status="unavailable",
                last_error=str(exc)[:120],
                latency_ms=None,
                checked_at=time.monotonic(),
            )
        self._health_cache[name] = h
        log.debug("LLMRouter: health[%s] = %s", name, h.status)
        return h

    # ------------------------------------------------------------------
    # Backend / model selection
    # ------------------------------------------------------------------

    def _find_profile(self, model: str) -> "ModelProfileConfig | None":
        """Return the ModelProfile whose preferred_models contains model."""
        for p in self._cfg.model_profiles:
            if not p.enabled:
                continue
            if model in p.preferred_models:
                return p
        return None

    def _rank_candidates(
        self,
        candidates: list[tuple["BackendConfig", OpenAICompatibleLLMClient, str]],
    ) -> tuple["BackendConfig", OpenAICompatibleLLMClient, str]:
        """Pick the best candidate.

        Default behaviour preserves priority order (candidates are already
        priority-sorted, so the first is returned). When latency-aware routing
        is enabled and there is more than one candidate, candidates are scored
        using config-driven weights:

            score = priority_weight * (1 / (1 + priority))
                    + warm_bonus       (if backend is healthy/warm)
                    - p95_penalty_per_second * (p95_latency_ms / 1000)

        The highest score wins. All weights come from [llm.latency_routing].
        """
        lr = self._latency_routing
        if lr is None or not lr.enabled or len(candidates) < 2:
            return candidates[0]

        def _score(item: tuple["BackendConfig", OpenAICompatibleLLMClient, str]) -> float:
            bcfg, _client, resolved = item
            score = lr.priority_weight * (1.0 / (1.0 + float(bcfg.priority)))
            health = self._health_cache.get(bcfg.name)
            if health and health.status == "healthy":
                score += lr.warm_bonus
            p95 = self.get_model_p95_latency(resolved)
            if p95 is not None:
                score -= lr.p95_penalty_per_second * (p95 / 1000.0)
            return score

        best = max(candidates, key=_score)
        log.debug(
            "LLMRouter: latency-aware selection chose backend=%s (from %d candidates)",
            best[0].name,
            len(candidates),
        )
        return best

    def _select(
        self,
        model: str,
        *,
        require_local: bool = False,
        _depth: int = 0,
    ) -> tuple["BackendConfig", OpenAICompatibleLLMClient, str]:
        """Select (backend_config, client, resolved_model) for a given model name.

        Args:
            model: Model name or alias to route.
            require_local: If True, skip backends with privacy_level='remote'.
            _depth: Recursion guard for fallback resolution.
        """
        if _depth > 1:
            raise LLMRoutingError(f"No available backend for model {model!r}")

        _PRIVATE = frozenset({"local", "lan"})

        candidates = []
        for name, (bcfg, client) in sorted(
            self._clients.items(), key=lambda kv: kv[1][0].priority
        ):
            if require_local and bcfg.privacy_level not in _PRIVATE:
                log.debug("LLMRouter: skip %s — privacy_level=%s (require_local)", name, bcfg.privacy_level)
                continue
            h = self._get_health(name)
            if h.status != "healthy":
                log.debug("LLMRouter: skip %s — status=%s", name, h.status)
                continue
            # model in static config list OR in dynamically detected list
            available = set(bcfg.models) | set(h.models_detected)
            if model in available:
                candidates.append((bcfg, client, model))

        if candidates:
            chosen = self._rank_candidates(candidates)
            log.debug("LLMRouter: selected backend=%s model=%s", chosen[0].name, model)
            return chosen

        # No direct match — try model profile fallback
        if self._cfg.fallback_enabled:
            profile = self._find_profile(model)
            if profile and profile.fallback_model and profile.fallback_model != model:
                log.info(
                    "LLMRouter: model %r not found — falling back to %r (profile %r)",
                    model,
                    profile.fallback_model,
                    profile.alias,
                )
                try:
                    return self._select(
                        profile.fallback_model,
                        require_local=require_local,
                        _depth=_depth + 1,
                    )
                except LLMRoutingError:
                    pass

            # Try each preferred model in the profile before giving up
            if profile:
                for preferred in profile.preferred_models:
                    if preferred == model:
                        continue
                    try:
                        result = self._select(
                            preferred,
                            require_local=require_local,
                            _depth=_depth + 1,
                        )
                        log.info(
                            "LLMRouter: model %r not found — using profile alternative %r",
                            model,
                            preferred,
                        )
                        return result
                    except LLMRoutingError:
                        continue

        all_models = self.list_models()
        suggestion = f" Available: {all_models}" if all_models else " (is any backend running?)"
        raise LLMRoutingError(f"No available backend for model {model!r}.{suggestion}")

    # ------------------------------------------------------------------
    # LLMClient protocol implementation
    # ------------------------------------------------------------------

    def generate(
        self,
        prompt: str,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        return self.chat(
            [{"role": "user", "content": prompt}],
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    def chat(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        bcfg, client, resolved = self._select(model)
        if resolved != model:
            log.info("LLMRouter: %r → %r via backend=%s", model, resolved, bcfg.name)
        self._last_backend_used = bcfg.name
        max_tokens = self._resolve_max_tokens(model, resolved, max_tokens)
        timeout = self._resolve_timeout(bcfg, timeout)
        messages = self._govern_messages(
            messages,
            requested_model=model,
            resolved_model=resolved,
            backend_name=bcfg.name,
            max_tokens=max_tokens,
            phase="chat",
            stream=False,
        )
        t0 = time.monotonic()
        result = client.chat(messages, resolved, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        elapsed = (time.monotonic() - t0) * 1000
        self._call_count[bcfg.name] = self._call_count.get(bcfg.name, 0) + 1
        self._total_latency_ms[bcfg.name] = self._total_latency_ms.get(bcfg.name, 0.0) + elapsed
        emit_capability_event(
            "llm_call",
            backend=bcfg.name,
            requested_model=model,
            resolved_model=resolved,
            fallback_used=resolved != model,
            latency_ms=round(elapsed, 1),
            max_tokens=max_tokens,
            stream=False,
            accelerator=_backend_accelerator_evidence(bcfg.name, getattr(bcfg, "base_url", ""), resolved),
        )
        return result

    def chat_instrumented(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
        num_ctx: int | None = None,
        use_native_ollama: bool = False,
        intent: str | None = None,
        complexity: str | None = None,
        require_local: bool = False,
    ) -> "LLMChatResult":
        """chat() with full observability metadata returned in LLMChatResult."""
        from orchestrator.observability.models import RouterDecision
        from orchestrator.observability.telemetry import trace_llm_call

        bcfg, client, resolved = self._select(model, require_local=require_local)
        fallback_used = resolved != model
        max_tokens = self._resolve_max_tokens(model, resolved, max_tokens)
        timeout = self._resolve_timeout(bcfg, timeout)
        messages = self._govern_messages(
            messages,
            requested_model=model,
            resolved_model=resolved,
            backend_name=bcfg.name,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            phase="chat",
            stream=False,
        )

        decision = RouterDecision(
            intent=intent,
            complexity=complexity,
            requested_model=model,
            resolved_model=resolved,
            backend=bcfg.name,
            fallback_used=fallback_used,
            fallback_reason="model_unavailable" if fallback_used else None,
            privacy_mode=require_local,
            decision_reason="capability_first",
        )

        with trace_llm_call(resolved, bcfg.name, stream=False) as tctx:
            chat_result = client.chat_instrumented(
                messages, resolved,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                num_ctx=num_ctx,
                use_native_ollama=use_native_ollama,
            )
            chat_result.router_decision = decision

            # Populate trace context with result metrics
            tctx["prompt_tokens"] = getattr(chat_result, "prompt_tokens", None)
            tctx["completion_tokens"] = getattr(chat_result, "completion_tokens", None)
            tctx["total_tokens"] = getattr(chat_result, "total_tokens", None)
            tctx["latency_ms"] = chat_result.latency_ms
            tctx["cold_start"] = getattr(chat_result, "cold_start", False)
            tctx["tokens_per_second"] = getattr(chat_result, "tokens_per_second", None)
            tctx["first_token_latency_ms"] = getattr(chat_result, "first_token_latency_ms", None)

        # Track stats
        self._call_count[bcfg.name] = self._call_count.get(bcfg.name, 0) + 1
        self._total_latency_ms[bcfg.name] = self._total_latency_ms.get(bcfg.name, 0.0) + chat_result.latency_ms
        self._record_model_latency(resolved, chat_result.latency_ms)
        self._last_backend_used = bcfg.name
        emit_capability_event(
            "llm_call",
            backend=bcfg.name,
            requested_model=model,
            resolved_model=resolved,
            fallback_used=fallback_used,
            fallback_reason="model_unavailable" if fallback_used else None,
            latency_ms=round(float(chat_result.latency_ms or 0.0), 1),
            max_tokens=max_tokens,
            stream=False,
            intent=intent,
            complexity=complexity,
            accelerator=_backend_accelerator_evidence(bcfg.name, getattr(bcfg, "base_url", ""), resolved),
        )
        return chat_result

    def chat_stream(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        bcfg, client, resolved = self._select(model)
        if resolved != model:
            log.info("LLMRouter: stream %r → %r via backend=%s", model, resolved, bcfg.name)
        max_tokens = self._resolve_max_tokens(model, resolved, max_tokens)
        timeout = self._resolve_timeout(bcfg, timeout, stream=True)
        messages = self._govern_messages(
            messages,
            requested_model=model,
            resolved_model=resolved,
            backend_name=bcfg.name,
            max_tokens=max_tokens,
            phase="streaming",
            stream=True,
        )
        t0 = time.monotonic()
        yield from client.chat_stream(
            messages, resolved, temperature=temperature, max_tokens=max_tokens, timeout=timeout
        )
        elapsed = (time.monotonic() - t0) * 1000
        self._call_count[bcfg.name] = self._call_count.get(bcfg.name, 0) + 1
        self._total_latency_ms[bcfg.name] = self._total_latency_ms.get(bcfg.name, 0.0) + elapsed
        self._record_model_latency(resolved, elapsed)
        emit_capability_event(
            "llm_call",
            backend=bcfg.name,
            requested_model=model,
            resolved_model=resolved,
            fallback_used=resolved != model,
            latency_ms=round(elapsed, 1),
            max_tokens=max_tokens,
            stream=True,
            accelerator=_backend_accelerator_evidence(bcfg.name, getattr(bcfg, "base_url", ""), resolved),
        )

    def chat_stream_instrumented(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
        num_ctx: int | None = None,
        intent: str | None = None,
        complexity: str | None = None,
        require_local: bool = False,
    ) -> "InstrumentedStreamResult":
        """chat_stream() with full observability metadata.

        Returns an InstrumentedStreamResult (iterable of str).
        After iteration, result.result is the LLMChatResult with full metadata.
        """
        from orchestrator.observability.events import EventName
        from orchestrator.observability.models import RouterDecision
        from orchestrator.observability.telemetry import emit_event

        bcfg, client, resolved = self._select(model, require_local=require_local)
        fallback_used = resolved != model
        max_tokens = self._resolve_max_tokens(model, resolved, max_tokens)
        timeout = self._resolve_timeout(bcfg, timeout, stream=True)
        messages = self._govern_messages(
            messages,
            requested_model=model,
            resolved_model=resolved,
            backend_name=bcfg.name,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            phase="streaming",
            stream=True,
        )

        decision = RouterDecision(
            intent=intent,
            complexity=complexity,
            requested_model=model,
            resolved_model=resolved,
            backend=bcfg.name,
            fallback_used=fallback_used,
            fallback_reason="model_unavailable" if fallback_used else None,
            privacy_mode=require_local,
            decision_reason="capability_first",
        )

        # Emit stream start event
        emit_event(
            EventName.LLM_STREAM_STARTED,
            model=resolved,
            backend=bcfg.name,
            stream=True,
        )

        stream_result = client.chat_stream_instrumented(
            messages, resolved,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            num_ctx=num_ctx,
        )
        # Attach decision so caller can access it after iteration
        stream_result._router_decision = decision  # type: ignore[attr-defined]

        # Track stats after iteration via a wrapper
        original_iter = stream_result.__iter__

        def _tracking_iter():
            yield from original_iter()
            # After stream completes, update stats and attach decision
            if stream_result.result:
                stream_result.result.router_decision = decision
                self._call_count[bcfg.name] = self._call_count.get(bcfg.name, 0) + 1
                self._total_latency_ms[bcfg.name] = (
                    self._total_latency_ms.get(bcfg.name, 0.0)
                    + stream_result.result.latency_ms
                )
                self._record_model_latency(resolved, stream_result.result.latency_ms)
                emit_capability_event(
                    "llm_call",
                    backend=bcfg.name,
                    requested_model=model,
                    resolved_model=resolved,
                    fallback_used=fallback_used,
                    fallback_reason="model_unavailable" if fallback_used else None,
                    latency_ms=round(float(stream_result.result.latency_ms or 0.0), 1),
                    max_tokens=max_tokens,
                    stream=True,
                    intent=intent,
                    complexity=complexity,
                    accelerator=_backend_accelerator_evidence(bcfg.name, getattr(bcfg, "base_url", ""), resolved),
                )
                # Emit stream completion event
                emit_event(
                    EventName.LLM_STREAM_COMPLETED,
                    model=resolved,
                    backend=bcfg.name,
                    total_latency_ms=stream_result.result.latency_ms,
                    total_tokens=getattr(stream_result.result, "total_tokens", None),
                    tokens_per_second=getattr(stream_result.result, "tokens_per_second", None),
                    stream=True,
                    success=True,
                )

        stream_result.__iter__ = _tracking_iter  # type: ignore[method-assign]
        return stream_result

    # ------------------------------------------------------------------
    # Async methods (v1.2 — Pipeline Parallelism)
    # ------------------------------------------------------------------

    async def chat_async(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Async chat — routes then delegates to client.chat_async()."""
        bcfg, client, resolved = self._select(model)
        if resolved != model:
            log.info("LLMRouter: async %r → %r via backend=%s", model, resolved, bcfg.name)
        self._last_backend_used = bcfg.name
        max_tokens = self._resolve_max_tokens(model, resolved, max_tokens)
        timeout = self._resolve_timeout(bcfg, timeout)
        messages = self._govern_messages(
            messages,
            requested_model=model,
            resolved_model=resolved,
            backend_name=bcfg.name,
            max_tokens=max_tokens,
            phase="chat",
            stream=False,
        )
        t0 = time.monotonic()
        result = await client.chat_async(
            messages, resolved, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout,
        )
        elapsed = (time.monotonic() - t0) * 1000
        self._call_count[bcfg.name] = self._call_count.get(bcfg.name, 0) + 1
        self._total_latency_ms[bcfg.name] = self._total_latency_ms.get(bcfg.name, 0.0) + elapsed
        self._record_model_latency(resolved, elapsed)
        emit_capability_event(
            "llm_call",
            backend=bcfg.name,
            requested_model=model,
            resolved_model=resolved,
            fallback_used=resolved != model,
            latency_ms=round(elapsed, 1),
            max_tokens=max_tokens,
            stream=False,
            async_call=True,
            accelerator=_backend_accelerator_evidence(bcfg.name, getattr(bcfg, "base_url", ""), resolved),
        )
        return result

    async def chat_batch(
        self,
        requests: list["BatchRequest"],
    ) -> list["BatchResult"]:
        """Batch multiple chat requests — groups by backend for efficiency."""
        from orchestrator.llm.base import BatchRequest, BatchResult

        if not requests:
            return []

        # Group by resolved backend
        groups: dict[str, tuple[OpenAICompatibleLLMClient, list[BatchRequest]]] = {}
        order: list[tuple[str, int]] = []  # (backend_name, idx_in_group) for reassembly

        for req in requests:
            bcfg, client, resolved = self._select(req.model)
            key = bcfg.name
            if key not in groups:
                groups[key] = (client, [])
            idx = len(groups[key][1])
            groups[key][1].append(BatchRequest(
                messages=req.messages,
                model=resolved,
                temperature=req.temperature,
                max_tokens=self._resolve_max_tokens(req.model, resolved, req.max_tokens),
                request_id=req.request_id,
            ))
            order.append((key, idx))

        # Execute per-backend batches concurrently
        import asyncio
        backend_results: dict[str, list[BatchResult]] = {}
        tasks = []
        keys = []
        for key, (client, batch) in groups.items():
            tasks.append(client.chat_batch(batch))
            keys.append(key)

        results_per_backend = await asyncio.gather(*tasks, return_exceptions=True)
        for key, res in zip(keys, results_per_backend):
            if isinstance(res, Exception):
                backend_results[key] = [
                    BatchResult(text="", request_id=r.request_id, model=r.model,
                                success=False, error=str(res))
                    for r in groups[key][1]
                ]
            else:
                backend_results[key] = res

        # Reassemble in original order
        final: list[BatchResult] = []
        for key, idx in order:
            final.append(backend_results[key][idx])
        return final

    def health(self) -> bool:
        """True if at least one enabled backend is reachable."""
        return any(
            self._get_health(name).status == "healthy"
            for name in self._clients
        )

    def list_models(self) -> list[str]:
        """Union of models detected on all healthy backends."""
        seen: set[str] = set()
        for name in self._clients:
            h = self._get_health(name)
            if h.status == "healthy":
                seen.update(h.models_detected)
        return sorted(seen)

    # ------------------------------------------------------------------
    # Diagnostics (used by CLI and API)
    # ------------------------------------------------------------------

    def health_report(self) -> list[dict]:
        """Return per-backend health status for display and API."""
        result = []

        # Enabled backends — refresh health
        for name, (bcfg, _) in sorted(
            self._clients.items(), key=lambda kv: kv[1][0].priority
        ):
            h = self._get_health(name)
            calls = self._call_count.get(name, 0)
            avg_lat = (
                round(self._total_latency_ms[name] / calls, 1)
                if calls > 0 else None
            )
            result.append({
                "name": name,
                "type": "openai_compatible",
                "url": mask_url(bcfg.base_url),
                "status": h.status,
                "models_configured": list(bcfg.models),
                "models_detected": h.models_detected,
                "latency_ms": h.latency_ms,
                "last_error": h.last_error,
                "privacy_level": bcfg.privacy_level,
                "priority": bcfg.priority,
                "calls": calls,
                "avg_call_latency_ms": avg_lat,
            })

        # Disabled backends — no health probe, just report disabled
        for bcfg in sorted(self._cfg.backends, key=lambda b: b.priority):
            if bcfg.enabled:
                continue
            result.append({
                "name": bcfg.name,
                "type": "openai_compatible",
                "url": mask_url(bcfg.base_url),
                "status": "disabled",
                "models_configured": list(bcfg.models),
                "models_detected": [],
                "latency_ms": None,
                "last_error": None,
                "privacy_level": bcfg.privacy_level,
                "priority": bcfg.priority,
            })

        result.sort(key=lambda x: x["priority"])
        return result

    def default_backend_name(self) -> str | None:
        """Name of the highest-priority healthy backend, or None."""
        for name, (bcfg, _) in sorted(
            self._clients.items(), key=lambda kv: kv[1][0].priority
        ):
            if self._get_health(name).status == "healthy":
                return name
        return None

    # ------------------------------------------------------------------
    # Model escalation (v1.4 — Model-Agnostic Intelligence)
    # ------------------------------------------------------------------

    def escalate(self, current_model: str, reason: str) -> str | None:
        """Return next model in escalation chain, or None if at top.

        Used by the critic loop to retry with a more capable model when
        the response quality is below threshold.
        """
        from orchestrator.config import get_settings

        cfg = get_settings()
        if not cfg.escalation.enabled:
            return None

        chain = cfg.escalation.chain
        if not chain or current_model not in chain:
            return None

        idx = chain.index(current_model)
        if idx + 1 < len(chain):
            next_model = chain[idx + 1]
            log.info(
                "LLMRouter: escalating %s → %s (reason: %s)",
                current_model, next_model, reason,
            )
            return next_model
        return None

    @property
    def last_backend_used(self) -> str:
        """Name of the backend used in the most recent chat() call."""
        return self._last_backend_used


def _backend_accelerator_evidence(backend_name: str, base_url: str, model: str) -> dict[str, object]:
    """Best-effort accelerator evidence for metadata-only trace events."""

    if os.environ.get("ORC_CAPABILITY_TRACE_OLLAMA_PS", "true").strip().lower() in {"0", "false", "no", "off"}:
        return {"probe": "disabled"}
    if "ollama" not in backend_name.lower() and "11434" not in (base_url or ""):
        return {"probe": "not_ollama_backend"}
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if not base:
        return {"probe": "missing_base_url"}
    now = time.monotonic()
    cached = _OLLAMA_PS_CACHE.get(base)
    if cached and now - cached[0] < 10.0:
        data = cached[1]
    else:
        try:
            response = httpx.get(f"{base}/api/ps", timeout=0.75, follow_redirects=False)
            response.raise_for_status()
            data = response.json()
            _OLLAMA_PS_CACHE[base] = (now, data)
        except Exception as exc:
            return {"probe": "ollama_ps_failed", "error": str(exc)[:160]}
    models = data.get("models", []) if isinstance(data, dict) else []
    loaded: list[dict[str, object]] = []
    for item in models if isinstance(models, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "")
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        processor = str(item.get("processor") or details.get("processor") or "")
        if name:
            loaded.append({
                "name": name,
                "processor": processor,
                "size_vram": item.get("size_vram"),
            })
    matching = [item for item in loaded if item.get("name") == model]
    processors = [str(item.get("processor") or "") for item in (matching or loaded) if item.get("processor")]
    return {
        "probe": "ollama_ps",
        "model_loaded": bool(matching),
        "loaded_models": [item["name"] for item in loaded],
        "processors": processors,
        "accelerated": any("gpu" in processor.lower() for processor in processors),
    }
