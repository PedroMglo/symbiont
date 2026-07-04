"""Reversible autonomous actuators for the agentic runtime."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from typing import Any

from orchestrator.agentic.policy import check_policy
from orchestrator.agentic.store import AgenticStore, get_agentic_store

log = logging.getLogger(__name__)

AUTO_APPLY_ACTION = "agentic.actuator.set_runtime_flag"
ALLOWED_RUNTIME_FLAG_KEYS = frozenset({
    "block_heavy_tasks",
    "rag_retrieval_degraded",
})
ALLOWED_RUNTIME_FLAG_PREFIXES = (
    "service_degraded:",
    "model_degraded:",
    "route_fallback:",
)
SENSITIVE_VALUE_MARKERS = ("secret", "password", "token", "api_key", "private_key")


def runtime_flag_key_allowed(key: str) -> bool:
    return key in ALLOWED_RUNTIME_FLAG_KEYS or any(key.startswith(prefix) for prefix in ALLOWED_RUNTIME_FLAG_PREFIXES)


def contains_sensitive_value(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SENSITIVE_VALUE_MARKERS):
                return True
            if contains_sensitive_value(item):
                return True
    elif isinstance(value, list):
        return any(contains_sensitive_value(item) for item in value)
    elif isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in SENSITIVE_VALUE_MARKERS)
    return False


def validate_runtime_flag_operation(
    operation: dict[str, Any],
    *,
    max_ttl_seconds: int,
    policy_action: str = AUTO_APPLY_ACTION,
) -> tuple[bool, str]:
    if not isinstance(operation, dict):
        return False, "payload_not_object"
    if operation.get("operation") != "set_runtime_flag":
        return False, "unsupported_operation"
    key = str(operation.get("key") or "")
    if not runtime_flag_key_allowed(key):
        return False, "runtime_flag_not_safelisted"
    value = operation.get("value") or {}
    if not isinstance(value, dict):
        return False, "runtime_flag_value_not_object"
    if not value.get("safe_action"):
        return False, "safe_action_missing"
    if contains_sensitive_value(value):
        return False, "runtime_flag_value_contains_sensitive_marker"
    ttl_seconds = int(operation.get("ttl_seconds") or 0)
    if ttl_seconds <= 0:
        return False, "ttl_missing"
    if ttl_seconds > int(max_ttl_seconds):
        return False, "ttl_above_auto_limit"
    decision = check_policy(policy_action, operation)
    if decision.should_block or decision.decision == "deny":
        return False, f"policy_blocked:{decision.decision}"
    return True, "eligible"


class AgenticActuator:
    """Applies only reversible, TTL-scoped runtime changes."""

    def __init__(
        self,
        *,
        store: AgenticStore | None = None,
        poll_interval_seconds: float | None = None,
        auto_apply_runtime_flags: bool | None = None,
        max_auto_ttl_seconds: int | None = None,
        min_confidence: float | None = None,
        min_score: float | None = None,
        impact_interval_seconds: int | None = None,
        bypass_failure_threshold: int | None = None,
        closed_loop_enabled: bool | None = None,
        renew_enforced_flags: bool | None = None,
        renewal_window_seconds: int | None = None,
        max_renewals: int | None = None,
        attention_ttl_seconds: int | None = None,
        auto_rollback_missing_flags: bool | None = None,
        escalation_ladder_enabled: bool | None = None,
        escalation_window_seconds: int | None = None,
        escalation_l2_threshold: int | None = None,
        escalation_l3_threshold: int | None = None,
        escalation_flag_ttl_seconds: int | None = None,
        escalation_create_proposals: bool | None = None,
        escalation_policy_router_enabled: bool | None = None,
        escalation_policy_router_create_proposals: bool | None = None,
        escalation_route_flag_ttl_seconds: int | None = None,
        preapproval_windows_enabled: bool | None = None,
        worker_id: str | None = None,
    ) -> None:
        from orchestrator.config import get_settings

        cfg = get_settings().agentic_runtime
        self.store = store or get_agentic_store()
        self.poll_interval_seconds = float(
            poll_interval_seconds if poll_interval_seconds is not None else cfg.actuator_poll_interval_seconds
        )
        self.auto_apply_runtime_flags = bool(
            auto_apply_runtime_flags
            if auto_apply_runtime_flags is not None
            else cfg.actuator_auto_apply_runtime_flags
        )
        self.max_auto_ttl_seconds = int(
            max_auto_ttl_seconds if max_auto_ttl_seconds is not None else cfg.actuator_max_auto_ttl_seconds
        )
        self.min_confidence = float(min_confidence if min_confidence is not None else cfg.actuator_min_confidence)
        self.min_score = float(min_score if min_score is not None else cfg.actuator_min_score)
        self.impact_interval_seconds = int(
            impact_interval_seconds
            if impact_interval_seconds is not None
            else cfg.actuator_impact_interval_seconds
        )
        self.bypass_failure_threshold = int(
            bypass_failure_threshold
            if bypass_failure_threshold is not None
            else cfg.actuator_bypass_failure_threshold
        )
        self.closed_loop_enabled = bool(
            closed_loop_enabled
            if closed_loop_enabled is not None
            else cfg.actuator_closed_loop_enabled
        )
        self.renew_enforced_flags = bool(
            renew_enforced_flags
            if renew_enforced_flags is not None
            else cfg.actuator_renew_enforced_flags
        )
        self.renewal_window_seconds = int(
            renewal_window_seconds
            if renewal_window_seconds is not None
            else cfg.actuator_renewal_window_seconds
        )
        self.max_renewals = int(max_renewals if max_renewals is not None else cfg.actuator_max_renewals)
        self.attention_ttl_seconds = int(
            attention_ttl_seconds
            if attention_ttl_seconds is not None
            else cfg.actuator_attention_ttl_seconds
        )
        self.auto_rollback_missing_flags = bool(
            auto_rollback_missing_flags
            if auto_rollback_missing_flags is not None
            else cfg.actuator_auto_rollback_missing_flags
        )
        self.escalation_ladder_enabled = bool(
            escalation_ladder_enabled
            if escalation_ladder_enabled is not None
            else cfg.actuator_escalation_ladder_enabled
        )
        self.escalation_window_seconds = int(
            escalation_window_seconds
            if escalation_window_seconds is not None
            else cfg.actuator_escalation_window_seconds
        )
        self.escalation_l2_threshold = int(
            escalation_l2_threshold
            if escalation_l2_threshold is not None
            else cfg.actuator_escalation_l2_threshold
        )
        self.escalation_l3_threshold = int(
            escalation_l3_threshold
            if escalation_l3_threshold is not None
            else cfg.actuator_escalation_l3_threshold
        )
        self.escalation_flag_ttl_seconds = int(
            escalation_flag_ttl_seconds
            if escalation_flag_ttl_seconds is not None
            else cfg.actuator_escalation_flag_ttl_seconds
        )
        self.escalation_create_proposals = bool(
            escalation_create_proposals
            if escalation_create_proposals is not None
            else cfg.actuator_escalation_create_proposals
        )
        self.escalation_policy_router_enabled = bool(
            escalation_policy_router_enabled
            if escalation_policy_router_enabled is not None
            else cfg.actuator_escalation_policy_router_enabled
        )
        self.escalation_policy_router_create_proposals = bool(
            escalation_policy_router_create_proposals
            if escalation_policy_router_create_proposals is not None
            else cfg.actuator_escalation_policy_router_create_proposals
        )
        self.escalation_route_flag_ttl_seconds = int(
            escalation_route_flag_ttl_seconds
            if escalation_route_flag_ttl_seconds is not None
            else cfg.actuator_escalation_route_flag_ttl_seconds
        )
        self.preapproval_windows_enabled = bool(
            preapproval_windows_enabled
            if preapproval_windows_enabled is not None
            else cfg.preapproval_windows_enabled
        )
        self.worker_id = worker_id or f"agentic-actuator-{uuid.uuid4().hex[:8]}"
        self._loop_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_tick_at: float | None = None

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="agentic-actuator")
        self.store.record_event(event_type="actuator.started", actor="agentic.actuator", payload=self.status())

    async def stop(self) -> None:
        self._stopping.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
        self.store.record_event(
            event_type="actuator.stopped",
            actor="agentic.actuator",
            payload={"worker_id": self.worker_id},
        )

    def status(self) -> dict[str, Any]:
        return {
            "running": self._loop_task is not None and not self._loop_task.done(),
            "worker_id": self.worker_id,
            "poll_interval_seconds": self.poll_interval_seconds,
            "last_tick_at": self._last_tick_at,
            "auto_apply_runtime_flags": self.auto_apply_runtime_flags,
            "max_auto_ttl_seconds": self.max_auto_ttl_seconds,
            "min_confidence": self.min_confidence,
            "min_score": self.min_score,
            "impact_interval_seconds": self.impact_interval_seconds,
            "bypass_failure_threshold": self.bypass_failure_threshold,
            "closed_loop_enabled": self.closed_loop_enabled,
            "renew_enforced_flags": self.renew_enforced_flags,
            "renewal_window_seconds": self.renewal_window_seconds,
            "max_renewals": self.max_renewals,
            "attention_ttl_seconds": self.attention_ttl_seconds,
            "auto_rollback_missing_flags": self.auto_rollback_missing_flags,
            "escalation_ladder_enabled": self.escalation_ladder_enabled,
            "escalation_window_seconds": self.escalation_window_seconds,
            "escalation_l2_threshold": self.escalation_l2_threshold,
            "escalation_l3_threshold": self.escalation_l3_threshold,
            "escalation_flag_ttl_seconds": self.escalation_flag_ttl_seconds,
            "escalation_create_proposals": self.escalation_create_proposals,
            "escalation_policy_router_enabled": self.escalation_policy_router_enabled,
            "escalation_policy_router_create_proposals": self.escalation_policy_router_create_proposals,
            "escalation_route_flag_ttl_seconds": self.escalation_route_flag_ttl_seconds,
            "preapproval_windows_enabled": self.preapproval_windows_enabled,
            "allowed_runtime_flags": sorted(ALLOWED_RUNTIME_FLAG_KEYS),
            "allowed_runtime_flag_prefixes": list(ALLOWED_RUNTIME_FLAG_PREFIXES),
            "safe_actions_only": True,
        }

    async def run_once(self) -> int:
        actions = 0
        actions += self._expire_due_actuations()
        actions += self._measure_applied_actuations()
        if self.auto_apply_runtime_flags:
            for proposal in self.store.list_improvement_proposals(status="proposed", limit=100):
                if not self._is_auto_apply_candidate(proposal):
                    continue
                eligible, reason = self._eligible_for_auto_apply(proposal)
                if not eligible:
                    self._record_skip_once(proposal, reason=reason)
                    continue
                try:
                    self.apply_runtime_flag_proposal(proposal)
                    actions += 1
                except Exception as exc:
                    log.warning("Agentic actuator failed for proposal %s: %s", proposal.get("id"), exc)
                    self.store.record_event(
                        task_id=proposal.get("task_id"),
                        event_type="actuator.apply_failed",
                        actor="agentic.actuator",
                        payload={"proposal_id": proposal.get("id"), "error": str(exc)[:500]},
                    )
        self._last_tick_at = time.time()
        if actions:
            self.store.record_event(
                event_type="actuator.tick",
                actor="agentic.actuator",
                payload={"actions": actions},
            )
        return actions

    def apply_runtime_flag_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        eligible, reason = self._eligible_for_auto_apply(proposal)
        if not eligible:
            raise ValueError(reason)
        operation = dict(proposal.get("payload") or {})
        key = str(operation["key"])
        ttl_seconds = min(int(operation.get("ttl_seconds") or 300), self.max_auto_ttl_seconds)
        preapproval_window = self._consume_preapproval_window(proposal) if self.preapproval_windows_enabled else None
        if self.preapproval_windows_enabled and preapproval_window is None:
            raise PermissionError("preapproval_window_required")
        before = self.store.get_runtime_flag(key)
        expires_at = time.time() + ttl_seconds
        actuation = self.store.create_actuation(
            proposal_id=str(proposal["id"]),
            task_id=proposal.get("task_id"),
            action="set_runtime_flag",
            mode="auto_policy",
            before={"runtime_flag": before},
            operation={**operation, "ttl_seconds": ttl_seconds},
            expires_at=expires_at,
            metadata={
                "worker_id": self.worker_id,
                "proposal_fingerprint": proposal.get("fingerprint"),
                "confidence": proposal.get("confidence"),
                "score": proposal.get("score"),
                "policy_action": AUTO_APPLY_ACTION,
                "preapproval_window_id": preapproval_window.get("id") if preapproval_window else None,
            },
        )
        flag = self.store.set_runtime_flag(
            key,
            dict(operation.get("value") or {}),
            ttl_seconds=ttl_seconds,
        )
        finished = self.store.finish_actuation(
            str(actuation["id"]),
            status="applied",
            after={"runtime_flag": flag},
            impact={
                "expected_effect": (operation.get("value") or {}).get("safe_action"),
                "reversible": True,
                "rollback": "clear_runtime_flag",
                "ttl_seconds": ttl_seconds,
                "verdict": "awaiting_signal",
            },
        )
        self.store.mark_improvement_applied(
            str(proposal["id"]),
            result={
                "operation": "set_runtime_flag",
                "flag": flag,
                "actuation_id": actuation["id"],
                "applied_by": "agentic.actuator",
                "auto_policy": True,
            },
        )
        return finished or actuation

    def _eligible_for_auto_apply(self, proposal: dict[str, Any]) -> tuple[bool, str]:
        if proposal.get("status") != "proposed":
            return False, "proposal_not_proposed"
        if float(proposal.get("confidence") or 0.0) < self.min_confidence:
            return False, "confidence_below_threshold"
        if float(proposal.get("score") or 0.0) < self.min_score:
            return False, "score_below_threshold"
        operation = proposal.get("payload") or {}
        eligible, reason = validate_runtime_flag_operation(
            operation,
            max_ttl_seconds=self.max_auto_ttl_seconds,
            policy_action=AUTO_APPLY_ACTION,
        )
        if not eligible:
            return False, reason
        if self.preapproval_windows_enabled and self._find_preapproval_window(proposal) is None:
            return False, "preapproval_window_required"
        return True, "eligible"

    def _preapproval_payload(self, proposal: dict[str, Any]) -> dict[str, Any]:
        operation = proposal.get("payload") or {}
        return {
            "proposal_id": proposal.get("id"),
            "operation": operation if isinstance(operation, dict) else {},
            "confidence": proposal.get("confidence"),
            "score": proposal.get("score"),
        }

    def _find_preapproval_window(self, proposal: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.find_preapproval_window(
            action=AUTO_APPLY_ACTION,
            payload=self._preapproval_payload(proposal),
            task_id=proposal.get("task_id"),
        )

    def _consume_preapproval_window(self, proposal: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.consume_preapproval_window(
            action=AUTO_APPLY_ACTION,
            payload=self._preapproval_payload(proposal),
            task_id=proposal.get("task_id"),
            actor="agentic.actuator",
        )

    @staticmethod
    def _is_auto_apply_candidate(proposal: dict[str, Any]) -> bool:
        payload = proposal.get("payload") or {}
        return isinstance(payload, dict) and payload.get("operation") == "set_runtime_flag"

    def _record_skip_once(self, proposal: dict[str, Any], *, reason: str) -> None:
        proposal_id = str(proposal.get("id") or "")
        if not proposal_id:
            return
        flag_key = f"actuator_skip:{proposal_id}"
        if self.store.get_runtime_flag(flag_key) is not None:
            return
        self.store.set_runtime_flag(flag_key, {"reason": reason, "proposal_id": proposal_id}, ttl_seconds=300)
        self.store.record_event(
            task_id=proposal.get("task_id"),
            event_type="actuator.proposal_skipped",
            actor="agentic.actuator",
            payload={"proposal_id": proposal_id, "reason": reason},
        )

    def _expire_due_actuations(self) -> int:
        now = time.time()
        changed = 0
        for actuation in self.store.list_actuations(status="applied", limit=500):
            expires_at = actuation.get("expires_at")
            if expires_at is None or float(expires_at) >= now:
                continue
            operation = actuation.get("operation") or {}
            key = str(operation.get("key") or "")
            current = self.store.get_runtime_flag(key) if key else None
            expected = (actuation.get("after") or {}).get("runtime_flag")
            if key and self._current_matches_expected(current, expected):
                self.store.clear_runtime_flag(key, reason=f"actuation_expired:{actuation['id']}")
            self.store.finish_actuation(
                str(actuation["id"]),
                status="expired",
                after={"runtime_flag": self.store.get_runtime_flag(key) if key else None},
                impact={"expired": True, "runtime_flag_cleared": bool(key)},
            )
            changed += 1
        return changed

    def _measure_applied_actuations(self) -> int:
        changed = 0
        now = time.time()
        for actuation in self.store.list_actuations(status="applied", limit=500):
            metadata = actuation.get("metadata") or {}
            last_measured = float(metadata.get("last_impact_measured_at") or 0)
            if last_measured and now - last_measured < max(1, self.impact_interval_seconds):
                continue
            impact = self._impact_for_actuation(actuation, measured_at=now)
            if not impact:
                continue
            measured = self.store.update_actuation_impact(
                str(actuation["id"]),
                impact=impact,
                metadata={"last_impact_measured_at": now},
            )
            if impact.get("attention_required"):
                self.store.set_runtime_flag(
                    f"actuation_attention:{actuation['id']}",
                    {
                        "reason": impact.get("verdict"),
                        "actuation_id": actuation["id"],
                        "proposal_id": actuation.get("proposal_id"),
                        "safe_action": "review_actuation_impact",
                    },
                    ttl_seconds=max(300, self.impact_interval_seconds * 2),
                )
            self._apply_closed_loop_decision(measured or actuation, impact, measured_at=now)
            self._apply_escalation_ladder(measured or actuation, impact, measured_at=now)
            changed += 1
        return changed

    def _apply_closed_loop_decision(
        self,
        actuation: dict[str, Any],
        impact: dict[str, Any],
        *,
        measured_at: float,
    ) -> bool:
        decision = self._closed_loop_decision(actuation, impact, measured_at=measured_at)
        action = decision.get("action")
        if action == "observe":
            return False

        signature = self._decision_signature(decision)
        metadata = dict(actuation.get("metadata") or {})
        if metadata.get("last_closed_loop_signature") == signature and action != "renew_runtime_flag":
            return False

        self._record_closed_loop_decision(actuation, decision)
        common_metadata = {
            "last_closed_loop_signature": signature,
            "last_closed_loop_decision": decision,
            "last_closed_loop_decision_at": measured_at,
        }

        if action == "mark_attention":
            self.store.set_runtime_flag(
                f"actuation_attention:{actuation['id']}",
                {
                    "reason": decision.get("reason"),
                    "actuation_id": actuation["id"],
                    "proposal_id": actuation.get("proposal_id"),
                    "safe_action": "review_actuation_impact",
                    "decision": decision,
                },
                ttl_seconds=max(60, self.attention_ttl_seconds),
            )
            self.store.update_actuation_metadata(str(actuation["id"]), metadata=common_metadata)
            return True

        if action == "renew_runtime_flag":
            operation = actuation.get("operation") or {}
            key = str(operation.get("key") or "")
            value = dict(operation.get("value") or {})
            ttl_seconds = min(int(operation.get("ttl_seconds") or self.max_auto_ttl_seconds), self.max_auto_ttl_seconds)
            flag = self.store.set_runtime_flag(key, value, ttl_seconds=ttl_seconds)
            renewal_count = int(metadata.get("closed_loop_renewal_count") or 0) + 1
            self.store.renew_actuation(
                str(actuation["id"]),
                expires_at=float(flag["expires_at"]),
                after={"runtime_flag": flag},
                metadata={
                    **common_metadata,
                    "closed_loop_renewal_count": renewal_count,
                    "closed_loop_last_renewed_at": measured_at,
                },
            )
            return True

        if action == "rollback_missing_flag":
            try:
                rollback_actuation(
                    str(actuation["id"]),
                    reason=str(decision.get("reason") or "closed_loop_flag_missing"),
                    store=self.store,
                )
                self.store.update_actuation_metadata(str(actuation["id"]), metadata=common_metadata)
            except Exception as exc:
                self.store.set_runtime_flag(
                    f"actuation_attention:{actuation['id']}",
                    {
                        "reason": "closed_loop_rollback_failed",
                        "error": str(exc)[:500],
                        "actuation_id": actuation["id"],
                        "safe_action": "review_actuation_impact",
                    },
                    ttl_seconds=max(60, self.attention_ttl_seconds),
                )
                self.store.update_actuation_metadata(
                    str(actuation["id"]),
                    metadata={
                        **common_metadata,
                        "closed_loop_rollback_error": str(exc)[:500],
                    },
                )
            return True

        return False

    def _closed_loop_decision(
        self,
        actuation: dict[str, Any],
        impact: dict[str, Any],
        *,
        measured_at: float,
    ) -> dict[str, Any]:
        if not self.closed_loop_enabled:
            return {"action": "observe", "reason": "closed_loop_disabled"}
        if actuation.get("status") != "applied":
            return {"action": "observe", "reason": "actuation_not_applied"}
        operation = actuation.get("operation") or {}
        if operation.get("operation") != "set_runtime_flag":
            return {"action": "observe", "reason": "unsupported_actuation_operation"}

        verdict = str(impact.get("verdict") or "unknown")
        if verdict in {"bypass_detected", "continued_rag_miss"}:
            return {
                "action": "mark_attention",
                "reason": verdict,
                "verdict": verdict,
                "requires_approval": False,
                "safe_actions_only": True,
            }
        if verdict == "flag_missing":
            if self.auto_rollback_missing_flags:
                return {
                    "action": "rollback_missing_flag",
                    "reason": "closed_loop_flag_missing",
                    "verdict": verdict,
                    "requires_approval": False,
                    "safe_actions_only": True,
                }
            return {
                "action": "mark_attention",
                "reason": "flag_missing",
                "verdict": verdict,
                "requires_approval": False,
                "safe_actions_only": True,
            }
        if verdict != "enforced":
            return {"action": "observe", "reason": f"verdict:{verdict}", "verdict": verdict}

        metadata = actuation.get("metadata") or {}
        renewal_count = int(metadata.get("closed_loop_renewal_count") or 0)
        expires_at = actuation.get("expires_at")
        remaining_seconds = float(expires_at) - measured_at if expires_at is not None else None
        if not self.renew_enforced_flags:
            return {
                "action": "observe",
                "reason": "enforced_no_renewal_policy",
                "verdict": verdict,
                "remaining_seconds": remaining_seconds,
            }
        if renewal_count >= max(0, self.max_renewals):
            return {
                "action": "observe",
                "reason": "max_renewals_reached",
                "verdict": verdict,
                "renewal_count": renewal_count,
                "remaining_seconds": remaining_seconds,
            }
        if remaining_seconds is None or remaining_seconds > self.renewal_window_seconds:
            return {
                "action": "observe",
                "reason": "renewal_window_not_reached",
                "verdict": verdict,
                "renewal_count": renewal_count,
                "remaining_seconds": remaining_seconds,
            }
        return {
            "action": "renew_runtime_flag",
            "reason": "enforced_with_expiry_near",
            "verdict": verdict,
            "renewal_count": renewal_count,
            "remaining_seconds": remaining_seconds,
            "requires_approval": False,
            "safe_actions_only": True,
        }

    @staticmethod
    def _decision_signature(decision: dict[str, Any]) -> str:
        return ":".join(
            str(decision.get(key, ""))
            for key in ("action", "reason", "verdict", "renewal_count")
        )

    def _record_closed_loop_decision(self, actuation: dict[str, Any], decision: dict[str, Any]) -> None:
        self.store.record_event(
            task_id=actuation.get("task_id"),
            event_type="actuation.closed_loop_decision",
            actor="agentic.actuator",
            payload={
                "actuation_id": actuation.get("id"),
                "proposal_id": actuation.get("proposal_id"),
                "decision": decision,
            },
        )

    def _apply_escalation_ladder(
        self,
        actuation: dict[str, Any],
        impact: dict[str, Any],
        *,
        measured_at: float,
    ) -> bool:
        escalation = self._escalation_for_impact(actuation, impact, measured_at=measured_at)
        level = int(escalation.get("level") or 0)
        if level <= 0:
            return False

        metadata = dict(actuation.get("metadata") or {})
        previous_level = int(metadata.get("last_escalation_level") or 0)
        signature = self._escalation_signature(escalation)
        if previous_level >= level and metadata.get("last_escalation_signature") == signature:
            return False

        self._record_escalation(actuation, escalation)
        self.store.set_runtime_flag(
            f"actuation_escalation:{actuation['id']}",
            {
                "level": level,
                "reason": escalation.get("reason"),
                "actuation_id": actuation.get("id"),
                "proposal_id": actuation.get("proposal_id"),
                "safe_action": escalation.get("safe_action"),
                "requires_approval": escalation.get("requires_approval", False),
                "signals": escalation.get("signals", {}),
            },
            ttl_seconds=max(60, self.escalation_flag_ttl_seconds),
        )

        proposal = None
        if level >= 3 and self.escalation_create_proposals:
            proposal = self._create_escalation_proposal(actuation, escalation)

        route_result = None
        if self.escalation_policy_router_enabled:
            route_result = self._route_escalation(actuation, escalation)

        escalation_metadata = {
            "last_escalation_level": level,
            "last_escalation": escalation,
            "last_escalation_at": measured_at,
            "last_escalation_signature": signature,
        }
        if proposal:
            escalation_metadata["last_escalation_proposal_id"] = proposal.get("id")
        if route_result:
            escalation_metadata["last_escalation_route"] = route_result["route"]
            if route_result.get("proposal"):
                escalation_metadata["last_escalation_route_proposal_id"] = route_result["proposal"]["id"]

        self.store.update_actuation_metadata(
            str(actuation["id"]),
            metadata=escalation_metadata,
            event_type="actuation.escalation_metadata_updated",
            event_payload={"level": level, "reason": escalation.get("reason")},
        )
        return True

    def _escalation_for_impact(
        self,
        actuation: dict[str, Any],
        impact: dict[str, Any],
        *,
        measured_at: float,
    ) -> dict[str, Any]:
        if not self.escalation_ladder_enabled:
            return {"level": 0, "reason": "escalation_ladder_disabled"}
        if actuation.get("status") != "applied":
            return {"level": 0, "reason": "actuation_not_applied"}

        verdict = str(impact.get("verdict") or "unknown")
        if verdict not in {"bypass_detected", "continued_rag_miss"}:
            return {"level": 0, "reason": f"verdict:{verdict}"}

        window_start = max(0.0, measured_at - max(1, self.escalation_window_seconds))
        signals = self._escalation_signals_for_impact(impact, since=window_start)
        incident_count = int(signals.get("incident_count") or 0)
        if incident_count >= max(self.escalation_l3_threshold, self.escalation_l2_threshold):
            return {
                "level": 3,
                "reason": verdict,
                "safe_action": "create_governed_remediation_proposal",
                "requires_approval": True,
                "window_seconds": self.escalation_window_seconds,
                "signals": signals,
                "recommendations": self._escalation_recommendations(verdict, level=3),
            }
        if incident_count >= self.escalation_l2_threshold:
            return {
                "level": 2,
                "reason": verdict,
                "safe_action": "mark_persistent_issue_for_operator_review",
                "requires_approval": False,
                "window_seconds": self.escalation_window_seconds,
                "signals": signals,
                "recommendations": self._escalation_recommendations(verdict, level=2),
            }
        return {
            "level": 1,
            "reason": verdict,
            "safe_action": "mark_attention",
            "requires_approval": False,
            "window_seconds": self.escalation_window_seconds,
            "signals": signals,
            "recommendations": self._escalation_recommendations(verdict, level=1),
        }

    def _escalation_signals_for_impact(self, impact: dict[str, Any], *, since: float) -> dict[str, Any]:
        verdict = str(impact.get("verdict") or "")
        if verdict == "bypass_detected":
            agent_name = str(impact.get("agent_name") or "")
            failures = self._count_agent_events("agent.invoke.failed", agent_name=agent_name, since=since)
            skips = self._count_agent_events("agent.invoke.skipped_degraded", agent_name=agent_name, since=since)
            return {
                "kind": "agent_bypass",
                "agent_name": agent_name,
                "incident_count": failures,
                "degraded_skips": skips,
            }
        if verdict == "continued_rag_miss":
            misses = self.store.count_events(event_type="rag.miss", since=since)
            return {
                "kind": "rag_miss",
                "incident_count": misses,
            }
        return {"kind": "unknown", "incident_count": 0}

    @staticmethod
    def _escalation_recommendations(verdict: str, *, level: int) -> list[dict[str, Any]]:
        if verdict == "bypass_detected":
            return [
                {
                    "kind": "dispatch_guardrail_bypass",
                    "action": "audit_invocation_paths_for_missing_runtime_flag_checks",
                    "requires_approval": False,
                    "level": level,
                },
                {
                    "kind": "service_health_review",
                    "action": "inspect_agent_logs_before_any_restart",
                    "requires_approval": False,
                    "level": level,
                },
            ]
        if verdict == "continued_rag_miss":
            return [
                {
                    "kind": "rag_retrieval_review",
                    "action": "inspect_retrieval_debug_before_reprocess",
                    "requires_approval": False,
                    "level": level,
                },
                {
                    "kind": "rag_reprocess_guarded",
                    "action": "request_approval_before_broad_reprocess",
                    "requires_approval": True,
                    "level": level,
                },
            ]
        return []

    @staticmethod
    def _escalation_signature(escalation: dict[str, Any]) -> str:
        signals = escalation.get("signals") or {}
        return ":".join(
            str(part)
            for part in (
                escalation.get("level", 0),
                escalation.get("reason", ""),
                signals.get("kind", ""),
                signals.get("agent_name", ""),
                signals.get("incident_count", 0),
            )
        )

    def _record_escalation(self, actuation: dict[str, Any], escalation: dict[str, Any]) -> None:
        self.store.record_event(
            task_id=actuation.get("task_id"),
            event_type="actuation.escalated",
            actor="agentic.actuator",
            payload={
                "actuation_id": actuation.get("id"),
                "proposal_id": actuation.get("proposal_id"),
                "escalation": escalation,
            },
        )

    def _create_escalation_proposal(
        self,
        actuation: dict[str, Any],
        escalation: dict[str, Any],
    ) -> dict[str, Any]:
        signals = escalation.get("signals") or {}
        kind = "actuation_escalation_review"
        return self.store.create_improvement_proposal(
            kind=kind,
            title=f"Review level {escalation.get('level')} agentic escalation: {escalation.get('reason')}",
            risk_level="high",
            confidence=min(0.95, 0.7 + (0.05 * int(escalation.get("level") or 0))),
            score=float(escalation.get("level") or 0),
            payload={
                "operation": "request_human_review",
                "actuation_id": actuation.get("id"),
                "source_proposal_id": actuation.get("proposal_id"),
                "level": escalation.get("level"),
                "reason": escalation.get("reason"),
                "safe_actions_only": True,
                "recommended_actions": escalation.get("recommendations", []),
                "signals": signals,
            },
            evidence={
                "actuation_id": actuation.get("id"),
                "operation": actuation.get("operation") or {},
                "impact": actuation.get("impact") or {},
                "escalation": escalation,
            },
            task_id=actuation.get("task_id"),
            ttl_seconds=self.escalation_flag_ttl_seconds,
            metadata={
                "origin": "agentic_escalation_ladder",
                "requires_approval_to_apply": True,
                "safe_actions_only": True,
            },
        )

    def _route_escalation(self, actuation: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
        from orchestrator.agentic.escalation_router import EscalationPolicyRouter

        return EscalationPolicyRouter(
            store=self.store,
            route_flag_ttl_seconds=self.escalation_route_flag_ttl_seconds,
            create_proposals=self.escalation_policy_router_create_proposals,
        ).route(actuation, escalation)

    def _impact_for_actuation(self, actuation: dict[str, Any], *, measured_at: float) -> dict[str, Any]:
        operation = actuation.get("operation") or {}
        if operation.get("operation") != "set_runtime_flag":
            return {}
        key = str(operation.get("key") or "")
        created_at = float(actuation.get("created_at") or measured_at)
        current_flag = self.store.get_runtime_flag(key) if key else None
        impact: dict[str, Any] = {
            "measured_at": measured_at,
            "runtime_flag_key": key,
            "current_flag_present": current_flag is not None,
            "age_seconds": round(max(0.0, measured_at - created_at), 2),
        }
        if key.startswith("service_degraded:"):
            agent_name = key.removeprefix("service_degraded:")
            failures = self._count_agent_events("agent.invoke.failed", agent_name=agent_name, since=created_at)
            skips = self._count_agent_events("agent.invoke.skipped_degraded", agent_name=agent_name, since=created_at)
            impact.update({
                "agent_name": agent_name,
                "post_apply_failures": failures,
                "post_apply_degraded_skips": skips,
            })
            if failures >= self.bypass_failure_threshold:
                impact.update({
                    "verdict": "bypass_detected",
                    "attention_required": True,
                    "recommendations": [
                        {
                            "kind": "dispatch_guardrail_bypass",
                            "action": "inspect_call_paths_missing_runtime_flag_check",
                            "requires_approval": False,
                        }
                    ],
                })
            elif skips > 0:
                impact.update({
                    "verdict": "enforced",
                    "attention_required": False,
                    "recommendations": [
                        {
                            "kind": "runtime_guardrail",
                            "action": "keep_until_ttl_or_manual_rollback",
                            "requires_approval": False,
                        }
                    ],
                })
            elif current_flag is None:
                impact.update({
                    "verdict": "flag_missing",
                    "attention_required": True,
                    "recommendations": [
                        {
                            "kind": "runtime_guardrail",
                            "action": "verify_flag_expiry_or_external_clear",
                            "requires_approval": False,
                        }
                    ],
                })
            else:
                impact.update({
                    "verdict": "awaiting_signal",
                    "attention_required": False,
                    "recommendations": [],
                })
            return impact

        if key == "rag_retrieval_degraded":
            misses = self.store.count_events(event_type="rag.miss", since=created_at)
            impact.update({
                "post_apply_rag_misses": misses,
                "verdict": "observing" if misses == 0 else "continued_rag_miss",
                "attention_required": misses > 0,
                "recommendations": [
                    {
                        "kind": "rag_retrieval_review",
                        "action": "inspect_retrieval_debug_before_reprocess",
                        "requires_approval": False,
                    }
                ] if misses > 0 else [],
            })
            return impact

        impact.update({
            "verdict": "observing",
            "attention_required": False,
            "recommendations": [],
        })
        return impact

    def _count_agent_events(self, event_type: str, *, agent_name: str, since: float) -> int:
        count = 0
        for event in self.store.list_events(event_type=event_type, limit=1000):
            if float(event.get("timestamp") or 0) < since:
                continue
            payload = event.get("payload") or {}
            if str(payload.get("agent_name") or "") == agent_name:
                count += 1
        return count

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                log.warning("Agentic actuator tick failed: %s", exc)
                self.store.record_event(
                    event_type="actuator.error",
                    actor="agentic.actuator",
                    payload={"error": str(exc)[:500]},
                )
            await asyncio.sleep(self.poll_interval_seconds)

    @staticmethod
    def _runtime_flag_key_allowed(key: str) -> bool:
        return runtime_flag_key_allowed(key)

    @classmethod
    def _contains_sensitive_value(cls, value: Any) -> bool:
        return contains_sensitive_value(value)

    @staticmethod
    def _current_matches_expected(current: dict[str, Any] | None, expected: dict[str, Any] | None) -> bool:
        if current is None:
            return True
        if not expected:
            return False
        return current.get("value") == expected.get("value")


def rollback_actuation(
    actuation_id: str,
    *,
    reason: str = "manual_rollback",
    store: AgenticStore | None = None,
) -> dict[str, Any] | None:
    store = store or get_agentic_store()
    actuation = store.get_actuation(actuation_id)
    if actuation is None:
        return None
    operation = actuation.get("operation") or {}
    if operation.get("operation") != "set_runtime_flag":
        raise NotImplementedError("Only runtime-flag actuations can be rolled back in this phase")
    key = str(operation.get("key") or "")
    if not key:
        raise ValueError("Actuation has no runtime flag key")
    current = store.get_runtime_flag(key)
    expected = (actuation.get("after") or {}).get("runtime_flag")
    if not AgenticActuator._current_matches_expected(current, expected):
        raise ValueError("Runtime flag changed after actuation; refusing unsafe rollback")
    if current is not None:
        store.clear_runtime_flag(key, reason=f"actuation_rollback:{actuation_id}")
    return store.mark_actuation_rolled_back(
        actuation_id,
        reason=reason,
        after={"runtime_flag": store.get_runtime_flag(key)},
    )


_ACTUATOR: AgenticActuator | None = None


def set_agentic_actuator(actuator: AgenticActuator | None) -> None:
    global _ACTUATOR
    _ACTUATOR = actuator


def get_agentic_actuator() -> AgenticActuator | None:
    return _ACTUATOR


def get_actuator_status() -> dict[str, Any]:
    if _ACTUATOR is None:
        return {"running": False, "safe_actions_only": True}
    return _ACTUATOR.status()
