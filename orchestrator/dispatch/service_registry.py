"""Service registry — discovery, health checking, and endpoint resolution."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.lifecycle.manager import ContainerLifecycleManager

from orchestrator.dispatch.client import HTTPServiceClient
from orchestrator.dispatch.types import (
    ServiceEndpoint,
    ServiceHealth,
    ServiceStatus,
    ServiceType,
)

log = logging.getLogger(__name__)


class ServiceRegistry:
    """Registry of external agent and feature services.

    Responsibilities:
    - Load service endpoints from configuration
    - Periodic background health checking
    - Resolve service names to endpoints
    - Provide healthy/unhealthy status
    - Allow dynamic registration/deregistration

    Usage:
        registry = ServiceRegistry.from_config(settings)
        registry.start_health_checks(interval=30)
        endpoint = registry.get("reasoning_and_response")
        registry.stop()
    """

    def __init__(self, client: HTTPServiceClient | None = None, lifecycle_manager: "ContainerLifecycleManager | None" = None):
        self._client = client or HTTPServiceClient()
        self._lifecycle = lifecycle_manager
        self._endpoints: dict[str, ServiceEndpoint] = {}
        self._health: dict[str, ServiceHealth] = {}
        self._health_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, services_config: list[dict[str, Any]], client: HTTPServiceClient | None = None, lifecycle_manager: "ContainerLifecycleManager | None" = None) -> "ServiceRegistry":
        """Create registry from config list.

        Each entry in services_config should have:
            name, url, type ("agent"|"feature"), enabled, timeout, retries, capabilities, description
        """
        registry = cls(client=client, lifecycle_manager=lifecycle_manager)
        for svc in services_config:
            endpoint = ServiceEndpoint(
                name=svc["name"],
                url=svc["url"],
                service_type=ServiceType(svc.get("type", "agent")),
                enabled=svc.get("enabled", True),
                timeout_seconds=svc.get("timeout", 10.0),
                retries=svc.get("retries", 2),
                capabilities=svc.get("capabilities", []),
                description=svc.get("description", ""),
                health_path=svc.get("health_path", "/health"),
            )
            registry.register(endpoint)
        return registry

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, endpoint: ServiceEndpoint) -> None:
        """Register a service endpoint."""
        with self._lock:
            self._endpoints[endpoint.name] = endpoint
            self._health[endpoint.name] = ServiceHealth(
                name=endpoint.name, status=ServiceStatus.UNKNOWN
            )
        log.info("Registered service: %s (%s) at %s", endpoint.name, endpoint.service_type.value, endpoint.url)

    def deregister(self, name: str) -> None:
        """Remove a service from the registry."""
        with self._lock:
            self._endpoints.pop(name, None)
            self._health.pop(name, None)
        log.info("Deregistered service: %s", name)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> ServiceEndpoint | None:
        """Get endpoint by name. Returns None if not found or disabled."""
        with self._lock:
            ep = self._endpoints.get(name)
        if ep and ep.enabled:
            return ep
        return None

    def get_healthy(self, name: str) -> ServiceEndpoint | None:
        """Get endpoint only if it's confirmed healthy."""
        ep = self.get(name)
        if ep is None:
            return None
        with self._lock:
            health = self._health.get(name)
        if health and health.status == ServiceStatus.HEALTHY:
            # If lifecycle is available, verify container wasn't recently
            # stopped or externally replaced with stale config.
            if self._lifecycle and self._lifecycle.available:
                if name in self._lifecycle.recently_stopped:
                    return None
                if not self._lifecycle_service_is_current(name):
                    return None
            return ep
        return None

    def _lifecycle_service_is_current(self, name: str) -> bool:
        """Ask lifecycle whether a healthy endpoint belongs to current config."""
        if not self._lifecycle or not self._lifecycle.available:
            return True
        checker = getattr(self._lifecycle, "is_current", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(name))
        except Exception as exc:
            log.debug("Lifecycle current-config check failed for %s: %s", name, exc)
            return True

    def ensure_available(self, name: str, timeout: float = 30.0) -> ServiceEndpoint | None:
        """Get endpoint, starting the container if needed.

        If the service is unhealthy and a lifecycle manager is configured,
        attempts to start the container and waits for it to become healthy.

        Args:
            name: Service name
            timeout: Max seconds to wait for container to become healthy

        Returns:
            ServiceEndpoint if available, None otherwise
        """
        # Touch lifecycle tracking
        if self._lifecycle and self._lifecycle.available:
            self._lifecycle.touch(name)

        # Fast path: already healthy
        ep = self.get_healthy(name)
        if ep is not None:
            return ep

        ep = self.get(name)
        if ep is None:
            log.warning("Service %s not registered — cannot start", name)
            return None

        # Health checks are cached and can briefly lag behind container state
        # after manual compose operations or lifecycle reconciliation. Probe the
        # registered endpoint before attempting a restart so dispatch is not
        # blocked by a stale health cache.
        health = self._client.check_health(ep)
        with self._lock:
            self._health[name] = health
        if health.status == ServiceStatus.HEALTHY:
            if self._lifecycle_service_is_current(name):
                log.info("Service %s available after direct health probe", name)
                return ep
            log.info("Service %s is healthy but stale — requesting lifecycle reconciliation", name)

        # No lifecycle manager or not available — return endpoint even if
        # unhealthy so callers can fail with the concrete transport error.
        if not self._lifecycle or not self._lifecycle.available:
            return ep

        # Try to start the container.
        log.info("Service %s unhealthy — requesting container start", name)
        started = self._lifecycle.ensure_running(name)
        if not started:
            # A lifecycle start may fail because a matching container is already
            # being recreated or was started out-of-band. Re-check the endpoint
            # before declaring the service unavailable.
            health = self._client.check_health(ep)
            with self._lock:
                self._health[name] = health
            if health.status == ServiceStatus.HEALTHY and self._lifecycle_service_is_current(name):
                log.info("Service %s became healthy despite lifecycle start failure", name)
                return ep
            log.warning("Failed to start container for %s", name)
            return None

        # Reset circuit breaker since we just started a fresh container
        self._client.reset_circuit(name)

        # Poll for healthy status
        deadline = time.time() + timeout
        while time.time() < deadline:
            health = self._client.check_health(ep)
            with self._lock:
                self._health[name] = health
            if health.status == ServiceStatus.HEALTHY and self._lifecycle_service_is_current(name):
                log.info("Service %s now healthy after container start", name)
                return ep
            time.sleep(0.5)

        log.warning("Service %s not healthy after container start (%.0fs)", name, timeout)
        return ep  # Return anyway — let dispatch attempt and fail gracefully

    def list_agents(self) -> list[ServiceEndpoint]:
        """List all registered agent services."""
        with self._lock:
            return [ep for ep in self._endpoints.values() if ep.service_type == ServiceType.AGENT and ep.enabled]

    def list_features(self) -> list[ServiceEndpoint]:
        """List all registered feature services."""
        with self._lock:
            return [ep for ep in self._endpoints.values() if ep.service_type == ServiceType.FEATURE and ep.enabled]

    def list_healthy_agents(self) -> list[ServiceEndpoint]:
        """List agents that passed their last health check."""
        with self._lock:
            return [
                ep for ep in self._endpoints.values()
                if ep.service_type == ServiceType.AGENT
                and ep.enabled
                and self._health.get(ep.name, ServiceHealth(name=ep.name)).status
                in (ServiceStatus.HEALTHY, ServiceStatus.UNKNOWN)
            ]

    def list_healthy_features(self) -> list[ServiceEndpoint]:
        """List features that passed their last health check."""
        with self._lock:
            return [
                ep for ep in self._endpoints.values()
                if ep.service_type == ServiceType.FEATURE
                and ep.enabled
                and self._health.get(ep.name, ServiceHealth(name=ep.name)).status
                in (ServiceStatus.HEALTHY, ServiceStatus.UNKNOWN)
            ]

    def find_by_capability(self, capability: str) -> list[ServiceEndpoint]:
        """Find services that declare a specific capability."""
        with self._lock:
            return [
                ep for ep in self._endpoints.values()
                if ep.enabled and capability in ep.capabilities
            ]

    def get_health(self, name: str) -> ServiceHealth | None:
        """Get the last known health status for a service."""
        with self._lock:
            return self._health.get(name)

    def get_all_health(self) -> dict[str, ServiceHealth]:
        """Get health status for all services."""
        with self._lock:
            return dict(self._health)

    # ------------------------------------------------------------------
    # Health checking
    # ------------------------------------------------------------------

    def check_all_health(self) -> dict[str, ServiceHealth]:
        """Synchronously check health of all registered services."""
        results: dict[str, ServiceHealth] = {}
        with self._lock:
            endpoints = list(self._endpoints.values())

        for ep in endpoints:
            if not ep.enabled:
                continue
            health = self._client.check_health(ep)
            with self._lock:
                self._health[ep.name] = health
            results[ep.name] = health

        return results

    def start_health_checks(self, interval: float = 30.0) -> None:
        """Start background health checking thread."""
        if self._health_thread and self._health_thread.is_alive():
            return

        self._stop_event.clear()

        def _loop():
            while not self._stop_event.is_set():
                try:
                    self.check_all_health()
                except Exception as exc:
                    log.debug("Health check round failed: %s", exc)
                self._stop_event.wait(interval)

        self._health_thread = threading.Thread(target=_loop, daemon=True, name="service-health")
        self._health_thread.start()
        log.info("Started background health checks (interval=%.0fs)", interval)

    def stop(self) -> None:
        """Stop background health checking and close HTTP client."""
        self._stop_event.set()
        if self._health_thread:
            self._health_thread.join(timeout=5)
        self._client.close()

    # ------------------------------------------------------------------
    # Export for LLM routing
    # ------------------------------------------------------------------

    def export_for_llm(self) -> str:
        """Export registry as text for LLM-based routing decisions.

        Format suitable for including in a routing prompt.
        """
        lines: list[str] = []
        with self._lock:
            for ep in self._endpoints.values():
                if not ep.enabled:
                    continue
                health = self._health.get(ep.name)
                status = health.status.value if health else "unknown"
                caps = ", ".join(ep.capabilities) if ep.capabilities else "general"
                lines.append(
                    f"- **{ep.name}** [{ep.service_type.value}] "
                    f"({status}) capabilities: {caps}"
                    f"{' — ' + ep.description if ep.description else ''}"
                )
        return "\n".join(lines) if lines else "(no services registered)"

    def export_agent_names(self) -> list[str]:
        """Export list of healthy agent names for routing."""
        return [ep.name for ep in self.list_healthy_agents()]

    def export_feature_names(self) -> list[str]:
        """Export list of healthy feature names."""
        return [ep.name for ep in self.list_healthy_features()]
