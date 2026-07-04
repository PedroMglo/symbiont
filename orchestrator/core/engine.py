"""Symbiont engine — main entry point for query processing.

Flow:
  1. IntentClassifier → Intent
  2. ComplexityClassifier → Complexity
  3. ContextRouter → list of source names
  4. ContextProviders → ContextBlocks (with budget)
  5. ModelRouter → model name
  6. LLMClient → response
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import httpx

if TYPE_CHECKING:
    from orchestrator.core.context_budget import ContextBudget
    from orchestrator.observability.models import MetricsEvent
    from orchestrator.types import Complexity

from orchestrator.config import get_settings
from orchestrator.core.sanitize import sanitize_context
from orchestrator.core.tools import ToolRegistry
from orchestrator.llm.base import LLMClient
from orchestrator.registry import get_registry
from orchestrator.routing.complexity import HeuristicComplexityClassifier
from orchestrator.routing.context_router import ConfigContextRouter
from orchestrator.routing.intent import HeuristicIntentClassifier
from orchestrator.routing.model_router import ConfigModelRouter
from orchestrator.types import (
    ContextBlock,
    Intent,
    RoutingResult,
    SymbiontResult,
)

log = logging.getLogger(__name__)
_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE: dict[str, str] = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


def _try_emit(event: "MetricsEvent") -> None:
    """Emit an observability event; no-op if layer is disabled or import fails."""
    try:
        from orchestrator.observability.collector import emit
        emit(event)
    except Exception:
        pass


def _try_emit_v2(event_name, **kwargs) -> None:
    """Emit a v2 observability event; no-op if not initialized."""
    try:
        from orchestrator.observability.telemetry import emit_event
        emit_event(event_name, **kwargs)
    except Exception:
        pass


def _emit_observability(
    *,
    query: str,
    response: str,
    model: str,
    intent: "Intent",
    complexity: "Complexity",
    profile_key: str,
    latency: float,
    session_id: str | None,
    entrypoint: str,
    backend: str,
    context_build_latency_ms: float | None = None,
    llm_latency_ms: float | None = None,
    router_latency_ms: float | None = None,
    context_tokens: int = 0,
    sources: list[str] | None = None,
    ollama_timing: object | None = None,
    agentic: bool = False,
    iterations: int = 0,
    tools_used: list[str] | None = None,
) -> None:
    """Unified observability emit — sends to both v1 (SQLite) and v2 (dispatcher)."""
    sources = sources or []

    # Extract Ollama native timing
    model_load_latency_ms = None
    prompt_eval_latency_ms = None
    generation_latency_ms = None
    prompt_tps = None
    gen_tps = None
    total_tps = None
    ollama_fields: dict = {}

    if ollama_timing is not None:
        model_load_latency_ms = getattr(ollama_timing, "load_duration_ms", None)
        prompt_eval_latency_ms = getattr(ollama_timing, "prompt_eval_duration_ms", None)
        generation_latency_ms = getattr(ollama_timing, "eval_duration_ms", None)
        prompt_tps = getattr(ollama_timing, "prompt_tokens_per_second", None)
        gen_tps = getattr(ollama_timing, "generation_tokens_per_second", None)
        total_tps = getattr(ollama_timing, "total_tokens_per_second", None)
        ollama_fields = {
            "ollama_total_duration": getattr(ollama_timing, "total_duration", None),
            "ollama_load_duration": getattr(ollama_timing, "load_duration", None),
            "ollama_prompt_eval_count": getattr(ollama_timing, "prompt_eval_count", None),
            "ollama_prompt_eval_duration": getattr(ollama_timing, "prompt_eval_duration", None),
            "ollama_eval_count": getattr(ollama_timing, "eval_count", None),
            "ollama_eval_duration": getattr(ollama_timing, "eval_duration", None),
        }

    cold_start = bool(model_load_latency_ms and model_load_latency_ms > 500)

    # --- v1: MetricsEvent → SQLite store ---
    try:
        from orchestrator.observability.models import LLMUsage, MetricsEvent, RouterDecision
        _try_emit(MetricsEvent(
            session_id=session_id,
            entrypoint=entrypoint,
            model=model,
            backend=backend,
            router_decision=RouterDecision(
                requested_model=model,
                resolved_model=model,
                intent=intent.value,
                complexity=complexity.value,
                backend=backend,
                profile_key=profile_key,
            ),
            usage=LLMUsage.estimated(query, response),
            latency_ms=latency,
            context_build_latency_ms=context_build_latency_ms,
            llm_latency_ms=llm_latency_ms,
            router_latency_ms=router_latency_ms,
            model_load_latency_ms=model_load_latency_ms,
            prompt_eval_latency_ms=prompt_eval_latency_ms,
            generation_latency_ms=generation_latency_ms,
            total_latency_ms=latency,
            cold_start=cold_start,
            prompt_tokens_per_second=prompt_tps,
            generation_tokens_per_second=gen_tps,
            total_tokens_per_second=total_tps,
            profile_key=profile_key,
            query_length=len(query),
            response_length=len(response),
            query_hash=MetricsEvent.hash_query(query),
            rag_used="rag" in sources,
            graph_used="graph" in sources,
            tools_used=tuple(tools_used) if tools_used else (),
            agentic=agentic,
            iterations=iterations,
            **ollama_fields,
        ))
    except Exception:
        pass

    # --- v2: ObservabilityEvent → dispatcher sinks ---
    try:
        from orchestrator.observability.events import EventName
        est_prompt = len(query) // 4
        est_completion = len(response) // 4
        _try_emit_v2(
            EventName.REQUEST_COMPLETED,
            session_id=session_id,
            entrypoint=entrypoint,
            model=model,
            backend=backend,
            backend_type=backend,
            intent=intent.value,
            complexity=complexity.value,
            profile=profile_key,
            requested_model=model,
            selected_model=model,
            selected_backend=backend,
            total_latency_ms=latency,
            context_build_latency_ms=context_build_latency_ms,
            llm_latency_ms=llm_latency_ms,
            router_latency_ms=router_latency_ms,
            model_load_latency_ms=model_load_latency_ms,
            prompt_eval_latency_ms=prompt_eval_latency_ms,
            generation_latency_ms=generation_latency_ms,
            tokens_per_second=total_tps,
            prompt_tokens_per_second=prompt_tps,
            generation_tokens_per_second=gen_tps,
            prompt_tokens=est_prompt,
            completion_tokens=est_completion,
            total_tokens=est_prompt + est_completion,
            cold_start=cold_start,
            rag_used="rag" in sources,
            graph_used="graph" in sources,
            context_tokens=context_tokens,
            query_length=len(query),
            response_length=len(response),
            agentic=agentic,
            iterations=iterations,
            success=True,
        )
    except Exception:
        pass


def _get_system_prompt() -> str:
    """Load system prompt from the centralized registry (models.json)."""
    return get_registry().get_system_prompt_for_agent("reasoning_and_response")


def _get_context_instruction() -> str:
    """Load context instruction from the centralized registry (models.json)."""
    return get_registry().get_context_instruction()


_LLM_UNAVAILABLE_MSG = (
    "⚠️ O serviço LLM não está disponível de momento. "
    "Verifique o estado com `orc health` e certifique-se de que um backend está a correr."
)


def _create_default_llm() -> LLMClient:
    """Create a default LLM client from current settings (lazy import to avoid circular deps)."""
    from orchestrator.config import get_settings
    from orchestrator.llm.router import LLMRouter
    _s = get_settings()
    return LLMRouter(
        _s.llm,
        latency_routing=_s.latency_routing,
        inference_profiles=_s.inference_profiles,
    )

# How long to cache the LLM health check result (seconds)
_LLM_HEALTH_CACHE_TTL = 5.0


class Engine:
    """Main orchestration engine."""

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        security_layer: Any | None = None,
    ) -> None:
        self._intent_clf = HeuristicIntentClassifier()
        self._complexity_clf = HeuristicComplexityClassifier()
        self._model_router = ConfigModelRouter()
        self._context_router = ConfigContextRouter()
        self._llm = llm or _create_default_llm()
        self._tool_registry = ToolRegistry()
        # Agent registry (populated by factory)
        self.agent_registry: Any | None = None
        # Critic agent (populated by factory, Sprint 3)
        self.critic_agent: Any | None = None
        # Security layer (v1.3)
        self.security_layer: Any | None = security_layer
        # Cached LLM health state
        self._llm_healthy: bool = True
        self._llm_health_ts: float = 0.0
        # Context cache — avoids re-fetching same context within short window
        self._context_cache: dict[str, tuple[float, list]] = {}  # key → (ts, blocks)
        self._context_cache_ttl: float = 60.0  # 60s TTL for context dedup

    def _is_llm_available(self) -> bool:
        """Check LLM health with a short-lived cache to avoid per-query overhead."""
        now = time.monotonic()
        if now - self._llm_health_ts < _LLM_HEALTH_CACHE_TTL:
            return self._llm_healthy
        self._llm_healthy = self._llm.health()
        self._llm_health_ts = now
        if not self._llm_healthy:
            log.warning("Engine: LLM health check failed — Ollama unavailable")
        return self._llm_healthy

    @property
    def tool_registry(self) -> ToolRegistry:
        """Access the generic runtime tool registry."""
        return self._tool_registry

    def invoke_tool(
        self,
        name: str,
        query: str,
        budget_tokens: int | None = None,
    ) -> ContextBlock | None:
        """Invoke a registered tool by name."""
        tool = self._tool_registry.get(name)
        if tool is None:
            return None
        budget_tokens = (
            budget_tokens
            if budget_tokens is not None
            else get_settings().dispatch.feature_budget_tokens
        )
        return tool.callable(query, budget_tokens=budget_tokens)

    def health_report(self) -> dict[str, Any]:
        """Return health status of all components.

        Returns a dict with keys:
        - ``ollama`` (bool): True if any LLM backend is healthy (backward compat key).
        - ``providers`` (dict[str, bool]): compatibility health map for dispatch capabilities.
        - ``backends`` (list[dict]): per-backend detail from LLMRouter (if available).
        - ``all_ok`` (bool): True only when at least one LLM backend is healthy.
        """
        providers_health: dict[str, bool] = {}

        # Include service registry capabilities in the compatibility health map.
        registry = getattr(self, "_service_registry", None)
        if registry is not None:
            for capability in ("rag", "cag"):
                if capability not in providers_health:
                    services = registry.find_by_capability(capability)
                    providers_health[capability] = any(
                        registry.get_healthy(svc.name) is not None for svc in services
                    )

        llm_ok = self._llm.health()

        # Per-backend breakdown from LLMRouter (if available)
        backends: list[dict] = []
        health_report_fn = getattr(self._llm, "health_report", None)
        if callable(health_report_fn):
            try:
                backends = health_report_fn()
            except Exception:
                pass

        return {
            "ollama": llm_ok,   # backward-compat key (True = any backend healthy)
            "providers": providers_health,
            "backends": backends,
            "all_ok": llm_ok,
        }

    def classify(self, query: str, *, history: list[dict] | None = None) -> RoutingResult:
        """Classify intent and complexity without executing."""
        intent = self._classify_intent(query, history=history)
        complexity = self._complexity_clf.classify(query)
        return RoutingResult(intent=intent, complexity=complexity)

    def _llm_intent_fallback(self, query: str) -> Intent | None:
        """Ask the fast LLM to classify intent when the heuristic returns GENERAL.

        Returns an ``Intent`` value if the LLM responds with a recognised label,
        or ``None`` on any failure (timeout, parse error, LLM unavailable).
        """
        cfg = get_settings()
        model = cfg.models.fast
        _VALID = "|".join(i.value for i in Intent)
        _PATTERN = re.compile(rf"\b({_VALID})\b", re.IGNORECASE)

        prompt = _prompt("intent_fallback.md").format(
            intents=", ".join(i.value for i in Intent),
            query=query,
        )
        messages = [
            {"role": "system", "content": get_registry().get_system_prompt_for_agent("reasoning_and_response")},
            {"role": "user", "content": prompt},
        ]
        try:
            raw = self._llm.chat(messages, model, temperature=0.0, max_tokens=16, timeout=3.0)
            m = _PATTERN.search(raw.strip())
            if m:
                return Intent(m.group(1).upper())
        except Exception as exc:  # noqa: BLE001
            log.debug("Engine: LLM intent fallback failed: %s", exc)
        return None

    def _classify_intent(self, query: str, *, history: list[dict] | None = None) -> Intent:
        """Classify intent using the heuristic; fall back to LLM for ambiguous queries.

        The LLM fallback is triggered only when:
        - the heuristic returns ``Intent.GENERAL`` (no strong local signals), AND
        - the query has more than 5 words (short queries are clear enough).
        """
        intent = self._intent_clf.classify(query, history=history)
        if intent is Intent.GENERAL and len(query.split()) > 5:
            fallback = self._llm_intent_fallback(query)
            if fallback is not None:
                log.debug("Engine: intent heuristic=GENERAL llm_fallback=%s", fallback.value)
                return fallback
        return intent

    def _gather_context(
        self, query: str, sources: list[str], *, budget: int = 6000, intent: Intent | None = None,
        rag_top_k_override: int | None = None,
    ) -> list[ContextBlock]:
        """Gather context through the dispatch feature client.

        The graph/dispatch service catalog is the owner of feature calls.
        """
        cfg = get_settings()
        provider_timeout = cfg.context.provider_timeout

        # Context deduplication cache — avoid re-fetching for same query + sources
        import hashlib as _hashlib
        _ctx_key = _hashlib.sha256(f"{query}|{','.join(sources)}|{budget}".encode()).hexdigest()[:20]
        now = time.perf_counter()
        cached = self._context_cache.get(_ctx_key)
        if cached is not None:
            ts, cached_blocks = cached
            if (now - ts) < self._context_cache_ttl:
                log.debug("Engine: context cache HIT (%d blocks)", len(cached_blocks))
                return cached_blocks

        dispatch_blocks = self._gather_context_via_dispatch(
            query,
            sources,
            budget=budget,
            provider_timeout=provider_timeout,
            intent=intent,
            rag_top_k_override=rag_top_k_override,
        )
        if dispatch_blocks is not None:
            self._context_cache[_ctx_key] = (time.perf_counter(), dispatch_blocks)
            return dispatch_blocks

        return []

    def _gather_context_via_dispatch(
        self,
        query: str,
        sources: list[str],
        *,
        budget: int,
        provider_timeout: float,
        intent: Intent | None,
        rag_top_k_override: int | None,
    ) -> list[ContextBlock] | None:
        feature_client = getattr(self, "_feature_client", None)
        gather = getattr(feature_client, "gather_context_parallel", None)
        if not callable(gather):
            return None

        metadata: dict[str, Any] = {}
        if intent is not None:
            metadata["intent"] = intent.value if hasattr(intent, "value") else str(intent)
        if rag_top_k_override is not None:
            metadata["rag_top_k"] = rag_top_k_override

        responses = gather(
            sources,
            query,
            budget_tokens=budget,
            timeout_per_source=provider_timeout,
            metadata=metadata,
        )
        by_source = {
            response.source: response
            for response in responses
            if getattr(response, "success", False) and getattr(response, "content", "")
        }
        blocks: list[ContextBlock] = []
        remaining = budget
        for source_name in sources:
            response = by_source.get(source_name)
            if response is None:
                continue
            token_estimate = response.token_estimate or max(1, len(response.content) // 4)
            if token_estimate > remaining:
                break
            blocks.append(
                ContextBlock(
                    source=source_name,
                    content=response.content,
                    token_estimate=token_estimate,
                    metadata=response.metadata,
                )
            )
            remaining -= token_estimate
        return blocks

    async def _gather_context_async(
        self, query: str, sources: list[str], *, budget: int = 6000, intent: Intent | None = None,
    ) -> list[ContextBlock]:
        """Async context gathering through the dispatch feature client."""
        import asyncio as _asyncio
        return await _asyncio.to_thread(
            self._gather_context,
            query,
            sources,
            budget=budget,
            intent=intent,
        )

    def _build_messages(
        self,
        query: str,
        context_blocks: list[ContextBlock],
        *,
        history: list[dict] | None = None,
    ) -> list[dict]:
        """Build the message list for the LLM."""
        messages: list[dict] = [{"role": "system", "content": _get_system_prompt()}]

        if context_blocks:
            context_parts = []
            for block in context_blocks:
                safe_content = sanitize_context(block.content)
                context_parts.append(f"[{block.source.upper()}]\n{safe_content}\n[/{block.source.upper()}]")
            context_text = "\n\n".join(context_parts)
            messages.append({
                "role": "system",
                "content": f"{_get_context_instruction()}\n\n{context_text}",
            })

        if history:
            messages.extend(history)

        messages.append({"role": "user", "content": query})
        return messages

    def run(
        self,
        query: str,
        *,
        history: list[dict] | None = None,
        session_id: str | None = None,
        entrypoint: str = "api",
        model_override: str | None = None,
    ) -> SymbiontResult:
        """Full orchestration: classify → context → model → LLM → result."""
        t0 = time.perf_counter()
        cfg = get_settings()

        # 1. Classify
        t_route_start = time.perf_counter()
        intent = self._classify_intent(query, history=history)
        complexity = self._complexity_clf.classify(query)
        log.info("Engine: intent=%s complexity=%s", intent.value, complexity.value)

        # 2. Context routing
        sources = self._context_router.route(intent, complexity)

        # 3. Model selection (with profile)
        from orchestrator.core.inference_profile import resolve_profile

        selection = self._model_router.select_with_profile(intent, complexity)
        model = model_override or selection.model
        profile_key = selection.profile_key

        profile = resolve_profile(profile_key)
        router_latency_ms = (time.perf_counter() - t_route_start) * 1000

        # --- Response cache check (before context gathering) ---
        cache_hit = None
        if cfg.hardware.response_cache_enabled and not history:
            try:
                from orchestrator.core.response_cache import get_response_cache
                cache = get_response_cache()
                cache_hit = cache.get(query, intent.value, model, sources)
                if cache_hit is not None:
                    latency = (time.perf_counter() - t0) * 1000
                    log.info("Engine: cache HIT (latency=%.0fms)", latency)
                    return SymbiontResult(
                        response=cache_hit.response,
                        model_used=cache_hit.model_used,
                        intent=intent,
                        complexity=complexity,
                        sources_used=sources,
                        context_tokens=cache_hit.context_tokens,
                        latency_ms=latency,
                    )
            except Exception as exc:
                log.debug("Engine: response cache error: %s", exc)

        # 4. Gather context (with budget)
        from orchestrator.core.context_budget import (
            deduplicate_blocks,
            resolve_budget,
            truncate_by_budget,
        )

        budget = resolve_budget(profile_key)

        # Apply adaptive budget multiplier
        effective_budget_tokens = budget.max_context_tokens
        try:
            from orchestrator.core.adaptive_config import DegradationMode, get_adaptive_overrides
            overrides = get_adaptive_overrides()
            effective_budget_tokens = int(budget.max_context_tokens * overrides.context_budget_multiplier)

            # --- Graceful degradation ---
            if overrides.degradation_mode == DegradationMode.CONSTRAINED:
                # Reduce budgets, disable expensive sources, use fast profile
                effective_budget_tokens = int(effective_budget_tokens * 0.5)
                filtered_sources = [s for s in sources if s not in ("graph", "rss", "email")]
                if profile_key not in ("fast",):
                    profile_key = "fast"
                    profile = resolve_profile("fast")
                log.warning("Engine: CONSTRAINED mode — budget halved, graph/rss/email disabled")
            elif overrides.degradation_mode == DegradationMode.MINIMAL:
                # Emergency: cache-only or minimal context, smallest model
                effective_budget_tokens = min(effective_budget_tokens, 500)
                filtered_sources = []  # Skip all context gathering
                profile_key = "fast"
                profile = resolve_profile("fast")
                # Try to use smallest available model
                model = cfg.models.fast or model
                log.warning("Engine: MINIMAL mode — emergency, no context, fast model only")
        except Exception:
            pass

        t_ctx_start = time.perf_counter()

        # Filter sources based on budget (skip if degradation already set filtered_sources)
        _degradation_active = False
        try:
            from orchestrator.core.adaptive_config import DegradationMode
            from orchestrator.core.adaptive_config import get_adaptive_overrides as _get_ao
            _degradation_active = _get_ao().degradation_mode != DegradationMode.NORMAL
        except Exception:
            pass

        if not _degradation_active:
            filtered_sources = self._filter_sources_by_budget(sources, budget, query)

        blocks = self._gather_context(
            query, filtered_sources, budget=effective_budget_tokens, intent=intent,
            rag_top_k_override=budget.rag_top_k,
        )
        blocks = deduplicate_blocks(blocks)
        blocks = truncate_by_budget(blocks, effective_budget_tokens)
        context_build_latency_ms = (time.perf_counter() - t_ctx_start) * 1000

        log.info("Engine: model=%s profile=%s sources=%s blocks=%d", model, profile_key, filtered_sources, len(blocks))

        # 5. Build messages and call LLM
        messages = self._build_messages(query, blocks, history=history)

        # Health-gate: fail fast if Ollama is known-down
        if not self._is_llm_available():
            latency = (time.perf_counter() - t0) * 1000
            return SymbiontResult(
                response=_LLM_UNAVAILABLE_MSG,
                model_used=model,
                intent=intent,
                complexity=complexity,
                sources_used=[b.source for b in blocks],
                context_tokens=sum(b.token_estimate for b in blocks),
                latency_ms=latency,
            )

        # Call LLM with profile parameters and native Ollama timing
        t_llm_start = time.perf_counter()
        ollama_timing = None
        try:
            # Use instrumented call for Ollama native metrics
            chat_instrumented = getattr(self._llm, "chat_instrumented", None)
            if callable(chat_instrumented):
                result_obj = chat_instrumented(
                    messages, model,
                    temperature=profile.temperature,
                    max_tokens=profile.num_predict,
                    num_ctx=profile.num_ctx,
                    use_native_ollama=True,
                )
                response = result_obj.text
                ollama_timing = getattr(result_obj, "ollama_timing", None)
            else:
                response = self._llm.chat(messages, model)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            log.error("Engine: LLM call failed: %s", exc)
            self._llm_healthy = False
            self._llm_health_ts = time.monotonic()
            latency = (time.perf_counter() - t0) * 1000
            return SymbiontResult(
                response=_LLM_UNAVAILABLE_MSG,
                model_used=model,
                intent=intent,
                complexity=complexity,
                sources_used=[b.source for b in blocks],
                context_tokens=sum(b.token_estimate for b in blocks),
                latency_ms=latency,
            )

        llm_latency_ms = (time.perf_counter() - t_llm_start) * 1000
        latency = (time.perf_counter() - t0) * 1000
        context_tokens = sum(b.token_estimate for b in blocks)

        result = SymbiontResult(
            response=response,
            model_used=model,
            intent=intent,
            complexity=complexity,
            sources_used=[b.source for b in blocks],
            context_tokens=context_tokens,
            latency_ms=latency,
        )

        # Store in response cache (non-blocking, best-effort)
        if cfg.hardware.response_cache_enabled and not history:
            try:
                from orchestrator.core.response_cache import get_response_cache
                cache = get_response_cache()
                cache.put(
                    query, intent.value, model, response,
                    context_tokens=context_tokens,
                    sources=[b.source for b in blocks],
                )
            except Exception:
                pass

        # Observability — emit granular metrics
        _emit_observability(
            query=query,
            response=response,
            model=model,
            intent=intent,
            complexity=complexity,
            profile_key=profile_key,
            latency=latency,
            session_id=session_id,
            entrypoint=entrypoint,
            backend=getattr(self._llm, "last_backend_used", "") or "",
            context_build_latency_ms=context_build_latency_ms,
            llm_latency_ms=llm_latency_ms,
            router_latency_ms=router_latency_ms,
            context_tokens=context_tokens,
            sources=[b.source for b in blocks],
            ollama_timing=ollama_timing,
        )

        return result

    async def run_async(
        self,
        query: str,
        *,
        history: list[dict] | None = None,
        session_id: str | None = None,
        entrypoint: str = "api",
    ) -> SymbiontResult:
        """Async wrapper — offloads the synchronous engine to a thread.

        Prevents blocking the FastAPI event loop while LLM and context
        providers do I/O-bound work. Uses asyncio.to_thread for true
        thread-level concurrency.
        """
        import asyncio
        return await asyncio.to_thread(
            self.run,
            query,
            history=history,
            session_id=session_id,
            entrypoint=entrypoint,
        )

    async def stream_async(
        self,
        query: str,
        *,
        history: list[dict] | None = None,
    ):
        """Async streaming — yields chunks without blocking the event loop.

        Uses asyncio queue bridging: a background thread feeds chunks
        into a queue that the async generator consumes.
        """
        import asyncio

        q: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def _produce():
            try:
                for chunk in self.stream(query, history=history):
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        import threading
        t = threading.Thread(target=_produce, daemon=True)
        t.start()

        while True:
            chunk = await q.get()
            if chunk is None:
                break
            yield chunk

    def _filter_sources_by_budget(
        self, sources: list[str], budget: "ContextBudget", query: str
    ) -> list[str]:
        """Filter context sources based on the context budget settings."""

        filtered = []
        for source in sources:
            if source == "graph" and not budget.should_use_graph():
                log.debug("Engine: skipping graph (budget=%s)", budget.key)
                continue
            if source == "system" and not budget.should_use_system_snapshot(query):
                log.debug("Engine: skipping system snapshot (budget=%s)", budget.key)
                continue
            filtered.append(source)
        return filtered

    def stream(
        self,
        query: str,
        *,
        history: list[dict] | None = None,
        model_override: str | None = None,
    ) -> Iterator[str]:
        """Streaming variant: yields text chunks."""
        cfg = get_settings()

        intent = self._classify_intent(query, history=history)
        complexity = self._complexity_clf.classify(query)
        sources = self._context_router.route(intent, complexity)
        blocks = self._gather_context(query, sources, budget=cfg.context.token_budget, intent=intent)
        model = model_override or self._model_router.select(intent, complexity)
        messages = self._build_messages(query, blocks, history=history)

        # Health-gate: fail fast if Ollama is known-down
        if not self._is_llm_available():
            yield _LLM_UNAVAILABLE_MSG
            return

        try:
            yield from self._llm.chat_stream(messages, model)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            log.error("Engine: LLM stream failed: %s", exc)
            self._llm_healthy = False
            self._llm_health_ts = time.monotonic()
            yield _LLM_UNAVAILABLE_MSG
