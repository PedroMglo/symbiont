"""Typed HTTP client for querying feature services (context providers)."""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any

import httpx

from orchestrator.config import get_settings
from orchestrator.dispatch.client import CircuitOpen, HTTPServiceClient
from orchestrator.dispatch.response_contracts import normalize_feature_response
from orchestrator.dispatch.service_registry import ServiceRegistry
from orchestrator.dispatch.types import (
    FeatureInvokeResponse,
    FeatureQueryRequest,
    FeatureQueryResponse,
    ServiceEndpoint,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EndpointDispatchProfile:
    auth_profile: str = "internal_api"
    tls_alias_profile: str = ""
    policy_action: str = ""


# ---------------------------------------------------------------------------
# Feature endpoint mappings
# ---------------------------------------------------------------------------
# The primary endpoint per feature ([dispatch.feature_endpoints]) and the
# context-source → feature routing table ([dispatch.source_map]) are defined
# in config/orc/agents.toml — there are no hardcoded mappings here.


class FeatureClient:
    """Typed client for querying feature services (context providers) via HTTP.

    Usage:
        client = FeatureClient(registry)
        response = client.query("research", FeatureQueryRequest(query="..."))
        # Or use source-based routing:
        response = client.query_source("rag", query="...", budget_tokens=2000)
    """

    def __init__(self, registry: ServiceRegistry, http_client: HTTPServiceClient | None = None):
        self._registry = registry
        self._http = http_client or registry._client

    def query(
        self,
        feature_name: str,
        request: FeatureQueryRequest,
        *,
        endpoint_override: str | None = None,
        method_override: str | None = None,
        source_mapping: Any | None = None,
    ) -> FeatureQueryResponse:
        """Query a feature service.

        Args:
            feature_name: Registered feature name (e.g. "research")
            request: The query request
            endpoint_override: Override the default endpoint path
            method_override: Override the HTTP method

        Returns:
            FeatureQueryResponse with content or error
        """
        request = self._with_dispatch_defaults(request)

        ep = self._registry.ensure_available(feature_name)
        if ep is None:
            return FeatureQueryResponse(
                content="",
                source=feature_name,
                success=False,
                error=f"Feature '{feature_name}' not available",
            )
        mapping = get_settings().dispatch.feature_endpoints.get(feature_name)
        ep = self._endpoint_with_dispatch_profile(ep, mapping)
        method = method_override or (mapping[0] if mapping else None)
        path = endpoint_override or (mapping[1] if mapping else None)
        if method is None or path is None:
            return FeatureQueryResponse(
                content="",
                source=feature_name,
                success=False,
                error=(
                    f"No endpoint configured for feature '{feature_name}'. "
                    "Add it to [dispatch.feature_endpoints] in agents.toml."
                ),
            )

        start = time.time()
        policy_action = self._policy_action_for_feature(feature_name, path, source_mapping, mapping)
        policy = self._audit_policy(
            policy_action,
            payload={
                "feature": feature_name,
                "method": method,
                "path": path,
                "budget_tokens": request.budget_tokens,
            },
            component="FeatureClient",
        )
        if policy is not None and policy.should_block:
            return FeatureQueryResponse(
                content="",
                source=feature_name,
                success=False,
                error=f"Policy blocked {policy.action}: {policy.reason}",
            )
        headers = self._headers_with_context(self._auth_headers_for_endpoint(mapping))
        lifecycle = getattr(self._registry, "_lifecycle", None)
        lifecycle_active = bool(lifecycle and lifecycle.available)
        if lifecycle_active:
            lifecycle.begin_use(feature_name)

        try:
            if method == "GET":
                params = {"query": request.query, "budget_tokens": str(request.budget_tokens)}
                kwargs = {"params": params, "timeout": request.timeout_seconds}
                if headers:
                    kwargs["headers"] = headers
                resp = self._http.get(ep, path, **kwargs)
            else:
                payload = {
                    "query": request.query,
                    "budget_tokens": request.budget_tokens,
                    "timeout_seconds": request.timeout_seconds,
                }
                metadata = self._metadata_with_agentic_context(request.metadata)
                if metadata:
                    payload["metadata"] = metadata
                kwargs = {"json": payload, "timeout": request.timeout_seconds}
                if headers:
                    kwargs["headers"] = headers
                resp = self._http.post(ep, path, **kwargs)

            latency_ms = (time.time() - start) * 1000
            data = resp.json()

            response = normalize_feature_response(
                data=data,
                source=feature_name,
                latency_ms=latency_ms,
            )
            self._record_rag_miss_if_needed(policy_action, response)
            return response

        except CircuitOpen:
            response = FeatureQueryResponse(
                content="",
                source=feature_name,
                success=False,
                error=f"Circuit breaker open for '{feature_name}'",
            )
            self._record_rag_miss_if_needed(policy_action, response)
            return response

        except httpx.HTTPStatusError as exc:
            latency_ms = (time.time() - start) * 1000
            payload = _http_status_error_payload(exc)
            log.warning(
                "Feature %s query returned HTTP error: %s",
                feature_name,
                _http_status_error_message(feature_name, path, payload),
            )
            response = FeatureQueryResponse(
                content="",
                source=feature_name,
                success=False,
                latency_ms=latency_ms,
                metadata={"http_error": payload},
                error=_http_status_error_message(feature_name, path, payload),
            )
            self._record_rag_miss_if_needed(policy_action, response)
            return response

        except Exception as exc:
            latency_ms = (time.time() - start) * 1000
            log.warning("Feature %s query failed: %s", feature_name, exc)
            response = FeatureQueryResponse(
                content="",
                source=feature_name,
                success=False,
                latency_ms=latency_ms,
                error=str(exc)[:300],
            )
            self._record_rag_miss_if_needed(policy_action, response)
            return response
        finally:
            if lifecycle_active:
                lifecycle.end_use(feature_name)

    def query_source(
        self,
        source_name: str,
        *,
        query: str = "",
        budget_tokens: int | None = None,
        timeout: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> FeatureQueryResponse:
        """Query a context source by its routing table name.

        This maps source names (rag, cag, graph, repo, system, calendar, email, rss)
        to the appropriate feature service and endpoint.

        Args:
            source_name: Context source from the routing table
            query: The user query
            budget_tokens: Token budget for the response
            timeout: Request timeout

        Returns:
            FeatureQueryResponse with content
        """
        dispatch_cfg = get_settings().dispatch
        budget_tokens = budget_tokens if budget_tokens is not None else dispatch_cfg.feature_budget_tokens
        timeout = timeout if timeout is not None else dispatch_cfg.feature_timeout_seconds

        mapping = get_settings().dispatch.source_map.get(source_name)
        if mapping is None:
            return FeatureQueryResponse(
                content="",
                source=source_name,
                success=False,
                error=f"Unknown source: '{source_name}' (not mapped to any feature service)",
            )

        response = self.query(
            mapping.feature,
            FeatureQueryRequest(
                query=query,
                budget_tokens=budget_tokens,
                timeout_seconds=timeout,
                metadata=metadata or {},
            ),
            endpoint_override=mapping.path,
            method_override=mapping.method,
            source_mapping=mapping,
        )
        return replace(response, source=source_name)

    def invoke_endpoint(
        self,
        feature_name: str,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        policy_action: str | None = None,
        auth_profile: str = "internal_api",
        tls_alias_profile: str = "",
    ) -> FeatureInvokeResponse:
        """Invoke a feature endpoint without embedding feature-specific clients.

        The caller owns the API payload shape; dispatch owns endpoint resolution,
        lifecycle startup, policy audit, auth headers, TLS aliases, retries, and
        circuit breaker behavior.
        """

        ep = self._registry.ensure_available(feature_name)
        if ep is None:
            return FeatureInvokeResponse(
                source=feature_name,
                success=False,
                error=f"Feature '{feature_name}' not available",
            )

        mapping = _EndpointDispatchProfile(
            auth_profile=auth_profile,
            tls_alias_profile=tls_alias_profile,
            policy_action=policy_action or f"{feature_name}.invoke",
        )
        ep = self._endpoint_with_dispatch_profile(ep, mapping)
        action = policy_action or self._policy_action_for_feature(feature_name, path, endpoint_mapping=mapping)
        policy = self._audit_policy(
            action,
            payload={
                "feature": feature_name,
                "method": method,
                "path": path,
            },
            component="FeatureClient",
        )
        if policy is not None and policy.should_block:
            return FeatureInvokeResponse(
                source=feature_name,
                success=False,
                error=f"Policy blocked {policy.action}: {policy.reason}",
            )

        headers = self._headers_with_context(self._auth_headers_for_endpoint(mapping))
        start = time.time()
        lifecycle = getattr(self._registry, "_lifecycle", None)
        lifecycle_active = bool(lifecycle and lifecycle.available)
        if lifecycle_active:
            lifecycle.begin_use(feature_name)
        try:
            normalized_method = method.upper()
            if normalized_method == "GET":
                resp = self._http.get(ep, path, params=params, headers=headers, timeout=timeout)
            elif normalized_method == "POST":
                resp = self._http.post(ep, path, json=payload, headers=headers, timeout=timeout)
            else:
                resp = self._http.request(
                    ep,
                    normalized_method,
                    path,
                    json=payload,
                    params=params,
                    headers=headers,
                    timeout=timeout,
                )
            latency_ms = (time.time() - start) * 1000
            data = resp.json()
            if not isinstance(data, dict):
                data = {"value": data}
            return FeatureInvokeResponse(
                data=data,
                source=feature_name,
                success=True,
                latency_ms=latency_ms,
            )
        except CircuitOpen:
            return FeatureInvokeResponse(
                source=feature_name,
                success=False,
                latency_ms=(time.time() - start) * 1000,
                error=f"Circuit breaker open for '{feature_name}'",
            )
        except httpx.HTTPStatusError as exc:
            payload = _http_status_error_payload(exc)
            error = _http_status_error_message(feature_name, path, payload)
            log.warning("Feature %s endpoint %s returned HTTP error: %s", feature_name, path, error)
            return FeatureInvokeResponse(
                data=payload,
                source=feature_name,
                success=False,
                latency_ms=(time.time() - start) * 1000,
                error=error,
            )
        except Exception as exc:
            log.warning("Feature %s endpoint %s failed: %s", feature_name, path, exc)
            return FeatureInvokeResponse(
                source=feature_name,
                success=False,
                latency_ms=(time.time() - start) * 1000,
                error=str(exc)[:300],
            )
        finally:
            if lifecycle_active:
                lifecycle.end_use(feature_name)

    def gather_context(
        self,
        sources: list[str],
        query: str,
        *,
        budget_tokens: int | None = None,
        timeout_per_source: float | None = None,
        translated_query: str | None = None,
        dual_query_sources: set[str] | None = None,
    ) -> list[FeatureQueryResponse]:
        """Gather context from multiple sources sequentially.

        Distributes the token budget equally across sources.

        Args:
            sources: List of source names from the routing table
            query: The user query
            budget_tokens: Total token budget (split across sources)
            timeout_per_source: Timeout per source

        Returns:
            List of responses (including failed ones for debugging)
        """
        if not sources:
            return []

        dispatch_cfg = get_settings().dispatch
        budget_tokens = budget_tokens if budget_tokens is not None else dispatch_cfg.context_budget_tokens
        timeout_per_source = (
            timeout_per_source
            if timeout_per_source is not None
            else dispatch_cfg.context_timeout_per_source
        )
        per_source_budget = budget_tokens // len(sources)
        results: list[FeatureQueryResponse] = []

        for source in sources:
            resp = self._query_source_maybe_dual(
                source,
                query=query,
                translated_query=translated_query,
                budget_tokens=per_source_budget,
                timeout=timeout_per_source,
                dual_query_sources=dual_query_sources,
            )
            results.append(resp)

        return results

    def gather_context_parallel(
        self,
        sources: list[str],
        query: str,
        *,
        budget_tokens: int | None = None,
        timeout_per_source: float | None = None,
        translated_query: str | None = None,
        dual_query_sources: set[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[FeatureQueryResponse]:
        """Gather context from multiple sources in parallel using threads.

        Args:
            sources: List of source names
            query: The user query
            budget_tokens: Total token budget (split across sources)
            timeout_per_source: Timeout per source

        Returns:
            List of responses
        """
        if not sources:
            return []

        import concurrent.futures

        dispatch_cfg = get_settings().dispatch
        budget_tokens = budget_tokens if budget_tokens is not None else dispatch_cfg.context_budget_tokens
        timeout_per_source = (
            timeout_per_source
            if timeout_per_source is not None
            else dispatch_cfg.context_timeout_per_source
        )
        per_source_budget = budget_tokens // len(sources)

        def _fetch(source: str) -> FeatureQueryResponse:
            return self._query_source_maybe_dual(
                source,
                query=query,
                translated_query=translated_query,
                budget_tokens=per_source_budget,
                timeout=timeout_per_source,
                dual_query_sources=dual_query_sources,
                metadata=metadata,
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(sources), dispatch_cfg.context_parallel_max_workers)
        ) as pool:
            futures = {pool.submit(_fetch, s): s for s in sources}
            results: list[FeatureQueryResponse] = []
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    source = futures[future]
                    results.append(FeatureQueryResponse(
                        content="",
                        source=source,
                        success=False,
                        error=str(exc)[:200],
                    ))

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _with_dispatch_defaults(self, request: FeatureQueryRequest) -> FeatureQueryRequest:
        if request.budget_tokens is not None and request.timeout_seconds is not None:
            return request
        dispatch_cfg = get_settings().dispatch
        return replace(
            request,
            budget_tokens=(
                request.budget_tokens
                if request.budget_tokens is not None
                else dispatch_cfg.feature_budget_tokens
            ),
            timeout_seconds=(
                request.timeout_seconds
                if request.timeout_seconds is not None
                else dispatch_cfg.feature_timeout_seconds
            ),
        )

    def _query_source_maybe_dual(
        self,
        source_name: str,
        *,
        query: str,
        translated_query: str | None,
        budget_tokens: int,
        timeout: float,
        dual_query_sources: set[str] | None,
        metadata: dict[str, Any] | None = None,
    ) -> FeatureQueryResponse:
        should_dual = (
            bool(translated_query)
            and source_name in (dual_query_sources or set())
            and translated_query.strip() != query.strip()
        )
        if not should_dual:
            return self.query_source(
                source_name,
                query=query,
                budget_tokens=budget_tokens,
                timeout=timeout,
                metadata=metadata,
            )

        per_query_budget = max(400, budget_tokens // 2)
        original_resp = self.query_source(
            source_name,
            query=query,
            budget_tokens=per_query_budget,
            timeout=timeout,
            metadata=metadata,
        )
        translated_resp = self.query_source(
            source_name,
            query=translated_query or "",
            budget_tokens=per_query_budget,
            timeout=timeout,
            metadata=metadata,
        )
        return merge_dual_query_responses(
            source_name,
            original_resp,
            translated_resp,
            translated_query=translated_query or "",
        )

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

    def _auth_headers_for_endpoint(self, mapping: Any) -> dict[str, str]:
        auth_profile = str(getattr(mapping, "auth_profile", "") or "internal_api")
        handlers: dict[str, Callable[[], dict[str, str]]] = {
            "none": lambda: {},
            "internal_api": self._internal_auth_headers,
            "audio_transcribe_api_key": self._audio_transcribe_auth_headers,
            "storage_guardian_internal_token": self._storage_auth_headers,
        }
        handler = handlers.get(auth_profile)
        if handler is None:
            log.warning("Unknown dispatch auth profile %r; falling back to internal_api", auth_profile)
            handler = self._internal_auth_headers
        return handler()

    def _endpoint_with_dispatch_profile(self, endpoint: ServiceEndpoint, mapping: Any) -> ServiceEndpoint:
        tls_alias_profile = str(getattr(mapping, "tls_alias_profile", "") or "")
        def identity(ep: ServiceEndpoint) -> ServiceEndpoint:
            return ep

        handlers: dict[str, Callable[[ServiceEndpoint], ServiceEndpoint]] = {
            "": identity,
            "storage_guardian_compose": self._storage_tls_compatible_endpoint,
        }
        handler = handlers.get(tls_alias_profile)
        if handler is None:
            log.warning("Unknown dispatch TLS alias profile %r; using endpoint as configured", tls_alias_profile)
            handler = identity
        return handler(endpoint)

    def _metadata_with_agentic_context(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        merged: dict[str, Any] = dict(metadata or {})
        merged.update(self._agentic_metadata())
        return merged

    @staticmethod
    def _storage_tls_compatible_endpoint(endpoint: ServiceEndpoint) -> ServiceEndpoint:
        url = endpoint.url.replace("://orc-storage-guardian:", "://storage-guardian:")
        if url == endpoint.url:
            return endpoint
        return replace(endpoint, url=url)

    def _storage_auth_headers(self) -> dict[str, str]:
        token = os.environ.get("STORAGE_GUARDIAN_INTERNAL_TOKEN", "").strip()
        token_file = os.environ.get("STORAGE_GUARDIAN_INTERNAL_TOKEN_FILE", "").strip()
        if not token and not token_file:
            token = os.environ.get("INTERNAL_API_KEY", "").strip()
            token_file = os.environ.get("INTERNAL_API_KEY_FILE", "").strip()
        if not token and token_file:
            try:
                with open(token_file, encoding="utf-8") as f:
                    token = f.read().strip()
            except OSError:
                token = ""
        return {"X-Internal-Token": token} if token else {}

    def _audio_transcribe_auth_headers(self) -> dict[str, str]:
        key = os.environ.get("AUDIO_TRANSCRIBE_API_KEY", "").strip()
        key_file = os.environ.get("AUDIO_TRANSCRIBE_API_KEY_FILE", "").strip()
        if not key and key_file:
            try:
                with open(key_file, encoding="utf-8") as f:
                    key = f.read().strip()
            except OSError:
                key = ""
        if not key:
            return {}
        return {"Authorization": f"Bearer {key}", "X-API-Key": key}

    def _headers_with_context(self, base: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(base or {})
        try:
            from orchestrator.agentic.policy import headers_for_current_context

            headers = {**headers_for_current_context(), **headers}
        except Exception:
            pass
        return headers

    def _agentic_metadata(self) -> dict[str, str]:
        try:
            from orchestrator.agentic.context import get_agentic_context

            ctx = get_agentic_context()
            if ctx is None:
                return {}
            return {
                "task_id": ctx.task_id,
                "request_id": ctx.request_id,
                "trace_id": ctx.trace_id,
                "session_id": ctx.session_id,
                "mode": ctx.mode,
                "idempotency_key": f"agentic:{ctx.task_id}",
            }
        except Exception:
            return {}

    def _audit_policy(self, action: str, *, payload: dict[str, Any], component: str):
        try:
            from orchestrator.agentic.policy import audit_policy_check

            return audit_policy_check(action, payload=payload, component=component)
        except Exception as exc:
            log.debug("Feature policy audit skipped for %s: %s", action, exc)
            return None

    def _record_rag_miss_if_needed(self, action: str, response: FeatureQueryResponse) -> None:
        if action != "rag.query":
            return
        miss_review = self._rag_miss_review(response.metadata)
        should_record = bool(miss_review.get("should_record"))
        if response.success and response.content.strip() and not should_record:
            return
        try:
            from orchestrator.agentic.context import get_agentic_context
            from orchestrator.agentic.store import get_agentic_store

            ctx = get_agentic_context()
            payload = {
                "source": response.source,
                "success": response.success,
                "error": response.error,
            }
            hint_payload = miss_review.get("payload")
            if isinstance(hint_payload, dict):
                payload.update(hint_payload)
            payload.setdefault("source", response.source)
            evidence_refs = self._string_list(miss_review.get("evidence_refs"))
            if evidence_refs and "evidence_refs" not in payload:
                payload["evidence_refs"] = evidence_refs
            producer = str(miss_review.get("producer") or response.source or "rag")
            severity = str(miss_review.get("severity") or "low")
            store = get_agentic_store()
            store.record_event(
                task_id=ctx.task_id if ctx is not None else None,
                trace_id=ctx.trace_id if ctx is not None else None,
                event_type="rag.miss",
                actor="FeatureClient",
                payload=payload,
            )
            store.record_ai_local_event(
                {
                    "event_id": f"evt_{uuid.uuid4().hex}",
                    "producer": producer,
                    "type": "rag.miss",
                    "severity": severity,
                    "task_id": ctx.task_id if ctx is not None else None,
                    "trace_id": ctx.trace_id if ctx is not None else None,
                    "payload": payload,
                    "created_at": time.time(),
                },
                actor="FeatureClient",
            )
        except Exception:
            pass

    @staticmethod
    def _rag_miss_review(metadata: dict[str, Any] | None) -> dict[str, Any]:
        value = (metadata or {}).get("miss_review")
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, (str, bytes)):
            values = [value]
        else:
            try:
                values = list(value)
            except TypeError:
                values = [value]
        result: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def _policy_action_for_feature(
        self,
        feature_name: str,
        path: str,
        source_mapping: Any = None,
        endpoint_mapping: Any = None,
    ) -> str:
        configured = str(getattr(source_mapping, "policy_action", "") or "")
        if not configured:
            configured = str(getattr(endpoint_mapping, "policy_action", "") or "")
        if configured:
            return configured
        if "cag" in path or "explain" in path:
            return "cag.explain"
        if "/research/" in path:
            return "rag.query"
        return f"{feature_name}.query"

    def list_available(self) -> list[str]:
        """List names of available (healthy) features."""
        return self._registry.export_feature_names()

    def is_available(self, feature_name: str) -> bool:
        """Check if a specific feature is available."""
        return self._registry.get_healthy(feature_name) is not None


def _http_status_error_payload(exc: httpx.HTTPStatusError) -> dict[str, Any]:
    response = exc.response
    payload: dict[str, Any] = {"status_code": response.status_code}
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        if text:
            payload["body"] = _preview(text, 1000)
        return payload

    if isinstance(body, dict):
        payload.update(body)
    else:
        payload["body"] = body
    return payload


def _http_status_error_message(feature_name: str, path: str, payload: dict[str, Any]) -> str:
    status = payload.get("status_code", "unknown")
    detail = payload.get("message") or payload.get("detail") or payload.get("error")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("code") or str(detail)
    elif isinstance(detail, list):
        detail = "; ".join(str(item) for item in detail[:3])
    elif detail is None and payload.get("body") is not None:
        detail = str(payload["body"])

    prefix = f"HTTP {status} from {feature_name}{path}"
    if not detail:
        return prefix
    return f"{prefix}: {_preview(str(detail), 300)}"


def merge_dual_query_responses(
    source_name: str,
    original: FeatureQueryResponse,
    translated: FeatureQueryResponse,
    *,
    translated_query: str,
) -> FeatureQueryResponse:
    """Merge original-language and translated retrieval responses.

    This preserves the original response first, appends unique translated-query
    hits, and carries metadata showing whether each side succeeded.
    """

    if not original.success and not translated.success:
        return FeatureQueryResponse(
            content="",
            source=source_name,
            success=False,
            error=original.error or translated.error or "dual query failed",
            metadata={
                "i18n_dual_query_used": True,
                "i18n_original_success": False,
                "i18n_translated_success": False,
                "i18n_translated_query": _preview(translated_query),
            },
        )

    if original.success and not translated.success:
        metadata = dict(original.metadata)
        metadata.update({
            "i18n_dual_query_used": True,
            "i18n_original_success": True,
            "i18n_translated_success": False,
            "i18n_translated_error": translated.error,
            "i18n_translated_query": _preview(translated_query),
        })
        return FeatureQueryResponse(
            content=original.content,
            source=original.source or source_name,
            token_estimate=original.token_estimate,
            success=True,
            latency_ms=original.latency_ms + translated.latency_ms,
            metadata=metadata,
        )

    if translated.success and not original.success:
        metadata = dict(translated.metadata)
        metadata.update({
            "i18n_dual_query_used": True,
            "i18n_original_success": False,
            "i18n_original_error": original.error,
            "i18n_translated_success": True,
            "i18n_translated_query": _preview(translated_query),
        })
        return FeatureQueryResponse(
            content=translated.content,
            source=translated.source or source_name,
            token_estimate=translated.token_estimate,
            success=True,
            latency_ms=original.latency_ms + translated.latency_ms,
            metadata=metadata,
        )

    merged_content = _merge_content_blocks(original.content, translated.content)
    metadata = dict(original.metadata)
    metadata.update({
        "i18n_dual_query_used": True,
        "i18n_original_success": True,
        "i18n_translated_success": True,
        "i18n_translated_query": _preview(translated_query),
    })
    return FeatureQueryResponse(
        content=merged_content,
        source=original.source or translated.source or source_name,
        token_estimate=original.token_estimate + translated.token_estimate,
        success=True,
        latency_ms=original.latency_ms + translated.latency_ms,
        metadata=metadata,
    )


def _merge_content_blocks(original: str, translated: str) -> str:
    blocks: list[str] = []
    seen: set[str] = set()
    for text in (original, translated):
        for block in _split_blocks(text):
            key = sha256(block.strip().lower()[:800].encode("utf-8")).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            blocks.append(block)
    return "\n\n".join(blocks)


def _split_blocks(text: str) -> list[str]:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    if paragraphs:
        return paragraphs
    return [line.strip() for line in text.splitlines() if line.strip()]


def _preview(text: str, limit: int = 160) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."
