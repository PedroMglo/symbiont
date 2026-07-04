"""Durable supervised runner for queued agentic tasks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError
from sharedai.llm.utils import strip_think

from orchestrator.agentic.context import AgenticContext, reset_agentic_context, set_agentic_context
from orchestrator.agentic.contracts import (
    ActionResult,
    AgentAnswer,
    AgentDecision,
    AgenticMemoryQuery,
    AgenticParallelPlan,
    AgenticParallelRound,
    AgentObservation,
    AgentQuestion,
    AiLocalEvent,
    CapabilityActionMetadata,
    CritiqueDecision,
)
from orchestrator.agentic.deliberation import summarize_agentic_deliberation
from orchestrator.agentic.deliberation_planner import (
    CapabilityCandidate,
    CapabilityExpandedPlan,
    InitialDeliberationPlan,
    build_autonomous_initial_deliberation_plan,
    expand_parallel_plan_from_capability_catalog,
    plan_initial_deliberation_questions,
    plan_next_deliberation_round,
    plan_revision_questions_from_critiques,
)
from orchestrator.agentic.models import ApprovalStatus, PolicyDecisionKind, TaskStatus
from orchestrator.agentic.policy import audit_policy_check
from orchestrator.agentic.runtime import (
    ShadowTaskHandle,
    final_state_summary,
    record_graph_steps,
    task_requires_material_output,
)
from orchestrator.agentic.store import AgenticStore, get_agentic_store
from orchestrator.ops.maintenance import is_safe_maintenance_task, run_safe_maintenance

log = logging.getLogger(__name__)

MATERIAL_MODEL_LANES = {
    "plan": "material_plan",
    "code": "material_code",
    "repair": "material_repair",
    "critic": "material_critic",
}

MATERIAL_TERMINAL_ISSUE_EVENTS = (
    "material.kernel.blocked",
    "material.failed_closed",
    "material.blocked_by_contract",
    "material.blocked_by_missing_tool",
    "material.blocked_by_sandbox_profile",
    "material.blocked_by_vm_isolation",
    "material.validation.failed",
)


class AgenticTaskCancelled(Exception):
    """Raised when a running task was cancelled through the durable task store."""


def _compact_json_payload(payload: Any, *, max_chars: int) -> Any:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(payload)
    if len(text) <= max_chars:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return {"truncated_json": text[: max(0, max_chars - 3)] + "..."}


@dataclass(frozen=True)
class LeaseOutcome:
    decision: str = "granted"
    lease_id: str | None = None
    ttl_seconds: int | None = None
    heartbeat_interval_seconds: int | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    retry_after_seconds: int | None = None

    @property
    def granted(self) -> bool:
        return self.decision in {"granted", "granted_with_limits", "run_cpu_only"}


@dataclass(frozen=True)
class ApiCallTarget:
    service_name: str
    path: str
    policy_action: str
    capability_metadata: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class ActionBoundaryCheck:
    allowed: bool
    action_id: str
    action_type: str
    state_hash: str = ""
    capability_id: str = ""
    policy_action: str = ""
    owner: str = ""
    reason: str = ""
    error_type: str = ""
    sandbox_required: bool = False
    sandbox_proof: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentQuestionOutcome:
    question_id: str
    answer_id: str
    status: str
    confidence: float = 0.0
    follow_up_questions: tuple[AgentQuestion, ...] = ()
    agreed_facts: tuple[str, ...] = ()
    contested_facts: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    error: str = ""


class AgenticRunner:
    """Polls the ledger and executes queued/recovering tasks via LangGraph."""

    def __init__(
        self,
        *,
        graph_factory: Callable[[], Any],
        store: AgenticStore | None = None,
        lease_provider: Callable[[Any, str], Any] | None = None,
        poll_interval_seconds: float | None = None,
        max_concurrent_tasks: int | None = None,
        task_timeout_seconds: int | None = None,
        execute_proposals: bool | None = None,
        worker_id: str | None = None,
    ) -> None:
        from orchestrator.config import get_settings

        cfg = get_settings().agentic_runtime
        self.store = store or get_agentic_store()
        self.graph_factory = graph_factory
        self.lease_provider = lease_provider or self._request_resource_lease
        self.poll_interval_seconds = float(poll_interval_seconds if poll_interval_seconds is not None else cfg.runner_poll_interval_seconds)
        self.max_concurrent_tasks = max(1, int(max_concurrent_tasks if max_concurrent_tasks is not None else cfg.max_concurrent_tasks))
        self.task_timeout_seconds = max(1, int(task_timeout_seconds if task_timeout_seconds is not None else cfg.task_default_timeout_seconds))
        self.execute_proposals = bool(execute_proposals if execute_proposals is not None else cfg.runner_execute_proposals)
        self.worker_id = worker_id or f"agentic-runner-{uuid.uuid4().hex[:8]}"
        self._loop_task: asyncio.Task | None = None
        self._active: dict[str, asyncio.Task] = {}
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="agentic-runner")
        self.store.record_event(
            event_type="runner.started",
            actor="agentic.runner",
            payload=self.status(),
        )

    async def stop(self) -> None:
        self._stopping.set()
        active_ids = list(self._active)
        recoverable_active_ids = [
            task_id
            for task_id in active_ids
            if not self._task_is_terminal(task_id)
        ]
        if recoverable_active_ids:
            self.store.mark_tasks_recovering(recoverable_active_ids, reason="runner_shutdown")
        for task in list(self._active.values()):
            task.cancel()
        if self._active:
            await asyncio.gather(*self._active.values(), return_exceptions=True)
        self._active.clear()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
        self.store.record_event(
            event_type="runner.stopped",
            actor="agentic.runner",
            payload={"worker_id": self.worker_id, "active_tasks_recovered": recoverable_active_ids},
        )

    def status(self) -> dict[str, Any]:
        return {
            "running": self._loop_task is not None and not self._loop_task.done(),
            "worker_id": self.worker_id,
            "active_task_ids": sorted(self._active),
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "poll_interval_seconds": self.poll_interval_seconds,
            "execute_proposals": self.execute_proposals,
        }

    async def run_once(self) -> int:
        """Run a single synchronous polling pass. Useful for tests."""

        await self._resolve_waiting_approvals()
        processed = 0
        while processed < self.max_concurrent_tasks:
            task = self.store.claim_next_task(worker_id=self.worker_id, include_proposals=self.execute_proposals)
            if task is None:
                break
            if self._defer_if_runtime_blocked(task):
                processed += 1
                continue
            await self._execute_task(task)
            processed += 1
        return processed

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            await self._resolve_waiting_approvals()
            self._cleanup_active()
            while len(self._active) < self.max_concurrent_tasks:
                task = self.store.claim_next_task(worker_id=self.worker_id, include_proposals=self.execute_proposals)
                if task is None:
                    break
                if self._defer_if_runtime_blocked(task):
                    continue
                self._active[task.id] = asyncio.create_task(self._execute_task(task), name=f"agentic-task-{task.id}")
            await asyncio.sleep(self.poll_interval_seconds)

    def _cleanup_active(self) -> None:
        for task_id, task in list(self._active.items()):
            if not task.done():
                continue
            with suppress(BaseException):
                task.result()
            self._active.pop(task_id, None)

    async def _resolve_waiting_approvals(self) -> None:
        self.store.expire_pending_approvals()
        waiting = self.store.list_tasks(status=TaskStatus.WAITING_APPROVAL.value, limit=500)
        for task in waiting:
            approvals = self.store.approvals_for_task(str(task["id"]))
            pending = [a for a in approvals if a["status"] == ApprovalStatus.PENDING.value]
            if pending:
                continue
            rejected = [a for a in approvals if a["status"] in {ApprovalStatus.REJECTED.value, ApprovalStatus.EXPIRED.value}]
            if rejected:
                self._fail_task_dict(
                    task,
                    error={
                        "type": "ApprovalTerminal",
                        "message": "Approval rejected or expired",
                        "approvals": [a["id"] for a in rejected],
                    },
                )
                continue
            approved = [a for a in approvals if a["status"] == ApprovalStatus.APPROVED.value]
            if approved:
                self.store.resume_task(str(task["id"]), reason="approval_approved")

    async def _execute_task(self, task: Any) -> None:
        request_id = uuid.uuid4().hex[:16]
        run_started_at = time.time()
        run_id = self.store.start_run(
            task_id=task.id,
            trace_id=task.trace_id,
            graph_run_id=None,
            entrypoint="agentic.runner",
            metadata={"request_id": request_id, "worker_id": self.worker_id},
        )
        handle = ShadowTaskHandle(
            task_id=task.id,
            trace_id=task.trace_id,
            request_id=request_id,
            session_id=task.session_id or task.id,
            mode=task.mode,
            run_id=run_id,
        )
        agent_state_snapshot = self.store.initialize_agent_state(task.id)

        if is_safe_maintenance_task(task):
            await self._execute_safe_maintenance_task(task, run_id=run_id, request_id=request_id, started_at=run_started_at)
            return

        lease = self._normalize_lease(self.lease_provider(task, request_id))
        self._record_lease(task.id, lease)
        if lease.decision == "defer":
            self.store.finish_run(run_id, status="deferred", metadata={"lease": lease.__dict__})
            self.store.defer_task(
                task.id,
                reason=lease.reason or "resource_governor_defer",
                retry_after_seconds=lease.retry_after_seconds,
                metadata={"lease_decision": lease.__dict__},
            )
            return
        if lease.decision == "deny":
            self.store.finish_run(run_id, status="failed", metadata={"lease": lease.__dict__})
            self._fail_task(
                task.id,
                trace_id=task.trace_id,
                error={"type": "LeaseDenied", "message": lease.reason or "Resource lease denied"},
            )
            return

        self.store.update_task(
            task.id,
            status=TaskStatus.RUNNING.value,
            metadata={
                "request_id": request_id,
                "runner_run_id": run_id,
                "lease_decision": lease.__dict__,
                "cpu_only": lease.decision == "run_cpu_only",
                "resource_limits": lease.limits,
            },
        )

        heartbeat_task = self._start_lease_heartbeat(lease)
        task_heartbeat_task = self._start_task_heartbeat(task.id, task.trace_id, run_id, stage="runner_active")
        token = set_agentic_context(handle.context())
        try:
            self._raise_if_task_cancelled(task.id)
            if self._material_fast_path_allowed(task):
                handled = await self._execute_material_fast_path(
                    task,
                    run_id=run_id,
                    run_started_at=run_started_at,
                    agent_state_snapshot=agent_state_snapshot,
                )
                if handled:
                    return

            graph = self.graph_factory()
            from orchestrator.pipeline.language_context import language_context_fallback
            from orchestrator.pipeline.tracer import GraphObservabilityTracer

            task_metadata = dict(getattr(task, "metadata", None) or {})
            raw_language_context = task_metadata.get("language_context")
            language_context = (
                raw_language_context
                if isinstance(raw_language_context, dict)
                else language_context_fallback(task.goal, reason="agentic_runner")
            )
            original_query = str(task_metadata.get("original_query") or task.goal)
            working_query = str(task_metadata.get("working_query") or task.goal)
            graph_tracer = GraphObservabilityTracer(
                request_id=request_id,
                session_id=task.session_id or task.id,
                query=working_query,
            )
            initial_state = {
                "query": working_query,
                "original_query": original_query,
                "history": [],
                "session_id": task.session_id or task.id,
                "language_context": language_context,
                "iterations": 0,
                "tokens_used": 0,
                "fallback_used": False,
                "agentic_limits": lease.limits,
                "agentic_cpu_only": lease.decision == "run_cpu_only",
            }
            if agent_state_snapshot is not None:
                initial_state["agent_state"] = agent_state_snapshot.get("state")
                initial_state["agent_state_hash"] = agent_state_snapshot.get("state_hash")
            agentic_memory = self._load_agentic_memory_context(task)
            if agentic_memory:
                initial_state["agentic_memory"] = agentic_memory
                initial_state["agentic_memory_refs"] = [
                    {
                        "memory_id": item.get("memory_id"),
                        "kind": item.get("kind"),
                        "source": item.get("source"),
                    }
                    for item in agentic_memory
                ]
            final_state = await asyncio.wait_for(
                graph.ainvoke(initial_state, {"callbacks": [graph_tracer]}),
                timeout=self.task_timeout_seconds,
            )
            graph_tracer.finalize(final_state)
            latency_ms = (time.time() - run_started_at) * 1000
            record_graph_steps(handle, graph_tracer)
            self._process_agentic_control_output(
                task,
                final_state,
                input_state_hash=str(initial_state.get("agent_state_hash") or ""),
            )
            await asyncio.to_thread(self._ensure_material_agent_decision, task, final_state)

            deny_calls = self.store.tool_calls_for_task(
                task.id,
                statuses=(PolicyDecisionKind.DENY.value,),
                since=run_started_at,
            )
            if deny_calls:
                self.store.finish_run(run_id, status="failed", metadata={"denied_tool_calls": [c["id"] for c in deny_calls]})
                self._fail_task(
                    task.id,
                    trace_id=task.trace_id,
                    error={"type": "PolicyDenied", "message": "Denied policy action blocked execution"},
                )
                return False

            pending = self.store.approvals_for_task(task.id, status=ApprovalStatus.PENDING.value)
            if pending:
                self.store.finish_run(run_id, status="waiting_approval", metadata={"approval_ids": [a["id"] for a in pending]})
                self.store.update_task(
                    task.id,
                    status=TaskStatus.WAITING_APPROVAL.value,
                    metadata={"pending_approval_ids": [a["id"] for a in pending], "waiting_since": time.time()},
                )
                return

            material_error = self._material_completion_error(task)
            if material_error is not None:
                if self._material_error_can_trigger_repair(task, material_error):
                    repair_invoked = await asyncio.to_thread(
                        self._ensure_material_agent_decision,
                        task,
                        final_state,
                        repair_context=material_error,
                    )
                    if repair_invoked:
                        material_error = self._material_completion_error(task)
                if material_error is not None:
                    self.store.finish_run(run_id, status="failed", metadata={"error": material_error})
                    self._fail_task(task.id, trace_id=task.trace_id, error=material_error)
                    return

            latest_agent_state = self.store.latest_agent_state_snapshot(task.id)
            agent_state = latest_agent_state.get("state") if isinstance(latest_agent_state, dict) else None
            if isinstance(agent_state, dict) and agent_state.get("status") == "blocked":
                if self._task_requires_material_output(task) and self._has_material_completion_evidence(task):
                    agent_state = None
                else:
                    self.store.finish_run(
                        run_id,
                        status="failed",
                        metadata={
                            "agent_state_hash": latest_agent_state.get("state_hash") if isinstance(latest_agent_state, dict) else None,
                            "agent_state_status": "blocked",
                        },
                    )
                    self._fail_task(
                        task.id,
                        trace_id=task.trace_id,
                        error={"type": "AgentStateBlocked", "message": "Structured agent state blocked task completion"},
                    )
                    return
            result = final_state_summary(final_state, latency_ms=latency_ms, graph_run_id=graph_tracer.graph_run_id)
            response = strip_think(str(final_state.get("response", "")))
            if response:
                result["response_preview"] = response[:1000]
            deliberation_result = summarize_agentic_deliberation(self.store, task.id)
            if deliberation_result.get("available"):
                result["agentic_deliberation"] = deliberation_result
                self.store.record_event(
                    task_id=task.id,
                    trace_id=task.trace_id,
                    event_type="agent.deliberation.integrated",
                    actor="agentic.runner",
                    payload=deliberation_result,
                )
            self.store.finish_run(run_id, status="completed", metadata={"graph_run_id": graph_tracer.graph_run_id, "latency_ms": latency_ms})
            self.store.update_task(task.id, status=TaskStatus.COMPLETED.value, result=result)
            self.store.record_event(
                task_id=task.id,
                event_type="pipeline.completed",
                actor="agentic.runner",
                payload=result,
                trace_id=task.trace_id,
            )
        except asyncio.TimeoutError:
            self.store.finish_run(run_id, status="failed", metadata={"timeout_seconds": self.task_timeout_seconds})
            self._fail_task(
                task.id,
                trace_id=task.trace_id,
                error={"type": "TimeoutError", "message": f"Task exceeded {self.task_timeout_seconds}s"},
            )
        except AgenticTaskCancelled:
            self.store.finish_run(run_id, status="cancelled", metadata={"reason": "task_cancelled"})
            self.store.update_task(task.id, status=TaskStatus.CANCELLED.value, metadata={"cancel_observed_by": self.worker_id})
        except asyncio.CancelledError:
            self.store.finish_run(run_id, status="recovering", metadata={"reason": "runner_cancelled"})
            self.store.update_task(task.id, status=TaskStatus.RECOVERING.value, metadata={"recovery_reason": "runner_cancelled"})
            raise
        except Exception as exc:
            self.store.finish_run(run_id, status="failed", metadata={"error": {"type": type(exc).__name__, "message": str(exc)[:1000]}})
            self._fail_task(
                task.id,
                trace_id=task.trace_id,
                error={"type": type(exc).__name__, "message": str(exc)[:1000]},
            )
        finally:
            reset_agentic_context(token)
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            if task_heartbeat_task is not None:
                task_heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await task_heartbeat_task
            self._release_lease(lease)

    def _raise_if_task_cancelled(self, task_id: str) -> None:
        current = self.store.get_task(task_id)
        if current is not None and current.status == TaskStatus.CANCELLED.value:
            raise AgenticTaskCancelled()

    def _task_is_terminal(self, task_id: str) -> bool:
        current = self.store.get_task(task_id)
        return current is not None and current.status in {
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        }

    async def _execute_safe_maintenance_task(self, task: Any, *, run_id: str, request_id: str, started_at: float) -> None:
        self.store.update_task(
            task.id,
            status=TaskStatus.RUNNING.value,
            metadata={
                "request_id": request_id,
                "runner_run_id": run_id,
                "safe_maintenance": True,
                "resource_limits": {"read_only": True},
            },
        )
        step_started = time.time()
        try:
            result = run_safe_maintenance(task, self.store)
            proposals = self._record_improvement_candidates(task, result)
            if proposals:
                result["improvement_proposals"] = proposals
            latency_ms = (time.time() - started_at) * 1000
            result["latency_ms"] = round(latency_ms, 2)
            self.store.record_step(
                task_id=task.id,
                run_id=run_id,
                step_name=f"maintenance.{result.get('playbook', 'unknown')}",
                step_type="safe_maintenance",
                status="completed",
                started_at=step_started,
                duration_ms=(time.time() - step_started) * 1000,
                output_preview=str(result)[:1000],
                metadata={"read_only": True, "safe_maintenance": True},
            )
            self.store.finish_run(run_id, status="completed", metadata={"safe_maintenance": True, "latency_ms": latency_ms})
            self.store.update_task(task.id, status=TaskStatus.COMPLETED.value, result=result)
            self.store.record_event(
                task_id=task.id,
                event_type="maintenance.completed",
                actor="agentic.runner",
                payload=result,
                trace_id=task.trace_id,
            )
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)[:1000], "safe_maintenance": True}
            self.store.record_step(
                task_id=task.id,
                run_id=run_id,
                step_name="maintenance.failed",
                step_type="safe_maintenance",
                status="failed",
                started_at=step_started,
                duration_ms=(time.time() - step_started) * 1000,
                error=error,
                metadata={"read_only": True, "safe_maintenance": True},
            )
            self.store.finish_run(run_id, status="failed", metadata={"error": error})
            self._fail_task(task.id, trace_id=task.trace_id, error=error)

    def _record_improvement_candidates(self, task: Any, result: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = result.get("improvement_candidates") or []
        if not isinstance(candidates, list):
            return []
        proposals: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            payload = candidate.get("payload")
            evidence = candidate.get("evidence")
            if not isinstance(payload, dict) or not isinstance(evidence, dict):
                continue
            proposal = self.store.create_improvement_proposal(
                task_id=task.id,
                kind=str(candidate.get("kind") or "runtime_guardrail"),
                title=str(candidate.get("title") or "Governed runtime improvement"),
                risk_level=str(candidate.get("risk_level") or "high"),
                confidence=float(candidate.get("confidence") or 0.0),
                score=float(candidate.get("score") or 0.0),
                payload=payload,
                evidence={
                    **evidence,
                    "origin_task_id": task.id,
                    "origin_playbook": result.get("playbook"),
                },
                metadata={
                    "origin": "safe_maintenance",
                    "read_only_evaluation": True,
                    "requires_approval_to_apply": True,
                },
                ttl_seconds=float(payload.get("ttl_seconds") or 3600),
            )
            proposals.append({
                "id": proposal.get("id"),
                "kind": proposal.get("kind"),
                "status": proposal.get("status"),
                "confidence": proposal.get("confidence"),
                "score": proposal.get("score"),
            })
        return proposals

    def _process_agentic_control_output(self, task: Any, final_state: dict[str, Any], *, input_state_hash: str) -> None:
        """Validate structured decisions and record raw output as audit evidence."""

        snapshot = self.store.current_agent_state(task.id)
        if snapshot is None:
            return
        current_hash = input_state_hash or str(snapshot.get("state_hash") or "")
        payload = self._decision_payload(final_state)
        raw_text = self._raw_output_text(final_state)
        raw_ref = None
        if raw_text:
            raw_ref = self.store.record_agent_raw_output(
                task_id=task.id,
                trace_id=task.trace_id,
                agent=str(final_state.get("agent_name") or "agentic.graph"),
                output=raw_text,
                metadata={"run_output": True},
            ).get("ref")

        self._process_structured_agentic_artifacts(task, final_state)

        if payload is not None:
            decision_data = dict(payload)
            if raw_ref is not None and not decision_data.get("raw_output_ref"):
                decision_data["raw_output_ref"] = raw_ref
            try:
                decision = AgentDecision.model_validate(decision_data)
            except ValidationError as exc:
                self.store.record_agent_decision_rejected(
                    task_id=task.id,
                    trace_id=task.trace_id,
                    input_state_hash=str(decision_data.get("input_state_hash") or current_hash),
                    raw_decision=decision_data,
                    error={"type": "ValidationError", "message": str(exc)[:2000]},
                )
                return

            outcome = self.store.record_agent_decision(decision)
            if outcome.get("valid"):
                self._execute_agent_actions(task, decision.proposed_actions)
            return

    def _process_structured_agentic_artifacts(self, task: Any, final_state: dict[str, Any]) -> None:
        plan_payload = final_state.get("agentic_parallel_plan")
        if isinstance(plan_payload, dict):
            try:
                plan = AgenticParallelPlan.model_validate(plan_payload)
                self._execute_parallel_plan(task, plan)
            except ValidationError as exc:
                self.store.record_event(
                    task_id=task.id,
                    trace_id=task.trace_id,
                    event_type="agent.parallel_plan.rejected",
                    actor="agentic.runner",
                    payload={"error": str(exc)[:2000], "plan_preview": str(plan_payload)[:1000]},
                )

        for key in ("agentic_parallel_rounds", "parallel_rounds"):
            for item in self._as_list(final_state.get(key)):
                with suppress(Exception):
                    self.store.record_parallel_round(item, actor="agentic.runner")

        questions: list[AgentQuestion] = []
        seen_question_ids: set[str] = set()
        explicit_deliberation_seen = False

        for key in ("agentic_initial_deliberation_plan", "agentic_deliberation_plan", "initial_deliberation_plan"):
            items = self._as_list(final_state.get(key))
            if items:
                explicit_deliberation_seen = True
            for item in items:
                try:
                    plan = AgenticParallelPlan.model_validate(item)
                except ValidationError as exc:
                    self.store.record_event(
                        task_id=task.id,
                        trace_id=task.trace_id,
                        event_type="agent.deliberation.initial_plan_rejected",
                        actor="agentic.runner",
                        payload={"error": str(exc)[:2000], "plan_preview": str(item)[:1000]},
                    )
                    continue
                for planned_question in self._record_initial_deliberation_plan(
                    task,
                    plan,
                    seen_question_ids=seen_question_ids,
                ):
                    seen_question_ids.add(planned_question.question_id)
                    if planned_question.metadata.get("auto_answer") is False:
                        continue
                    questions.append(planned_question)

        critique_decisions: list[CritiqueDecision] = []
        for key in ("agent_messages", "agent_answers", "validation_votes", "critique_decisions", "consensus_decisions"):
            items = self._as_list(final_state.get(key))
            if items:
                explicit_deliberation_seen = True
            for item in items:
                with suppress(Exception):
                    self.store.record_agent_message(item, actor="agentic.runner")
                if key == "critique_decisions":
                    with suppress(Exception):
                        critique_decisions.append(CritiqueDecision.model_validate(item))

        revision_plan = plan_revision_questions_from_critiques(
            task_id=task.id,
            trace_id=task.trace_id,
            critiques=critique_decisions,
            existing_question_ids=seen_question_ids,
        )
        for revision_question in self._record_revision_deliberation_plan(task, revision_plan):
            seen_question_ids.add(revision_question.question_id)
            if revision_question.metadata.get("auto_answer") is False:
                continue
            questions.append(revision_question)

        explicit_questions = self._as_list(final_state.get("agent_questions"))
        if explicit_questions:
            explicit_deliberation_seen = True
        for item in explicit_questions:
            try:
                question = AgentQuestion.model_validate(item)
            except Exception:
                with suppress(Exception):
                    self.store.record_agent_message(item, actor="agentic.runner")
                continue
            if question.question_id in seen_question_ids:
                continue
            seen_question_ids.add(question.question_id)
            self.store.record_agent_message(question, actor="agentic.runner")
            if question.metadata.get("auto_answer") is False:
                continue
            questions.append(question)

        if not explicit_deliberation_seen:
            auto_plan = build_autonomous_initial_deliberation_plan(
                task_id=task.id,
                trace_id=task.trace_id,
                goal=task.goal,
                task_source=task.source,
                task_mode=task.mode,
                task_metadata=task.metadata,
                final_state=final_state,
            )
            if auto_plan is not None:
                self.store.record_event(
                    task_id=task.id,
                    trace_id=task.trace_id,
                    event_type="agent.deliberation.autonomous_plan_created",
                    actor="agentic.runner",
                    payload={
                        "plan_id": auto_plan.plan_id,
                        "planner": auto_plan.metadata.get("planner"),
                        "planner_reason": auto_plan.metadata.get("planner_reason"),
                        "source_task_source": auto_plan.metadata.get("source_task_source"),
                        "source_event_type": auto_plan.metadata.get("source_event_type"),
                        "evidence_refs": auto_plan.metadata.get("evidence_refs") or [],
                        "capability_requirements": auto_plan.metadata.get("capability_requirements") or [],
                    },
                )
                for planned_question in self._record_initial_deliberation_plan(
                    task,
                    auto_plan,
                    seen_question_ids=seen_question_ids,
                ):
                    seen_question_ids.add(planned_question.question_id)
                    if planned_question.metadata.get("auto_answer") is False:
                        continue
                    questions.append(planned_question)
        self._run_agent_question_rounds(task, questions)

        for key in ("ai_local_events", "agentic_events"):
            for item in self._as_list(final_state.get(key)):
                with suppress(Exception):
                    self.store.record_ai_local_event(item, actor="agentic.runner")

        for key in ("agentic_memory", "agentic_memories"):
            for item in self._as_list(final_state.get(key)):
                with suppress(Exception):
                    self.store.record_agent_memory(item, actor="agentic.runner")

    def _record_initial_deliberation_plan(
        self,
        task: Any,
        plan: AgenticParallelPlan,
        *,
        seen_question_ids: set[str],
    ) -> list[AgentQuestion]:
        if plan.task_id != task.id or plan.trace_id != task.trace_id:
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.deliberation.initial_plan_rejected",
                actor="agentic.runner",
                payload={
                    "plan_id": plan.plan_id,
                    "reason": "task_or_trace_mismatch",
                    "plan_task_id": plan.task_id,
                    "plan_trace_id": plan.trace_id,
                },
            )
            return []
        expanded = self._expand_initial_plan_from_capability_catalog(plan)
        if expanded.expanded:
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.deliberation.capability_plan_created",
                actor="agentic.runner",
                payload=expanded.to_event_payload(),
            )
            plan = expanded.plan
        planned = plan_initial_deliberation_questions(plan, existing_question_ids=seen_question_ids)
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="agent.deliberation.initial_plan_created",
            actor="agentic.runner",
            payload=planned.to_event_payload(),
        )
        for question in planned.selected_questions:
            self.store.record_agent_message(question, actor="agentic.runner")
        return planned.selected_questions

    def _record_revision_deliberation_plan(
        self,
        task: Any,
        plan: InitialDeliberationPlan,
    ) -> list[AgentQuestion]:
        if not plan.questions and not plan.deferred:
            return []
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="agent.deliberation.revision_plan_created",
            actor="agentic.runner",
            payload=plan.to_event_payload(),
        )
        for question in plan.selected_questions:
            self.store.record_agent_message(question, actor="agentic.runner")
        return plan.selected_questions

    def _expand_initial_plan_from_capability_catalog(self, plan: AgenticParallelPlan) -> CapabilityExpandedPlan:
        return expand_parallel_plan_from_capability_catalog(
            plan,
            candidates=self._agentic_capability_candidates(),
        )

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    @staticmethod
    def _decision_payload(final_state: dict[str, Any]) -> dict[str, Any] | None:
        value = final_state.get("agent_decision")
        return value if isinstance(value, dict) else None

    @staticmethod
    def _raw_output_text(final_state: dict[str, Any]) -> str:
        for key in ("raw_output", "response", "output"):
            value = final_state.get(key)
            if isinstance(value, str) and value.strip():
                return value
        messages = final_state.get("stream_messages")
        if isinstance(messages, list):
            parts = [str(item.get("content", "")) for item in messages if isinstance(item, dict) and item.get("content")]
            return "\n".join(parts)
        return ""

    def _material_fast_path_allowed(self, task: Any) -> bool:
        if not self._task_requires_material_output(task):
            return False
        metadata = dict(getattr(task, "metadata", {}) or {})
        if metadata.get("disable_material_fast_path") is True:
            return False
        if metadata.get("requires_deliberation") is True or metadata.get("auto_deliberation") is True:
            return False
        if metadata.get("material_force_graph_prelude") is True:
            return False
        return True

    def _material_fast_path_final_state(
        self,
        task: Any,
        *,
        agent_state_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from orchestrator.pipeline.language_context import language_context_fallback

        task_metadata = dict(getattr(task, "metadata", {}) or {})
        raw_language_context = task_metadata.get("language_context")
        language_context = (
            raw_language_context
            if isinstance(raw_language_context, dict)
            else language_context_fallback(str(getattr(task, "goal", "") or ""), reason="agentic_material_fast_path")
        )
        working_query = str(
            task_metadata.get("working_query")
            or language_context.get("english_text")
            or language_context.get("normalized_text")
            or getattr(task, "goal", "")
            or ""
        )
        original_query = str(task_metadata.get("original_query") or getattr(task, "goal", "") or "")
        final_state: dict[str, Any] = {
            "query": working_query,
            "original_query": original_query,
            "language_context": language_context,
            "intent": "material_generation",
            "complexity": "material",
            "agent_name": "agentic.material_fast_path",
            "model_used": "material_execution_kernel",
            "tokens_used": 0,
            "response": "",
            "material_fast_path": True,
        }
        if isinstance(agent_state_snapshot, dict):
            final_state["agent_state"] = agent_state_snapshot.get("state")
            final_state["agent_state_hash"] = agent_state_snapshot.get("state_hash")
        return final_state

    def _prepare_material_fast_path_evidence(self, task: Any, final_state: dict[str, Any]) -> Any:
        """Prepare local evidence inside the queued material task runtime."""

        task_metadata = dict(getattr(task, "metadata", {}) or {})
        if task_metadata.get("material_evidence_prepared") is True:
            return task

        existing_context = task_metadata.get("material_evidence_context")
        if isinstance(existing_context, dict) and existing_context.get("enrichment_results"):
            self.store.update_task(
                task.id,
                metadata={
                    "material_evidence_prepared": True,
                    "material_evidence_owner_enriched": True,
                    "material_evidence_preparation_source": "existing_context",
                    "enrichment_result_count": len(existing_context.get("enrichment_results") or []),
                    "enrichment_summary": self._material_enrichment_summary(existing_context),
                },
            )
            return self.store.get_task(task.id) or task

        started = time.time()
        language_context = self._material_language_context(task, final_state)
        request_seed = task_metadata.get("material_evidence_request_seed")
        request_seed = request_seed if isinstance(request_seed, dict) else {}
        original_query = str(
            task_metadata.get("original_query")
            or request_seed.get("original_user_prompt")
            or final_state.get("original_query")
            or getattr(task, "goal", "")
            or ""
        )
        working_query = str(
            task_metadata.get("working_query")
            or request_seed.get("normalized_prompt")
            or final_state.get("query")
            or getattr(task, "goal", "")
            or ""
        )
        user_language = str(
            language_context.get("user_language")
            or language_context.get("original_language")
            or request_seed.get("user_language")
            or ""
        )
        expected_root = str(task_metadata.get("expected_artifact_root") or task_metadata.get("requested_project") or "")
        start_payload = {
            "deferred_from_gateway": bool(task_metadata.get("material_evidence_deferred")),
            "has_existing_context": isinstance(existing_context, dict),
            "expected_artifact_root": expected_root or None,
            "latency_source": "agentic.runner",
        }
        self._record_ai_event(
            task,
            event_type="material.evidence_acquisition.started",
            producer="agentic.runner",
            severity="info",
            payload=start_payload,
        )
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="material.evidence_acquisition.started",
            actor="agentic.runner",
            payload=start_payload,
        )

        context = existing_context if isinstance(existing_context, dict) else None
        request = task_metadata.get("material_evidence_request")
        request = request if isinstance(request, dict) else {}
        status = "completed"
        error_text = ""
        try:
            if context is None:
                from orchestrator.evidence.material_context import build_material_evidence_context

                context, request = build_material_evidence_context(
                    original_query=original_query,
                    working_query=working_query,
                    expected_artifact_root=expected_root,
                    user_language=user_language,
                )
            if context is not None:
                from orchestrator.evidence.owner_enrichment import enrich_material_evidence_context

                feature_client = self._feature_client()
                context = enrich_material_evidence_context(
                    context,
                    invoke_endpoint=feature_client.invoke_endpoint,
                    user_language=user_language,
                )
        except Exception as exc:
            status = "degraded"
            error_text = str(exc)[:300]
            log.warning("material evidence acquisition degraded for task %s: %s", task.id, exc)
            if context is not None:
                missing = list(context.get("missing_evidence") or [])
                missing.append(f"Owner evidence acquisition unavailable: {error_text}")
                context["missing_evidence"] = missing
            else:
                request = {
                    **request,
                    "missing_evidence": [f"Material evidence acquisition unavailable: {error_text}"],
                }

        duration_ms = (time.time() - started) * 1000
        update_metadata: dict[str, Any] = {
            "material_evidence_prepared": True,
            "material_evidence_owner_enriched": bool(
                isinstance(context, dict) and context.get("enrichment_results")
            ),
            "material_evidence_preparation_status": status,
            "material_evidence_preparation_latency_ms": round(duration_ms, 2),
            "material_evidence_request": request,
        }
        if context is not None:
            update_metadata["material_evidence_context"] = context
            update_metadata["enrichment_result_count"] = len(context.get("enrichment_results") or [])
            update_metadata["enrichment_summary"] = self._material_enrichment_summary(context)
        else:
            update_metadata["material_evidence_context_missing"] = True
        if error_text:
            update_metadata["material_evidence_preparation_error"] = error_text

        self.store.update_task(task.id, metadata=update_metadata)
        complete_payload = {
            "status": status,
            "workspace": context.get("workspace") if isinstance(context, dict) else None,
            "enrichment_result_count": update_metadata.get("enrichment_result_count", 0),
            "context_available": context is not None,
            "duration_ms": round(duration_ms, 2),
        }
        if error_text:
            complete_payload["error"] = error_text
        self._record_ai_event(
            task,
            event_type="material.evidence_acquisition.completed",
            producer="agentic.runner",
            severity="info" if status == "completed" else "medium",
            payload=complete_payload,
        )
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="material.evidence_acquisition.completed",
            actor="agentic.runner",
            payload=complete_payload,
        )
        return self.store.get_task(task.id) or task

    @staticmethod
    def _material_enrichment_summary(context: dict[str, Any]) -> dict[str, Any]:
        results = context.get("enrichment_results") if isinstance(context, dict) else []
        if not isinstance(results, list):
            results = []
        by_provider: dict[str, int] = {}
        by_status: dict[str, int] = {}
        success_count = 0
        semantic_count = 0
        for item in results:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "unknown").strip() or "unknown"
            status = str(item.get("status") or item.get("action") or "unknown").strip() or "unknown"
            by_provider[provider] = by_provider.get(provider, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
            if item.get("success") is True:
                success_count += 1
            if item.get("semantic_content_available") is True:
                semantic_count += 1
        return {
            "total": len([item for item in results if isinstance(item, dict)]),
            "success": success_count,
            "semantic_content_available": semantic_count,
            "by_provider": by_provider,
            "by_status": by_status,
        }

    async def _execute_material_fast_path(
        self,
        task: Any,
        *,
        run_id: str,
        run_started_at: float,
        agent_state_snapshot: dict[str, Any] | None,
    ) -> bool:
        started = time.time()
        final_state = self._material_fast_path_final_state(task, agent_state_snapshot=agent_state_snapshot)
        fast_path_payload = {
            "reason": "material_output_required",
            "graph_prelude_skipped": True,
            "latency_source": "orchestrator",
        }
        self._record_ai_event(
            task,
            event_type="material.fast_path.started",
            producer="agentic.runner",
            severity="info",
            payload=fast_path_payload,
        )
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="material.fast_path.started",
            actor="agentic.runner",
            payload=fast_path_payload,
        )
        task = await asyncio.to_thread(self._prepare_material_fast_path_evidence, task, final_state)
        invoked = await asyncio.to_thread(self._ensure_material_agent_decision, task, final_state)
        material_error = self._material_completion_error(task)
        if material_error is not None and self._material_error_can_trigger_repair(task, material_error):
            repair_invoked = await asyncio.to_thread(
                self._ensure_material_agent_decision,
                task,
                final_state,
                repair_context=material_error,
            )
            if repair_invoked:
                invoked = True
                material_error = self._material_completion_error(task)
        latency_ms = (time.time() - run_started_at) * 1000
        step_status = "completed"
        if material_error is not None:
            step_status = "failed"
            self.store.record_step(
                task_id=task.id,
                run_id=run_id,
                step_name="material.fast_path",
                step_type="material_dispatch",
                status="failed",
                started_at=started,
                duration_ms=(time.time() - started) * 1000,
                error=material_error,
                metadata={
                    "graph_prelude_skipped": True,
                    "material_invoked": invoked,
                    "latency_source": "orchestrator",
                },
            )
            self.store.finish_run(run_id, status="failed", metadata={"error": material_error, "material_fast_path": True, "latency_ms": latency_ms})
            self._fail_task(task.id, trace_id=task.trace_id, error=material_error)
            failed_payload = {
                "error_type": material_error.get("type"),
                "graph_prelude_skipped": True,
                "latency_ms": round(latency_ms, 2),
            }
            self._record_ai_event(
                task,
                event_type="material.fast_path.failed",
                producer="agentic.runner",
                severity="medium",
                payload=failed_payload,
            )
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="material.fast_path.failed",
                actor="agentic.runner",
                payload=failed_payload,
            )
            return True

        final_response = await asyncio.to_thread(self._synthesize_material_completion_response, task, final_state)
        if not final_response:
            synthesis_error = {
                "type": "FinalResponseSynthesisFailed",
                "message": "Material artifact completed, but the required LLM final response could not be produced.",
            }
            self.store.record_step(
                task_id=task.id,
                run_id=run_id,
                step_name="material.final_response",
                step_type="llm_synthesis",
                status="failed",
                started_at=time.time(),
                duration_ms=0,
                error=synthesis_error,
                metadata={"material_fast_path": True, "static_fallback_used": False},
            )
            self.store.finish_run(
                run_id,
                status="failed",
                metadata={"error": synthesis_error, "material_fast_path": True, "latency_ms": latency_ms},
            )
            self._fail_task(task.id, trace_id=task.trace_id, error=synthesis_error)
            return True

        final_state["response"] = final_response
        final_response_source = str(final_state.get("material_final_response_source") or "llm")
        result = final_state_summary(final_state, latency_ms=latency_ms, graph_run_id=None)
        result["material_fast_path"] = True
        result["graph_prelude_skipped"] = True
        result["response_preview"] = final_response[:1000]
        result["final_response_source"] = final_response_source
        self.store.record_step(
            task_id=task.id,
            run_id=run_id,
            step_name="material.fast_path",
            step_type="material_dispatch",
            status=step_status,
            started_at=started,
            duration_ms=(time.time() - started) * 1000,
            output_preview="Material execution kernel completed with artifact and validation evidence.",
            metadata={
                "graph_prelude_skipped": True,
                "material_invoked": invoked,
                "latency_source": "orchestrator",
                "final_response_source": final_response_source,
            },
        )
        self.store.finish_run(run_id, status="completed", metadata={"graph_run_id": None, "latency_ms": latency_ms, "material_fast_path": True})
        self.store.update_task(task.id, status=TaskStatus.COMPLETED.value, result=result)
        completed_payload = {
            "graph_prelude_skipped": True,
            "latency_ms": round(latency_ms, 2),
            "final_response_source": final_response_source,
        }
        self._record_ai_event(
            task,
            event_type="material.fast_path.completed",
            producer="agentic.runner",
            severity="info",
            payload=completed_payload,
        )
        self.store.record_event(
            task_id=task.id,
            event_type="material.fast_path.completed",
            actor="agentic.runner",
            payload=completed_payload,
            trace_id=task.trace_id,
        )
        self.store.record_event(
            task_id=task.id,
            event_type="pipeline.completed",
            actor="agentic.runner",
            payload=result,
            trace_id=task.trace_id,
        )
        return True

    def _synthesize_material_completion_response(self, task: Any, final_state: dict[str, Any]) -> str | None:
        from orchestrator.dispatch.types import AgentInvokeRequest

        evidence = self._material_completion_synthesis_evidence(task)
        language_context = self._material_language_context(task, final_state)
        deterministic = self._deterministic_material_completion_response(
            evidence,
            language_context=language_context,
        )
        if deterministic:
            final_state["material_final_response_source"] = "deterministic"
            self._record_ai_event(
                task,
                event_type="material.final_response.completed",
                producer="agentic.runner",
                severity="info",
                payload={
                    "source": "deterministic_material_evidence",
                    "success": True,
                    "tokens_used": 0,
                    "latency_ms": 0.0,
                    "static_fallback_used": False,
                },
            )
            return deterministic

        original_query = str(
            dict(getattr(task, "metadata", {}) or {}).get("original_query")
            or getattr(task, "goal", "")
            or ""
        )
        request = AgentInvokeRequest(
            query=(
                "Produce the final user-facing response for a completed material execution task. "
                "Use only the provided evidence. Mention the validated artifact, any "
                "user-machine materialized archive path, and any extracted project directory "
                "when present. Do not invent files, commands, "
                "or validation results. Match the user's response language."
            ),
            context={
                "original_user_query": original_query,
                "material_completion_evidence": evidence,
            },
            budget_tokens=600,
            timeout_seconds=120.0,
            language_context=language_context,
            metadata={
                "role": "material_completion_synthesis",
                "task_id": task.id,
                "trace_id": task.trace_id,
                "requires_llm_response": True,
                "static_fallback_allowed": False,
            },
        )
        started = time.time()
        response = self._agent_client().invoke("reasoning_and_response", request)
        output = strip_think(str(response.output or "")).strip() if response.success else ""
        status = "completed" if output else "failed"
        self.store.record_tool_call(
            task_id=task.id,
            tool_name="agent.invoke",
            risk_level="low",
            status=status,
            input_payload={
                "agent_name": "reasoning_and_response",
                "role": "material_completion_synthesis",
                "evidence_keys": sorted(evidence),
            },
            output_payload={
                "success": response.success,
                "agent_name": response.agent_name or "reasoning_and_response",
                "confidence": response.confidence,
                "tokens_used": response.tokens_used,
                "latency_ms": response.latency_ms,
                "error": response.error,
                "output_preview": output[:1000],
            },
            metadata={
                "component": "agentic.runner.material_final_response",
                "duration_ms": (time.time() - started) * 1000,
                "static_fallback_used": False,
            },
        )
        self._record_ai_event(
            task,
            event_type="material.final_response.completed" if output else "material.final_response.failed",
            producer="agentic.runner",
            severity="info" if output else "medium",
            payload={
                "source": "reasoning_and_response",
                "success": bool(output),
                "tokens_used": response.tokens_used,
                "latency_ms": response.latency_ms,
                "static_fallback_used": False,
            },
        )
        if not output:
            return None
        final_state["material_final_response_source"] = "llm"
        with suppress(Exception):
            self.store.record_agent_action_result(
                AgentAnswer(
                    answer_id=f"material_final_response:{uuid.uuid4().hex}",
                    question_id="material_completion_synthesis",
                    task_id=task.id,
                    trace_id=task.trace_id,
                    from_agent=response.agent_name or "reasoning_and_response",
                    answer=output,
                    evidence_refs=[
                        str(ref)
                        for ref in (
                            evidence.get("material_session_id"),
                            evidence.get("artifact", {}).get("path") if isinstance(evidence.get("artifact"), dict) else None,
                        )
                        if ref
                    ],
                    metadata={
                        "role": "material_completion_synthesis",
                        "confidence": response.confidence,
                        "tokens_used": response.tokens_used,
                        "static_fallback_used": False,
                    },
                ),
                task_id=task.id,
                trace_id=task.trace_id,
            )
        return output

    def _deterministic_material_completion_response(
        self,
        evidence: dict[str, Any],
        *,
        language_context: dict[str, Any],
    ) -> str:
        artifact = evidence.get("artifact") if isinstance(evidence.get("artifact"), dict) else {}
        if not artifact:
            return ""
        archive_path = self._display_material_path(str(artifact.get("materialized_path") or artifact.get("path") or ""))
        extracted_path = self._display_material_path(str(artifact.get("extracted_path") or ""))
        sha256 = str(artifact.get("materialized_sha256") or artifact.get("sha256") or "").strip()
        validation = evidence.get("validation_summary") if isinstance(evidence.get("validation_summary"), dict) else {}
        passed = [str(item) for item in validation.get("passed", []) if str(item).strip()] if isinstance(validation.get("passed"), list) else []
        failed = [str(item) for item in validation.get("failed", []) if str(item).strip()] if isinstance(validation.get("failed"), list) else []
        session_id = str(evidence.get("material_session_id") or "").strip()
        response_language = str(
            language_context.get("response_language")
            or language_context.get("final_response_language")
            or language_context.get("source_variant")
            or language_context.get("original_language")
            or ""
        ).casefold()
        if response_language.startswith("pt"):
            lines = ["Tarefa material concluida com evidencia real do runtime."]
            if archive_path:
                lines.append(f"Arquivo publicado: `{archive_path}`.")
            if extracted_path:
                lines.append(f"Pasta extraida: `{extracted_path}`.")
            if sha256:
                lines.append(f"SHA-256: `{sha256}`.")
            if passed:
                lines.append(f"Validacao executada com sucesso: `{', '.join(passed)}`.")
            if failed:
                lines.append(f"Validacoes falhadas: `{', '.join(failed)}`.")
            if session_id:
                lines.append(f"Sessao material: `{session_id}`.")
            lines.append("A publicacao passou pelo Storage Guardian; nao foi uma escrita directa do runner no host.")
            return "\n".join(lines)
        lines = ["Material task completed with real runtime evidence."]
        if archive_path:
            lines.append(f"Published archive: `{archive_path}`.")
        if extracted_path:
            lines.append(f"Extracted directory: `{extracted_path}`.")
        if sha256:
            lines.append(f"SHA-256: `{sha256}`.")
        if passed:
            lines.append(f"Validation passed: `{', '.join(passed)}`.")
        if failed:
            lines.append(f"Validation failed: `{', '.join(failed)}`.")
        if session_id:
            lines.append(f"Material session: `{session_id}`.")
        lines.append("Publication was handled through Storage Guardian, not by a direct runner host write.")
        return "\n".join(lines)

    @staticmethod
    def _display_material_path(path: str) -> str:
        text = str(path or "").strip()
        if not text:
            return ""
        if text == "/host_home" or text.startswith("/host_home/"):
            host_home = os.environ.get("HOST_HOME_PREFIX") or os.environ.get("AI_LOCAL_HOST_HOME") or ""
            host_home = host_home.rstrip("/")
            if host_home:
                rel = text.removeprefix("/host_home").lstrip("/")
                return host_home if not rel else f"{host_home}/{rel}"
        return text

    def _material_completion_synthesis_evidence(self, task: Any) -> dict[str, Any]:
        trace = self.store.trace(task.id) or {}
        material_tool = {}
        for call in trace.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            if call.get("tool_name") == "material_execution.session" and call.get("status") == "completed":
                material_tool = call.get("output_payload") if isinstance(call.get("output_payload"), dict) else {}
        material_events: list[dict[str, Any]] = []
        for event in trace.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or event.get("type") or "")
            if "material." not in event_type:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            material_events.append({
                "event_type": event_type,
                "payload": _compact_json_payload(payload, max_chars=1600),
            })
        artifact = material_tool.get("artifact") if isinstance(material_tool.get("artifact"), dict) else {}
        sandbox = material_tool.get("sandbox") if isinstance(material_tool.get("sandbox"), dict) else {}
        validation_summary = (
            material_tool.get("validation_summary")
            if isinstance(material_tool.get("validation_summary"), dict)
            else {}
        )
        if not artifact:
            state_result = self._latest_material_action_result(task)
            if state_result:
                artifact = state_result.get("artifact") if isinstance(state_result.get("artifact"), dict) else {}
                if not artifact:
                    artifacts = state_result.get("workspace_execution_artifacts")
                    if isinstance(artifacts, list):
                        artifact = next((item for item in artifacts if isinstance(item, dict)), {})
                sandbox = state_result.get("sandbox") if isinstance(state_result.get("sandbox"), dict) else sandbox
                validation_summary = (
                    state_result.get("validation_summary")
                    if isinstance(state_result.get("validation_summary"), dict)
                    else validation_summary
                )
                material_tool = {
                    **material_tool,
                    "session_id": state_result.get("material_session_id") or material_tool.get("session_id"),
                    "status": state_result.get("material_status") or material_tool.get("status"),
                }
        return {
            "task_id": task.id,
            "trace_id": task.trace_id,
            "material_session_id": material_tool.get("session_id"),
            "status": material_tool.get("status"),
            "artifact": artifact,
            "validation_summary": validation_summary,
            "sandbox": sandbox,
            "recent_material_events": material_events[-12:],
        }

    def _latest_material_action_result(self, task: Any) -> dict[str, Any]:
        latest = self.store.latest_agent_state_snapshot(task.id)
        state = latest.get("state") if isinstance(latest, dict) else None
        if not isinstance(state, dict):
            return {}
        for action in reversed(state.get("completed_actions") or []):
            if not isinstance(action, dict) or action.get("status") != "completed":
                continue
            result = action.get("result") if isinstance(action.get("result"), dict) else {}
            if result.get("material_output_evidence") is True or result.get("artifact") or result.get("workspace_execution_artifacts"):
                return result
        return {}

    def _ensure_material_agent_decision(
        self,
        task: Any,
        final_state: dict[str, Any],
        *,
        repair_context: dict[str, Any] | None = None,
    ) -> bool:
        if not self._task_requires_material_output(task) or self._has_material_completion_evidence(task):
            return False
        latest = self.store.latest_agent_state_snapshot(task.id)
        state_hash = str((latest or {}).get("state_hash") or "")
        if not state_hash:
            return False
        from orchestrator.config import get_settings

        material_timeout = max(30.0, float(get_settings().agentic_runtime.material_decision_timeout_seconds))
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        repair_payload = repair_context if isinstance(repair_context, dict) else {}
        metadata = {
            "material_execution_request": "material_session",
            "agent_state_hash": state_hash,
            "expected_artifact_root": task_metadata.get("expected_artifact_root", ""),
            "requested_project": task_metadata.get("requested_project", ""),
            "completion_evidence_required": "effectful_action_or_artifact",
            "timeout_seconds": material_timeout,
            "policy_action": "material_execution.session",
            "material_model_lanes": MATERIAL_MODEL_LANES,
            "resource_profile": {
                "resource_class": "gpu_llm",
                "lane": "interactive",
                "capability": "material_generation",
                "model_profile": MATERIAL_MODEL_LANES["code"],
            },
            "variation_nonce": uuid.uuid4().hex[:12],
        }
        if repair_payload:
            metadata["material_repair_context"] = repair_payload
            metadata["material_repair_mode"] = "sandbox_validation"
            self._record_ai_event(
                task,
                event_type="material.repair.started",
                producer="agentic.runner",
                severity="medium",
                payload={
                    "schema_version": "material_repair_attempt.v1",
                    "repair_mode": "sandbox_validation",
                    "trigger": repair_payload.get("type") or "material_validation_failed",
                    "issues": repair_payload.get("issues") or [],
                    "workspace_command_runs": repair_payload.get("workspace_command_runs"),
                },
            )
        runtime_metadata = self._runtime_capability_metadata_for_service("material_execution_kernel", metadata)
        self._record_material_model_selection_event(task, runtime_metadata)
        session_payload = self._material_session_request_payload(
            task,
            final_state,
            runtime_metadata=runtime_metadata,
        )
        payload = {
            "feature_name": "material_execution_kernel",
            "operation": "material_session",
            "session_request": session_payload,
            "metadata": runtime_metadata,
            "capability_metadata": self._capability_metadata_for_policy(runtime_metadata),
        }
        policy = self._audit_adapter_policy(
            task,
            action_id="material-repair-kernel-session" if repair_payload else "material-kernel-session",
            action_type="api_call",
            policy_action=self._policy_action_from_metadata(runtime_metadata, default="material_execution.session"),
            payload=payload,
            component="agentic.runner.material_decision",
        )
        if policy is None:
            return False
        started = time.time()
        status = "failed"
        response_data: dict[str, Any] = {}
        session_id = ""
        try:
            feature_client = self._feature_client()
            create_response = feature_client.invoke_endpoint(
                "material_execution_kernel",
                method="POST",
                path="/v1/material-execution/sessions",
                payload=session_payload,
                timeout=material_timeout,
                policy_action="material_execution.session",
            )
            if not create_response.success:
                response_data = {"error": create_response.error}
                self.store.record_tool_call(
                    task_id=task.id,
                    tool_name="material_execution.session",
                    risk_level=policy.risk_level,
                    status="failed",
                    input_payload=payload,
                    output_payload=response_data,
                    metadata={"component": "agentic.runner.material_decision", "duration_ms": (time.time() - started) * 1000},
                )
                self._record_ai_event(
                    task,
                    event_type="material.kernel.failed",
                    producer="agentic.runner",
                    severity="medium",
                    payload={
                        "phase": "session_create",
                        "error": create_response.error,
                    },
                )
                return False
            response_data = dict(create_response.data or {})
            session_id = str(response_data.get("session_id") or "")
            recorded_material_event_ids: set[str] = set()
            self._record_material_kernel_events(
                task,
                session_id,
                timeout=material_timeout,
                seen_event_ids=recorded_material_event_ids,
            )
            for step_index in range(self._material_kernel_step_limit(task_metadata)):
                self._raise_if_task_cancelled(task.id)
                current_status = str(response_data.get("status") or "")
                if self._material_kernel_terminal_status(current_status):
                    break
                if not session_id:
                    response_data = {"error": "material kernel response did not include a session_id"}
                    break
                step_started = time.time()
                self._record_ai_event(
                    task,
                    event_type="material.kernel.step.started",
                    producer="agentic.runner",
                    severity="info",
                    payload={
                        "session_id": session_id,
                        "step_index": step_index,
                        "status": current_status,
                        "phase": current_status,
                        "latency_source": "kernel",
                        "timeout_seconds": material_timeout,
                    },
                )
                step_response = feature_client.invoke_endpoint(
                    "material_execution_kernel",
                    method="POST",
                    path=f"/v1/material-execution/sessions/{session_id}/step",
                    payload={},
                    timeout=material_timeout,
                    policy_action="material_execution.step",
                )
                if not step_response.success:
                    response_data = {
                        **response_data,
                        "status": "failed_closed",
                        "error": step_response.error,
                    }
                    break
                response_data = dict(step_response.data or {})
                self._record_ai_event(
                    task,
                    event_type="material.kernel.step.completed",
                    producer="agentic.runner",
                    severity="info",
                    payload={
                        "session_id": session_id,
                        "step_index": step_index,
                        "status": response_data.get("status"),
                        "phase": response_data.get("status"),
                        "latency_source": "kernel",
                        "duration_ms": int((time.time() - step_started) * 1000),
                    },
                )
                self._record_material_kernel_events(
                    task,
                    session_id,
                    timeout=material_timeout,
                    seen_event_ids=recorded_material_event_ids,
                )
            manifest = self._record_material_kernel_manifest(task, session_id, timeout=material_timeout)
            self._record_material_kernel_events(
                task,
                session_id,
                timeout=material_timeout,
                seen_event_ids=recorded_material_event_ids,
            )
            completed = str(response_data.get("status") or "") == "completed"
            status = "completed" if completed else "failed"
            self.store.record_tool_call(
                task_id=task.id,
                tool_name="material_execution.session",
                risk_level=policy.risk_level,
                status=status,
                input_payload=payload,
                output_payload={
                    "session_id": session_id,
                    "status": response_data.get("status"),
                    "artifact": response_data.get("artifact"),
                    "validation_summary": response_data.get("validation_summary"),
                    "issues": response_data.get("issues"),
                    "sandbox": response_data.get("sandbox"),
                    "manifest_status": manifest.get("status") if isinstance(manifest, dict) else None,
                },
                metadata={"component": "agentic.runner.material_decision", "duration_ms": (time.time() - started) * 1000},
            )
            if not completed:
                self._record_ai_event(
                    task,
                    event_type="material.kernel.blocked",
                    producer="agentic.runner",
                    severity="medium",
                    payload={
                        "session_id": session_id,
                        "status": response_data.get("status"),
                        "issues": response_data.get("issues"),
                        "error": response_data.get("error"),
                    },
                )
                if repair_payload:
                    self._record_ai_event(
                        task,
                        event_type="material.repair.rejected",
                        producer="agentic.runner",
                        severity="medium",
                        payload={
                            "schema_version": "material_repair_result.v1",
                            "repair_mode": "sandbox_validation",
                            "issues": repair_payload.get("issues") or [],
                            "completion_evidence": False,
                        },
                    )
                return False
            self.store.record_agent_action_result(
                ActionResult(
                    action_id="material-kernel-session",
                    action_type="api_call",
                    status="completed",
                    observation="Material execution kernel completed with artifact and validation evidence.",
                    result=self._material_kernel_action_result(response_data),
                    policy_decision={"decision": policy.decision, "risk_level": policy.risk_level},
                ),
                task_id=task.id,
                trace_id=task.trace_id,
            )
            if repair_payload:
                self._record_ai_event(
                    task,
                    event_type="material.repair.accepted",
                    producer="agentic.runner",
                    severity="info",
                    payload={
                        "schema_version": "material_repair_result.v1",
                        "repair_mode": "sandbox_validation",
                        "issues": repair_payload.get("issues") or [],
                        "completion_evidence": True,
                    },
                )
            return True
        except Exception as exc:
            self.store.record_tool_call(
                task_id=task.id,
                tool_name="material_execution.session",
                risk_level=policy.risk_level,
                status=status,
                input_payload=payload,
                output_payload=response_data or {"error": str(exc)[:1000]},
                metadata={"component": "agentic.runner.material_decision", "duration_ms": (time.time() - started) * 1000},
            )
            self._record_ai_event(
                task,
                event_type="material.kernel.failed",
                producer="agentic.runner",
                severity="medium",
                payload={"session_id": session_id, "error": str(exc)[:1000]},
            )
            return False

    def _material_session_request_payload(
        self,
        task: Any,
        final_state: dict[str, Any],
        *,
        runtime_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        language_context = self._material_language_context(task, final_state)
        original_language = str(
            language_context.get("original_language")
            or language_context.get("user_language")
            or "unknown"
        )
        source_variant = language_context.get("source_variant")
        quality = language_context.get("quality")
        safety_error = language_context.get("safety_error")
        working_query = str(
            task_metadata.get("working_query")
            or language_context.get("english_text")
            or language_context.get("normalized_text")
            or task.goal
        )
        expected_root = str(task_metadata.get("expected_artifact_root") or task_metadata.get("requested_project") or "").strip()
        required_capabilities = task_metadata.get("required_capabilities") or task_metadata.get("material_required_capabilities") or []
        if not isinstance(required_capabilities, list):
            required_capabilities = []
        raw_context_refs = task_metadata.get("context_refs", [])
        context_refs = raw_context_refs if isinstance(raw_context_refs, list) else []
        material_builder_context = {
            "original_query": str(task_metadata.get("original_query") or task.goal),
            "working_query": working_query,
            "language_context": language_context,
        }
        evidence_context = task_metadata.get("material_evidence_context")
        if isinstance(evidence_context, dict):
            material_builder_context["evidence_context"] = evidence_context
        return {
            "task_id": task.id,
            "trace_id": task.trace_id,
            "idempotency_key": f"{task.id}:material:v3.2",
            "goal": working_query,
            "language_context": {
                "original_language": original_language,
                "source_variant": str(source_variant) if source_variant else None,
                "working_language": "en",
                "target_language": str(language_context.get("target_language") or "en"),
                "translation_available": bool(language_context.get("translation_available")),
                "translation_safe": bool(language_context.get("translation_safe", True)),
                "internal_contract_language": "en",
                "final_response_language": str(language_context.get("response_language") or original_language),
                "contract_version": (
                    str(language_context.get("contract_version"))
                    if language_context.get("contract_version")
                    else None
                ),
                "quality": quality if isinstance(quality, dict) else {},
                "safety_error": safety_error if isinstance(safety_error, dict) else None,
            },
            "material_builder_context": material_builder_context,
            "constraints": {
                "expected_artifact_root": expected_root or None,
                "must_use_vm_backed_sandbox": True,
                "must_not_execute_on_host": True,
                "durable_publish": bool(task_metadata.get("durable_publish")),
                "publish_destination_root": (
                    str(task_metadata.get("material_publish_destination_root"))
                    if task_metadata.get("material_publish_destination_root")
                    else None
                ),
                "publish_direct_to_destination_root": bool(
                    task_metadata.get("material_publish_direct_to_destination_root")
                ),
                "publish_store": str(task_metadata.get("material_publish_store") or "agent_outputs"),
                "publish_zone": str(task_metadata.get("material_publish_zone") or "ingest"),
                "network_policy": str(task_metadata.get("material_network_policy") or "disabled_by_default"),
                "generated_project_trust": "untrusted",
            },
            "required_capabilities": [str(item) for item in required_capabilities if str(item).strip()],
            "context_refs": [str(ref) for ref in context_refs if isinstance(ref, str)],
            "policy_context": {
                "sandbox_mode": "vm-workspace-write",
                "approval_mode": str(runtime_metadata.get("approval_mode") or "supervised"),
                "network_mode": str(task_metadata.get("material_network_policy") or "disabled_by_default"),
                "docker_mode": "vm-local-or-proxied-isolated",
            },
        }

    @staticmethod
    def _material_language_context(task: Any, final_state: dict[str, Any]) -> dict[str, Any]:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        for value in (task_metadata.get("language_context"), final_state.get("language_context")):
            if isinstance(value, dict):
                try:
                    from orchestrator.pipeline.language_context import normalize_language_context

                    normalized = normalize_language_context(value, original_query=str(getattr(task, "goal", "") or ""))
                    if normalized is not None:
                        return normalized
                except Exception:
                    pass
                return value
        try:
            from orchestrator.pipeline.language_context import language_context_fallback

            return language_context_fallback(str(getattr(task, "goal", "") or ""), reason="material_kernel_dispatch")
        except Exception:
            return {
                "user_language": "unknown",
                "response_language": "same_as_user",
                "translation_available": False,
                "english_text": str(getattr(task, "goal", "") or ""),
            }

    @staticmethod
    def _material_kernel_terminal_status(status: str) -> bool:
        return status in {
            "completed",
            "blocked_by_policy",
            "blocked_by_vm_isolation",
            "blocked_by_sandbox_profile",
            "blocked_by_contract",
            "blocked_by_missing_tool",
            "failed_closed",
            "stalled",
            "cancelled",
        }

    @staticmethod
    def _material_kernel_step_limit(task_metadata: dict[str, Any]) -> int:
        try:
            requested = int(task_metadata.get("material_kernel_step_limit") or 24)
        except (TypeError, ValueError):
            requested = 24
        return max(1, min(requested, 64))

    def _record_material_kernel_manifest(self, task: Any, session_id: str, *, timeout: float) -> dict[str, Any]:
        if not session_id:
            return {}
        response = self._feature_client().invoke_endpoint(
            "material_execution_kernel",
            method="GET",
            path=f"/v1/material-execution/sessions/{session_id}/manifest",
            timeout=timeout,
            policy_action="material_execution.read",
        )
        manifest = response.data if response.success and isinstance(response.data, dict) else {}
        if manifest:
            self._record_ai_event(
                task,
                event_type="material.manifest.created",
                producer="material_execution_kernel",
                severity="info",
                payload=manifest,
            )
        return manifest

    def _record_material_kernel_events(
        self,
        task: Any,
        session_id: str,
        *,
        timeout: float,
        seen_event_ids: set[str] | None = None,
    ) -> None:
        if not session_id:
            return
        response = self._feature_client().invoke_endpoint(
            "material_execution_kernel",
            method="GET",
            path=f"/v1/material-execution/sessions/{session_id}/events/json",
            timeout=timeout,
            policy_action="material_execution.read",
        )
        events = response.data.get("events") if response.success and isinstance(response.data, dict) else []
        if not isinstance(events, list):
            return
        for event in events:
            if not isinstance(event, dict):
                continue
            material_event_id = str(event.get("event_id") or "")
            if seen_event_ids is not None and material_event_id:
                if material_event_id in seen_event_ids:
                    continue
                seen_event_ids.add(material_event_id)
            event_type = str(event.get("event_type") or "")
            if not event_type:
                continue
            source = str(event.get("source") or "kernel")
            status = str(event.get("status") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            payload = {
                **payload,
                "material_event_id": material_event_id or event.get("event_id"),
                "material_session_id": session_id,
                "phase": event.get("phase"),
                "status": status,
                "latency_source": event.get("latency_source"),
                "duration_ms": event.get("duration_ms"),
            }
            self._record_ai_event(
                task,
                event_type=event_type,
                producer=self._material_event_producer(source),
                severity="medium" if status in {"failed", "blocked", "cancelled"} else "info",
                payload=payload,
            )

    @staticmethod
    def _material_event_producer(source: str) -> str:
        return {
            "kernel": "material_execution_kernel",
            "material_builder": "material_builder",
            "sandbox_owner": "workspace_execution",
            "policy": "orchestrator.policy",
            "orchestrator": "orchestrator",
        }.get(source, "material_execution_kernel")

    @staticmethod
    def _material_kernel_action_result(response_data: dict[str, Any]) -> dict[str, Any]:
        artifact = response_data.get("artifact") if isinstance(response_data.get("artifact"), dict) else {}
        artifacts = []
        if artifact.get("path"):
            artifacts.append(
                {
                    "path": artifact.get("path"),
                    "sha256": artifact.get("sha256"),
                    "size_bytes": artifact.get("size_bytes"),
                    "storage_object_ref": artifact.get("storage_object_ref"),
                    "chain_of_custody_ref": artifact.get("chain_of_custody_ref"),
                    "materialized_path": artifact.get("materialized_path"),
                    "materialized_sha256": artifact.get("materialized_sha256"),
                    "extracted_path": artifact.get("extracted_path"),
                    "extracted_files_count": artifact.get("extracted_files_count"),
                    "extracted_top_level_paths": artifact.get("extracted_top_level_paths") or [],
                }
            )
        return {
            "material_output_evidence": True,
            "material_session_id": response_data.get("session_id"),
            "material_status": response_data.get("status"),
            "artifact": artifact,
            "workspace_execution_artifacts": artifacts,
            "validation_summary": response_data.get("validation_summary"),
            "sandbox": response_data.get("sandbox"),
            "command_runs": response_data.get("command_runs"),
        }

    def _execute_agent_actions(self, task: Any, actions: list[Any]) -> None:
        index = 0
        while index < len(actions):
            action = actions[index]
            action_type = str(getattr(action, "type", ""))
            if action_type == "shell_command":
                batch = [action]
                index += 1
                while index < len(actions) and str(getattr(actions[index], "type", "")) == "shell_command":
                    batch.append(actions[index])
                    index += 1
                runnable = [item for item in batch if self._check_action_execution_boundary(task, item).allowed]
                if runnable:
                    self._execute_shell_action_batch(task, runnable)
                continue
            if not self._check_action_execution_boundary(task, action).allowed:
                index += 1
                continue
            if action_type == "noop":
                self.store.record_agent_action_result(
                    ActionResult(
                        action_id=action.action_id,
                        action_type=action.type,
                        status="completed",
                        observation=action.reason,
                    ),
                    task_id=task.id,
                    trace_id=task.trace_id,
                )
                index += 1
                continue
            if action_type == "agent_invoke":
                self._execute_agent_invoke_action(task, action)
                index += 1
                continue
            if action_type == "rag_query":
                self._execute_rag_query_action(task, action)
                index += 1
                continue
            if action_type == "api_call":
                self._execute_api_call_action(task, action)
                index += 1
                continue
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.action.policy_checked",
                actor="agentic.runner",
                payload={
                    "action": action.model_dump(mode="json") if hasattr(action, "model_dump") else {},
                    "decision": "blocked",
                    "reason": "unsupported_action_adapter",
                },
            )
            self.store.record_agent_action_result(
                ActionResult(
                    action_id=action.action_id,
                    action_type=action.type,
                    status="blocked",
                    observation="Action adapter is not implemented in this runtime version.",
                    error={"type": "UnsupportedActionAdapter", "message": action.type},
                ),
                task_id=task.id,
                trace_id=task.trace_id,
            )
            index += 1

    def _check_action_execution_boundary(self, task: Any, action: Any) -> ActionBoundaryCheck:
        action_id = str(getattr(action, "action_id", "") or "")
        action_type = str(getattr(action, "type", "") or "")
        snapshot = self.store.current_agent_state(task.id)
        state_hash = str((snapshot or {}).get("state_hash") or "")
        state = (snapshot or {}).get("state") if isinstance(snapshot, dict) else {}
        pending = state.get("pending_actions") if isinstance(state, dict) else []
        pending_action = next(
            (
                item
                for item in pending
                if isinstance(item, dict)
                and item.get("action_id") == action_id
                and item.get("type") == action_type
            ),
            None,
        )
        metadata = self._boundary_metadata_for_action(action)
        sandbox_required, sandbox_proof = self._sandbox_proof_for_action(action)
        if pending_action is None:
            return self._record_action_boundary_block(
                task,
                action,
                state_hash=state_hash,
                metadata=metadata,
                sandbox_required=sandbox_required,
                sandbox_proof=sandbox_proof,
                error_type="UnproposedAgentAction",
                reason="Action is not pending in AgentState; tool execution requires a valid AgentDecision proposal.",
            )
        if sandbox_required and not sandbox_proof.get("verified"):
            return self._record_action_boundary_block(
                task,
                action,
                state_hash=state_hash,
                metadata=metadata,
                sandbox_required=sandbox_required,
                sandbox_proof=sandbox_proof,
                error_type="SandboxProofMissing",
                reason="Shell command execution requires the workspace_execution command backend.",
            )
        check = ActionBoundaryCheck(
            allowed=True,
            action_id=action_id,
            action_type=action_type,
            state_hash=state_hash,
            capability_id=str(metadata.get("capability_id") or ""),
            policy_action=str(metadata.get("policy_action") or ""),
            owner=str(metadata.get("owner") or ""),
            sandbox_required=sandbox_required,
            sandbox_proof=sandbox_proof,
        )
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="agent.action.boundary_checked",
            actor="agentic.runner",
            payload={
                "action_id": check.action_id,
                "action_type": check.action_type,
                "decision": "allow",
                "state_hash": check.state_hash,
                "capability_id": check.capability_id,
                "policy_action": check.policy_action,
                "owner": check.owner,
                "agent_decision_required": True,
                "pending_action_verified": True,
                "sandbox_required": check.sandbox_required,
                "sandbox_proof": check.sandbox_proof,
                "capability_metadata": metadata,
            },
        )
        return check

    def _record_action_boundary_block(
        self,
        task: Any,
        action: Any,
        *,
        state_hash: str,
        metadata: dict[str, Any],
        sandbox_required: bool,
        sandbox_proof: dict[str, Any],
        error_type: str,
        reason: str,
    ) -> ActionBoundaryCheck:
        action_id = str(getattr(action, "action_id", "") or "")
        action_type = str(getattr(action, "type", "") or "")
        check = ActionBoundaryCheck(
            allowed=False,
            action_id=action_id,
            action_type=action_type,
            state_hash=state_hash,
            capability_id=str(metadata.get("capability_id") or ""),
            policy_action=str(metadata.get("policy_action") or ""),
            owner=str(metadata.get("owner") or ""),
            reason=reason,
            error_type=error_type,
            sandbox_required=sandbox_required,
            sandbox_proof=sandbox_proof,
        )
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="agent.action.boundary_checked",
            actor="agentic.runner",
            payload={
                "action_id": check.action_id,
                "action_type": check.action_type,
                "decision": "blocked",
                "reason": reason,
                "state_hash": check.state_hash,
                "capability_id": check.capability_id,
                "policy_action": check.policy_action,
                "owner": check.owner,
                "agent_decision_required": True,
                "pending_action_verified": False,
                "sandbox_required": check.sandbox_required,
                "sandbox_proof": check.sandbox_proof,
                "capability_metadata": metadata,
            },
        )
        self.store.record_agent_action_result(
            ActionResult(
                action_id=check.action_id or "unknown-action",
                action_type=check.action_type or "unknown",
                status="blocked",
                observation=reason,
                error={"type": error_type, "message": reason},
            ),
            task_id=task.id,
            trace_id=task.trace_id,
        )
        return check

    def _boundary_metadata_for_action(self, action: Any) -> dict[str, Any]:
        action_type = str(getattr(action, "type", "") or "")
        action_metadata = getattr(action, "metadata", {}) or {}
        metadata = dict(action_metadata) if isinstance(action_metadata, dict) else {}
        if action_type == "noop":
            return {
                **metadata,
                "capability_id": metadata.get("capability_id") or "orchestrator.noop",
                "policy_action": metadata.get("policy_action") or "agent.noop",
                "owner": metadata.get("owner") or "orchestrator/agentic",
            }
        if action_type == "shell_command":
            capability_id = self._shell_action_capability_id(action)
            capability_metadata = self._action_capability_metadata(capability_id)
            return {
                **capability_metadata,
                **metadata,
                "capability_id": metadata.get("capability_id") or capability_metadata.get("capability_id") or capability_id,
                "policy_action": metadata.get("policy_action") or capability_metadata.get("policy_action") or "workspace.sandbox.execute",
                "owner": metadata.get("owner") or capability_metadata.get("owner") or "workspace_execution",
            }
        if action_type == "api_call":
            target = self._resolve_api_call_target(action)
            if target is not None:
                capability_metadata = self._runtime_capability_metadata_for_service(
                    target.service_name,
                    {
                        **dict(target.capability_metadata or {}),
                        **metadata,
                    },
                )
                return {
                    **capability_metadata,
                    "capability_id": metadata.get("capability_id") or capability_metadata.get("capability_id") or target.service_name,
                    "policy_action": self._policy_action_from_metadata(capability_metadata, default=target.policy_action),
                    "owner": capability_metadata.get("owner") or target.service_name,
                }
        if action_type == "agent_invoke":
            return self._runtime_capability_metadata_for_service(getattr(action, "agent_name", ""), metadata)
        if action_type == "rag_query":
            source_name = str(metadata.get("source") or "rag")
            service_name = "research" if source_name in {"rag", "research", "cag"} else source_name
            return self._runtime_capability_metadata_for_service(service_name, metadata)
        return metadata

    @staticmethod
    def _shell_action_capability_id(action: Any) -> str:
        expected_effect = str(getattr(action, "expected_effect", "read_only") or "read_only")
        if expected_effect == "destructive":
            return "workspace_execution.command_destructive"
        return "workspace_execution.command_execute"

    @staticmethod
    def _action_capability_metadata(capability_id: str) -> dict[str, Any]:
        try:
            from orchestrator.capabilities.action_manifest import action_capability_manifest

            manifest = action_capability_manifest(capability_id)
        except Exception:
            manifest = None
        if manifest is None:
            return {}
        return manifest.to_action_metadata().model_dump(mode="json")

    @staticmethod
    def _sandbox_proof_for_action(action: Any) -> tuple[bool, dict[str, Any]]:
        if str(getattr(action, "type", "") or "") != "shell_command":
            return False, {}
        try:
            from orchestrator.config import get_settings

            backend = str(get_settings().agentic_runtime.command_tool_backend or "")
        except Exception:
            backend = ""
        return True, {
            "owner": "workspace_execution",
            "backend": backend,
            "verified": backend == "workspace_execution",
        }

    def _execute_agent_invoke_action(self, task: Any, action: Any) -> None:
        runtime_metadata = self._runtime_capability_metadata_for_service(action.agent_name, action.metadata)
        runtime_metadata["timeout_seconds"] = self._agentic_agent_timeout(runtime_metadata.get("timeout_seconds"))
        payload = {
            "action_id": action.action_id,
            "agent_name": action.agent_name,
            "query_preview": str(action.query)[:500],
            "metadata": runtime_metadata,
            "capability_metadata": self._capability_metadata_for_policy(runtime_metadata),
        }
        policy = self._audit_adapter_policy(
            task,
            action_id=action.action_id,
            action_type=action.type,
            policy_action=self._policy_action_from_metadata(runtime_metadata, default="agent.invoke"),
            payload=payload,
            component="agentic.runner.agent_invoke",
        )
        if policy is None:
            return
        lease = self._acquire_adapter_lease(task, action_id=action.action_id, action_type=action.type, metadata=runtime_metadata)
        if lease is not None and not lease.granted:
            self._record_adapter_lease_blocked(task, action, lease=lease, policy=policy, input_payload=payload)
            return
        started = time.time()
        try:
            from orchestrator.dispatch.types import AgentInvokeRequest

            response = self._agent_client().invoke(
                action.agent_name,
                AgentInvokeRequest(
                    query=action.query,
                    context=action.context,
                    budget_tokens=runtime_metadata.get("budget_tokens"),
                    timeout_seconds=runtime_metadata.get("timeout_seconds"),
                    metadata={
                        **runtime_metadata,
                        "agentic_action_id": action.action_id,
                        "task_id": task.id,
                        "trace_id": task.trace_id,
                    },
                ),
                endpoint_override=runtime_metadata.get("endpoint_override"),
            )
            result = {
                "agent_name": response.agent_name or action.agent_name,
                "confidence": response.confidence,
                "tokens_used": response.tokens_used,
                "latency_ms": response.latency_ms,
                "metadata": response.metadata,
                "agent_decision": response.agent_decision,
            }
            status = "completed" if response.success else "failed"
            observation = response.output if response.success else response.error
            self.store.record_tool_call(
                task_id=task.id,
                tool_name="agent.invoke",
                risk_level=policy.risk_level,
                status=status,
                input_payload=payload,
                output_payload=result if response.success else {"error": response.error},
                metadata={"component": "agentic.runner.agent_invoke", "duration_ms": (time.time() - started) * 1000},
            )
            if not response.success:
                self._record_ai_event(
                    task,
                    event_type="agent.invoke.failed",
                    producer="agentic.runner",
                    severity="medium",
                    payload={"agent_name": action.agent_name, "action_id": action.action_id, "error": response.error},
                )
            self.store.record_agent_action_result(
                ActionResult(
                    action_id=action.action_id,
                    action_type=action.type,
                    status=status,
                    observation=observation or status,
                    result=result,
                    error={"type": "AgentInvokeError", "message": response.error} if response.error else None,
                    policy_decision=policy.to_dict(),
                ),
                task_id=task.id,
                trace_id=task.trace_id,
            )
        except Exception as exc:
            self._record_adapter_failure(task, action, exc, policy=policy, tool_name="agent.invoke", input_payload=payload)
        finally:
            if lease is not None:
                self._release_lease(lease)

    def _run_agent_question_rounds(self, task: Any, questions: list[AgentQuestion]) -> None:
        pending = list(questions)
        if not pending:
            return
        max_rounds = 3
        max_questions = 12
        answered = 0
        seen = {question.question_id for question in pending}
        final_assessment: dict[str, Any] | None = None
        for round_index in range(max_rounds):
            if not pending or answered >= max_questions:
                break
            current = pending[: max_questions - answered]
            pending = []
            follow_ups: list[AgentQuestion] = []
            answer_ids: list[str] = []
            outcomes: list[AgentQuestionOutcome] = []
            for question in current:
                outcome = self._answer_agent_question(task, question)
                answered += 1
                outcomes.append(outcome)
                answer_ids.append(outcome.answer_id)
                for new_question in outcome.follow_up_questions:
                    if new_question.question_id in seen:
                        continue
                    follow_ups.append(new_question)
            round_plan = plan_next_deliberation_round(
                task_id=task.id,
                trace_id=task.trace_id,
                round_index=round_index,
                max_rounds=max_rounds,
                answered_total=answered,
                max_questions=max_questions,
                current_questions=current,
                outcomes=outcomes,
                candidate_questions=follow_ups,
                seen_question_ids=seen,
            )
            planned_follow_ups = round_plan.selected_questions
            assessment = self._score_deliberation_round(
                outcomes=outcomes,
                follow_ups=planned_follow_ups,
                round_index=round_index,
                max_rounds=max_rounds,
                answered_total=answered,
                max_questions=max_questions,
            )
            queued_follow_ups: list[AgentQuestion] = []
            if assessment["should_continue"]:
                for new_question in planned_follow_ups:
                    if new_question.question_id in seen:
                        continue
                    seen.add(new_question.question_id)
                    self.store.record_agent_message(new_question, actor="agentic.runner")
                    queued_follow_ups.append(new_question)
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.deliberation.round_completed",
                actor="agentic.runner",
                payload={
                    "round_index": round_index,
                    "question_ids": [question.question_id for question in current],
                    "answer_ids": answer_ids,
                    "proposed_follow_up_question_ids": [
                        question.question_id
                        for question in [*follow_ups, *(item.question for item in round_plan.generated)]
                    ],
                    "queued_follow_up_question_ids": [question.question_id for question in queued_follow_ups],
                    "answered_total": answered,
                    "max_rounds": max_rounds,
                    "max_questions": max_questions,
                    "assessment": assessment,
                    "planner": round_plan.to_event_payload(),
                    "stopped_reason": assessment["reason"],
                },
            )
            final_assessment = assessment
            if not assessment["should_continue"]:
                self._record_deliberation_termination(
                    task,
                    assessment=assessment,
                    round_index=round_index,
                    answered_total=answered,
                )
                pending = []
                break
            pending = queued_follow_ups
        if pending:
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.deliberation.truncated",
                actor="agentic.runner",
                payload={
                    "pending_question_ids": [question.question_id for question in pending],
                    "answered_total": answered,
                    "max_rounds": max_rounds,
                    "max_questions": max_questions,
                    "last_assessment": final_assessment,
                },
            )

    def _answer_agent_question(self, task: Any, question: AgentQuestion) -> AgentQuestionOutcome:
        runtime_metadata = self._runtime_capability_metadata_for_service(question.to_agent, question.metadata)
        runtime_metadata["timeout_seconds"] = self._agentic_agent_timeout(runtime_metadata.get("timeout_seconds"))
        payload = {
            "question_id": question.question_id,
            "from_agent": question.from_agent,
            "to_agent": question.to_agent,
            "round_id": question.round_id,
            "question_preview": question.question[:500],
            "metadata": runtime_metadata,
            "capability_metadata": self._capability_metadata_for_policy(runtime_metadata),
        }
        policy = self._audit_adapter_policy(
            task,
            action_id=question.question_id,
            action_type="agent_question",
            policy_action=self._policy_action_from_metadata(runtime_metadata, default="agent.invoke"),
            payload=payload,
            component="agentic.runner.agent_question",
        )
        if policy is None:
            self.store.record_agent_message(
                AgentAnswer(
                    answer_id=f"answer_{question.question_id}",
                    question_id=question.question_id,
                    task_id=question.task_id,
                    trace_id=question.trace_id,
                    from_agent=question.to_agent,
                    answer="Question could not be answered: policy blocked the agent invocation.",
                    evidence_refs=question.evidence_refs,
                    metadata={"status": "blocked", "to_agent": question.from_agent},
                ),
                actor="agentic.runner",
            )
            return AgentQuestionOutcome(
                question_id=question.question_id,
                answer_id=f"answer_{question.question_id}",
                status="blocked",
                error="policy_blocked",
            )

        lease = self._normalize_lease(
            self.lease_provider(
                self._question_lease_task(task, question, metadata=runtime_metadata),
                f"question:{question.question_id}",
            )
        )
        self._record_lease(
            task.id,
            lease,
            capability=self._lease_capability_name(self._resource_profile_from_metadata(runtime_metadata)),
        )
        if not lease.granted:
            self.store.record_agent_message(
                AgentAnswer(
                    answer_id=f"answer_{question.question_id}",
                    question_id=question.question_id,
                    task_id=question.task_id,
                    trace_id=question.trace_id,
                    from_agent=question.to_agent,
                    answer=f"Question could not be answered: resource lease {lease.decision}. {lease.reason}".strip(),
                    evidence_refs=question.evidence_refs,
                    metadata={"status": "skipped", "lease": lease.__dict__, "to_agent": question.from_agent},
                ),
                actor="agentic.runner",
            )
            return AgentQuestionOutcome(
                question_id=question.question_id,
                answer_id=f"answer_{question.question_id}",
                status="skipped",
                error=lease.reason or lease.decision,
            )

        started = time.time()
        try:
            from orchestrator.dispatch.types import AgentInvokeRequest

            response = self._agent_client().invoke(
                question.to_agent,
                AgentInvokeRequest(
                    query=question.question,
                    context={
                        "agent_question": question.model_dump(mode="json"),
                        "question_from_agent": question.from_agent,
                        "round_id": question.round_id,
                    },
                    timeout_seconds=runtime_metadata.get("timeout_seconds"),
                    budget_tokens=runtime_metadata.get("budget_tokens"),
                    metadata={
                        **runtime_metadata,
                        "agentic_question_id": question.question_id,
                        "task_id": task.id,
                        "trace_id": task.trace_id,
                        "role": "answer_agent_question",
                    },
                ),
            )
            response_output = str(getattr(response, "output", "") or "").strip()
            empty_success = bool(response.success and not response_output)
            status = "completed" if response.success and not empty_success else "failed"
            self.store.record_tool_call(
                task_id=task.id,
                tool_name="agent.invoke",
                risk_level=policy.risk_level,
                status=status,
                input_payload=payload,
                output_payload={
                    "success": response.success,
                    "agent_name": response.agent_name or question.to_agent,
                    "confidence": response.confidence,
                    "tokens_used": response.tokens_used,
                    "latency_ms": response.latency_ms,
                    "metadata": response.metadata,
                    "error": response.error or ("empty_agent_output" if empty_success else ""),
                },
                metadata={"component": "agentic.runner.agent_question", "duration_ms": (time.time() - started) * 1000},
            )
            if not response.success or empty_success:
                self._record_ai_event(
                    task,
                    event_type="agent.invoke.failed",
                    producer="agentic.runner",
                    severity="medium",
                    payload={
                        "agent_name": question.to_agent,
                        "question_id": question.question_id,
                        "error": response.error or "empty_agent_output",
                    },
                )
            answer_text = response_output if status == "completed" else response.error or "agent invocation returned empty output"
            self.store.record_agent_message(
                AgentAnswer(
                    answer_id=f"answer_{question.question_id}",
                    question_id=question.question_id,
                    task_id=question.task_id,
                    trace_id=question.trace_id,
                    from_agent=response.agent_name or question.to_agent,
                    answer=answer_text[:8000],
                    evidence_refs=question.evidence_refs,
                    metadata={
                        "status": status,
                        "to_agent": question.from_agent,
                        "confidence": response.confidence if status == "completed" else 0.0,
                        "agent_decision": response.agent_decision,
                        "policy_decision": policy.to_dict(),
                        "empty_output": empty_success,
                    },
                ),
                actor="agentic.runner",
            )
            return self._question_outcome_from_response(task, question=question, response=response, status=status)
        except Exception as exc:
            self._record_ai_event(
                task,
                event_type="agent.invoke.failed",
                producer="agentic.runner",
                severity="medium",
                payload={"agent_name": question.to_agent, "question_id": question.question_id, "error": str(exc)[:1000]},
            )
            self.store.record_agent_message(
                AgentAnswer(
                    answer_id=f"answer_{question.question_id}",
                    question_id=question.question_id,
                    task_id=question.task_id,
                    trace_id=question.trace_id,
                    from_agent=question.to_agent,
                    answer=f"Question adapter failed: {str(exc)[:1000]}",
                    evidence_refs=question.evidence_refs,
                    metadata={"status": "failed", "to_agent": question.from_agent},
                ),
                actor="agentic.runner",
            )
            return AgentQuestionOutcome(
                question_id=question.question_id,
                answer_id=f"answer_{question.question_id}",
                status="failed",
                error=str(exc)[:1000],
            )
        finally:
            self._release_lease(lease)

    def _question_outcome_from_response(
        self,
        task: Any,
        *,
        question: AgentQuestion,
        response: Any,
        status: str,
    ) -> AgentQuestionOutcome:
        signals = self._structured_deliberation_signals(response)
        follow_ups = tuple(self._follow_up_questions_from_agent_response(task, question=question, response=response))
        return AgentQuestionOutcome(
            question_id=question.question_id,
            answer_id=f"answer_{question.question_id}",
            status=status,
            confidence=max(0.0, min(1.0, self._safe_float(getattr(response, "confidence", 0.0)))),
            follow_up_questions=follow_ups,
            agreed_facts=tuple(signals["agreed_facts"]),
            contested_facts=tuple(signals["contested_facts"]),
            contradictions=tuple(signals["contradictions"]),
            error=str(getattr(response, "error", "") or ""),
        )

    def _score_deliberation_round(
        self,
        *,
        outcomes: list[AgentQuestionOutcome],
        follow_ups: list[AgentQuestion],
        round_index: int,
        max_rounds: int,
        answered_total: int,
        max_questions: int,
    ) -> dict[str, Any]:
        completed = [item for item in outcomes if item.status == "completed"]
        failed = [item for item in outcomes if item.status not in {"completed"}]
        confidences = [item.confidence for item in completed]
        confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
        agreed_facts = sorted({fact for item in outcomes for fact in item.agreed_facts})
        contested_facts = sorted({fact for item in outcomes for fact in item.contested_facts})
        contradictions = sorted({fact for item in outcomes for fact in item.contradictions})
        completion_ratio = round(len(completed) / len(outcomes), 4) if outcomes else 0.0
        score = round((confidence * 0.7) + (completion_ratio * 0.3), 4)
        confidence_threshold = 0.85

        if contested_facts or contradictions:
            reason = "contradiction_preserved"
            should_continue = False
        elif answered_total >= max_questions:
            reason = "budget"
            should_continue = False
        elif score >= confidence_threshold and completed and not failed:
            reason = "consensus_sufficient"
            should_continue = False
        elif follow_ups and round_index + 1 < max_rounds:
            reason = "continue"
            should_continue = True
        elif follow_ups:
            reason = "max_rounds"
            should_continue = False
        elif completed and failed:
            reason = "partial_evidence"
            should_continue = False
        elif completed:
            reason = "complete"
            should_continue = False
        else:
            reason = "failed"
            should_continue = False

        return {
            "score": score,
            "confidence": confidence,
            "confidence_threshold": confidence_threshold,
            "completion_ratio": completion_ratio,
            "completed_count": len(completed),
            "failed_count": len(failed),
            "agreed_facts": agreed_facts,
            "contested_facts": contested_facts,
            "contradictions": contradictions,
            "reason": reason,
            "should_continue": should_continue,
        }

    def _record_deliberation_termination(
        self,
        task: Any,
        *,
        assessment: dict[str, Any],
        round_index: int,
        answered_total: int,
    ) -> None:
        reason = str(assessment.get("reason") or "complete")
        status = "accepted"
        if reason == "contradiction_preserved":
            status = "contested"
        elif reason in {"budget", "max_rounds", "failed", "partial_evidence"}:
            status = "failed" if reason == "failed" else "needs_more_evidence"
        summary = {
            "consensus_sufficient": "Deliberation stopped because confidence and completion score reached the threshold.",
            "contradiction_preserved": "Deliberation stopped because contested facts or contradictions were preserved.",
            "budget": "Deliberation stopped because the question budget was exhausted.",
            "max_rounds": "Deliberation stopped because the round limit was reached.",
            "complete": "Deliberation completed with no further structured follow-up questions.",
            "partial_evidence": "Deliberation stopped with partial evidence because one or more agent answers failed.",
            "failed": "Deliberation stopped because no question answer completed successfully.",
        }.get(reason, "Deliberation completed.")
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="agent.deliberation.terminated",
            actor="agentic.runner",
            payload={
                "reason": reason,
                "round_index": round_index,
                "answered_total": answered_total,
                "assessment": assessment,
            },
        )
        self.store.record_agent_message(
            {
                "consensus_id": f"deliberation_consensus_{uuid.uuid4().hex}",
                "task_id": task.id,
                "trace_id": task.trace_id,
                "status": status,
                "summary": summary,
                "agreed_facts": assessment.get("agreed_facts") or [],
                "contested_facts": assessment.get("contested_facts") or [],
                "confidence": float(assessment.get("confidence") or 0.0),
                "metadata": {
                    "decider": "agentic.runner.deliberation",
                    "reason": reason,
                    "score": assessment.get("score"),
                    "round_index": round_index,
                    "answered_total": answered_total,
                    "contradictions": assessment.get("contradictions") or [],
                },
            },
            actor="agentic.runner",
        )

    def _follow_up_questions_from_agent_response(
        self,
        task: Any,
        *,
        question: AgentQuestion,
        response: Any,
    ) -> list[AgentQuestion]:
        candidates: list[Any] = []
        response_metadata = getattr(response, "metadata", None)
        if isinstance(response_metadata, dict):
            candidates.extend(self._as_list(response_metadata.get("agent_questions")))
            candidates.extend(self._as_list(response_metadata.get("follow_up_questions")))
        decision = getattr(response, "agent_decision", None)
        decision_data = decision if isinstance(decision, dict) else {}
        if isinstance(decision_data, dict):
            candidates.extend(self._as_list(decision_data.get("agent_questions")))
            metadata = decision_data.get("metadata")
            if isinstance(metadata, dict):
                candidates.extend(self._as_list(metadata.get("agent_questions")))
                candidates.extend(self._as_list(metadata.get("follow_up_questions")))

        questions: list[AgentQuestion] = []
        for index, candidate in enumerate(candidates):
            normalized = self._normalize_follow_up_question(
                task,
                parent=question,
                candidate=candidate,
                index=index,
                from_agent=str(getattr(response, "agent_name", None) or question.to_agent),
            )
            if normalized is None:
                continue
            questions.append(normalized)
        if questions:
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.deliberation.follow_up_questions_proposed",
                actor="agentic.runner",
                payload={
                    "parent_question_id": question.question_id,
                    "from_agent": getattr(response, "agent_name", None) or question.to_agent,
                    "question_ids": [item.question_id for item in questions],
                },
            )
        return questions

    def _structured_deliberation_signals(self, response: Any) -> dict[str, list[str]]:
        metadata = getattr(response, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        decision = getattr(response, "agent_decision", None)
        decision_data = decision if isinstance(decision, dict) else {}
        decision_metadata = decision_data.get("metadata") if isinstance(decision_data.get("metadata"), dict) else {}
        return {
            "agreed_facts": sorted(
                {
                    *self._string_list(metadata.get("agreed_facts")),
                    *self._string_list(metadata.get("validated_facts")),
                    *self._string_list(decision_data.get("new_facts")),
                    *self._string_list(decision_metadata.get("agreed_facts")),
                    *self._string_list(decision_metadata.get("validated_facts")),
                }
            ),
            "contested_facts": sorted(
                {
                    *self._string_list(metadata.get("contested_facts")),
                    *self._string_list(decision_metadata.get("contested_facts")),
                }
            ),
            "contradictions": sorted(
                {
                    *self._string_list(metadata.get("contradictions")),
                    *self._string_list(decision_metadata.get("contradictions")),
                }
            ),
        }

    @staticmethod
    def _normalize_follow_up_question(
        task: Any,
        *,
        parent: AgentQuestion,
        candidate: Any,
        index: int,
        from_agent: str,
    ) -> AgentQuestion | None:
        if hasattr(candidate, "model_dump"):
            payload = candidate.model_dump(mode="json")
        elif isinstance(candidate, dict):
            payload = dict(candidate)
        else:
            return None
        if not payload.get("question"):
            return None
        payload.setdefault("question_id", f"{parent.question_id}:followup:{index}:{uuid.uuid4().hex[:8]}")
        payload.setdefault("task_id", task.id)
        payload.setdefault("trace_id", task.trace_id)
        payload.setdefault("from_agent", from_agent)
        payload.setdefault("round_id", parent.round_id)
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.setdefault("parent_question_id", parent.question_id)
        payload["metadata"] = metadata
        try:
            return AgentQuestion.model_validate(payload)
        except Exception:
            return None

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _agentic_agent_timeout(cls, requested: Any) -> float:
        from orchestrator.config import get_settings

        floor = max(30.0, cls._safe_float(get_settings().dispatch.agent_timeout_seconds))
        requested_timeout = cls._safe_float(requested)
        if requested_timeout <= 0:
            return floor
        return max(requested_timeout, floor)

    def _execute_rag_query_action(self, task: Any, action: Any) -> None:
        source_name = str(action.metadata.get("source") or "rag")
        service_name = "research" if source_name in {"rag", "research", "cag"} else source_name
        runtime_metadata = self._runtime_capability_metadata_for_service(service_name, action.metadata)
        payload = {
            "action_id": action.action_id,
            "source": source_name,
            "namespace": action.namespace,
            "query_preview": str(action.query)[:500],
            "metadata": runtime_metadata,
            "capability_metadata": self._capability_metadata_for_policy(runtime_metadata),
        }
        policy = self._audit_adapter_policy(
            task,
            action_id=action.action_id,
            action_type=action.type,
            policy_action=self._policy_action_from_metadata(runtime_metadata, default="rag.query"),
            payload=payload,
            component="agentic.runner.rag_query",
        )
        if policy is None:
            return
        lease = self._acquire_adapter_lease(task, action_id=action.action_id, action_type=action.type, metadata=runtime_metadata)
        if lease is not None and not lease.granted:
            self._record_adapter_lease_blocked(task, action, lease=lease, policy=policy, input_payload=payload)
            return
        started = time.time()
        try:
            metadata = {
                **runtime_metadata,
                "namespace": action.namespace,
                "agentic_action_id": action.action_id,
                "task_id": task.id,
                "trace_id": task.trace_id,
            }
            response = self._feature_client().query_source(
                source_name,
                query=action.query,
                budget_tokens=runtime_metadata.get("budget_tokens"),
                timeout=runtime_metadata.get("timeout_seconds"),
                metadata=metadata,
            )
            result = {
                "source": response.source,
                "token_estimate": response.token_estimate,
                "latency_ms": response.latency_ms,
                "metadata": response.metadata,
            }
            status = "completed" if response.success else "failed"
            observation = response.content if response.success else response.error
            if not response.success or not response.content.strip():
                self._record_ai_event(
                    task,
                    event_type="rag.miss",
                    producer="agentic.runner",
                    severity="low",
                    payload={
                        "source": source_name,
                        "namespace": action.namespace,
                        "action_id": action.action_id,
                        "error": response.error,
                    },
                )
            self.store.record_tool_call(
                task_id=task.id,
                tool_name="rag.query",
                risk_level=policy.risk_level,
                status=status,
                input_payload=payload,
                output_payload={**result, "content_preview": response.content[:1000]},
                metadata={"component": "agentic.runner.rag_query", "duration_ms": (time.time() - started) * 1000},
            )
            self.store.record_agent_action_result(
                ActionResult(
                    action_id=action.action_id,
                    action_type=action.type,
                    status=status,
                    observation=observation or status,
                    result=result,
                    error={"type": "RagQueryError", "message": response.error} if response.error else None,
                    policy_decision=policy.to_dict(),
                ),
                task_id=task.id,
                trace_id=task.trace_id,
            )
        except Exception as exc:
            self._record_adapter_failure(task, action, exc, policy=policy, tool_name="rag.query", input_payload=payload)
        finally:
            if lease is not None:
                self._release_lease(lease)

    def _execute_api_call_action(self, task: Any, action: Any) -> None:
        target = self._resolve_api_call_target(action)
        if target is None:
            self.store.record_agent_action_result(
                ActionResult(
                    action_id=action.action_id,
                    action_type=action.type,
                    status="blocked",
                    observation="api_call must target a registered service by capability/service metadata, not a raw URL.",
                    error={"type": "InvalidApiCallTarget", "message": action.endpoint},
                ),
                task_id=task.id,
                trace_id=task.trace_id,
            )
            return
        service_name = target.service_name
        path = target.path
        runtime_metadata = self._runtime_capability_metadata_for_service(
            service_name,
            {
                **dict(target.capability_metadata or {}),
                **dict(action.metadata or {}),
            },
        )
        policy_action = self._policy_action_from_metadata(runtime_metadata, default=target.policy_action)
        payload = {
            "action_id": action.action_id,
            "service": service_name,
            "method": action.method,
            "path": path,
            "expected_effect": action.expected_effect,
            "metadata": runtime_metadata,
            "capability_metadata": self._capability_metadata_for_policy(runtime_metadata),
        }
        if self._block_unsafe_capability_write(task, action=action, metadata=runtime_metadata, payload=payload):
            return
        missing_input_fields = self._missing_schema_fields(
            runtime_metadata.get("input_schema"),
            action.payload,
            required_only=True,
        )
        if missing_input_fields and self._schema_validation_required(action, runtime_metadata):
            self._record_adapter_schema_failure(
                task,
                action,
                phase="input",
                status="blocked",
                missing_fields=missing_input_fields,
                payload=payload,
            )
            return
        policy = self._audit_adapter_policy(
            task,
            action_id=action.action_id,
            action_type=action.type,
            policy_action=policy_action,
            payload={**payload, "payload": action.payload},
            component="agentic.runner.api_call",
        )
        if policy is None:
            return
        lease = self._acquire_adapter_lease(task, action_id=action.action_id, action_type=action.type, metadata=runtime_metadata)
        if lease is not None and not lease.granted:
            self._record_adapter_lease_blocked(task, action, lease=lease, policy=policy, input_payload=payload)
            return
        started = time.time()
        try:
            response = self._feature_client().invoke_endpoint(
                service_name,
                method=action.method,
                path=path,
                payload=action.payload,
                params=runtime_metadata.get("params"),
                timeout=runtime_metadata.get("timeout_seconds") or target.timeout_seconds,
                policy_action=policy_action,
                auth_profile=str(runtime_metadata.get("auth_profile") or "internal_api"),
                tls_alias_profile=str(runtime_metadata.get("tls_alias_profile") or ""),
            )
            result = {
                "source": response.source,
                "data": response.data,
                "latency_ms": response.latency_ms,
            }
            missing_output_fields = self._missing_schema_fields(runtime_metadata.get("output_schema"), response.data)
            if response.success and missing_output_fields and self._schema_validation_required(action, runtime_metadata):
                self.store.record_tool_call(
                    task_id=task.id,
                    tool_name=policy_action,
                    risk_level=policy.risk_level,
                    status="failed",
                    input_payload=payload,
                    output_payload={
                        "error": "schema_output_missing_fields",
                        "missing_fields": missing_output_fields,
                    },
                    metadata={"component": "agentic.runner.api_call", "duration_ms": (time.time() - started) * 1000},
                )
                self._record_adapter_schema_failure(
                    task,
                    action,
                    phase="output",
                    status="failed",
                    missing_fields=missing_output_fields,
                    payload=payload,
                    policy=policy,
                )
                return
            status = "completed" if response.success else "failed"
            self.store.record_tool_call(
                task_id=task.id,
                tool_name=policy_action,
                risk_level=policy.risk_level,
                status=status,
                input_payload=payload,
                output_payload=result if response.success else {"error": response.error},
                metadata={"component": "agentic.runner.api_call", "duration_ms": (time.time() - started) * 1000},
            )
            self.store.record_agent_action_result(
                ActionResult(
                    action_id=action.action_id,
                    action_type=action.type,
                    status=status,
                    observation=str(response.data)[:8000] if response.success else response.error,
                    result=result,
                    error={"type": "ApiCallError", "message": response.error} if response.error else None,
                    policy_decision=policy.to_dict(),
                ),
                task_id=task.id,
                trace_id=task.trace_id,
            )
        except Exception as exc:
            self._record_adapter_failure(task, action, exc, policy=policy, tool_name=policy_action, input_payload=payload)
        finally:
            if lease is not None:
                self._release_lease(lease)

    def _audit_adapter_policy(
        self,
        task: Any,
        *,
        action_id: str,
        action_type: str,
        policy_action: str,
        payload: dict[str, Any],
        component: str,
    ) -> Any | None:
        decision = audit_policy_check(policy_action, payload=payload, component=component)
        decision_data = decision.to_dict()
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="agent.action.policy_checked",
            actor="agentic.runner",
            payload={
                "action_id": action_id,
                "action_type": action_type,
                "policy_action": decision.action,
                "policy_decision": decision_data,
                "component": component,
                "metadata": payload.get("metadata") if isinstance(payload, dict) else None,
                "capability_metadata": payload.get("capability_metadata") if isinstance(payload, dict) else None,
            },
        )
        if not decision.should_block:
            return decision
        if decision.decision == PolicyDecisionKind.REQUIRE_APPROVAL.value:
            status = "waiting_approval"
        elif decision.decision == PolicyDecisionKind.DENY.value:
            status = "denied"
        else:
            status = "blocked"
        self.store.record_agent_action_result(
            ActionResult(
                action_id=action_id,
                action_type=action_type,
                status=status,
                observation=f"Policy blocked {decision.action}: {decision.reason}",
                error={"type": "PolicyBlocked", "message": decision.reason},
                policy_decision=decision_data,
            ),
            task_id=task.id,
            trace_id=task.trace_id,
        )
        return None

    def _runtime_capability_metadata_for_service(self, service_name: str, metadata: Any) -> dict[str, Any]:
        runtime_metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
        manifest_metadata = self._service_capability_metadata(service_name)
        for key, value in manifest_metadata.items():
            runtime_metadata.setdefault(key, value)
        model_selection = self._model_selection_from_metadata(runtime_metadata)
        if model_selection and "agentic_model_selection" not in runtime_metadata:
            runtime_metadata["agentic_model_selection"] = model_selection
        return runtime_metadata

    @staticmethod
    def _model_selection_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
        profile = metadata.get("model_profile")
        if not profile and isinstance(metadata.get("resource_profile"), dict):
            profile = metadata["resource_profile"].get("model_profile")
        if not isinstance(profile, str) or not profile.strip():
            return None
        try:
            from orchestrator.routing.model_router import ConfigModelRouter

            selection = ConfigModelRouter().select_model_profile(profile, fallback_profile=None)
        except Exception:
            selection = None
        return selection.to_event_payload() if selection is not None else None

    @staticmethod
    def _service_capability_metadata(service_name: str) -> dict[str, Any]:
        try:
            from orchestrator.agentic.tool_envelope import service_tool_envelopes

            envelope = next(
                (item for item in service_tool_envelopes() if item.service_name == service_name),
                None,
            )
        except Exception:
            envelope = None
        if envelope is None:
            return {}
        payload = envelope.to_public_dict()
        if envelope.service_kind:
            payload["kind"] = envelope.service_kind
        return payload

    @staticmethod
    def _policy_action_from_metadata(metadata: dict[str, Any], *, default: str) -> str:
        return str(metadata.get("policy_action") or default)

    @staticmethod
    def _capability_metadata_for_policy(metadata: dict[str, Any]) -> dict[str, Any]:
        keys = {
            "service_name",
            "capability_id",
            "kind",
            "capabilities",
            "policy_action",
            "risk_level",
            "is_read_only",
            "is_concurrency_safe",
            "supported_action_types",
            "resource_profile",
            "input_schema",
            "output_schema",
            "evidence_types",
            "result_persistence",
            "model_profile",
            "writes_allowed",
            "idempotency_policy",
            "dry_run_supported",
            "rollback_supported",
            "events_published",
            "service_kind",
            "source",
            "timeout_seconds",
        }
        return {key: metadata[key] for key in keys if key in metadata}

    def _acquire_adapter_lease(
        self,
        task: Any,
        *,
        action_id: str,
        action_type: str,
        metadata: dict[str, Any],
    ) -> LeaseOutcome | None:
        profile = self._resource_profile_from_metadata(metadata)
        if not profile:
            return None
        lease_task = self._runtime_lease_task(
            task,
            request_kind=action_type,
            request_ref=action_id,
            metadata=metadata,
            resource_profile=profile,
        )
        lease = self._normalize_lease(self.lease_provider(lease_task, f"{action_type}:{action_id}"))
        self._record_lease(task.id, lease, capability=self._lease_capability_name(profile))
        return lease

    @staticmethod
    def _resource_profile_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        profile = metadata.get("resource_profile") or {}
        return dict(profile) if isinstance(profile, dict) else {}

    def _record_adapter_lease_blocked(
        self,
        task: Any,
        action: Any,
        *,
        lease: LeaseOutcome,
        policy: Any,
        input_payload: dict[str, Any],
    ) -> None:
        status = "skipped" if lease.decision in {"defer", "skip_optional"} else "blocked"
        self.store.record_tool_call(
            task_id=task.id,
            tool_name=str(getattr(action, "type", "action")),
            risk_level=str(getattr(policy, "risk_level", "medium")),
            status=status,
            input_payload=input_payload,
            output_payload={"lease": lease.__dict__},
            metadata={"component": "agentic.runner.adapter_lease", "action_id": action.action_id},
        )
        self.store.record_agent_action_result(
            ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                status=status,
                observation=f"Resource lease {lease.decision}: {lease.reason or 'capacity unavailable'}",
                error={"type": "ResourceLeaseNotGranted", "message": lease.reason or lease.decision},
                policy_decision=policy.to_dict() if hasattr(policy, "to_dict") else None,
            ),
            task_id=task.id,
            trace_id=task.trace_id,
        )

    def _block_unsafe_capability_write(
        self,
        task: Any,
        *,
        action: Any,
        metadata: dict[str, Any],
        payload: dict[str, Any],
    ) -> bool:
        expected_effect = str(getattr(action, "expected_effect", "") or "")
        method = str(getattr(action, "method", "") or "").upper()
        writes_allowed = bool(metadata.get("writes_allowed") or expected_effect in {"write", "destructive"} or method in {"PUT", "PATCH", "DELETE"})
        if not writes_allowed:
            return False
        dry_run_supported = bool(metadata.get("dry_run_supported"))
        rollback_supported = bool(metadata.get("rollback_supported"))
        if dry_run_supported or rollback_supported:
            return False
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="agent.action.policy_checked",
            actor="agentic.runner",
            payload={
                "action_id": action.action_id,
                "action_type": action.type,
                "policy_action": metadata.get("policy_action") or "capability.write",
                "decision": "blocked",
                "reason": "capability_write_without_dry_run_or_rollback",
                "capability_metadata": self._capability_metadata_for_policy(metadata),
            },
        )
        self.store.record_agent_action_result(
            ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                status="blocked",
                observation="Capability write blocked because the manifest does not declare dry-run or rollback support.",
                error={
                    "type": "UnsafeCapabilityWrite",
                    "message": "Capability writes require dry_run_supported or rollback_supported in the manifest.",
                },
            ),
            task_id=task.id,
            trace_id=task.trace_id,
        )
        return True

    @staticmethod
    def _schema_validation_required(action: Any, metadata: dict[str, Any]) -> bool:
        expected_effect = str(getattr(action, "expected_effect", "") or "")
        return bool(
            metadata.get("writes_allowed")
            or metadata.get("risk_level") == "high"
            or expected_effect in {"write", "destructive"}
        )

    @staticmethod
    def _missing_schema_fields(schema: Any, payload: Any, *, required_only: bool = False) -> list[str]:
        if not isinstance(schema, dict):
            return []
        field_name = "required" if required_only else "fields"
        declared = schema.get(field_name)
        if not isinstance(declared, list) or not declared:
            return []
        if not isinstance(payload, dict):
            return [str(item) for item in declared]
        return [str(item) for item in declared if str(item) not in payload]

    def _record_adapter_schema_failure(
        self,
        task: Any,
        action: Any,
        *,
        phase: str,
        status: str,
        missing_fields: list[str],
        payload: dict[str, Any],
        policy: Any | None = None,
    ) -> None:
        error = {
            "type": "SchemaValidationError",
            "message": f"Capability {phase} payload is missing manifest-declared fields.",
            "phase": phase,
            "missing_fields": list(missing_fields),
        }
        self.store.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="agent.action.schema_validation_failed",
            actor="agentic.runner",
            payload={
                "action_id": action.action_id,
                "action_type": action.type,
                "phase": phase,
                "missing_fields": list(missing_fields),
                "capability_metadata": self._capability_metadata_for_policy(payload.get("metadata", {})),
            },
        )
        self.store.record_agent_action_result(
            ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                status=status,
                observation=error["message"],
                error=error,
                policy_decision=policy.to_dict() if hasattr(policy, "to_dict") else None,
            ),
            task_id=task.id,
            trace_id=task.trace_id,
        )

    def _record_adapter_failure(
        self,
        task: Any,
        action: Any,
        exc: Exception,
        *,
        policy: Any,
        tool_name: str,
        input_payload: dict[str, Any],
    ) -> None:
        error = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        self.store.record_tool_call(
            task_id=task.id,
            tool_name=tool_name,
            risk_level=str(getattr(policy, "risk_level", "medium")),
            status="failed",
            input_payload=input_payload,
            output_payload={"error": error},
            error=error["message"],
            metadata={"component": "agentic.runner.adapter", "action_id": action.action_id},
        )
        self._record_ai_event(
            task,
            event_type=f"{action.type}.failed",
            producer="agentic.runner",
            severity="medium",
            payload={"action_id": action.action_id, "error": error},
        )
        self.store.record_agent_action_result(
            ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                status="failed",
                observation=f"{action.type} adapter failed: {error['message']}",
                error=error,
                policy_decision=policy.to_dict() if hasattr(policy, "to_dict") else None,
            ),
            task_id=task.id,
            trace_id=task.trace_id,
        )

    def _execute_parallel_plan(self, task: Any, plan: AgenticParallelPlan) -> None:
        if plan.task_id != task.id or plan.trace_id != task.trace_id:
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.parallel_plan.rejected",
                actor="agentic.runner",
                payload={
                    "plan_id": plan.plan_id,
                    "reason": "task_or_trace_mismatch",
                    "plan_task_id": plan.task_id,
                    "plan_trace_id": plan.trace_id,
                },
            )
            return

        started_at = time.time()
        round_id = f"round_{uuid.uuid4().hex}"
        request_id = f"parallel-{uuid.uuid4().hex[:16]}"
        participants = list(plan.participants)
        lease_decisions: list[dict[str, Any]] = []
        observations: list[AgentObservation] = []
        runnable: list[tuple[int, Any, LeaseOutcome, dict[str, Any], dict[str, Any]]] = []

        for index, participant in enumerate(participants):
            runtime_metadata = self._runtime_capability_metadata_for_service(participant.agent_name, participant.metadata)
            resource_profile = self._resource_profile_from_metadata(runtime_metadata)
            lease_task = self._parallel_lease_task(
                task,
                plan=plan,
                participant=participant,
                index=index,
                metadata=runtime_metadata,
            )
            lease = self._normalize_lease(self.lease_provider(lease_task, f"{request_id}:{index}"))
            self._record_lease(task.id, lease, capability=self._lease_capability_name(resource_profile))
            lease_decision = {
                "agent_name": participant.agent_name,
                "role": participant.role,
                "decision": lease.decision,
                "lease_id": lease.lease_id,
                "reason": lease.reason,
                "limits": lease.limits,
                "resource_profile": resource_profile,
            }
            lease_decisions.append(lease_decision)
            if not lease.granted:
                observations.append(
                    AgentObservation(
                        observation_id=f"obs_{uuid.uuid4().hex}",
                        source=f"parallel:{participant.agent_name}",
                        content=f"Resource lease {lease.decision}: {lease.reason or 'capacity unavailable'}",
                        metadata={"agent_name": participant.agent_name, "role": participant.role, "success": False, "lease": lease_decision},
                    )
                )
                continue

            policy_action = self._policy_action_from_metadata(runtime_metadata, default="agent.invoke")
            policy = audit_policy_check(
                policy_action,
                payload={
                    "plan_id": plan.plan_id,
                    "round_id": round_id,
                    "agent_name": participant.agent_name,
                    "role": participant.role,
                    "metadata": runtime_metadata,
                    "capability_metadata": self._capability_metadata_for_policy(runtime_metadata),
                },
                component="agentic.runner.parallel",
            )
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.parallel_agent.policy_checked",
                actor="agentic.runner",
                payload={
                    "plan_id": plan.plan_id,
                    "round_id": round_id,
                    "agent_name": participant.agent_name,
                    "policy_action": policy.action,
                    "policy_decision": policy.to_dict(),
                    "metadata": runtime_metadata,
                    "capability_metadata": self._capability_metadata_for_policy(runtime_metadata),
                },
            )
            if policy.should_block:
                self._release_lease(lease)
                observations.append(
                    AgentObservation(
                        observation_id=f"obs_{uuid.uuid4().hex}",
                        source=f"parallel:{participant.agent_name}",
                        content=f"Policy blocked agent invocation: {policy.reason}",
                        metadata={
                            "agent_name": participant.agent_name,
                            "role": participant.role,
                            "success": False,
                            "policy_decision": policy.to_dict(),
                        },
                    )
                )
                continue
            runnable.append((index, participant, lease, policy.to_dict(), runtime_metadata))

        max_workers = min(plan.max_parallel, len(runnable))
        if plan.fallback_policy == "sequential" and len(runnable) < len(participants):
            max_workers = min(1, len(runnable))
        if max_workers > 0:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(
                            self._invoke_parallel_agent,
                            task,
                            plan,
                            round_id,
                            participant,
                            f"{request_id}:{index}",
                            policy_decision,
                            runtime_metadata,
                        ): (participant, lease)
                        for index, participant, lease, policy_decision, runtime_metadata in runnable
                    }
                    for future in concurrent.futures.as_completed(futures):
                        participant, _lease = futures[future]
                        try:
                            observations.append(future.result())
                        except Exception as exc:
                            observations.append(
                                AgentObservation(
                                    observation_id=f"obs_{uuid.uuid4().hex}",
                                    source=f"parallel:{participant.agent_name}",
                                    content=f"Parallel agent invocation failed: {str(exc)[:1000]}",
                                    metadata={
                                        "agent_name": participant.agent_name,
                                        "role": participant.role,
                                        "success": False,
                                        "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
                                    },
                                )
                            )
                            self._record_ai_event(
                                task,
                                event_type="agent.invoke.failed",
                                producer="agentic.runner",
                                severity="medium",
                                payload={"agent_name": participant.agent_name, "round_id": round_id, "error": str(exc)[:1000]},
                            )
            finally:
                for _, _, lease, _, _ in runnable:
                    self._release_lease(lease)

        success_by_agent = {
            str(observation.metadata.get("agent_name")): bool(observation.metadata.get("success"))
            for observation in observations
            if observation.metadata.get("agent_name")
        }
        required_failed = any(participant.required and not success_by_agent.get(participant.agent_name) for participant in participants)
        success_count = sum(1 for value in success_by_agent.values() if value)
        failure_count = max(0, len(participants) - success_count)
        if not participants:
            status = "completed"
        elif required_failed or (success_count == 0 and plan.fallback_policy == "fail"):
            status = "failed"
        elif failure_count:
            status = "degraded"
        else:
            status = "completed"
        consensus = self._build_parallel_consensus(
            plan=plan,
            participants=participants,
            observations=observations,
            status=status,
            success_by_agent=success_by_agent,
            max_workers=max_workers,
            failure_count=failure_count,
            required_failed=required_failed,
        )
        round_record = AgenticParallelRound(
            round_id=round_id,
            plan_id=plan.plan_id,
            task_id=task.id,
            trace_id=task.trace_id,
            status=status,
            participants=participants,
            observations=observations,
            lease_decisions=lease_decisions,
            consensus=consensus,
            degraded=status != "completed",
            started_at=started_at,
            finished_at=time.time(),
            metadata={"resource_aware": True, "request_id": request_id},
        )
        self.store.record_parallel_round(round_record, plan=plan, actor="agentic.runner")
        self.store.record_agent_message(
            {
                "consensus_id": f"consensus_{round_id}",
                "task_id": task.id,
                "trace_id": task.trace_id,
                "round_id": round_id,
                "status": consensus["decision_status"],
                "summary": consensus["summary"],
                "agreed_facts": consensus["agreed_facts"],
                "contested_facts": consensus["contested_facts"],
                "confidence": consensus["confidence"],
                "metadata": {
                    "decider": "agentic.runner",
                    "parallel_status": status,
                    "successful_agents": consensus["successful_agents"],
                    "failed_agents": consensus["failed_agents"],
                    "fallback_policy": plan.fallback_policy,
                    "max_workers": max_workers,
                    "failure_count": failure_count,
                    "required_failed": required_failed,
                    "contradictions": consensus["contradictions"],
                },
            },
            actor="agentic.runner",
        )

    @staticmethod
    def _build_parallel_consensus(
        *,
        plan: AgenticParallelPlan,
        participants: list[Any],
        observations: list[AgentObservation],
        status: str,
        success_by_agent: dict[str, bool],
        max_workers: int,
        failure_count: int,
        required_failed: bool,
    ) -> dict[str, Any]:
        successful_agents = [name for name, ok in success_by_agent.items() if ok]
        failed_agents = [participant.agent_name for participant in participants if not success_by_agent.get(participant.agent_name)]
        facts_by_key: dict[str, dict[str, Any]] = {}
        contested_facts: dict[str, str] = {}
        contradictions: list[str] = []
        confidences: list[float] = []

        for observation in observations:
            metadata = observation.metadata or {}
            if metadata.get("success") and isinstance(metadata.get("confidence"), (int, float)):
                confidences.append(float(metadata["confidence"]))
            decision = metadata.get("agent_decision")
            decision_data = decision if isinstance(decision, dict) else {}
            decision_meta = decision_data.get("metadata") if isinstance(decision_data.get("metadata"), dict) else {}
            sources = [str(metadata.get("agent_name") or observation.source)]
            for fact in AgenticRunner._string_list(decision_data.get("new_facts")):
                key = fact.casefold().strip()
                if not key:
                    continue
                bucket = facts_by_key.setdefault(key, {"fact": fact, "sources": set()})
                bucket["sources"].update(sources)
            for fact in AgenticRunner._string_list(metadata.get("validated_facts")):
                key = fact.casefold().strip()
                if not key:
                    continue
                bucket = facts_by_key.setdefault(key, {"fact": fact, "sources": set()})
                bucket["sources"].update(sources)
            for fact in [
                *AgenticRunner._string_list(metadata.get("contested_facts")),
                *AgenticRunner._string_list(decision_meta.get("contested_facts")),
            ]:
                contested_facts[fact.casefold().strip()] = fact
            contradictions.extend(AgenticRunner._string_list(metadata.get("contradictions")))
            contradictions.extend(AgenticRunner._string_list(decision_meta.get("contradictions")))

        min_sources = 2 if len(successful_agents) > 1 else 1
        agreed_facts = [
            str(bucket["fact"])
            for key, bucket in facts_by_key.items()
            if len(bucket["sources"]) >= min_sources and key not in contested_facts
        ]
        contested = sorted({fact for key, fact in contested_facts.items() if key})
        contradictions = sorted({item for item in contradictions if item})
        if status == "failed":
            decision_status = "failed"
        elif contested or contradictions:
            decision_status = "contested"
        elif failure_count:
            decision_status = "needs_more_evidence"
        else:
            decision_status = "accepted"
        confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
        summary = (
            f"{len(successful_agents)}/{len(participants)} parallel agents completed"
            if participants
            else "No parallel participants requested"
        )
        if decision_status == "contested":
            summary = f"{summary}; contradictions or contested facts preserved"
        elif decision_status == "needs_more_evidence":
            summary = f"{summary}; partial evidence only"
        return {
            "summary": summary,
            "decision_status": decision_status,
            "successful_agents": successful_agents,
            "failed_agents": failed_agents,
            "fallback_policy": plan.fallback_policy,
            "max_workers": max_workers,
            "failure_count": failure_count,
            "required_failed": required_failed,
            "agreed_facts": agreed_facts,
            "contested_facts": contested,
            "contradictions": contradictions,
            "confidence": max(0.0, min(1.0, confidence)),
        }

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _invoke_parallel_agent(
        self,
        task: Any,
        plan: AgenticParallelPlan,
        round_id: str,
        participant: Any,
        request_id: str,
        policy_decision: dict[str, Any],
        runtime_metadata: dict[str, Any],
    ) -> AgentObservation:
        from orchestrator.dispatch.types import AgentInvokeRequest

        ctx = AgenticContext(
            task_id=task.id,
            trace_id=task.trace_id,
            request_id=request_id,
            session_id=task.session_id or task.id,
            mode=task.mode,
        )
        token = set_agentic_context(ctx)
        try:
            started = time.time()
            response = self._agent_client().invoke(
                participant.agent_name,
                AgentInvokeRequest(
                    query=participant.query,
                    context={
                        **dict(participant.context or {}),
                        "agentic_parallel_plan": plan.model_dump(mode="json"),
                        "parallel_round_id": round_id,
                    },
                    timeout_seconds=runtime_metadata.get("timeout_seconds") or participant.timeout_seconds,
                    budget_tokens=runtime_metadata.get("budget_tokens") or participant.budget_tokens,
                    metadata={
                        **runtime_metadata,
                        "agentic_parallel_plan_id": plan.plan_id,
                        "agentic_parallel_round_id": round_id,
                        "role": participant.role,
                        "task_id": task.id,
                        "trace_id": task.trace_id,
                    },
                ),
            )
            status = "completed" if response.success else "failed"
            self.store.record_tool_call(
                task_id=task.id,
                tool_name="agent.invoke",
                risk_level=str(policy_decision.get("risk_level") or "low"),
                status=status,
                input_payload={
                    "plan_id": plan.plan_id,
                    "round_id": round_id,
                    "agent_name": participant.agent_name,
                    "role": participant.role,
                    "metadata": runtime_metadata,
                    "capability_metadata": self._capability_metadata_for_policy(runtime_metadata),
                },
                output_payload={
                    "success": response.success,
                    "confidence": response.confidence,
                    "tokens_used": response.tokens_used,
                    "latency_ms": response.latency_ms,
                    "metadata": response.metadata,
                    "error": response.error,
                },
                metadata={"component": "agentic.runner.parallel", "duration_ms": (time.time() - started) * 1000},
            )
            if not response.success:
                self._record_ai_event(
                    task,
                    event_type="agent.invoke.failed",
                    producer="agentic.runner",
                    severity="medium",
                    payload={"agent_name": participant.agent_name, "round_id": round_id, "error": response.error},
                )
            return AgentObservation(
                observation_id=f"obs_{uuid.uuid4().hex}",
                source=f"parallel:{participant.agent_name}",
                content=response.output if response.success else response.error or "agent invocation failed",
                metadata={
                    "agent_name": participant.agent_name,
                    "role": participant.role,
                    "success": response.success,
                    "confidence": response.confidence,
                    "tokens_used": response.tokens_used,
                    "latency_ms": response.latency_ms,
                    "agent_decision": response.agent_decision,
                    "policy_decision": policy_decision,
                },
            )
        finally:
            reset_agentic_context(token)

    @staticmethod
    def _parallel_lease_task(
        task: Any,
        *,
        plan: AgenticParallelPlan,
        participant: Any,
        index: int,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        runtime_metadata = dict(metadata or {})
        profile = runtime_metadata.get("resource_profile") or {}
        task_metadata.update(
            {
                "agentic_parallel_plan_id": plan.plan_id,
                "agentic_parallel_participant": participant.agent_name,
                "agentic_runtime_metadata": runtime_metadata,
                "agentic_resource_profile": dict(profile) if isinstance(profile, dict) else {},
                "background": True,
            }
        )
        return type(
            "ParallelLeaseTask",
            (),
            {
                "id": f"{task.id}:parallel:{index}",
                "session_id": getattr(task, "session_id", None),
                "trace_id": getattr(task, "trace_id", ""),
                "metadata": task_metadata,
            },
        )()

    @staticmethod
    def _question_lease_task(task: Any, question: AgentQuestion, *, metadata: dict[str, Any] | None = None) -> Any:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        runtime_metadata = dict(metadata or {})
        profile = runtime_metadata.get("resource_profile") or {}
        task_metadata.update(
            {
                "agentic_question_id": question.question_id,
                "agentic_question_target": question.to_agent,
                "agentic_runtime_metadata": runtime_metadata,
                "agentic_resource_profile": dict(profile) if isinstance(profile, dict) else {},
                "background": True,
            }
        )
        return type(
            "QuestionLeaseTask",
            (),
            {
                "id": f"{task.id}:question:{question.question_id}",
                "session_id": getattr(task, "session_id", None),
                "trace_id": getattr(task, "trace_id", ""),
                "metadata": task_metadata,
            },
        )()

    @staticmethod
    def _runtime_lease_task(
        task: Any,
        *,
        request_kind: str,
        request_ref: str,
        metadata: dict[str, Any],
        resource_profile: dict[str, Any],
    ) -> Any:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        task_metadata.update(
            {
                "agentic_request_kind": request_kind,
                "agentic_request_ref": request_ref,
                "agentic_runtime_metadata": metadata,
                "agentic_resource_profile": resource_profile,
                "background": True,
            }
        )
        return type(
            "RuntimeLeaseTask",
            (),
            {
                "id": f"{task.id}:{request_kind}:{request_ref}",
                "session_id": getattr(task, "session_id", None),
                "trace_id": getattr(task, "trace_id", ""),
                "metadata": task_metadata,
            },
        )()

    def _record_ai_event(
        self,
        task: Any,
        *,
        event_type: str,
        producer: str,
        severity: str,
        payload: dict[str, Any],
        evidence_ref: str | None = None,
    ) -> None:
        with suppress(Exception):
            self.store.record_ai_local_event(
                AiLocalEvent(
                    event_id=f"evt_{uuid.uuid4().hex}",
                    producer=producer,
                    type=event_type,
                    severity=severity,
                    trace_id=task.trace_id,
                    task_id=task.id,
                    payload=payload,
                    evidence_ref=evidence_ref,
                    created_at=time.time(),
                ),
                actor=producer,
            )

    def _record_material_model_selection_event(self, task: Any, metadata: dict[str, Any]) -> None:
        selection = metadata.get("agentic_model_selection")
        if not isinstance(selection, dict):
            return
        resource_profile = metadata.get("resource_profile") if isinstance(metadata.get("resource_profile"), dict) else {}
        lanes = metadata.get("material_model_lanes") if isinstance(metadata.get("material_model_lanes"), dict) else {}
        self._record_ai_event(
            task,
            event_type="material.model.selected",
            producer="agentic.runner",
            severity="info",
            payload={
                "model_profile": metadata.get("model_profile") or resource_profile.get("model_profile"),
                "material_lane": metadata.get("material_lane"),
                "material_model_lanes": lanes,
                "resource_profile": resource_profile,
                "selection": selection,
                "model": selection.get("model"),
                "backend_name": selection.get("backend_name"),
                "backend_type": selection.get("backend_type"),
                "timeout_seconds": metadata.get("timeout_seconds"),
                "transport_retries": metadata.get("transport_retries"),
            },
        )

    def _record_material_manifest_events(self, task: Any, manifest: dict[str, Any]) -> None:
        plan = manifest.get("plan") if isinstance(manifest.get("plan"), dict) else {}
        files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
        first_file = next((item for item in files if isinstance(item, dict)), {})
        model_runtime = manifest.get("model_runtime") if isinstance(manifest.get("model_runtime"), dict) else {}
        self._record_ai_event(
            task,
            event_type="material.plan.created",
            producer="reasoning_and_response",
            severity="info",
            payload={
                "schema_version": manifest.get("schema_version"),
                "project_root": manifest.get("project_root"),
                "planned_file_count": plan.get("planned_file_count", len(files)),
                "selected_file_count": plan.get("selected_file_count"),
                "generated_file_count": plan.get("generated_file_count"),
                "required_file_count": plan.get("required_file_count"),
                "model": first_file.get("model"),
                "model_profile": first_file.get("model_profile") or model_runtime.get("model_profile"),
                "backend": first_file.get("backend"),
                "model_runtime": model_runtime,
                "status": manifest.get("status"),
            },
        )
        for item in files[:50]:
            if not isinstance(item, dict):
                continue
            status = str(item.get("quality_status") or "")
            if status == "passed":
                event_type = "material.file.completed"
                severity = "info"
            elif status and status != "planned":
                event_type = "material.file.failed"
                severity = "medium"
            else:
                continue
            self._record_ai_event(
                task,
                event_type=event_type,
                producer="reasoning_and_response",
                severity=severity,
                payload={
                    "path": item.get("path"),
                    "purpose": item.get("purpose"),
                    "kind": item.get("kind"),
                    "required": item.get("required"),
                    "model": item.get("model"),
                    "model_profile": item.get("model_profile"),
                    "backend": item.get("backend"),
                    "transport_protocol": item.get("transport_protocol"),
                    "chunks_expected": item.get("chunks_expected"),
                    "chunks_received": item.get("chunks_received"),
                    "content_hash": item.get("content_hash"),
                    "quality_status": status,
                    "repair_round": item.get("repair_round"),
                },
            )

    def _load_agentic_memory_context(self, task: Any, *, limit: int = 12) -> list[dict[str, Any]]:
        from orchestrator.config import get_settings

        with suppress(Exception):
            limit = max(1, min(limit, int(get_settings().collaboration.max_memory_entries)))
        task_metadata = task.metadata if isinstance(getattr(task, "metadata", None), dict) else {}
        retrieval = self.store.retrieve_agent_memory(
            AgenticMemoryQuery(
                query_id=f"memq:{task.id}",
                task_id=task.id,
                trace_id=task.trace_id,
                query=str(task.goal or ""),
                kinds=["episodic", "semantic_ref", "procedural_ref", "preference_ref", "working"],
                evidence_refs=[str(ref) for ref in task_metadata.get("evidence_refs") or []],
                limit=limit,
                min_score=0.1,
            ),
            actor="agentic.runner",
        )
        memories: list[dict[str, Any]] = []
        for item in retrieval.get("memories") or []:
            memory = item.get("memory") if isinstance(item, dict) else None
            if not isinstance(memory, dict):
                continue
            memory_task_id = memory.get("task_id")
            memory_kind = str(memory.get("kind") or "")
            memories.append(
                {
                    "memory_id": memory.get("memory_id"),
                    "kind": memory_kind,
                    "owner": memory.get("owner"),
                    "source": memory.get("source"),
                    "content": str(memory.get("content") or "")[:2000],
                    "evidence_refs": memory.get("evidence_refs") or [],
                    "task_id": memory_task_id,
                    "trace_id": memory.get("trace_id"),
                    "redaction_status": memory.get("redaction_status"),
                    "storage_artifact_ref": memory.get("storage_artifact_ref"),
                    "semantic_ref": memory.get("semantic_ref") or {},
                    "metadata": memory.get("metadata") or {},
                    "retrieval": {
                        "query_id": retrieval.get("query", {}).get("query_id"),
                        "score": item.get("score"),
                        "reasons": item.get("reasons") or [],
                        "expired": bool(item.get("expired")),
                    },
                }
            )
        if memories:
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.memory.loaded",
                actor="agentic.runner",
                payload={
                    "memory_ids": [item.get("memory_id") for item in memories],
                    "kinds": sorted({str(item.get("kind") or "") for item in memories}),
                    "count": len(memories),
                    "query_id": retrieval.get("query", {}).get("query_id"),
                    "scores": {
                        str(item.get("memory_id")): item.get("retrieval", {}).get("score")
                        for item in memories
                    },
                    "reasons": {
                        str(item.get("memory_id")): item.get("retrieval", {}).get("reasons")
                        for item in memories
                    },
                },
            )
        return memories

    @staticmethod
    def _resolve_api_call_target(action: Any) -> ApiCallTarget | None:
        endpoint = str(action.endpoint or "").strip()
        if endpoint.startswith(("http://", "https://")):
            return None
        metadata = dict(action.metadata or {})
        explicit_capability_metadata = metadata.get("capability_metadata")
        if isinstance(explicit_capability_metadata, dict):
            with suppress(Exception):
                capability = CapabilityActionMetadata.model_validate(explicit_capability_metadata)
                service = capability.owner.strip()
                path = capability.endpoint.strip()
                if ":" in path and not path.startswith("/"):
                    candidate, _, candidate_path = path.partition(":")
                    service = candidate.strip() or service
                    path = candidate_path.strip()
                if not path:
                    path = endpoint
                if not path.startswith("/"):
                    path = f"/{path}"
                return ApiCallTarget(
                    service_name=service,
                    path=path,
                    policy_action=capability.policy_action,
                    capability_metadata=capability.model_dump(mode="json"),
                )

        service = str(metadata.get("service") or metadata.get("feature") or "").strip()
        capability_id = str(metadata.get("capability_id") or "").strip()
        path = endpoint
        if capability_id:
            manifest_target = AgenticRunner._target_for_action_capability(capability_id)
            if manifest_target is not None:
                return manifest_target
        if ":" in endpoint and not endpoint.startswith("/"):
            candidate, _, candidate_path = endpoint.partition(":")
            service = service or candidate.strip()
            path = candidate_path.strip()
        if capability_id and not service:
            service = AgenticRunner._service_for_feature_capability(capability_id) or ""
        if not service or not path:
            return None
        if not path.startswith("/"):
            path = f"/{path}"
        policy_action = str(
            metadata.get("policy_action")
            or ("config.write" if action.expected_effect in {"write", "destructive"} else f"{service}.invoke")
        )
        capability_metadata: dict[str, Any] = {}
        if capability_id:
            capability_metadata = {
                "capability_id": capability_id,
                "owner": service,
                "endpoint": f"{service}:{path}",
                "policy_action": policy_action,
                "risk_level": str(metadata.get("risk_level") or "medium"),
                "supported_action_types": list(metadata.get("supported_action_types") or ["api_call"]),
                "resource_profile": dict(metadata.get("resource_profile") or {}),
                "evidence_types": list(metadata.get("evidence_types") or []),
                "writes_allowed": action.expected_effect in {"write", "destructive"},
                "idempotency_policy": str(metadata.get("idempotency_policy") or "required_for_writes"),
            }
        return ApiCallTarget(service_name=service, path=path, policy_action=policy_action, capability_metadata=capability_metadata)

    @staticmethod
    def _target_for_action_capability(capability_id: str) -> ApiCallTarget | None:
        try:
            from orchestrator.agentic.tool_envelope import runtime_tool_envelope

            envelope = runtime_tool_envelope(capability_id)
            if envelope is None or envelope.kind != "action":
                return None
            metadata = envelope.to_public_dict()
            service = str(envelope.transport.get("service") or envelope.owner)
            path = str(envelope.endpoint or envelope.transport.get("path") or "")
            if ":" in path and not path.startswith("/"):
                candidate, _, candidate_path = path.partition(":")
                service = candidate.strip() or service
                path = candidate_path.strip()
            if not path.startswith("/"):
                path = f"/{path}"
            return ApiCallTarget(
                service_name=service,
                path=path,
                policy_action=envelope.policy_action,
                capability_metadata=metadata,
                timeout_seconds=envelope.timeout_seconds,
            )
        except Exception:
            return None

    @staticmethod
    def _service_for_feature_capability(capability_id: str) -> str | None:
        try:
            from orchestrator.capabilities.catalog import service_capability_manifests

            for manifest in service_capability_manifests():
                if manifest.kind != "feature":
                    continue
                if capability_id in {manifest.service_name, manifest.capability_id, *manifest.capabilities}:
                    return manifest.service_name
        except Exception:
            return None
        return None

    @staticmethod
    def _agentic_capability_candidates() -> tuple[CapabilityCandidate, ...]:
        try:
            from orchestrator.agentic.tool_envelope import service_tool_envelopes
        except Exception:
            return ()
        agent_model_metadata: dict[str, dict[str, Any]] = {}
        with suppress(Exception):
            from orchestrator.registry import get_registry

            registry = get_registry()
            for agent_name, cfg in registry.get_all_agent_configs().items():
                agent_model_metadata[agent_name] = {
                    "llm_model": cfg.model,
                    "llm_backend_type": cfg.backend_type,
                    "llm_timeout": cfg.timeout,
                }
        candidates: list[CapabilityCandidate] = []
        for envelope in service_tool_envelopes():
            if envelope.service_kind != "agent" or not envelope.service_name:
                continue
            metadata = envelope.to_public_dict()
            metadata["kind"] = envelope.service_kind
            metadata.update(agent_model_metadata.get(envelope.service_name, {}))
            candidates.append(
                CapabilityCandidate(
                    name=envelope.service_name,
                    kind=envelope.service_kind,
                    capabilities=tuple(envelope.capabilities),
                    description=envelope.description,
                    timeout_seconds=envelope.timeout_seconds,
                    metadata=metadata,
                )
            )
        return tuple(candidates)

    @staticmethod
    def _agent_client() -> Any:
        from orchestrator.dispatch.agent_client import AgentClient
        from orchestrator.factory import _build_service_registry

        return AgentClient(_build_service_registry())

    @staticmethod
    def _feature_client() -> Any:
        from orchestrator.dispatch.feature_client import FeatureClient
        from orchestrator.factory import _build_service_registry

        return FeatureClient(_build_service_registry())

    def _execute_shell_action_batch(self, task: Any, actions: list[Any]) -> None:
        if not actions:
            return
        session_id = None
        try:
            from orchestrator.agentic.tools.command.service import CommandToolService

            service = CommandToolService(store=self.store)
            first = actions[0]
            context_profile = self._shell_action_context_profile(task, first)
            session = service.create_session(
                context_profile=context_profile,
                cwd=first.cwd,
                task_id=task.id,
                trace_id=task.trace_id,
                metadata={
                    "agent_action_ids": [action.action_id for action in actions],
                    "context_profile": context_profile,
                },
            )
            session_id = str(session["id"])
            for action in actions:
                run = service.run_command(
                    session_id,
                    command=action.command,
                    cwd=action.cwd,
                    task_id=task.id,
                    trace_id=task.trace_id,
                    metadata=getattr(action, "metadata", {}) or {},
                )
                self.store.record_event(
                    task_id=task.id,
                    trace_id=task.trace_id,
                    event_type="agent.action.policy_checked",
                    actor="agentic.runner",
                    payload={
                        "action_id": action.action_id,
                        "action_type": action.type,
                        "policy_decision": run.get("policy_decision"),
                        "risk_level": run.get("risk_level"),
                        "command_run_id": run.get("id"),
                        "context_profile": context_profile,
                    },
                )
                self._record_material_command_result_event(
                    task,
                    action=action,
                    run=run,
                    context_profile=context_profile,
                )
                self.store.record_agent_action_result(
                    ActionResult(
                        action_id=action.action_id,
                        action_type=action.type,
                        status=self._action_result_status_for_command(run),
                        observation=self._command_observation(run),
                        result={
                            "command_run_id": run.get("id"),
                            "exit_code": run.get("exit_code"),
                            "context_profile": context_profile,
                        },
                        error={"type": "CommandError", "message": str(run.get("error"))} if run.get("error") else None,
                        policy_decision={
                            "decision": run.get("policy_decision"),
                            "risk_level": run.get("risk_level"),
                            "approval_id": run.get("approval_id"),
                        },
                    ),
                    task_id=task.id,
                    trace_id=task.trace_id,
                )
                if self._action_result_status_for_command(run) != "completed":
                    break
        except Exception as exc:
            for action in actions:
                self.store.record_event(
                    task_id=task.id,
                    trace_id=task.trace_id,
                    event_type="agent.action.policy_checked",
                    actor="agentic.runner",
                    payload={
                        "action_id": action.action_id,
                        "action_type": action.type,
                        "decision": "blocked",
                        "reason": type(exc).__name__,
                    },
                )
                self.store.record_agent_action_result(
                    ActionResult(
                        action_id=action.action_id,
                        action_type=action.type,
                        status="blocked",
                        observation="Shell command action could not be executed by the governed command service.",
                        error={"type": type(exc).__name__, "message": str(exc)[:1000]},
                    ),
                    task_id=task.id,
                    trace_id=task.trace_id,
                )
        finally:
            if session_id:
                with suppress(Exception):
                    CommandToolService(store=self.store).close_session(session_id, reason="agent_action_complete")

    def _shell_action_context_profile(self, task: Any, action: Any) -> str | None:
        action_metadata = getattr(action, "metadata", {}) or {}
        if isinstance(action_metadata, dict) and action_metadata.get("context_profile"):
            return str(action_metadata["context_profile"])
        task_metadata = getattr(task, "metadata", {}) or {}
        if self._task_requires_material_output(task):
            return str(task_metadata.get("workspace_generation_context_profile") or "workspace_generation")
        return None

    def _record_material_command_result_event(
        self,
        task: Any,
        *,
        action: Any,
        run: dict[str, Any],
        context_profile: str,
    ) -> None:
        action_metadata = getattr(action, "metadata", {}) or {}
        action_metadata = action_metadata if isinstance(action_metadata, dict) else {}
        run_metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        classification = run_metadata.get("classification") if isinstance(run_metadata.get("classification"), dict) else {}
        classification_metadata = (
            classification.get("metadata") if isinstance(classification.get("metadata"), dict) else {}
        )
        effective_profile = (
            context_profile
            or str(action_metadata.get("context_profile") or "")
            or str(run_metadata.get("context_profile") or "")
            or str(classification_metadata.get("context_profile") or "")
            or str(run.get("context_profile") or "")
        )
        if effective_profile != "workspace_generation":
            return
        action_id = str(getattr(action, "action_id", "") or "")
        status = self._action_result_status_for_command(run)
        validation_profile = str(
            action_metadata.get("validation_profile")
            or run_metadata.get("validation_profile")
            or run_metadata.get("workspace_execution_validation_profile")
            or ""
        )
        if action_id == "package-generated-artifact-root":
            self._record_material_package_event(task, action_id=action_id, run=run, status=status)
            return
        if not validation_profile and not action_id.startswith("validate-"):
            return
        event_type = "material.validation.passed" if status == "completed" else "material.validation.failed"
        payload: dict[str, Any] = {
            "schema_version": "material_validation_result.v1",
            "action_id": action_id,
            "validation_profile": validation_profile,
            "command_run_id": run.get("id"),
            "status": status,
            "command_status": run.get("status"),
            "exit_code": run.get("exit_code"),
            "cwd": run.get("cwd"),
            "duration_ms": run.get("duration_ms"),
            "stdout_ref": run_metadata.get("stdout_ref") or run_metadata.get("workspace_execution_stdout_ref"),
            "stderr_ref": run_metadata.get("stderr_ref") or run_metadata.get("workspace_execution_stderr_ref"),
            "output_truncated": bool(run.get("output_truncated") or run_metadata.get("output_truncated")),
            "validation_profile_policy": run_metadata.get("workspace_execution_validation_profile_policy"),
        }
        if status != "completed":
            payload["issue"] = self._material_validation_issue(
                action_id=action_id,
                validation_profile=validation_profile,
                run=run,
                metadata=run_metadata,
            )
        self._record_ai_event(
            task,
            event_type=event_type,
            producer="workspace_execution",
            severity="info" if status == "completed" else "medium",
            payload=payload,
        )

    def _record_material_package_event(
        self,
        task: Any,
        *,
        action_id: str,
        run: dict[str, Any],
        status: str,
    ) -> None:
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        event_type = "material.package.created" if status == "completed" else "material.package.failed"
        self._record_ai_event(
            task,
            event_type=event_type,
            producer="workspace_execution",
            severity="info" if status == "completed" else "medium",
            payload={
                "schema_version": "material_package_result.v1",
                "action_id": action_id,
                "command_run_id": run.get("id"),
                "status": status,
                "command_status": run.get("status"),
                "exit_code": run.get("exit_code"),
                "cwd": run.get("cwd"),
                "duration_ms": run.get("duration_ms"),
                "artifacts": metadata.get("workspace_execution_artifacts") or metadata.get("artifacts") or [],
                "stdout_ref": metadata.get("stdout_ref") or metadata.get("workspace_execution_stdout_ref"),
                "stderr_ref": metadata.get("stderr_ref") or metadata.get("workspace_execution_stderr_ref"),
            },
        )

    @staticmethod
    def _material_validation_issue(
        *,
        action_id: str,
        validation_profile: str,
        run: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_error = metadata.get("workspace_execution_error")
        workspace_error = workspace_error if isinstance(workspace_error, dict) else {}
        code = str(workspace_error.get("code") or "")
        if not code:
            status = str(run.get("status") or "")
            if status == "timeout":
                code = "validation_timed_out"
            elif status in {"denied", "blocked"}:
                code = "validation_command_blocked"
            else:
                code = "validation_command_failed"
        return {
            "schema_version": "material_issue.v1",
            "code": code,
            "scope": "validation",
            "gate": "sandbox_validation",
            "severity": "error",
            "status": "blocking",
            "owner": "workspace_execution",
            "path": str(run.get("cwd") or ""),
            "details": {
                "action_id": action_id,
                "validation_profile": validation_profile,
                "command_run_id": run.get("id"),
                "exit_code": run.get("exit_code"),
                "error": workspace_error or run.get("error"),
            },
        }

    def _execute_shell_action(self, task: Any, action: Any) -> None:
        session_id = None
        try:
            from orchestrator.agentic.tools.command.service import CommandToolService

            service = CommandToolService(store=self.store)
            session = service.create_session(
                context_profile=self._shell_action_context_profile(task, action),
                cwd=action.cwd,
                task_id=task.id,
                trace_id=task.trace_id,
                metadata={"agent_action_id": action.action_id},
            )
            session_id = str(session["id"])
            run = service.run_command(
                session_id,
                command=action.command,
                cwd=action.cwd,
                task_id=task.id,
                trace_id=task.trace_id,
                metadata=getattr(action, "metadata", {}) or {},
            )
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.action.policy_checked",
                actor="agentic.runner",
                payload={
                    "action_id": action.action_id,
                    "action_type": action.type,
                    "policy_decision": run.get("policy_decision"),
                    "risk_level": run.get("risk_level"),
                    "command_run_id": run.get("id"),
                },
            )
            self._record_material_command_result_event(
                task,
                action=action,
                run=run,
                context_profile=self._shell_action_context_profile(task, action) or "",
            )
            self.store.record_agent_action_result(
                ActionResult(
                    action_id=action.action_id,
                    action_type=action.type,
                    status=self._action_result_status_for_command(run),
                    observation=self._command_observation(run),
                    result={"command_run_id": run.get("id"), "exit_code": run.get("exit_code")},
                    error={"type": "CommandError", "message": str(run.get("error"))} if run.get("error") else None,
                    policy_decision={
                        "decision": run.get("policy_decision"),
                        "risk_level": run.get("risk_level"),
                        "approval_id": run.get("approval_id"),
                    },
                ),
                task_id=task.id,
                trace_id=task.trace_id,
            )
        except Exception as exc:
            self.store.record_event(
                task_id=task.id,
                trace_id=task.trace_id,
                event_type="agent.action.policy_checked",
                actor="agentic.runner",
                payload={
                    "action_id": action.action_id,
                    "action_type": action.type,
                    "decision": "blocked",
                    "reason": type(exc).__name__,
                },
            )
            self.store.record_agent_action_result(
                ActionResult(
                    action_id=action.action_id,
                    action_type=action.type,
                    status="blocked",
                    observation="Shell command action could not be executed by the governed command service.",
                    error={"type": type(exc).__name__, "message": str(exc)[:1000]},
                ),
                task_id=task.id,
                trace_id=task.trace_id,
            )
        finally:
            if session_id:
                with suppress(Exception):
                    CommandToolService(store=self.store).close_session(session_id, reason="agent_action_complete")

    @staticmethod
    def _action_result_status_for_command(run: dict[str, Any]) -> str:
        status = str(run.get("status") or "")
        if status == "completed":
            return "completed"
        if status == "waiting_approval":
            return "waiting_approval"
        if status == "denied":
            return "denied"
        return "failed"

    @staticmethod
    def _command_observation(run: dict[str, Any]) -> str:
        stdout = str(run.get("stdout_preview") or "").strip()
        stderr = str(run.get("stderr_preview") or "").strip()
        if stdout and stderr:
            return f"stdout: {stdout}\nstderr: {stderr}"
        if stdout:
            return stdout
        if stderr:
            return stderr
        return str(run.get("error") or run.get("status") or "")

    @staticmethod
    def _task_requires_material_output(task: Any) -> bool:
        return task_requires_material_output(
            str(getattr(task, "goal", "") or ""),
            getattr(task, "metadata", {}) or {},
        )

    def _material_completion_error(self, task: Any) -> dict[str, Any] | None:
        if not self._task_requires_material_output(task):
            return None
        if self._has_material_completion_evidence(task):
            return None
        metadata = getattr(task, "metadata", {}) or {}
        blocked = self._material_generation_blocked_error(task)
        if blocked:
            return blocked
        blocked = self._material_generation_blocked_event_error(task)
        if blocked:
            return blocked
        return {
            "type": "MaterialOutputMissing",
            "message": "Task requires material output, but no successful effectful action or artifact evidence was recorded.",
            "expected_artifact_root": metadata.get("expected_artifact_root"),
            "evidence_required": metadata.get("completion_evidence_required") or "effectful_action_or_artifact",
        }

    def _material_error_can_trigger_repair(self, task: Any, error: dict[str, Any]) -> bool:
        if error.get("type") != "MaterialGenerationBlocked":
            return False
        try:
            if self.store.list_ai_local_events(task_id=task.id, event_type="material.repair.started", limit=1):
                return False
        except Exception:
            return False
        issues = error.get("issues") if isinstance(error.get("issues"), list) else []
        return any(
            isinstance(issue, dict)
            and (
                issue.get("gate") == "sandbox_validation"
                or issue.get("owner") == "workspace_execution"
            )
            for issue in issues
        )

    def _material_generation_blocked_error(self, task: Any) -> dict[str, Any] | None:
        manifest = self._latest_material_manifest(task)
        if not manifest:
            return None
        status = str(manifest.get("status") or "")
        quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
        plan = manifest.get("plan") if isinstance(manifest.get("plan"), dict) else {}
        blocking_issues = {
            "safe_to_write_errors": quality.get("safe_to_write_errors") or {},
            "project_errors": quality.get("project_errors") or {},
            "residual_file_errors": quality.get("residual_file_errors") or {},
            "missing_required_specs": plan.get("missing_required_specs") or [],
        }
        typed_issues = [
            issue
            for issue in manifest.get("issues", [])
            if isinstance(issue, dict)
            and (
                issue.get("status") == "blocking"
                or issue.get("severity") in {"critical", "error", "blocking_completion", "security_block"}
            )
        ]
        validation_issues = self._material_failed_validation_issues(task)
        typed_issues.extend(validation_issues)
        typed_issues.extend(self._material_terminal_event_issues(task))
        has_blocking_issue = any(bool(value) for value in blocking_issues.values()) or bool(typed_issues)
        blocked_statuses = {
            "blocked",
            "blocked_before_workspace",
            "ready_for_workspace_validation",
            "blocked_by_policy",
            "blocked_by_vm_isolation",
            "blocked_by_sandbox_profile",
            "blocked_by_contract",
            "blocked_by_missing_tool",
            "failed_closed",
            "stalled",
        }
        if status not in blocked_statuses and not has_blocking_issue:
            return None
        command_runs = self.store.list_command_runs(task_id=task.id, limit=100)
        workspace_runs = [
            run
            for run in command_runs
            if (
                run.get("context_profile") == "workspace_generation"
                or (run.get("metadata") if isinstance(run.get("metadata"), dict) else {}).get("context_profile")
                == "workspace_generation"
            )
        ]
        return {
            "type": "MaterialGenerationBlocked",
            "message": "Material generation produced a manifest but did not reach artifact-ready completion.",
            "expected_artifact_root": (getattr(task, "metadata", {}) or {}).get("expected_artifact_root"),
            "manifest_status": status or "unknown",
            "project_root": manifest.get("project_root"),
            "issues": typed_issues,
            "blocking_issues": blocking_issues,
            "workspace_command_runs": len(workspace_runs),
            "artifact": manifest.get("artifact") if isinstance(manifest.get("artifact"), dict) else {},
        }

    def _material_generation_blocked_event_error(self, task: Any) -> dict[str, Any] | None:
        issues = self._material_terminal_event_issues(task)
        if not issues:
            return None
        metadata = getattr(task, "metadata", {}) or {}
        latest_issue = issues[0]
        return {
            "type": "MaterialGenerationBlocked",
            "message": "Material generation was blocked by the material execution kernel before artifact-ready completion.",
            "expected_artifact_root": metadata.get("expected_artifact_root"),
            "manifest_status": latest_issue.get("material_status") or latest_issue.get("source_event_type") or "unknown",
            "project_root": None,
            "issues": issues,
            "blocking_issues": {
                "safe_to_write_errors": {},
                "project_errors": {},
                "residual_file_errors": {},
                "missing_required_specs": [],
            },
            "workspace_command_runs": 0,
            "artifact": {},
        }

    def _material_failed_validation_issues(self, task: Any) -> list[dict[str, Any]]:
        try:
            events = self.store.list_ai_local_events(
                task_id=task.id,
                event_type="material.validation.failed",
                limit=50,
            )
        except Exception:
            return []
        issues: list[dict[str, Any]] = []
        for row in events:
            event = row.get("event") if isinstance(row, dict) else None
            payload = event.get("payload") if isinstance(event, dict) else None
            if not isinstance(payload, dict):
                payload = row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else None
            issue = payload.get("issue") if isinstance(payload, dict) else None
            if isinstance(issue, dict):
                issues.append(issue)
        return issues

    def _material_terminal_event_issues(self, task: Any) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event_type in MATERIAL_TERMINAL_ISSUE_EVENTS:
            try:
                events = self.store.list_ai_local_events(task_id=task.id, event_type=event_type, limit=20)
            except Exception:
                continue
            for row in events:
                event = row.get("event") if isinstance(row, dict) else None
                payload = event.get("payload") if isinstance(event, dict) else None
                if not isinstance(payload, dict):
                    payload = row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else None
                if not isinstance(payload, dict):
                    continue
                raw_issues = payload.get("issues")
                candidate_issues = raw_issues if isinstance(raw_issues, list) else [payload.get("issue")]
                if not any(isinstance(issue, dict) for issue in candidate_issues):
                    synthetic = self._material_synthetic_terminal_issue(event_type, payload)
                    candidate_issues = [synthetic] if synthetic else []
                for issue in candidate_issues:
                    if not isinstance(issue, dict):
                        continue
                    material_status = str(payload.get("status") or payload.get("phase") or "")
                    normalized = {
                        **issue,
                        "source_event_type": event_type,
                        "material_status": material_status or event_type.removeprefix("material."),
                    }
                    if "code" not in normalized and normalized.get("issue_type"):
                        normalized["code"] = normalized["issue_type"]
                    try:
                        key = json.dumps(normalized, sort_keys=True, default=str)
                    except (TypeError, ValueError):
                        key = str(normalized)
                    if key in seen:
                        continue
                    seen.add(key)
                    issues.append(normalized)
        return issues

    @staticmethod
    def _material_synthetic_terminal_issue(event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if event_type != "material.kernel.blocked":
            return None
        error = str(payload.get("error") or "").strip()
        status = str(payload.get("status") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        code = "material_kernel_blocked"
        message = "Material execution kernel blocked before artifact-ready completion."
        if "session not found" in error.lower() or "404" in error:
            code = "material_session_lost"
            message = "Material execution session disappeared before completion."
        return {
            "issue_id": f"{code}:{session_id or 'unknown'}",
            "issue_type": code,
            "code": code,
            "severity": "blocking_completion",
            "status": "blocking",
            "message": message,
            "owner": "orchestrator/agentic",
            "session_id": session_id or None,
            "kernel_status": status or None,
            "error": error[:1000] if error else None,
        }

    def _latest_material_manifest(self, task: Any) -> dict[str, Any] | None:
        try:
            events = self.store.list_ai_local_events(
                task_id=task.id,
                event_type="material.manifest.created",
                limit=1,
            )
        except Exception:
            return None
        if not events:
            return None
        event = events[0].get("event") if isinstance(events[0], dict) else None
        payload = event.get("payload") if isinstance(event, dict) else None
        return payload if isinstance(payload, dict) else None

    def _has_material_completion_evidence(self, task: Any) -> bool:
        expected_root = self._expected_material_artifact_root(task)
        manifest = self._latest_material_manifest(task)
        if isinstance(manifest, dict) and self._material_manifest_has_completion_evidence(
            manifest,
            expected_root=expected_root,
        ):
            return True
        latest_agent_state = self.store.latest_agent_state_snapshot(task.id)
        state = latest_agent_state.get("state") if isinstance(latest_agent_state, dict) else None
        if isinstance(state, dict):
            for action in state.get("completed_actions") or []:
                if not isinstance(action, dict):
                    continue
                if action.get("status") != "completed":
                    continue
                result = action.get("result") if isinstance(action.get("result"), dict) else {}
                metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
                if expected_root:
                    if self._artifacts_include_expected_root(result.get("workspace_execution_artifacts"), expected_root):
                        return True
                    continue
                if result.get("context_profile") == "workspace_generation":
                    return True
                if result.get("workspace_execution_diff_ref") or result.get("workspace_execution_artifacts"):
                    return True
                if metadata.get("material_output_evidence") is True:
                    return True

        for run in self.store.list_command_runs(task_id=task.id, limit=100):
            if run.get("status") != "completed":
                continue
            metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
            classification = metadata.get("classification") if isinstance(metadata.get("classification"), dict) else {}
            context_profile = (
                metadata.get("context_profile")
                or classification.get("metadata", {}).get("context_profile")
                or run.get("context_profile")
            )
            if context_profile == "workspace_generation":
                if not expected_root:
                    return True
            artifacts = metadata.get("workspace_execution_artifacts")
            if expected_root:
                if self._artifacts_include_expected_root(artifacts, expected_root):
                    return True
                continue
            if isinstance(artifacts, list) and artifacts:
                return True
            if metadata.get("workspace_execution_diff_ref"):
                return True
            if str(run.get("action") or "").startswith("workspace.sandbox."):
                return True
            if str(run.get("action") or "") == "command.run.medium":
                return True
        return False

    @staticmethod
    def _expected_material_artifact_root(task: Any) -> str:
        metadata = getattr(task, "metadata", {}) or {}
        raw = str(metadata.get("expected_artifact_root") or metadata.get("requested_project") or "").strip()
        normalized = raw.replace("\\", "/").strip("/")
        if not normalized or normalized in {".", ".."} or ".." in normalized.split("/"):
            return ""
        return normalized

    @staticmethod
    def _artifacts_include_expected_root(artifacts: Any, expected_root: str) -> bool:
        if not isinstance(artifacts, list) or not expected_root:
            return False
        root = expected_root.rstrip("/")
        archive_names = {f"{root}.tar.gz", f"{root}.tgz", f"{root}.zip"}
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            path = str(artifact.get("path") or "").replace("\\", "/").strip("/")
            basename = path.rsplit("/", 1)[-1]
            if path == root or path.startswith(f"{root}/") or path in archive_names or basename in archive_names:
                return True
        return False

    @staticmethod
    def _material_manifest_has_completion_evidence(manifest: dict[str, Any], *, expected_root: str) -> bool:
        if str(manifest.get("status") or "") != "completed":
            return False
        sandbox = manifest.get("sandbox") if isinstance(manifest.get("sandbox"), dict) else {}
        if sandbox.get("host_execution_used") is True:
            return False
        if sandbox.get("docker_socket_available_to_generated_project") is True:
            return False
        if sandbox.get("cleanup_recorded") is not True:
            return False
        artifact = manifest.get("artifact") if isinstance(manifest.get("artifact"), dict) else {}
        artifact_path = str(artifact.get("path") or "")
        if not artifact_path or not artifact.get("sha256"):
            return False
        if not (
            artifact.get("storage_object_ref")
            and artifact.get("materialized_path")
            and artifact.get("materialized_sha256")
            and artifact.get("extracted_path")
        ):
            return False
        if expected_root and not AgenticRunner._artifacts_include_expected_root([artifact], expected_root):
            return False
        validations = manifest.get("validations") if isinstance(manifest.get("validations"), list) else []
        if not validations:
            return False
        if any(isinstance(item, dict) and str(item.get("status") or "") not in {"passed", "completed"} for item in validations):
            return False
        issues = manifest.get("issues") if isinstance(manifest.get("issues"), list) else []
        return not any(
            isinstance(issue, dict) and issue.get("severity") in {"blocking_completion", "security_block"}
            for issue in issues
        )

    def _fail_task(self, task_id: str, *, trace_id: str, error: dict[str, Any]) -> None:
        if self._task_is_terminal(task_id):
            return
        self.store.update_task(task_id, status=TaskStatus.FAILED.value, error=error)
        self.store.record_event(
            task_id=task_id,
            event_type="task.failed",
            actor="agentic.runner",
            payload=error,
            trace_id=trace_id,
        )

    def _fail_task_dict(self, task: dict[str, Any], *, error: dict[str, Any]) -> None:
        self._fail_task(str(task["id"]), trace_id=str(task.get("trace_id") or ""), error=error)

    def _defer_if_runtime_blocked(self, task: Any) -> bool:
        flag = self.store.get_runtime_flag("block_heavy_tasks")
        if flag is None or not self._is_heavy_or_background(task):
            return False
        self.store.defer_task(
            task.id,
            reason="runtime_flag:block_heavy_tasks",
            retry_after_seconds=30,
            metadata={"runtime_block": flag},
        )
        return True

    @staticmethod
    def _is_heavy_or_background(task: Any) -> bool:
        metadata = getattr(task, "metadata", {}) or {}
        budget = getattr(task, "budget", {}) or {}
        return bool(
            metadata.get("heavy")
            or metadata.get("background")
            or metadata.get("requires_external_storage")
            or metadata.get("storage_required")
            or budget.get("requires_external_storage")
        )

    def _normalize_lease(self, raw: Any) -> LeaseOutcome:
        if isinstance(raw, LeaseOutcome):
            return raw
        if raw is None:
            return LeaseOutcome(decision="defer", reason="resource_governor_unavailable", retry_after_seconds=30)
        if hasattr(raw, "model_dump"):
            data = raw.model_dump(mode="json")
        elif hasattr(raw, "dict"):
            data = raw.dict()
        elif isinstance(raw, dict):
            data = raw
        else:
            data = {}
        decision = str(data.get("decision") or "granted").lower()
        return LeaseOutcome(
            decision=decision,
            lease_id=data.get("lease_id"),
            ttl_seconds=data.get("ttl_seconds"),
            heartbeat_interval_seconds=data.get("heartbeat_interval_seconds"),
            limits=data.get("limits") or {},
            reason=str(data.get("reason") or ""),
            retry_after_seconds=data.get("retry_after_seconds"),
        )

    def _request_resource_lease(self, task: Any, request_id: str) -> Any:
        try:
            from orchestrator.resource_governor import get_resource_governor_service
            from orchestrator.resource_governor.schemas import (
                Capability,
                Lane,
                LeaseRequest,
                LeaseScope,
                QualityImpact,
                QualityPolicy,
                ResourceClass,
            )
            task_metadata = dict(getattr(task, "metadata", {}) or {})
            resource_profile = self._lease_resource_profile(task_metadata)
            lane = self._lease_lane(Lane, resource_profile)
            resource_class = self._lease_resource_class(ResourceClass, resource_profile)
            capability = self._lease_capability(Capability, resource_profile)
            foreground = lane == Lane.INTERACTIVE
            runtime_metadata = task_metadata.get("agentic_runtime_metadata")
            runtime_timeout = runtime_metadata.get("timeout_seconds") if isinstance(runtime_metadata, dict) else None
            timeout_seconds = self._safe_int(task_metadata.get("timeout_seconds") or runtime_timeout)
            estimated_duration = timeout_seconds or self.task_timeout_seconds

            return get_resource_governor_service().request_lease(
                LeaseRequest(
                    idempotency_key=f"agentic:{task.id}:runner",
                    requester="symbiont",
                    component="agentic.runner",
                    lane=lane,
                    lease_scope=LeaseScope.REQUEST,
                    resource_class=resource_class,
                    capability=capability,
                    estimated_duration_seconds=estimated_duration,
                    requested_ttl_seconds=min(estimated_duration, 300),
                    preemptible=not foreground,
                    quality_policy=QualityPolicy.PRESERVE if foreground else QualityPolicy.DEGRADE_ALLOWED,
                    estimated_quality_impact=QualityImpact.LOW if foreground else QualityImpact.MEDIUM,
                    request_id=request_id,
                    session_id=task.session_id,
                )
            )
        except Exception as exc:
            self.store.record_event(
                task_id=getattr(task, "id", None),
                event_type="resource.lease_skipped",
                actor="agentic.runner",
                payload={"reason": str(exc)[:300]},
                trace_id=getattr(task, "trace_id", None),
            )
            return LeaseOutcome(decision="defer", reason="resource_governor_unavailable", retry_after_seconds=30)

    def _record_lease(self, task_id: str, lease: LeaseOutcome, *, capability: str = "deep_reasoning_batch") -> None:
        self.store.record_resource_lease(
            task_id=task_id,
            lease_id=lease.lease_id,
            capability=capability,
            decision=lease.decision,
            status="active" if lease.granted else "not_granted",
            payload={"lease": lease.__dict__},
            expires_at=(time.time() + lease.ttl_seconds) if lease.ttl_seconds else None,
        )

    @staticmethod
    def _lease_resource_profile(task_metadata: dict[str, Any]) -> dict[str, Any]:
        resource_profile = task_metadata.get("agentic_resource_profile") or task_metadata.get("resource_profile") or {}
        if not isinstance(resource_profile, dict):
            resource_profile = {}
        profile = dict(resource_profile)
        if profile.get("lane"):
            return profile
        if task_metadata.get("background") or task_metadata.get("heavy"):
            profile["lane"] = "background"
        elif task_metadata.get("material_output_required") or task_metadata.get("workspace_generation_context_profile") == "workspace_generation":
            profile["lane"] = "interactive"
            profile.setdefault("resource_class", "gpu_llm")
            profile.setdefault("capability", "material_generation")
        else:
            profile["lane"] = "background"
        return profile

    @staticmethod
    def _lease_lane(Lane: Any, profile: dict[str, Any]) -> Any:
        value = str(profile.get("lane") or "background").strip().lower()
        aliases = {
            "fast": "interactive",
            "cpu": "background",
            "rag": "interactive_enrichment",
            "storage_io": "storage",
            "gpu_audio": "heavy_gpu",
        }
        return Lane(aliases.get(value, value))

    @staticmethod
    def _lease_resource_class(ResourceClass: Any, profile: dict[str, Any]) -> Any:
        value = str(profile.get("resource_class") or "").strip().lower()
        aliases = {
            "gpu_llm": "model_runtime",
            "gpu_audio": "vram",
            "rag": "model_runtime",
            "cpu": "cpu",
            "cpu_io": "cpu",
            "storage_io": "io_write",
        }
        return ResourceClass(aliases.get(value, value or "model_runtime"))

    @staticmethod
    def _lease_capability(Capability: Any, profile: dict[str, Any]) -> Any:
        value = str(profile.get("governor_capability") or profile.get("capability") or "").strip().lower()
        if not value:
            resource_class = str(profile.get("resource_class") or "").strip().lower()
            lane = str(profile.get("lane") or "").strip().lower()
            if resource_class == "gpu_audio":
                value = "audio_transcribe_gpu"
            elif resource_class == "storage_io":
                value = "storage_archive"
            elif resource_class == "rag":
                value = "rerank"
            elif lane == "fast":
                value = "chat_stream"
            else:
                value = "deep_reasoning_batch"
        return Capability(value)

    @staticmethod
    def _lease_capability_name(profile: dict[str, Any]) -> str:
        value = str(profile.get("governor_capability") or profile.get("capability") or "").strip().lower()
        if value:
            return value
        resource_class = str(profile.get("resource_class") or "").strip().lower()
        if resource_class == "gpu_audio":
            return "audio_transcribe_gpu"
        if resource_class == "storage_io":
            return "storage_archive"
        if resource_class == "rag":
            return "rerank"
        return "deep_reasoning_batch"

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    def _start_lease_heartbeat(self, lease: LeaseOutcome) -> asyncio.Task | None:
        if not lease.lease_id or not lease.granted:
            return None
        interval = max(1, int(lease.heartbeat_interval_seconds or max(1, (lease.ttl_seconds or 30) // 3)))
        return asyncio.create_task(self._lease_heartbeat_loop(lease.lease_id, interval), name=f"agentic-lease-{lease.lease_id}")

    def _start_task_heartbeat(self, task_id: str, trace_id: str, run_id: str, *, stage: str) -> asyncio.Task | None:
        return asyncio.create_task(
            self._task_heartbeat_loop(task_id, trace_id, run_id, stage=stage),
            name=f"agentic-task-heartbeat-{task_id}",
        )

    async def _task_heartbeat_loop(
        self,
        task_id: str,
        trace_id: str,
        run_id: str,
        *,
        stage: str,
        interval_seconds: float = 10.0,
    ) -> None:
        interval = max(0.1, float(interval_seconds))
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            now = time.time()
            self.store.update_task(
                task_id,
                metadata={
                    "last_heartbeat_at": now,
                    "last_heartbeat_run_id": run_id,
                    "last_heartbeat_stage": stage,
                    "last_heartbeat_trace_id": trace_id,
                },
            )

    async def _lease_heartbeat_loop(self, lease_id: str, interval_seconds: int) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(interval_seconds)
            try:
                from orchestrator.resource_governor import get_resource_governor_service

                if get_resource_governor_service().heartbeat_lease(lease_id):
                    self.store.renew_resource_lease(lease_id)
            except Exception:
                return

    def _release_lease(self, lease: LeaseOutcome) -> None:
        if not lease.lease_id or not lease.granted:
            return
        try:
            from orchestrator.resource_governor import get_resource_governor_service

            get_resource_governor_service().release_lease(lease.lease_id)
        except Exception:
            pass
        self.store.release_resource_lease(lease.lease_id)


_RUNNER: AgenticRunner | None = None


def set_agentic_runner(runner: AgenticRunner | None) -> None:
    global _RUNNER
    _RUNNER = runner


def get_agentic_runner() -> AgenticRunner | None:
    return _RUNNER


def get_runner_status() -> dict[str, Any]:
    if _RUNNER is None:
        return {"running": False, "active_task_ids": [], "max_concurrent_tasks": 0}
    return _RUNNER.status()
