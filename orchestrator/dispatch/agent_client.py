"""Typed HTTP client for invoking agent services."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from orchestrator.config import get_settings
from orchestrator.dispatch.client import CircuitOpen, HTTPServiceClient
from orchestrator.dispatch.service_registry import ServiceRegistry
from orchestrator.dispatch.types import (
    AgentInvokeRequest,
    AgentInvokeResponse,
)

log = logging.getLogger(__name__)

_CONTEXT_SOURCE_RE = re.compile(
    r"^\[(?P<source>[A-Za-z0-9_.:-]+)\]\s*(?P<content>.*?)(?=^\[[A-Za-z0-9_.:-]+\]\s*|\Z)",
    re.DOTALL | re.MULTILINE,
)

_REASONING_SERVICE = "reasoning_and_response"

_AGENT_PAYLOAD_PROFILES = {
    _REASONING_SERVICE: "_reasoning_and_response_payload",
}

_AGENT_RESPONSE_PROFILES = {
    _REASONING_SERVICE: "_reasoning_and_response_response_fields",
}


# ---------------------------------------------------------------------------
# Agent endpoint path mapping
# ---------------------------------------------------------------------------

# Agent invocation endpoints are defined in [dispatch.agent_endpoints] in
# config/orc/agents.toml. This client intentionally has no fallback route.


class AgentClient:
    """Typed client for calling agent services via HTTP.

    Usage:
        client = AgentClient(registry)
        response = client.invoke("reasoning_and_response", request)
    """

    def __init__(self, registry: ServiceRegistry, http_client: HTTPServiceClient | None = None):
        self._registry = registry
        self._http = http_client or registry._client
        self._backend_model_cache: dict[str, tuple[float, list[str]]] = {}

    def invoke(
        self,
        agent_name: str,
        request: AgentInvokeRequest,
        *,
        endpoint_override: str | None = None,
    ) -> AgentInvokeResponse:
        """Invoke an agent service.

        Args:
            agent_name: Registered agent name (e.g. "reasoning_and_response")
            request: The invocation request
            endpoint_override: Override the default endpoint path

        Returns:
            AgentInvokeResponse with output or error
        """
        start = time.time()
        degraded = self.degraded_runtime_flag(agent_name)
        if degraded is not None:
            latency_ms = (time.time() - start) * 1000
            self._record_agent_degraded_skip(
                agent_name,
                flag=degraded,
                latency_ms=latency_ms,
                phase="runtime_flag",
            )
            return AgentInvokeResponse(
                output="",
                success=False,
                latency_ms=latency_ms,
                agent_name=agent_name,
                error=f"Agent '{agent_name}' is temporarily degraded by runtime flag",
                metadata={
                    "degraded_by_runtime_flag": True,
                    "runtime_flag": degraded,
                    "fallback_expected": True,
                },
            )

        ep = self._registry.ensure_available(agent_name)
        if ep is None:
            self._record_agent_failure(
                agent_name,
                error=f"Agent '{agent_name}' not available (not registered or unhealthy)",
                latency_ms=(time.time() - start) * 1000,
                phase="ensure_available",
            )
            return AgentInvokeResponse(
                output="",
                success=False,
                agent_name=agent_name,
                error=f"Agent '{agent_name}' not available (not registered or unhealthy)",
            )

        mapping = get_settings().dispatch.agent_endpoints.get(agent_name)
        method = mapping[0] if mapping else None
        path = endpoint_override or (mapping[1] if mapping else None)
        if method is None or path is None:
            self._record_agent_failure(
                agent_name,
                error=f"No endpoint configured for agent '{agent_name}'",
                latency_ms=(time.time() - start) * 1000,
                phase="endpoint_config",
            )
            return AgentInvokeResponse(
                output="",
                success=False,
                agent_name=agent_name,
                error=(
                    f"No endpoint configured for agent '{agent_name}'. "
                    "Add it to [dispatch.agent_endpoints] in agents.toml."
                ),
            )

        request = self._with_dispatch_defaults(agent_name, request)
        payload = self._build_payload(agent_name, request)
        policy = self._audit_policy(
            "agent.invoke",
            payload={"agent": agent_name, "method": method, "path": path},
        )
        if policy is not None and policy.should_block:
            return AgentInvokeResponse(
                output="",
                success=False,
                agent_name=agent_name,
                error=f"Policy blocked {policy.action}: {policy.reason}",
            )

        try:
            kwargs: dict[str, Any] = {"json": payload, "timeout": request.timeout_seconds}
            transport_retries = self._transport_retries(request.metadata)
            if transport_retries is not None:
                kwargs["retries"] = transport_retries
            headers = self._headers_with_context()
            if headers:
                kwargs["headers"] = headers
            resp = self._http.request(
                ep,
                method,
                path,
                **kwargs,
            )
            latency_ms = (time.time() - start) * 1000
            data = resp.json()

            # Touch after successful invocation to extend idle timeout
            lifecycle = getattr(self._registry, '_lifecycle', None)
            if lifecycle and lifecycle.available:
                lifecycle.touch(agent_name)

            confidence, metadata = self._response_fields(agent_name, data)
            agent_decision = data.get("agent_decision")

            return AgentInvokeResponse(
                output=data.get("output", data.get("response", data.get("result", ""))),
                success=True,
                confidence=confidence,
                tokens_used=data.get("tokens_used", 0),
                latency_ms=latency_ms,
                agent_name=agent_name,
                metadata=metadata,
                agent_decision=agent_decision if isinstance(agent_decision, dict) else None,
            )

        except CircuitOpen:
            latency_ms = (time.time() - start) * 1000
            self._record_agent_failure(
                agent_name,
                error=f"Circuit breaker open for '{agent_name}'",
                latency_ms=latency_ms,
                phase="circuit_open",
                method=method,
                path=path,
            )
            return AgentInvokeResponse(
                output="",
                success=False,
                agent_name=agent_name,
                error=f"Circuit breaker open for '{agent_name}'",
            )

        except Exception as exc:
            latency_ms = (time.time() - start) * 1000
            log.warning("Agent %s invocation failed: %s", agent_name, exc)
            self._record_agent_failure(
                agent_name,
                error=str(exc)[:300],
                latency_ms=latency_ms,
                phase="invoke",
                method=method,
                path=path,
            )
            return AgentInvokeResponse(
                output="",
                success=False,
                latency_ms=latency_ms,
                agent_name=agent_name,
                error=str(exc)[:300],
            )

    def invoke_critic(
        self,
        query: str,
        response: str,
        *,
        agent_name: str = _REASONING_SERVICE,
        timeout: float | None = None,
        risk_level: str | None = None,
        metadata: dict[str, Any] | None = None,
        **extra_metadata: Any,
    ) -> AgentInvokeResponse:
        """Invoke reasoning_and_response critique mode to evaluate a response."""
        agent_name = _REASONING_SERVICE
        timeout = timeout if timeout is not None else self._timeout_for(agent_name)
        request_metadata = dict(metadata or {})
        request_metadata["provider_mode"] = "critique"
        if risk_level:
            request_metadata["risk_level"] = risk_level
        request_metadata.update({k: v for k, v in extra_metadata.items() if v is not None})
        return self.invoke(
            agent_name,
            AgentInvokeRequest(
                query=query,
                context={"response": response},
                timeout_seconds=timeout,
                metadata=request_metadata,
            ),
            endpoint_override="/v1/reasoning/critique",
        )

    def invoke_decomposer(
        self,
        query: str,
        available_agents: list[str],
        *,
        timeout: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentInvokeResponse:
        """Invoke reasoning_and_response decomposition mode."""
        timeout = timeout if timeout is not None else self._timeout_for(_REASONING_SERVICE)
        request_metadata = dict(metadata or {})
        request_metadata["provider_mode"] = "decompose"
        return self.invoke(
            _REASONING_SERVICE,
            AgentInvokeRequest(
                query=query,
                context={"available_agents": available_agents},
                timeout_seconds=timeout,
                metadata=request_metadata,
            ),
            endpoint_override="/v1/reasoning/decompose",
        )

    def invoke_synthesis(
        self,
        query: str,
        results: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentInvokeResponse:
        """Invoke reasoning_and_response synthesis mode."""
        timeout = timeout if timeout is not None else self._timeout_for(_REASONING_SERVICE)
        request_metadata = dict(metadata or {})
        request_metadata["provider_mode"] = "synthesize"
        return self.invoke(
            _REASONING_SERVICE,
            AgentInvokeRequest(
                query=query,
                context={"results": results},
                timeout_seconds=timeout,
                metadata=request_metadata,
            ),
            endpoint_override="/v1/reasoning/synthesize",
        )

    def invoke_responder(
        self,
        query: str,
        history: list[dict[str, str]] | None = None,
        context: str = "",
        *,
        timeout: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentInvokeResponse:
        """Invoke reasoning_and_response direct response mode."""
        timeout = timeout if timeout is not None else self._timeout_for(_REASONING_SERVICE)
        request_metadata = dict(metadata or {})
        request_metadata["provider_mode"] = "respond"
        return self.invoke(
            _REASONING_SERVICE,
            AgentInvokeRequest(
                query=query,
                context={"context": context} if context else {},
                history=history or [],
                timeout_seconds=timeout,
                metadata=request_metadata,
            ),
            endpoint_override="/v1/reasoning/respond",
        )

    def invoke_router(
        self,
        query: str,
        available_agents: list[str],
        *,
        timeout: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentInvokeResponse:
        """Invoke reasoning_and_response classification mode."""
        timeout = timeout if timeout is not None else self._timeout_for(_REASONING_SERVICE)
        request_metadata = dict(metadata or {})
        request_metadata["provider_mode"] = "classify"
        return self.invoke(
            _REASONING_SERVICE,
            AgentInvokeRequest(
                query=query,
                context={"available_agents": available_agents},
                timeout_seconds=timeout,
                metadata=request_metadata,
            ),
            endpoint_override="/v1/reasoning/classify",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _timeout_for(self, agent_name: str) -> float:
        dispatch_cfg = get_settings().dispatch
        return dispatch_cfg.agent_timeouts.get(agent_name, dispatch_cfg.agent_timeout_seconds)

    def _with_dispatch_defaults(
        self,
        agent_name: str,
        request: AgentInvokeRequest,
    ) -> AgentInvokeRequest:
        if request.budget_tokens is not None and request.timeout_seconds is not None:
            return request
        dispatch_cfg = get_settings().dispatch
        return replace(
            request,
            budget_tokens=(
                request.budget_tokens
                if request.budget_tokens is not None
                else dispatch_cfg.agent_budget_tokens
            ),
            timeout_seconds=(
                request.timeout_seconds
                if request.timeout_seconds is not None
                else self._timeout_for(agent_name)
            ),
        )

    def _transport_retries(self, metadata: dict[str, Any] | None) -> int | None:
        if not metadata or "transport_retries" not in metadata:
            return None
        try:
            return max(0, int(metadata.get("transport_retries")))
        except (TypeError, ValueError):
            return None

    def _build_payload(self, agent_name: str, request: AgentInvokeRequest) -> dict[str, Any]:
        """Build HTTP payload using the configured agent payload profile.

        Injects LLM config from the central registry (models.json) so agents
        use the centrally-defined model, backend, and system prompt.
        """
        # Fetch centralized LLM config for this agent and apply explicit
        # invocation-scoped output budgets when the dispatch policy requests it.
        llm_config_payload = self._llm_config_for_request(agent_name, request)
        builder_name = _AGENT_PAYLOAD_PROFILES.get(agent_name, "_generic_agent_payload")
        builder = getattr(self, builder_name)
        return builder(agent_name, request, llm_config_payload)

    def _response_fields(self, agent_name: str, data: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        builder_name = _AGENT_RESPONSE_PROFILES.get(agent_name, "_generic_response_fields")
        builder = getattr(self, builder_name)
        return builder(data)

    def _generic_response_fields(self, data: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        return data.get("confidence", data.get("score", 1.0)), data.get("metadata", {})

    def _reasoning_and_response_response_fields(self, data: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        confidence, metadata = self._generic_response_fields(data)
        confidence = data.get("confidence_score", confidence)
        if data.get("issues"):
            metadata = {**metadata, "issues": data.get("issues", [])}
        return confidence, metadata

    def _reasoning_and_response_payload(
        self,
        agent_name: str,
        request: AgentInvokeRequest,
        llm_config_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        mode = str((request.metadata or {}).get("provider_mode") or "respond")
        if mode == "critique":
            return self._reasoning_critique_payload(request, llm_config_payload)
        if mode == "synthesize":
            return self._reasoning_synthesis_payload(request, llm_config_payload)
        if mode == "decompose":
            return self._reasoning_decomposition_payload(request, llm_config_payload)
        if mode == "classify":
            return self._reasoning_classification_payload(request, llm_config_payload)
        return self._generic_agent_payload(agent_name, request, llm_config_payload)

    def _reasoning_critique_payload(
        self,
        request: AgentInvokeRequest,
        llm_config_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = {
            "output": str(request.context.get("response", "")),
            "original_query": request.query,
            "agent_name": request.metadata.get("agent_name", "symbiont") if request.metadata else "symbiont",
            "risk_level": request.metadata.get("risk_level") if request.metadata else None,
            "metadata": request.metadata or {},
        }
        language_context = self._request_language_context(request)
        if language_context is not None:
            payload["language_context"] = language_context
        if llm_config_payload:
            payload["llm_config"] = llm_config_payload
        return self._with_agentic_metadata(payload)

    def _reasoning_synthesis_payload(
        self,
        request: AgentInvokeRequest,
        llm_config_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        sources = []
        if isinstance(request.context, dict):
            results = request.context.get("results", [])
            if results:
                for result in results:
                    if isinstance(result, dict):
                        sources.append({
                            "agent_name": result.get("agent_name", result.get("agent", "unknown")),
                            "output": result.get("output", result.get("result", "")),
                            "confidence": result.get("confidence", 1.0),
                        })
            else:
                ctx = request.context.get("context", "")
                if ctx:
                    sources.extend(self._context_text_to_sources(str(ctx)))
        payload: dict[str, Any] = {"query": request.query, "sources": sources}
        if request.metadata:
            payload["metadata"] = request.metadata
        language_context = self._request_language_context(request)
        if language_context is not None:
            payload["language_context"] = language_context
        if llm_config_payload:
            payload["llm_config"] = llm_config_payload
        return self._with_agentic_metadata(payload)

    def _reasoning_decomposition_payload(
        self,
        request: AgentInvokeRequest,
        llm_config_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        context = request.context if isinstance(request.context, dict) else {}
        payload: dict[str, Any] = {
            "query": request.query,
            "available_agents": context.get("available_agents", []),
            "metadata": request.metadata or {},
        }
        language_context = self._request_language_context(request)
        if language_context is not None:
            payload["language_context"] = language_context
        if llm_config_payload:
            payload["llm_config"] = llm_config_payload
        return self._with_agentic_metadata(payload)

    def _reasoning_classification_payload(
        self,
        request: AgentInvokeRequest,
        llm_config_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        context = request.context if isinstance(request.context, dict) else {}
        payload: dict[str, Any] = {
            "query": request.query,
            "available_agents": context.get("available_agents", []),
        }
        if request.metadata:
            payload["metadata"] = request.metadata
        language_context = self._request_language_context(request)
        if language_context is not None:
            payload["language_context"] = language_context
        if llm_config_payload:
            payload["llm_config"] = llm_config_payload
        return self._with_agentic_metadata(payload)

    def _generic_agent_payload(
        self,
        agent_name: str,
        request: AgentInvokeRequest,
        llm_config_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        context = request.context
        if isinstance(context, list):
            context = "\n\n".join(
                block.get("content", str(block)) if isinstance(block, dict) else str(block)
                for block in context
            ) if context else ""
        elif isinstance(context, dict):
            context = context.get("content", context.get("context", str(context)))

        payload: dict[str, Any] = {
            "query": request.query,
            "context": context,
            "budget_tokens": request.budget_tokens,
        }
        if request.history:
            payload["history"] = request.history
        if request.metadata:
            payload["metadata"] = request.metadata
        language_context = self._request_language_context(request)
        if language_context is not None:
            payload["language_context"] = language_context
        if llm_config_payload:
            payload["llm_config"] = llm_config_payload
        return self._with_agentic_metadata(payload)

    def _request_language_context(self, request: AgentInvokeRequest) -> dict[str, Any] | None:
        if isinstance(request.language_context, dict) and request.language_context:
            return request.language_context
        context = request.metadata.get("language_context") if isinstance(request.metadata, dict) else None
        return context if isinstance(context, dict) else None

    def _context_text_to_sources(self, context_text: str) -> list[dict[str, Any]]:
        """Split aggregated context blocks into synthesis sources."""
        sources: list[dict[str, Any]] = []
        for match in _CONTEXT_SOURCE_RE.finditer(context_text or ""):
            name = match.group("source").strip() or "context"
            content = match.group("content").strip()
            if content:
                sources.append({
                    "agent_name": name,
                    "output": content,
                    "confidence": 1.0,
                })
        if sources:
            return sources
        stripped = (context_text or "").strip()
        return [{
            "agent_name": "context",
            "output": stripped,
            "confidence": 1.0,
        }] if stripped else []

    def _headers_with_context(self) -> dict[str, str]:
        headers = self._internal_auth_headers()
        try:
            from orchestrator.agentic.policy import headers_for_current_context

            headers = {**headers, **headers_for_current_context()}
        except Exception:
            pass
        return headers

    def _internal_auth_headers(self) -> dict[str, str]:
        key = os.environ.get("INTERNAL_API_KEY", "").strip()
        key_file = os.environ.get("INTERNAL_API_KEY_FILE", "").strip()
        if not key and key_file:
            try:
                with open(key_file, encoding="utf-8") as f:
                    key = f.read().strip()
            except OSError:
                key = ""
        if not key:
            return {}
        return {"Authorization": f"Bearer {key}", "X-API-Key": key}

    def _with_agentic_metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from orchestrator.agentic.context import get_agentic_context

            ctx = get_agentic_context()
            if ctx is None:
                return payload
            metadata = dict(payload.get("metadata") or {})
            metadata.update({
                "task_id": ctx.task_id,
                "request_id": ctx.request_id,
                "trace_id": ctx.trace_id,
                "idempotency_key": f"agentic:{ctx.task_id}",
            })
            return {**payload, "metadata": metadata}
        except Exception:
            return payload

    def _audit_policy(self, action: str, *, payload: dict[str, Any]):
        try:
            from orchestrator.agentic.policy import audit_policy_check

            return audit_policy_check(action, payload=payload, component="AgentClient")
        except Exception as exc:
            log.debug("Agent policy audit skipped for %s: %s", action, exc)
            return None

    def _record_agent_failure(
        self,
        agent_name: str,
        *,
        error: str,
        latency_ms: float,
        phase: str,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        try:
            from orchestrator.agentic.context import get_agentic_context

            ctx = get_agentic_context()
            if ctx is None:
                return
            from orchestrator.agentic.store import get_agentic_store

            payload = {
                "agent_name": agent_name,
                "phase": phase,
                "method": method,
                "path": path,
                "latency_ms": round(latency_ms, 2),
                "error": error[:500],
            }
            store = get_agentic_store()
            store.record_event(
                task_id=ctx.task_id,
                trace_id=ctx.trace_id,
                event_type="agent.invoke.failed",
                actor="AgentClient",
                payload=payload,
            )
            store.record_ai_local_event(
                {
                    "event_id": f"evt_{uuid.uuid4().hex}",
                    "producer": agent_name,
                    "type": "agent.invoke.failed",
                    "severity": "medium",
                    "task_id": ctx.task_id,
                    "trace_id": ctx.trace_id,
                    "payload": payload,
                    "created_at": time.time(),
                },
                actor="AgentClient",
            )
        except Exception as exc:
            log.debug("Agent failure event skipped for %s: %s", agent_name, exc)

    def _llm_config_for_request(
        self,
        agent_name: str,
        request: AgentInvokeRequest,
    ) -> dict[str, Any] | None:
        payload = self._get_llm_config_payload(agent_name)
        if not payload:
            return None

        metadata = request.metadata or {}
        payload = self._apply_model_selection_to_llm_config(payload, metadata)
        parameters = dict(payload.get("parameters") or {})
        if request.timeout_seconds is not None:
            try:
                current_timeout = float(parameters.get("timeout", 0) or 0)
            except (TypeError, ValueError):
                current_timeout = 0.0
            parameters["timeout"] = max(current_timeout, float(request.timeout_seconds))
            payload = {**payload, "parameters": parameters}

        requested_budget = metadata.get("llm_output_budget_tokens")
        if requested_budget is None and metadata.get("propagate_budget_to_llm"):
            requested_budget = request.budget_tokens
        if requested_budget is None:
            return payload

        try:
            budget_tokens = int(requested_budget)
        except (TypeError, ValueError):
            budget_tokens = 0
        if budget_tokens > 0:
            try:
                current_max_tokens = int(parameters.get("max_tokens", 0) or 0)
            except (TypeError, ValueError):
                current_max_tokens = 0
            parameters["max_tokens"] = max(current_max_tokens, budget_tokens)

        return {**payload, "parameters": parameters}

    def _apply_model_selection_to_llm_config(
        self,
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        selection = metadata.get("agentic_model_selection") or metadata.get("model_selection")
        if not isinstance(selection, dict):
            return payload
        model = str(selection.get("model") or "").strip()
        if not model:
            return payload
        updated = dict(payload)
        updated["model"] = model
        backend_type = str(selection.get("backend_type") or "").strip()
        backend_url = str(selection.get("backend_url") or "").strip()
        if backend_type:
            updated["backend_type"] = backend_type
        if backend_url:
            updated["backend_url"] = backend_url
        return updated

    def _get_llm_config_payload(self, agent_name: str) -> dict[str, Any] | None:
        """Get LLM config payload for an agent from the central registry."""
        try:
            from orchestrator.registry import get_registry
            reg = get_registry()
            agent_cfg = reg.get_agent_config(agent_name)
            if agent_cfg is None:
                return None
            backend_name, backend_type, backend_url, model = self._resolve_agent_llm_backend(agent_cfg)
            return {
                "model": model,
                "backend_type": backend_type,
                "backend_url": backend_url,
                "system_prompt": agent_cfg.system_prompt,
                "parameters": agent_cfg.parameters,
                "extra_prompts": agent_cfg.extra_prompts,
            }
        except Exception as exc:
            log.debug("Could not load LLM config for agent %s: %s", agent_name, exc)
            return None

    def _resolve_agent_llm_backend(self, agent_cfg: Any) -> tuple[str, str, str, str]:
        """Resolve agent LLM config against enabled central runtime backends."""
        settings = get_settings()
        preferred_name = self._agent_backend_name(agent_cfg)
        enabled = [backend for backend in settings.llm.backends if backend.enabled and backend.base_url]
        if not enabled:
            return (
                preferred_name or agent_cfg.backend_type,
                agent_cfg.backend_type,
                self._normalize_backend_url(agent_cfg.backend_url),
                agent_cfg.model,
            )

        def score(backend: Any) -> tuple[int, int, int]:
            exact = 0 if backend.name == preferred_name else 1
            model_supported = 0 if agent_cfg.model in backend.models else 1
            return (exact, model_supported, int(backend.priority))

        backend = sorted(enabled, key=score)[0]
        model = self._resolve_agent_model(agent_cfg.model, backend)
        backend_type = "ollama" if backend.name == "ollama" else "openai"
        backend_url = self._agent_backend_url(backend)
        if backend.name != preferred_name:
            log.info(
                "agent LLM backend resolved: agent=%s configured=%s active=%s model=%s",
                agent_cfg.agent_name,
                preferred_name,
                backend.name,
                model,
            )
        return backend.name, backend_type, backend_url, model

    def _agent_backend_name(self, agent_cfg: Any) -> str:
        backend_raw = getattr(agent_cfg, "backend_raw", {}) or {}
        service = str(backend_raw.get("service") or "").strip()
        if service:
            return service
        backend_type = str(getattr(agent_cfg, "backend_type", "") or "").strip()
        if backend_type == "ollama":
            return "ollama"
        return backend_type

    def _agent_backend_url(self, backend: Any) -> str:
        url = str(backend.base_url or "").rstrip("/")
        if backend.name == "ollama" and url.endswith("/v1"):
            return url[:-3].rstrip("/")
        return url

    def _resolve_agent_model(self, configured_model: str, backend: Any) -> str:
        available = self._available_backend_models(backend)
        if available:
            if configured_model in available:
                return configured_model
            for candidate in backend.models:
                if candidate in available:
                    log.info(
                        "agent LLM model resolved: configured=%s active_backend=%s available_model=%s",
                        configured_model,
                        backend.name,
                        candidate,
                    )
                    return candidate
            log.info(
                "agent LLM model resolved: configured=%s active_backend=%s first_available=%s",
                configured_model,
                backend.name,
                available[0],
            )
            return available[0]
        if configured_model in backend.models:
            return configured_model
        return backend.models[0] if backend.models else configured_model

    def _available_backend_models(self, backend: Any) -> list[str]:
        now = time.time()
        cache_key = str(backend.base_url)
        cached = self._backend_model_cache.get(cache_key)
        if cached and now - cached[0] < 30:
            return cached[1]
        try:
            url = self._models_url_for_backend(backend)
            resp = httpx.get(url, timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            names = self._parse_model_names(data)
            self._backend_model_cache[cache_key] = (now, names)
            return names
        except Exception as exc:
            log.debug("Could not list models for backend %s: %s", backend.name, exc)
            return []

    def _models_url_for_backend(self, backend: Any) -> str:
        url = str(backend.base_url or "").rstrip("/")
        if backend.name == "ollama":
            if url.endswith("/v1"):
                url = url[:-3].rstrip("/")
            return f"{url}/api/tags"
        return f"{url}/models" if url.endswith("/v1") else f"{url}/v1/models"

    def _parse_model_names(self, data: dict[str, Any]) -> list[str]:
        items = data.get("models")
        if isinstance(items, list):
            names = []
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("id") or item.get("model")
                else:
                    name = str(item)
                if name:
                    names.append(str(name))
            return names
        items = data.get("data")
        if isinstance(items, list):
            return [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]
        return []

    def _normalize_backend_url(self, url: str) -> str:
        """Make host-local Ollama URLs usable from sibling containers."""
        parsed = urlparse(url)
        if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != 11434:
            return url
        if not (os.path.exists("/.dockerenv") or os.getenv("ORC_IN_DOCKER", "").lower() in {"1", "true", "yes"}):
            return url

        replacement = os.environ.get("ORC_OLLAMA_BASE_URL") or os.environ.get("OLLAMA_BASE_URL") or "https://host.docker.internal:11434"
        replacement_parsed = urlparse(replacement.rstrip("/"))
        return urlunparse((
            replacement_parsed.scheme or parsed.scheme,
            replacement_parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ))

    def list_available(self) -> list[str]:
        """List names of available (healthy) agents."""
        return self._registry.export_agent_names()

    def is_available(self, agent_name: str) -> bool:
        """Check if a specific agent is available."""
        return self._registry.get_healthy(agent_name) is not None

    def is_degraded(self, agent_name: str) -> bool:
        """Return true when a reversible runtime flag has degraded this agent."""

        return self.degraded_runtime_flag(agent_name) is not None

    def degraded_runtime_flag(self, agent_name: str) -> dict[str, Any] | None:
        """Return the active `service_degraded:<agent>` flag, if present."""

        try:
            from orchestrator.agentic.store import get_agentic_store

            return get_agentic_store().get_runtime_flag(f"service_degraded:{agent_name}")
        except Exception as exc:
            log.debug("Agent degraded flag check skipped for %s: %s", agent_name, exc)
            return None

    def record_degraded_skip(
        self,
        agent_name: str,
        *,
        flag: dict[str, Any],
        phase: str = "dispatch_filter",
    ) -> None:
        self._record_agent_degraded_skip(
            agent_name,
            flag=flag,
            latency_ms=0.0,
            phase=phase,
        )

    def _record_agent_degraded_skip(
        self,
        agent_name: str,
        *,
        flag: dict[str, Any],
        latency_ms: float,
        phase: str,
    ) -> None:
        try:
            from orchestrator.agentic.context import get_agentic_context
            from orchestrator.agentic.store import get_agentic_store

            ctx = get_agentic_context()
            store = get_agentic_store()
            payload = {
                "agent_name": agent_name,
                "phase": phase,
                "latency_ms": round(latency_ms, 2),
                "runtime_flag": flag,
                "safe_action": (flag.get("value") or {}).get("safe_action"),
            }
            store.record_event(
                task_id=ctx.task_id if ctx else None,
                trace_id=ctx.trace_id if ctx else None,
                event_type="agent.invoke.skipped_degraded",
                actor="AgentClient",
                payload=payload,
            )
            if ctx is not None:
                store.record_tool_call(
                    task_id=ctx.task_id,
                    tool_name="agent.invoke",
                    risk_level="low",
                    status="skipped_degraded",
                    input_payload={"agent": agent_name},
                    output_payload=payload,
                    metadata={"component": "AgentClient", "runtime_flag_enforced": True},
                )
        except Exception as exc:
            log.debug("Agent degraded skip event skipped for %s: %s", agent_name, exc)
