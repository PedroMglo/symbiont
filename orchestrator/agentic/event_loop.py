"""Safe autonomous event loop for the agentic runtime.

The loop only records operational signals, sets temporary runtime flags and
queues proposal tasks. It never performs destructive maintenance directly.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from orchestrator.agentic.models import TaskStatus
from orchestrator.agentic.store import AgenticStore, get_agentic_store

log = logging.getLogger(__name__)


class AgenticEventLoop:
    """Observes safe operational signals and creates supervised proposals."""

    def __init__(
        self,
        *,
        store: AgenticStore | None = None,
        signal_probe: Callable[[], Any] | None = None,
        health_factory: Callable[[], dict[str, Any]] | None = None,
        poll_interval_seconds: float | None = None,
        min_repeat_interval_seconds: int | None = None,
        rag_miss_threshold: int | None = None,
        rag_miss_window_seconds: int | None = None,
        runtime_flag_ttl_seconds: int | None = None,
        vllm_unhealthy_enabled: bool | None = None,
        autonomous_maintenance_enabled: bool | None = None,
        governed_improvement_enabled: bool | None = None,
        maintenance_report_interval_seconds: int | None = None,
        improvement_review_interval_seconds: int | None = None,
        agent_failure_threshold: int | None = None,
        agent_failure_window_seconds: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        from orchestrator.config import get_settings

        cfg = get_settings().agentic_runtime
        self.store = store or get_agentic_store()
        self.signal_probe = signal_probe
        self.health_factory = health_factory
        self.poll_interval_seconds = float(
            poll_interval_seconds if poll_interval_seconds is not None else cfg.event_loop_poll_interval_seconds
        )
        self.min_repeat_interval_seconds = int(
            min_repeat_interval_seconds
            if min_repeat_interval_seconds is not None
            else cfg.event_loop_min_repeat_interval_seconds
        )
        self.rag_miss_threshold = int(rag_miss_threshold if rag_miss_threshold is not None else cfg.rag_miss_threshold)
        self.rag_miss_window_seconds = int(
            rag_miss_window_seconds if rag_miss_window_seconds is not None else cfg.rag_miss_window_seconds
        )
        self.runtime_flag_ttl_seconds = int(
            runtime_flag_ttl_seconds if runtime_flag_ttl_seconds is not None else cfg.runtime_flag_ttl_seconds
        )
        self.vllm_unhealthy_enabled = bool(
            vllm_unhealthy_enabled
            if vllm_unhealthy_enabled is not None
            else cfg.event_loop_vllm_unhealthy_enabled
        )
        self.autonomous_maintenance_enabled = bool(
            autonomous_maintenance_enabled
            if autonomous_maintenance_enabled is not None
            else cfg.autonomous_maintenance_enabled
        )
        self.governed_improvement_enabled = bool(
            governed_improvement_enabled
            if governed_improvement_enabled is not None
            else cfg.governed_improvement_enabled
        )
        self.maintenance_report_interval_seconds = int(
            maintenance_report_interval_seconds
            if maintenance_report_interval_seconds is not None
            else cfg.maintenance_report_interval_seconds
        )
        self.improvement_review_interval_seconds = int(
            improvement_review_interval_seconds
            if improvement_review_interval_seconds is not None
            else cfg.improvement_review_interval_seconds
        )
        self.agent_failure_threshold = int(
            agent_failure_threshold if agent_failure_threshold is not None else cfg.agent_failure_threshold
        )
        self.agent_failure_window_seconds = int(
            agent_failure_window_seconds
            if agent_failure_window_seconds is not None
            else cfg.agent_failure_window_seconds
        )
        self.worker_id = worker_id or f"agentic-event-loop-{uuid.uuid4().hex[:8]}"
        self._loop_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_tick_at: float | None = None

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="agentic-event-loop")
        self.store.record_event(event_type="event_loop.started", actor="agentic.event_loop", payload=self.status())

    async def stop(self) -> None:
        self._stopping.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
        self.store.record_event(
            event_type="event_loop.stopped",
            actor="agentic.event_loop",
            payload={"worker_id": self.worker_id},
        )

    def status(self) -> dict[str, Any]:
        return {
            "running": self._loop_task is not None and not self._loop_task.done(),
            "worker_id": self.worker_id,
            "poll_interval_seconds": self.poll_interval_seconds,
            "last_tick_at": self._last_tick_at,
            "safe_actions_only": True,
            "vllm_unhealthy_enabled": self.vllm_unhealthy_enabled,
            "autonomous_maintenance_enabled": self.autonomous_maintenance_enabled,
            "governed_improvement_enabled": self.governed_improvement_enabled,
            "maintenance_report_interval_seconds": self.maintenance_report_interval_seconds,
            "improvement_review_interval_seconds": self.improvement_review_interval_seconds,
            "agent_failure_threshold": self.agent_failure_threshold,
            "agent_failure_window_seconds": self.agent_failure_window_seconds,
        }

    async def run_once(self) -> int:
        """Run one safe observation pass. Returns number of actions recorded."""

        self.store.list_runtime_flags()
        signals = await self._collect_signals()
        actions: list[str] = []

        if self.autonomous_maintenance_enabled and self.store.get_runtime_flag("maintenance:health_report") is None:
            if self._create_safe_maintenance_once(
                event_key="health_report",
                playbook="health_report",
                goal="Generate a read-only agentic runtime health report.",
                priority="low",
                ttl_seconds=self.maintenance_report_interval_seconds,
                metadata={
                    "signals": signals,
                    "window_seconds": max(self.rag_miss_window_seconds, self.agent_failure_window_seconds),
                },
            ):
                actions.append("maintenance:health_report")

        if (
            self.autonomous_maintenance_enabled
            and self.governed_improvement_enabled
            and self.store.get_runtime_flag("maintenance:governed_improvement_review") is None
        ):
            if self._create_safe_maintenance_once(
                event_key="governed_improvement_review",
                playbook="governed_improvement_review",
                goal="Evaluate recent agentic runtime evidence and synthesize governed improvement proposals.",
                priority="low",
                ttl_seconds=self.improvement_review_interval_seconds,
                metadata={
                    "signals": signals,
                    "improvement_review_window_seconds": max(
                        self.rag_miss_window_seconds,
                        self.agent_failure_window_seconds,
                    ),
                },
            ):
                actions.append("maintenance:governed_improvement_review")

        if signals.get("storage_external_missing"):
            if self._runtime_flag_needs_refresh("block_heavy_tasks"):
                self.store.set_runtime_flag(
                    "block_heavy_tasks",
                    {
                        "reason": "storage_external_missing",
                        "source": "agentic.event_loop",
                        "safe_action": "defer_heavy_background_tasks",
                    },
                    ttl_seconds=self.runtime_flag_ttl_seconds,
                )
                self.store.record_event(
                    event_type="autonomous_safe.storage_external_missing",
                    actor="agentic.event_loop",
                    payload={"signals": signals, "action": "block_heavy_tasks"},
                )
                actions.append("block_heavy_tasks")
        elif signals.get("storage_external_available"):
            if self.store.clear_runtime_flag("block_heavy_tasks", reason="storage_external_available"):
                actions.append("clear_block_heavy_tasks")

        if self.vllm_unhealthy_enabled and signals.get("vllm_unhealthy"):
            if self._create_proposal_once(
                event_key="vllm_unhealthy",
                goal="Diagnose unhealthy vLLM and propose a safe fallback plan without restarting services.",
                priority="normal",
                metadata={"signals": signals},
            ):
                actions.append("proposal:vllm_unhealthy")

        if signals.get("service_or_model_degraded"):
            if self._create_proposal_once(
                event_key="service_or_model_degraded",
                goal="Diagnose degraded service or model signals and propose a safe supervised remediation plan.",
                priority="normal",
                metadata={"signals": signals},
            ):
                actions.append("proposal:service_or_model_degraded")

        if signals.get("rag_stale"):
            if self._create_proposal_once(
                event_key="rag_stale",
                goal="Evaluate stale RAG state and propose a safe reindex plan without running broad reprocess.",
                priority="low",
                metadata={"signals": signals},
            ):
                actions.append("proposal:rag_stale")

        repeated_rag_miss = bool(signals.get("repeated_rag_miss")) or self._has_repeated_rag_miss()
        if repeated_rag_miss:
            if self.autonomous_maintenance_enabled:
                created = self._create_safe_maintenance_once(
                    event_key="repeated_rag_miss",
                    playbook="rag_miss_diagnostic",
                    goal="Run a read-only diagnostic for repeated RAG misses without reindexing or reprocess.",
                    priority="normal",
                    ttl_seconds=self.rag_miss_window_seconds,
                    metadata={
                        "signals": signals,
                        "rag_miss_threshold": self.rag_miss_threshold,
                        "rag_miss_window_seconds": self.rag_miss_window_seconds,
                    },
                )
                if created:
                    actions.append("maintenance:repeated_rag_miss")
            elif self._create_proposal_once(
                event_key="repeated_rag_miss",
                goal="Analyze repeated RAG misses and propose safe indexing or retrieval fixes without automatic reprocess.",
                priority="normal",
                metadata={
                    "signals": signals,
                    "rag_miss_threshold": self.rag_miss_threshold,
                    "rag_miss_window_seconds": self.rag_miss_window_seconds,
                },
            ):
                actions.append("proposal:repeated_rag_miss")

        agent_failure_signal = signals.get("repeated_agent_failure")
        agent_failure = agent_failure_signal if isinstance(agent_failure_signal, dict) else self._agent_failure_summary()
        if agent_failure:
            agent_names = ", ".join(sorted(agent_failure.get("agents", {}))) or "unknown agents"
            if self.autonomous_maintenance_enabled:
                created = self._create_safe_maintenance_once(
                    event_key="repeated_agent_failure",
                    playbook="agent_failure_diagnostic",
                    goal=(
                        "Run a read-only diagnostic for repeated agent/service invocation failures for "
                        f"{agent_names} without restarting services."
                    ),
                    priority="normal",
                    ttl_seconds=self.agent_failure_window_seconds,
                    metadata={
                        "signals": signals,
                        "agent_failure": agent_failure,
                        "agent_failure_threshold": self.agent_failure_threshold,
                        "agent_failure_window_seconds": self.agent_failure_window_seconds,
                    },
                )
                if created:
                    actions.append("maintenance:repeated_agent_failure")
            elif self._create_proposal_once(
                event_key="repeated_agent_failure",
                goal=(
                    "Analyze repeated agent/service invocation failures for "
                    f"{agent_names} and propose a safe remediation plan without restarting services."
                ),
                priority="normal",
                metadata={
                    "signals": signals,
                    "agent_failure": agent_failure,
                    "agent_failure_threshold": self.agent_failure_threshold,
                    "agent_failure_window_seconds": self.agent_failure_window_seconds,
                },
            ):
                actions.append("proposal:repeated_agent_failure")

        self._last_tick_at = time.time()
        if actions:
            self.store.record_event(
                event_type="event_loop.tick",
                actor="agentic.event_loop",
                payload={"actions": actions, "signals": signals},
            )
        return len(actions)

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                log.warning("Agentic event loop tick failed: %s", exc)
                self.store.record_event(
                    event_type="event_loop.error",
                    actor="agentic.event_loop",
                    payload={"error": str(exc)[:500]},
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def _collect_signals(self) -> dict[str, Any]:
        signals: dict[str, Any] = self._event_signals_from_ledger()
        if self.signal_probe is not None:
            raw = self.signal_probe()
            if inspect.isawaitable(raw):
                raw = await raw
            signals.update(dict(raw or {}))
            return signals

        signals.update(self._storage_signals_from_env())
        signals.update(self._llm_signals_from_health())
        return signals

    def _event_signals_from_ledger(self) -> dict[str, Any]:
        since = time.time() - max(self.rag_miss_window_seconds, self.agent_failure_window_seconds, 1)
        try:
            events = self.store.list_ai_local_events(since=since, limit=1000)
        except Exception:
            return {}
        signals: dict[str, Any] = {
            "ai_local_events_count": len(events),
            "ai_local_event_window_seconds": max(self.rag_miss_window_seconds, self.agent_failure_window_seconds),
        }
        if events:
            signals["recent_ai_local_events"] = [
                {
                    "event_id": event.get("id"),
                    "event_type": event.get("event_type"),
                    "producer": event.get("producer"),
                    "severity": event.get("severity"),
                    "created_at": event.get("created_at"),
                    "evidence_ref": (
                        (event.get("event") or {}).get("evidence_ref")
                        if isinstance(event.get("event"), dict)
                        else None
                    ),
                }
                for event in events[:20]
            ]

        rag_events = [event for event in events if str(event.get("event_type")) in {"rag.miss", "rag.query.miss"}]
        if len(rag_events) >= self.rag_miss_threshold:
            signals["repeated_rag_miss"] = {
                "count": len(rag_events),
                "threshold": self.rag_miss_threshold,
                "window_seconds": self.rag_miss_window_seconds,
                "events": [event.get("id") for event in rag_events[:20]],
            }

        failure_events = [
            event
            for event in events
            if str(event.get("event_type")) in {"agent.invoke.failed", "agent.failure", "agent_invoke.failed"}
        ]
        agents: dict[str, int] = {}
        examples: dict[str, dict[str, Any]] = {}
        for event in failure_events:
            payload = (event.get("event") or {}).get("payload") if isinstance(event.get("event"), dict) else {}
            agent_name = str(payload.get("agent_name") or payload.get("agent") or event.get("producer") or "unknown")
            agents[agent_name] = agents.get(agent_name, 0) + 1
            examples.setdefault(agent_name, payload)
        repeated_agents = {agent: count for agent, count in agents.items() if count >= self.agent_failure_threshold}
        if repeated_agents:
            signals["repeated_agent_failure"] = {
                "agents": repeated_agents,
                "examples": {agent: examples.get(agent, {}) for agent in repeated_agents},
                "total_failures": sum(repeated_agents.values()),
                "window_seconds": self.agent_failure_window_seconds,
                "source": "ai-local-event-v1",
            }

        degraded = [event for event in events if str(event.get("event_type")) in {"service.degraded", "model.degraded"}]
        if degraded:
            signals["service_or_model_degraded"] = True
            signals["degraded_events"] = [event.get("id") for event in degraded[:20]]
        pressure = [event for event in events if str(event.get("event_type")) == "resource.pressure"]
        if pressure:
            signals["resource_pressure"] = True
            signals["resource_pressure_events"] = [event.get("id") for event in pressure[:20]]
        return signals

    def _storage_signals_from_env(self) -> dict[str, Any]:
        root = os.environ.get("AI_STORAGE_EXTERNAL_ROOT")
        require_external = os.environ.get("AI_STORAGE_REQUIRE_EXTERNAL", "").lower() in {"1", "true", "yes"}
        if not root:
            return {}
        exists = Path(root).exists()
        container_root = os.environ.get("AI_STORAGE_CONTAINER_BIND_ROOT") or ""
        container_exists = Path(container_root).exists() if container_root else False
        runtime_mounts = self._storage_runtime_mounts()
        runtime_mounts_available = (
            os.environ.get("AI_LOCAL_STORAGE_MODE") == "external"
            and bool(runtime_mounts)
            and all(item["exists"] for item in runtime_mounts)
        )
        available = bool(exists or container_exists or runtime_mounts_available)
        signal = {
            "storage_external_root": root,
            "storage_container_root": container_root,
            "storage_runtime_mounts": runtime_mounts,
        }
        if require_external and not available:
            return {"storage_external_missing": True, **signal}
        if available:
            return {"storage_external_available": True, **signal}
        return {}

    @staticmethod
    def _storage_runtime_mounts() -> list[dict[str, Any]]:
        candidates = (
            ("symbiont_data", "/app/data"),
            ("audio_input", "/app/audio_input"),
        )
        observed: list[dict[str, Any]] = []
        for name, path in candidates:
            try:
                observed.append({"name": name, "path": path, "exists": Path(path).exists()})
            except Exception:
                observed.append({"name": name, "path": path, "exists": False})
        return observed

    def _llm_signals_from_health(self) -> dict[str, Any]:
        if self.health_factory is None:
            return {}
        try:
            report = self.health_factory() or {}
        except Exception as exc:
            return {"health_probe_error": str(exc)[:300]}
        backends = report.get("backends") or []
        vllm_backends = [
            backend
            for backend in backends
            if "vllm" in str(backend.get("name") or backend.get("backend") or backend.get("id") or "").lower()
        ]
        if not vllm_backends:
            return {}
        enabled_backends = [backend for backend in vllm_backends if not self._backend_disabled(backend)]
        if not enabled_backends:
            return {"vllm_configured_disabled": True, "vllm_backends": vllm_backends}
        healthy = any(self._backend_healthy(backend) for backend in enabled_backends)
        return {"vllm_unhealthy": not healthy, "vllm_backends": enabled_backends}

    @staticmethod
    def _backend_healthy(backend: dict[str, Any]) -> bool:
        if bool(backend.get("healthy")):
            return True
        status = str(backend.get("status") or "").lower()
        return status in {"healthy", "ok", "ready", "available"}

    @staticmethod
    def _backend_disabled(backend: dict[str, Any]) -> bool:
        status = str(backend.get("status") or "").lower()
        return status in {"disabled", "off", "not_configured", "not-configured"}

    def _has_repeated_rag_miss(self) -> bool:
        since = time.time() - max(1, self.rag_miss_window_seconds)
        ledger_count = self.store.count_events(event_type="rag.miss", since=since)
        normalized_count = self.store.count_ai_local_events(event_type="rag.miss", since=since)
        return ledger_count + normalized_count >= self.rag_miss_threshold

    def _agent_failure_summary(self) -> dict[str, Any]:
        since = time.time() - max(1, self.agent_failure_window_seconds)
        events = [
            event
            for event in self.store.list_events(event_type="agent.invoke.failed", limit=1000)
            if float(event.get("timestamp") or 0) >= since
        ]
        normalized_events = self.store.list_ai_local_events(event_type="agent.invoke.failed", since=since, limit=1000)
        agents: dict[str, int] = {}
        examples: dict[str, dict[str, Any]] = {}
        for event in events:
            payload = event.get("payload") or {}
            agent_name = str(payload.get("agent_name") or "unknown")
            agents[agent_name] = agents.get(agent_name, 0) + 1
            examples.setdefault(agent_name, payload)
        for event in normalized_events:
            payload = (event.get("event") or {}).get("payload") if isinstance(event.get("event"), dict) else {}
            agent_name = str(payload.get("agent_name") or payload.get("agent") or event.get("producer") or "unknown")
            agents[agent_name] = agents.get(agent_name, 0) + 1
            examples.setdefault(agent_name, payload)
        repeated = {
            agent: count
            for agent, count in agents.items()
            if count >= self.agent_failure_threshold
        }
        if not repeated:
            return {}
        return {
            "agents": repeated,
            "examples": {agent: examples.get(agent, {}) for agent in repeated},
            "total_failures": sum(repeated.values()),
            "window_seconds": self.agent_failure_window_seconds,
        }

    def _runtime_flag_needs_refresh(self, key: str) -> bool:
        flag = self.store.get_runtime_flag(key)
        if flag is None:
            return True
        expires_at = flag.get("expires_at")
        if expires_at is None:
            return False
        refresh_at = time.time() + max(1.0, self.poll_interval_seconds * 2)
        return float(expires_at) <= refresh_at

    def _create_proposal_once(
        self,
        *,
        event_key: str,
        goal: str,
        priority: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        flag_key = f"proposal:{event_key}"
        if self.store.get_runtime_flag(flag_key) is not None:
            return False
        event_metadata = self._metadata_with_evidence_refs(event_key, metadata)
        task = self.store.create_task(
            goal=goal,
            mode="supervised",
            source="agentic.event_loop",
            priority=priority,
            metadata={
                "lane": "maintenance",
                "background": True,
                "proposal": True,
                "proposal_only": True,
                "requires_human_review": True,
                "safe_event": True,
                "event_type": event_key,
                "autonomous_safe": True,
                **event_metadata,
            },
            status=TaskStatus.QUEUED.value,
        )
        self.store.set_runtime_flag(
            flag_key,
            {"task_id": task.id, "event_type": event_key, "evidence_refs": event_metadata["evidence_refs"]},
            ttl_seconds=self.min_repeat_interval_seconds,
        )
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="autonomous_safe.proposal_created",
            actor="agentic.event_loop",
            payload={
                "event_type": event_key,
                "goal_preview": goal[:300],
                "evidence_refs": event_metadata["evidence_refs"],
            },
        )
        return True

    def _create_safe_maintenance_once(
        self,
        *,
        event_key: str,
        playbook: str,
        goal: str,
        priority: str,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> bool:
        flag_key = f"maintenance:{event_key}"
        if self.store.get_runtime_flag(flag_key) is not None:
            return False
        event_metadata = self._metadata_with_evidence_refs(event_key, metadata)
        task = self.store.create_task(
            goal=goal,
            mode="supervised",
            source="agentic.event_loop",
            priority=priority,
            metadata={
                "lane": "maintenance",
                "background": True,
                "safe_maintenance": True,
                "maintenance_playbook": playbook,
                "read_only": True,
                "safe_event": True,
                "event_type": event_key,
                "autonomous_safe": True,
                "autonomous_maintenance": True,
                **event_metadata,
            },
            status=TaskStatus.QUEUED.value,
        )
        self.store.set_runtime_flag(
            flag_key,
            {
                "task_id": task.id,
                "event_type": event_key,
                "playbook": playbook,
                "evidence_refs": event_metadata["evidence_refs"],
            },
            ttl_seconds=ttl_seconds or self.min_repeat_interval_seconds,
        )
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="autonomous_safe.maintenance_created",
            actor="agentic.event_loop",
            payload={
                "event_type": event_key,
                "playbook": playbook,
                "goal_preview": goal[:300],
                "evidence_refs": event_metadata["evidence_refs"],
            },
        )
        return True

    def _metadata_with_evidence_refs(
        self,
        event_key: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = dict(metadata or {})
        merged["evidence_refs"] = self._evidence_refs_for(event_key, merged)
        return merged

    @staticmethod
    def _evidence_refs_for(event_key: str, metadata: dict[str, Any]) -> list[str]:
        refs: list[str] = []

        def append(ref: Any) -> None:
            text = str(ref or "").strip()
            if text and text not in refs:
                refs.append(text[:1000])

        append(f"runtime_signal:{event_key}")
        for ref in metadata.get("evidence_refs") or []:
            append(ref)

        signals = metadata.get("signals")
        if isinstance(signals, dict):
            for item in signals.get("recent_ai_local_events") or []:
                if not isinstance(item, dict):
                    continue
                append(item.get("evidence_ref"))
                event_id = item.get("event_id")
                if event_id:
                    append(f"ai_local_event:{event_id}")

            rag_miss = signals.get("repeated_rag_miss")
            if isinstance(rag_miss, dict):
                for event_id in rag_miss.get("events") or []:
                    append(f"ai_local_event:{event_id}")

            for key in ("degraded_events", "resource_pressure_events"):
                for event_id in signals.get(key) or []:
                    append(f"ai_local_event:{event_id}")

        return refs[:25]


_EVENT_LOOP: AgenticEventLoop | None = None


def set_agentic_event_loop(event_loop: AgenticEventLoop | None) -> None:
    global _EVENT_LOOP
    _EVENT_LOOP = event_loop


def get_agentic_event_loop() -> AgenticEventLoop | None:
    return _EVENT_LOOP


def get_event_loop_status() -> dict[str, Any]:
    if _EVENT_LOOP is None:
        return {"running": False, "safe_actions_only": True}
    return _EVENT_LOOP.status()
