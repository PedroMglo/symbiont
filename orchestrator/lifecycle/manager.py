"""Container Lifecycle Manager — starts/stops Docker containers on demand.

Uses Docker SDK to manage agent and feature containers. Containers are
started when needed (first request) and stopped after idle timeout.
Communicates with Docker through a socket proxy for security.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Maps registry service name → Docker Compose service name
_SERVICE_NAME_MAP: dict[str, str] = {
    "reasoning_and_response": "reasoning-and-response",
    "audio_transcribe": "audio-transcribe",
    "audio_streaming": "audio-streaming",
    "redis": "redis",
    "research": "research",
    "local_evidence_operator": "local-evidence-operator",
    "personal_context": "personal-context",
    "execution_policy_operator": "execution-policy-operator",
    "material_builder": "material-builder",
    "material_execution_kernel": "material-execution-kernel",
    "workspace_execution": "workspace-execution",
    "extrator": "extrator",
    "storage_guardian": "storage_guardian",
    "translation": "translation",
}

_SERVICE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "audio_transcribe": ("redis",),
    "audio_streaming": ("audio_transcribe", "redis"),
    "workspace_execution": ("storage_guardian",),
    "material_execution_kernel": ("material_builder", "workspace_execution"),
}

_COMPOSE_ENV_FILENAMES = (
    ".env",
    ".env.storage.generated",
    ".env.llm.generated",
    ".env.services.generated",
    ".env.docker.resources.generated",
)

_SHARED_STATE_LOCK = threading.Lock()
_SHARED_LIFECYCLE_STATES: dict[tuple[str, str, str], dict[str, Any]] = {}


def _shared_state_key(compose_project: str, compose_file: str, compose_project_dir: str) -> tuple[str, str, str]:
    compose_file_key = str(Path(compose_file).expanduser().resolve()) if compose_file else ""
    project_dir_key = str(Path(compose_project_dir).expanduser().resolve()) if compose_project_dir else ""
    return (compose_project, compose_file_key, project_dir_key)


def _shared_lifecycle_state(key: tuple[str, str, str]) -> dict[str, Any]:
    with _SHARED_STATE_LOCK:
        state = _SHARED_LIFECYCLE_STATES.get(key)
        if state is None:
            state = {
                "last_used": {},
                "started_at": {},
                "starting": set(),
                "active_requests": {},
                "service_locks": {},
                "global_lock": threading.Lock(),
                "recently_stopped": set(),
            }
            _SHARED_LIFECYCLE_STATES[key] = state
        return state


def _docker_started_at(container: Any) -> float | None:
    raw = str(container.attrs.get("State", {}).get("StartedAt") or "")
    if not raw or raw.startswith("0001-"):
        return None
    try:
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


class ContainerLifecycleManager:
    """Manages Docker container lifecycle for symbiont services.

    Starts containers on demand when the symbiont needs them, and stops
    them after a configurable idle timeout to save resources.
    """

    def __init__(
        self,
        *,
        docker_host: str = "https://docker-proxy:2375",
        compose_project: str = "ai-local",
        compose_file: str = "",
        compose_project_dir: str = "",
        compose_profiles: list[str] | None = None,
        idle_timeout: int = 300,
        start_timeout: int = 30,
        health_poll_interval: float = 0.5,
        idle_check_interval: int = 30,
        startup_reap_grace_seconds: int = 120,
        always_on: list[str] | None = None,
        pre_warm: list[str] | None = None,
        per_service_overrides: dict[str, dict[str, Any]] | None = None,
    ):
        self._docker_host = docker_host
        self._compose_project = compose_project
        self._compose_file = compose_file
        self._compose_project_dir = compose_project_dir
        self._compose_profiles = tuple(profile for profile in (compose_profiles or ()) if profile)
        self._idle_timeout = idle_timeout
        self._start_timeout = start_timeout
        self._health_poll_interval = health_poll_interval
        self._idle_check_interval = idle_check_interval
        self._startup_reap_grace_seconds = max(0, startup_reap_grace_seconds)
        self._always_on: set[str] = set(always_on or [])
        self._pre_warm: set[str] = set(pre_warm or [])
        self._per_service_overrides = per_service_overrides or {}

        # State tracking. The gateway can build more than one registry/graph in
        # one process; share lifecycle activity so one reaper cannot stop a
        # container another registry is actively using.
        self._manager_started_at = time.time()
        shared_state = _shared_lifecycle_state(
            _shared_state_key(self._compose_project, self._compose_file, self._compose_project_dir)
        )
        self._last_used: dict[str, float] = shared_state["last_used"]
        self._started_at: dict[str, float] = shared_state["started_at"]  # When container was last started
        self._starting: set[str] = shared_state["starting"]  # Services currently being started
        self._active_requests: dict[str, int] = shared_state["active_requests"]
        self._service_locks: dict[str, threading.Lock] = shared_state["service_locks"]
        self._global_lock: threading.Lock = shared_state["global_lock"]
        self._reaper_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.recently_stopped: set[str] = shared_state["recently_stopped"]  # Services stopped by reaper

        # Docker client (lazy)
        self._docker = None
        self._containers_cache: dict[str, Any] = {}
        self._cache_time: float = 0.0
        self._cache_ttl: float = 5.0

        self._connect()

    def _connect(self) -> None:
        """Connect to Docker daemon via configured host."""
        try:
            import docker
            self._docker = docker.DockerClient(base_url=self._docker_host, timeout=10)
            self._docker.ping()
            log.info("Lifecycle manager connected to Docker at %s", self._docker_host)
        except Exception as exc:
            log.warning(
                "Lifecycle manager: Docker connection failed (%s). "
                "Container management disabled — services must be started manually.",
                exc,
            )
            self._docker = None

    @property
    def available(self) -> bool:
        """Whether the lifecycle manager has a working Docker connection."""
        return self._docker is not None

    def _get_lock(self, service_name: str) -> threading.Lock:
        """Get or create a per-service lock."""
        with self._global_lock:
            if service_name not in self._service_locks:
                self._service_locks[service_name] = threading.Lock()
            return self._service_locks[service_name]

    def _get_idle_timeout(self, service_name: str) -> int:
        """Get the idle timeout for a service (supports per-service overrides)."""
        overrides = self._per_service_overrides.get(service_name, {})
        timeout = overrides.get("idle_timeout", self._idle_timeout)
        # Pre-warm services get 2x idle timeout
        if service_name in self._pre_warm:
            timeout = timeout * 2
        return timeout

    def _get_start_timeout(self, service_name: str) -> int:
        """Get the start timeout for a service."""
        overrides = self._per_service_overrides.get(service_name, {})
        return overrides.get("start_timeout", self._start_timeout)

    def set_idle_timeout_floor(self, service_name: str, idle_timeout: int) -> None:
        """Ensure a service override is at least ``idle_timeout`` seconds."""
        timeout = int(idle_timeout)
        if timeout <= 0:
            return
        overrides = self._per_service_overrides.setdefault(service_name, {})
        configured = overrides.get("idle_timeout")
        overrides["idle_timeout"] = timeout if configured is None else max(int(configured), timeout)

    def _compose_name(self, service_name: str) -> str:
        """Map registry name to Docker Compose service name."""
        return _SERVICE_NAME_MAP.get(service_name, service_name.replace("_", "-"))

    def _with_dependencies(self, service_name: str) -> tuple[str, ...]:
        """Return service plus transitive runtime dependencies."""
        seen: set[str] = set()
        ordered: list[str] = []
        pending = [service_name]

        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            ordered.append(current)
            pending.extend(_SERVICE_DEPENDENCIES.get(current, ()))

        return tuple(ordered)

    def _touch_with_dependencies(self, service_name: str, now: float | None = None) -> None:
        """Refresh idle accounting for a service and its runtime dependencies."""
        touched_at = time.time() if now is None else now
        for name in self._with_dependencies(service_name):
            self._last_used[name] = touched_at
            if name not in self._started_at:
                self._started_at[name] = touched_at

    def _find_container(self, service_name: str) -> Any | None:
        """Find a managed service container by labels, then canonical name."""
        if not self._docker:
            return None

        compose_svc = self._compose_name(service_name)
        try:
            containers = self._docker.containers.list(
                all=True,
                filters={
                    "label": [
                        f"com.docker.compose.project={self._compose_project}",
                        f"com.docker.compose.service={compose_svc}",
                    ]
                },
            )
            if containers:
                return containers[0]

            # During image rebuilds or manual service recreation, Docker may
            # already have the canonical container name but compose labels from
            # a different project. Reuse that container instead of attempting
            # to create a duplicate and failing with a name conflict.
            canonical_name = f"orc-{compose_svc.replace('_', '-')}"
            named = self._docker.containers.list(all=True, filters={"name": f"^{canonical_name}$"})
            return named[0] if named else None
        except Exception as exc:
            log.debug("Failed to find container for %s: %s", service_name, exc)
            return None

    def is_running(self, service_name: str) -> bool:
        """Check if a service container is currently running."""
        container = self._find_container(service_name)
        if container is None:
            return False
        try:
            container.reload()
            running = container.status == "running"
            if running:
                self._sync_observed_running_start(service_name, container)
            return running
        except Exception:
            return False

    def _sync_observed_running_start(self, service_name: str, container: Any) -> None:
        """Refresh lifecycle timestamps from Docker's actual container start time."""
        started_at = _docker_started_at(container)
        if started_at is None:
            return
        current_started = self._started_at.get(service_name)
        if current_started is not None and started_at <= current_started + 1:
            return
        self._started_at[service_name] = started_at
        if self._last_used.get(service_name, 0) < started_at:
            self._last_used[service_name] = started_at

    def touch(self, service_name: str) -> None:
        """Update the last-used timestamp for a service."""
        now = time.time()
        self._touch_with_dependencies(service_name, now)
        log.debug("touch(%s) → last_used=%s", service_name, now)

    def begin_use(self, service_name: str) -> None:
        """Mark a service and its dependencies as actively serving a request."""
        now = time.time()
        with self._global_lock:
            for name in self._with_dependencies(service_name):
                self._last_used[name] = now
                if name not in self._started_at:
                    self._started_at[name] = now
                self._active_requests[name] = self._active_requests.get(name, 0) + 1

    def end_use(self, service_name: str) -> None:
        """Release active request protection for a service and dependencies."""
        now = time.time()
        with self._global_lock:
            for name in self._with_dependencies(service_name):
                count = self._active_requests.get(name, 0)
                if count <= 1:
                    self._active_requests.pop(name, None)
                else:
                    self._active_requests[name] = count - 1
                self._last_used[name] = now

    def _has_active_request(self, service_name: str) -> bool:
        return self._active_requests.get(service_name, 0) > 0

    def kick_start(self, service_name: str) -> bool:
        """Start a container without waiting for health — fire-and-forget for prewarming.

        Returns True if the start command was issued, False on failure.
        Does NOT wait for the container to become healthy.
        """
        if not self._docker:
            return False

        lock = self._get_lock(service_name)
        with lock:
            self.recently_stopped.discard(service_name)
            self._starting.add(service_name)
            self._touch_with_dependencies(service_name)

            # Already running — nothing to do
            if self.is_running(service_name):
                self._starting.discard(service_name)
                return True

            if not self._policy_allows_start(service_name, reason="kick_start"):
                self._starting.discard(service_name)
                return False

            container = self._find_container(service_name)
            if container is not None:
                try:
                    log.info("Prewarm kick_start: %s", service_name)
                    container.start()
                    self._started_at[service_name] = time.time()
                    self._starting.discard(service_name)
                    return True
                except Exception as exc:
                    log.warning("kick_start failed for %s: %s", service_name, exc)
                    self._starting.discard(service_name)
                    return False

            # Container doesn't exist — fall through to compose up (async)
            self._starting.discard(service_name)
            return False

    def ensure_running(self, service_name: str, *, _dependency_stack: tuple[str, ...] = ()) -> bool:
        """Ensure a service container is running. Start it if needed.

        Thread-safe: uses per-service locks to prevent duplicate starts.

        Returns:
            True if the container is running (or was just started), False on failure.
        """
        if not self._docker:
            return False
        if service_name in _dependency_stack:
            log.warning(
                "Lifecycle dependency cycle detected while starting %s: %s",
                service_name,
                " -> ".join((*_dependency_stack, service_name)),
            )
            return False

        for dependency in _SERVICE_DEPENDENCIES.get(service_name, ()):
            if not self.ensure_running(dependency, _dependency_stack=(*_dependency_stack, service_name)):
                log.warning(
                    "Lifecycle start refused for %s because dependency %s is unavailable",
                    service_name,
                    dependency,
                )
                return False

        lock = self._get_lock(service_name)
        with lock:
            # Clear recently_stopped flag since we're starting it
            self.recently_stopped.discard(service_name)

            # Mark as starting — reaper MUST skip this service
            self._starting.add(service_name)

            # Update last_used inside the lock to prevent reaper race
            self._touch_with_dependencies(service_name)

            container = self._find_container(service_name)
            if container is not None and self._container_is_running(container):
                if self._container_needs_recreate(service_name, container):
                    if not self._policy_allows_start(service_name, reason="recreate_stale_config"):
                        self._starting.discard(service_name)
                        return False
                    result = self._compose_up(service_name, force_recreate=True)
                    now = time.time()
                    self._touch_with_dependencies(service_name, now)
                    self._starting.discard(service_name)
                    return result
                self._sync_observed_running_start(service_name, container)
                self._starting.discard(service_name)
                return True

            if not self._policy_allows_start(service_name, reason="ensure_running"):
                self._starting.discard(service_name)
                return False

            # Try to start an existing stopped container when it still matches
            # the current compose config. Stale stopped containers must be
            # recreated so generated env updates are not silently ignored.
            if container is not None:
                if self._container_needs_recreate(service_name, container):
                    result = self._compose_up(service_name, force_recreate=True)
                    now = time.time()
                    self._touch_with_dependencies(service_name, now)
                    self._starting.discard(service_name)
                    return result
                try:
                    log.info("Starting stopped container for %s", service_name)
                    container.start()
                    result = self._wait_healthy(service_name)
                    now = time.time()
                    self._touch_with_dependencies(service_name, now)
                    self._starting.discard(service_name)
                    return result
                except Exception as exc:
                    log.warning("Failed to start container for %s: %s", service_name, exc)
                    self._starting.discard(service_name)

            # Container doesn't exist — try docker compose (best effort)
            # NOTE: compose works when running on host; inside a container it may
            # fail due to path resolution issues. Pre-create containers with:
            #   docker compose --profile agents --profile features create
            result = self._compose_up(service_name)
            now = time.time()
            self._touch_with_dependencies(service_name, now)
            self._starting.discard(service_name)
            return result

    def _policy_allows_start(self, service_name: str, *, reason: str) -> bool:
        if service_name not in _SERVICE_NAME_MAP:
            log.warning("Lifecycle start refused for unregistered service: %s", service_name)
            return False
        try:
            from orchestrator.agentic.policy import audit_policy_check

            decision = audit_policy_check(
                "lifecycle.start",
                payload={
                    "service": service_name,
                    "compose_service": self._compose_name(service_name),
                    "reason": reason,
                },
                component="ContainerLifecycleManager",
            )
            if decision.should_block:
                log.warning("Lifecycle start blocked by policy for %s: %s", service_name, decision.reason)
                return False
        except Exception as exc:
            log.debug("Lifecycle policy audit skipped for %s: %s", service_name, exc)
        return True

    def _local_image_exists(self, image: str) -> bool:
        if not self._docker:
            return False
        try:
            self._docker.images.get(image)
            return True
        except Exception:
            return False

    def _compose_image_tag_env(self, service_name: str) -> dict[str, str]:
        """Prefer an already-built local image tag for on-demand starts."""
        compose_svc = self._compose_name(service_name)
        image_base = f"ai-local-{compose_svc.replace('_', '-')}"
        if self._local_image_exists(f"{image_base}:dev"):
            return {}
        if self._local_image_exists(f"{image_base}:latest"):
            return {"AI_LOCAL_IMAGE_TAG": "latest"}
        return {}

    def _container_is_running(self, container: Any) -> bool:
        try:
            container.reload()
        except Exception:
            pass
        state = container.attrs.get("State", {}) if isinstance(getattr(container, "attrs", None), dict) else {}
        if isinstance(state, dict) and state.get("Running") is True:
            return True
        return str(getattr(container, "status", "") or "").lower() == "running"

    def _container_compose_hash(self, container: Any) -> str:
        labels = container.attrs.get("Config", {}).get("Labels", {}) if isinstance(getattr(container, "attrs", None), dict) else {}
        return str((labels or {}).get("com.docker.compose.config-hash") or "").strip()

    def _compose_config_hash(self, service_name: str) -> str:
        compose_svc = self._compose_name(service_name)
        cmd = ["docker", "compose"]
        for env_file in self._compose_env_files():
            cmd += ["--env-file", env_file]
        if self._compose_file:
            cmd += ["-f", self._compose_file]
        if self._compose_project_dir:
            cmd += ["--project-directory", self._compose_project_dir]
        for profile in self._compose_profiles:
            cmd += ["--profile", profile]
        cmd += ["-p", self._compose_project, "config", "--hash", compose_svc]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                env=self._compose_env(service_name),
            )
        except Exception as exc:
            log.debug("docker compose config hash failed for %s: %s", service_name, exc)
            return ""
        if result.returncode != 0:
            log.debug("docker compose config hash failed for %s: %s", service_name, result.stderr[:300])
            return ""
        line = next((part.strip() for part in result.stdout.splitlines() if part.strip()), "")
        if " " in line:
            line = line.split()[-1]
        return line

    def _container_needs_recreate(self, service_name: str, container: Any) -> bool:
        current_hash = self._container_compose_hash(container)
        expected_hash = self._compose_config_hash(service_name)
        if not current_hash or not expected_hash:
            return False
        if current_hash == expected_hash:
            return False
        log.info(
            "Recreating %s because compose config hash changed (%s -> %s)",
            service_name,
            current_hash[:12],
            expected_hash[:12],
        )
        return True

    def is_current(self, service_name: str) -> bool:
        """Return whether the observed container matches the current compose config.

        Health alone is not enough: a stale or externally recreated container can
        answer its health endpoint while carrying old env, mounts, source roots,
        or runtime backend settings. The lifecycle layer owns that reconciliation
        decision, so dispatch can call this before trusting a healthy endpoint.
        """
        container = self._find_container(service_name)
        if container is None:
            return False
        return not self._container_needs_recreate(service_name, container)

    def _compose_up(self, service_name: str, *, force_recreate: bool = False) -> bool:
        """Start a service via docker compose up (creates container if needed)."""
        compose_svc = self._compose_name(service_name)
        cmd = ["docker", "compose"]
        for env_file in self._compose_env_files():
            cmd += ["--env-file", env_file]
        if self._compose_file:
            cmd += ["-f", self._compose_file]
        if self._compose_project_dir:
            cmd += ["--project-directory", self._compose_project_dir]
        for profile in self._compose_profiles:
            cmd += ["--profile", profile]
        cmd += ["-p", self._compose_project, "up", "--no-deps", "--no-build", "--pull", "never"]
        if force_recreate:
            cmd.append("--force-recreate")
        cmd += ["-d", compose_svc]

        log.info("Creating container via compose: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._get_start_timeout(service_name) + 10,
                env=self._compose_env(service_name),
            )
            if result.returncode != 0:
                log.error(
                    "docker compose up failed for %s: %s",
                    service_name,
                    result.stderr[:500],
                )
                return False
            return self._wait_healthy(service_name)
        except subprocess.TimeoutExpired:
            log.error("docker compose up timed out for %s", service_name)
            return False
        except Exception as exc:
            log.error("docker compose up error for %s: %s", service_name, exc)
            return False

    def _compose_env_files(self) -> list[str]:
        """Return compose env files visible from the symbiont container."""
        candidates: list[Path] = []
        if self._compose_file:
            compose_dir = Path(self._compose_file).parent
            candidates.extend(compose_dir / name for name in _COMPOSE_ENV_FILENAMES)
        if self._compose_project_dir:
            project_dir = Path(self._compose_project_dir)
            candidates.extend(project_dir / name for name in _COMPOSE_ENV_FILENAMES)

        seen: set[str] = set()
        found: list[str] = []
        for path in candidates:
            raw = str(path)
            if raw in seen:
                continue
            seen.add(raw)
            if path.is_file():
                found.append(raw)
        return found

    def _compose_env(self, service_name: str | None = None) -> dict[str, str]:
        """Build environment for subprocess docker compose calls."""
        import os
        env = dict(os.environ)
        if self._docker_host.startswith("https://"):
            env["DOCKER_HOST"] = "tcp://" + self._docker_host.removeprefix("https://")
            env["DOCKER_TLS_VERIFY"] = "1"
            env.setdefault("DOCKER_CERT_PATH", "/run/ai-local-tls/services/symbiont")
        else:
            env["DOCKER_HOST"] = self._docker_host
        if service_name:
            env.update(self._compose_image_tag_env(service_name))
        if self._compose_project_dir:
            env.setdefault("ORC_SECRETS_DIR", str(Path(self._compose_project_dir) / "infra" / "docker" / "secrets"))
        if self._compose_profiles:
            env.setdefault("AI_COMPOSE_PROFILES", ",".join(self._compose_profiles))
        return env

    def _wait_healthy(self, service_name: str, timeout: float | None = None) -> bool:
        """Poll container health until healthy or timeout.

        Uses Docker's built-in HEALTHCHECK status.
        """
        if timeout is None:
            timeout = float(self._get_start_timeout(service_name))

        deadline = time.time() + timeout
        while time.time() < deadline:
            container = self._find_container(service_name)
            if container is None:
                time.sleep(self._health_poll_interval)
                continue

            try:
                container.reload()
                if container.status != "running":
                    time.sleep(self._health_poll_interval)
                    continue

                # Check Docker health status
                health = container.attrs.get("State", {}).get("Health", {})
                health_status = health.get("Status", "none")

                if health_status == "healthy":
                    log.info("Container %s is healthy (%.1fs)", service_name, timeout - (deadline - time.time()))
                    return True
                elif health_status == "none":
                    # No healthcheck defined — trust "running" status
                    log.info("Container %s running (no healthcheck)", service_name)
                    return True

            except Exception as exc:
                log.debug("Health poll error for %s: %s", service_name, exc)

            time.sleep(self._health_poll_interval)

        log.warning("Container %s did not become healthy within %.0fs", service_name, timeout)
        return False

    def stop_service(self, service_name: str, *, _from_reaper: bool = False) -> bool:
        """Stop a service container gracefully.

        Does not remove the container — just stops it for faster restart.
        """
        if not self._docker:
            return False

        if service_name in self._always_on:
            log.debug("Refusing to stop always-on service: %s", service_name)
            return False

        lock = self._get_lock(service_name)
        with lock:
            # If called from reaper, re-verify the service is still idle
            # (protects against TOCTOU race with touch/ensure_running)
            if _from_reaper:
                # Check if service is currently being started
                if service_name in self._starting:
                    log.debug("Reaper stop_service: %s in _starting, aborting", service_name)
                    return False
                if self._has_active_request(service_name):
                    log.debug("Reaper stop_service: %s has an active request, aborting", service_name)
                    return False
                # Check hard grace period (container started recently)
                started_at = self._started_at.get(service_name)
                idle_timeout = self._get_idle_timeout(service_name)
                if started_at is not None and (time.time() - started_at) < idle_timeout:
                    log.debug("Reaper stop_service: %s within grace period, aborting", service_name)
                    return False
                last_used = self._last_used.get(service_name)
                if last_used is None:
                    return False  # Just started, no usage yet
                if (time.time() - last_used) <= idle_timeout:
                    log.debug("Reaper: %s no longer idle, skipping stop", service_name)
                    return False

            container = self._find_container(service_name)
            if container is None:
                return True  # Already gone

            try:
                container.reload()
                if container.status != "running":
                    return True  # Already stopped

                log.info("Stopping idle container: %s", service_name)
                container.stop(timeout=30)
                self.recently_stopped.add(service_name)
                # Clear lifecycle state so next start gets fresh timestamps
                self._started_at.pop(service_name, None)
                self._last_used.pop(service_name, None)
                return True
            except Exception as exc:
                log.warning("Failed to stop %s: %s", service_name, exc)
                return False

    def status(self) -> list[dict[str, Any]]:
        """Get lifecycle status for all managed services."""
        now = time.time()
        result = []

        for registry_name in _SERVICE_NAME_MAP:
            running = self.is_running(registry_name)
            last_used = self._last_used.get(registry_name)
            started_at = self._started_at.get(registry_name)
            active_requests = self._active_requests.get(registry_name, 0)
            idle_for = (now - last_used) if last_used else None
            idle_timeout = self._get_idle_timeout(registry_name)

            result.append({
                "name": registry_name,
                "compose_service": self._compose_name(registry_name),
                "running": running,
                "last_used": last_used,
                "started_at": started_at,
                "idle_seconds": round(idle_for, 1) if idle_for else None,
                "idle_timeout": idle_timeout,
                "managed": registry_name not in self._always_on,
                "pre_warm": registry_name in self._pre_warm,
                "always_on": registry_name in self._always_on,
                "active_requests": active_requests,
            })

        return result

    # ------------------------------------------------------------------
    # Idle Reaper
    # ------------------------------------------------------------------

    def start_reaper(self) -> None:
        """Start the idle reaper background thread."""
        if self._reaper_thread and self._reaper_thread.is_alive():
            return

        self._stop_event.clear()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, daemon=True, name="lifecycle-reaper"
        )
        self._reaper_thread.start()
        log.info(
            "Lifecycle reaper started (check_interval=%ds, default_idle=%ds)",
            self._idle_check_interval,
            self._idle_timeout,
        )

    def _reaper_loop(self) -> None:
        """Background loop that stops idle containers."""
        while not self._stop_event.is_set():
            try:
                self._reap_idle()
            except Exception as exc:
                log.debug("Reaper cycle error: %s", exc)
            self._stop_event.wait(self._idle_check_interval)

    def _reap_idle(self) -> None:
        """Stop containers that have been idle longer than their timeout."""
        since_manager_start = time.time() - self._manager_started_at
        if since_manager_start < self._startup_reap_grace_seconds:
            log.debug(
                "Reaper skipping startup grace window (%.0fs < %ds)",
                since_manager_start,
                self._startup_reap_grace_seconds,
            )
            return

        for service_name in list(_SERVICE_NAME_MAP.keys()):
            if service_name in self._always_on:
                continue

            # NEVER reap a container that is currently being started
            if service_name in self._starting:
                log.debug("Reaper skipping %s (in _starting set)", service_name)
                continue
            if self._has_active_request(service_name):
                log.debug("Reaper skipping %s (active request)", service_name)
                continue

            if not self.is_running(service_name):
                continue

            if self._is_required_by_running_service(service_name):
                log.debug("Reaper skipping %s (dependency still in use)", service_name)
                continue

            idle_timeout = self._get_idle_timeout(service_name)

            # Hard grace period: never reap a container that was started recently
            started_at = self._started_at.get(service_name)
            if started_at is not None:
                since_start = time.time() - started_at
                if since_start < idle_timeout:
                    log.debug(
                        "Reaper: %s within start grace period (%.0fs < %ds), skipping",
                        service_name, since_start, idle_timeout,
                    )
                    continue

            last_used = self._last_used.get(service_name)
            if last_used is None:
                # Never used by a query — skip if pre-warmed or recently started
                if service_name in self._pre_warm:
                    continue
                # Grace period: give it one more cycle
                self._last_used[service_name] = time.time()
                continue

            # Use fresh time.time() for each service
            idle_seconds = time.time() - last_used
            if idle_seconds > idle_timeout:
                # Re-check _starting (may have been added while processing other services)
                if service_name in self._starting:
                    log.debug("Reaper: %s now in _starting, skipping", service_name)
                    continue
                # Re-read last_used in case touch() updated it
                current_last_used = self._last_used.get(service_name)
                if current_last_used is not None and current_last_used != last_used:
                    log.debug("Reaper: %s last_used changed, skipping", service_name)
                    continue
                # Final fresh idle check
                fresh_idle = time.time() - (current_last_used or last_used)
                if fresh_idle <= idle_timeout:
                    log.debug("Reaper: %s fresh idle %.0fs <= timeout, skipping", service_name, fresh_idle)
                    continue
                log.info(
                    "Reaping idle container %s (idle %.0fs > timeout %ds)",
                    service_name,
                    fresh_idle,
                    idle_timeout,
                )
                self.stop_service(service_name, _from_reaper=True)

    def _is_required_by_running_service(self, service_name: str) -> bool:
        """Return true when another running service depends on this container."""
        for candidate in _SERVICE_NAME_MAP:
            if candidate == service_name:
                continue
            if service_name not in _SERVICE_DEPENDENCIES.get(candidate, ()):
                continue
            if self.is_running(candidate):
                return True
        return False

    # ------------------------------------------------------------------
    # Pre-warming
    # ------------------------------------------------------------------

    def pre_warm_services(self) -> None:
        """Start all pre-warm services in the background."""
        if not self._docker:
            return

        for service_name in self._pre_warm:
            if not self.is_running(service_name):
                log.info("Pre-warming service: %s", service_name)
                # Start in a thread to not block boot
                threading.Thread(
                    target=self.ensure_running,
                    args=(service_name,),
                    daemon=True,
                    name=f"pre-warm-{service_name}",
                ).start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop the reaper thread. Does NOT stop containers."""
        self._stop_event.set()
        if self._reaper_thread:
            self._reaper_thread.join(timeout=5)
        if self._docker:
            try:
                self._docker.close()
            except Exception:
                pass
        log.info("Lifecycle manager stopped")
