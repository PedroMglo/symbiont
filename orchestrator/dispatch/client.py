"""Base HTTP client with retries, timeouts, and circuit breaker integration."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from orchestrator.dispatch.types import (
    ServiceEndpoint,
    ServiceHealth,
    ServiceStatus,
)

log = logging.getLogger(__name__)


class CircuitOpen(Exception):
    """Raised when circuit breaker is open for a service."""

    def __init__(self, service_name: str):
        self.service_name = service_name
        super().__init__(f"Circuit open for service: {service_name}")


class HTTPServiceClient:
    """HTTP client for communicating with external services.

    Features:
    - Configurable retries with exponential backoff
    - Per-service timeouts
    - Circuit breaker (configurable threshold)
    - Health probing
    - Connection pooling via httpx
    """

    def __init__(
        self,
        pool_size: int = 20,
        circuit_threshold: int = 3,
        circuit_reset_seconds: float = 60.0,
    ):
        self._pool_size = pool_size
        self._circuit_threshold = circuit_threshold
        self._circuit_reset_seconds = circuit_reset_seconds

        # Per-service failure tracking for circuit breaker
        self._failures: dict[str, int] = {}
        self._circuit_opened_at: dict[str, float] = {}

        # Shared httpx client with connection pooling
        self._client = httpx.Client(
            limits=httpx.Limits(
                max_connections=pool_size,
                max_keepalive_connections=pool_size // 2,
            ),
            follow_redirects=True,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _is_circuit_open(self, name: str) -> bool:
        if name not in self._circuit_opened_at:
            return False
        elapsed = time.time() - self._circuit_opened_at[name]
        if elapsed >= self._circuit_reset_seconds:
            # Half-open: allow one attempt
            del self._circuit_opened_at[name]
            self._failures[name] = 0
            return False
        return True

    def _record_success(self, name: str) -> None:
        self._failures[name] = 0
        self._circuit_opened_at.pop(name, None)

    def reset_circuit(self, name: str) -> None:
        """Explicitly reset the circuit breaker for a service (e.g. after container restart)."""
        self._failures.pop(name, None)
        self._circuit_opened_at.pop(name, None)

    def _record_failure(self, name: str) -> None:
        self._failures[name] = self._failures.get(name, 0) + 1
        if self._failures[name] >= self._circuit_threshold:
            self._circuit_opened_at[name] = time.time()
            log.warning("Circuit opened for service %s (failures: %d)", name, self._failures[name])

    # ------------------------------------------------------------------
    # Core request methods
    # ------------------------------------------------------------------

    def request(
        self,
        endpoint: ServiceEndpoint,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        """Make an HTTP request to a service endpoint with retries and circuit breaker.

        Args:
            endpoint: Service endpoint configuration
            method: HTTP method (GET, POST, etc.)
            path: URL path (e.g. /v1/critic/evaluate)
            json: JSON body (for POST/PUT)
            params: Query parameters
            headers: Extra request headers
            timeout: Override timeout (uses endpoint.timeout_seconds if None)

        Returns:
            httpx.Response on success

        Raises:
            CircuitOpen: If circuit breaker is open
            httpx.HTTPStatusError: On 4xx/5xx responses after retries
            httpx.ConnectError: On connection failure after retries
        """
        if self._is_circuit_open(endpoint.name):
            raise CircuitOpen(endpoint.name)

        url = f"{endpoint.url.rstrip('/')}{path}"
        effective_timeout = timeout or endpoint.timeout_seconds
        effective_retries = endpoint.retries if retries is None else max(0, int(retries))
        last_error: Exception | None = None

        for attempt in range(effective_retries + 1):
            try:
                resp = self._client.request(
                    method=method,
                    url=url,
                    json=json,
                    params=params,
                    headers=headers,
                    timeout=effective_timeout,
                )
                resp.raise_for_status()
                self._record_success(endpoint.name)
                return resp

            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                self._record_failure(endpoint.name)
                if attempt < effective_retries:
                    backoff = 0.5 * (2 ** attempt)
                    log.debug(
                        "Service %s connection failed (attempt %d/%d), retrying in %.1fs",
                        endpoint.name, attempt + 1, effective_retries + 1, backoff,
                    )
                    time.sleep(backoff)

            except httpx.HTTPStatusError as exc:
                last_error = exc
                # Don't retry client errors (4xx)
                if 400 <= exc.response.status_code < 500:
                    raise
                self._record_failure(endpoint.name)
                if attempt < effective_retries:
                    backoff = 0.5 * (2 ** attempt)
                    time.sleep(backoff)

            except httpx.TimeoutException as exc:
                last_error = exc
                self._record_failure(endpoint.name)
                if attempt < effective_retries:
                    backoff = 0.5 * (2 ** attempt)
                    time.sleep(backoff)

        # All retries exhausted
        raise last_error  # type: ignore[misc]

    def get(
        self,
        endpoint: ServiceEndpoint,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        """GET request to a service."""
        return self.request(endpoint, "GET", path, params=params, headers=headers, timeout=timeout, retries=retries)

    def post(
        self,
        endpoint: ServiceEndpoint,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        """POST request to a service."""
        return self.request(endpoint, "POST", path, json=json, headers=headers, timeout=timeout, retries=retries)

    # ------------------------------------------------------------------
    # Health probing
    # ------------------------------------------------------------------

    def check_health(self, endpoint: ServiceEndpoint) -> ServiceHealth:
        """Probe a service's health endpoint.

        Returns ServiceHealth with status and latency.
        Non-blocking: catches all exceptions and returns UNHEALTHY status.
        """
        start = time.time()
        try:
            resp = self._client.get(
                f"{endpoint.url.rstrip('/')}{endpoint.health_path}",
                timeout=3.0,
            )
            latency_ms = (time.time() - start) * 1000

            if resp.status_code == 200:
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                return ServiceHealth(
                    name=endpoint.name,
                    status=ServiceStatus.HEALTHY,
                    latency_ms=latency_ms,
                    last_checked=time.time(),
                    version=data.get("version", ""),
                )
            else:
                return ServiceHealth(
                    name=endpoint.name,
                    status=ServiceStatus.DEGRADED,
                    latency_ms=latency_ms,
                    last_checked=time.time(),
                    error=f"HTTP {resp.status_code}",
                )

        except Exception as exc:
            latency_ms = (time.time() - start) * 1000
            return ServiceHealth(
                name=endpoint.name,
                status=ServiceStatus.UNHEALTHY,
                latency_ms=latency_ms,
                last_checked=time.time(),
                error=str(exc)[:200],
            )

    def is_healthy(self, endpoint: ServiceEndpoint) -> bool:
        """Quick health check — returns True if service responds 200."""
        health = self.check_health(endpoint)
        return health.status == ServiceStatus.HEALTHY
