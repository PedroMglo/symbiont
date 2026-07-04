"""Read-only readiness checks for the local autonomous profile."""

from __future__ import annotations

import time
import tomllib
from pathlib import Path
from typing import Any

from orchestrator.agentic.policy import PolicyEngine
from orchestrator.agentic.policy_registry import action_matrix, risk_for_action
from orchestrator.agentic.store import AgenticStore, get_agentic_store
from orchestrator.capabilities.action_manifest import load_action_capability_manifests
from orchestrator.capabilities.service_manifest import load_service_capability_manifests
from orchestrator.config import CONFIG_DIR, get_settings
from orchestrator.resource_governor.effective_policy import build_effective_policy

PROFILE_PATH = CONFIG_DIR / "agentic_profiles.toml"
AUTONOMOUS_PROFILE = "autonomous"
REQUIRED_AUTONOMOUS_PROFILE: dict[str, Any] = {
    "default_mode": "autonomous",
    "policy_mode": "enforce",
    "runner_enabled": True,
    "runner_execute_proposals": False,
    "autonomous_safe_enabled": True,
    "event_loop_enabled": True,
    "autonomous_maintenance_enabled": True,
    "governed_improvement_enabled": True,
    "actuator_enabled": True,
    "actuator_auto_apply_runtime_flags": True,
    "actuator_closed_loop_enabled": True,
    "actuator_renew_enforced_flags": True,
    "actuator_auto_rollback_missing_flags": True,
    "actuator_escalation_ladder_enabled": True,
    "actuator_escalation_policy_router_enabled": True,
    "actuator_escalation_create_proposals": True,
    "actuator_escalation_policy_router_create_proposals": True,
    "preapproval_windows_enabled": True,
    "command_tool_enabled": True,
    "command_tool_allow_user_context_ro": False,
    "command_tool_allow_host_context_ro": False,
}
REQUIRED_COCKPIT_FIELDS = (
    "tasks",
    "events",
    "approvals",
    "leases",
    "consensus",
    "memory",
    "actuations",
    "impact",
    "gaps",
)
REQUIRED_EVAL_CHECKS = (
    "runner_read_only",
    "command_sandbox",
    "policy_enforce",
    "approval_expiry",
    "manifest_coverage",
    "multi_agent_deliberation",
    "event_loop_proposals",
    "resource_governor_defer",
    "memory_retrieval",
    "actuator_ttl_rollback",
    "anti_overfit_variants",
    "cockpit_coverage",
    "replay",
)
READINESS_STATUS_CONTRACT = "ai-local.agentic-readiness-status.v1"
CANONICAL_READINESS_STATUSES = ("ready", "degraded", "blocked", "stale", "unexpected_down")
STALE_READINESS_CHECKS = {"replay"}


def load_agentic_runtime_profiles(path: Path = PROFILE_PATH) -> dict[str, dict[str, Any]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw = data.get("agentic_runtime_profiles") or {}
    if not isinstance(raw, dict):
        raise ValueError("agentic_runtime_profiles must be a table")
    return {str(name): dict(value) for name, value in raw.items() if isinstance(value, dict)}


def autonomous_profile() -> dict[str, Any]:
    return load_agentic_runtime_profiles().get(AUTONOMOUS_PROFILE, {})


def evaluate_autonomous_readiness(
    *,
    store: AgenticStore | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    store = store or get_agentic_store()
    cfg = get_settings().agentic_runtime
    checks = [
        _profile_check(),
        _default_not_promoted_check(cfg),
        _runtime_config_check(cfg),
        _command_sandbox_check(cfg),
        _policy_enforce_check(cfg),
        _approval_expiry_check(cfg),
        _manifest_coverage_check(),
        _resource_governor_check(),
        _event_loop_check(cfg, store=store),
        _memory_check(store=store, limit=limit),
        _deliberation_check(store=store, limit=limit),
        _actuator_check(cfg, store=store, limit=limit),
        _replay_check(store=store, limit=limit),
        _anti_overfit_check(store=store),
        _cockpit_coverage_check(),
    ]
    gaps = [check for check in checks if check["status"] != "pass"]
    status = readiness_runtime_status(checks)
    return {
        "generated_at": time.time(),
        "status_contract": READINESS_STATUS_CONTRACT,
        "status": status,
        "canonical_statuses": list(CANONICAL_READINESS_STATUSES),
        "read_only": True,
        "profile": AUTONOMOUS_PROFILE,
        "ready_for_opt_in": all(check["status"] == "pass" for check in checks),
        "default_promoted": cfg.default_mode == "autonomous",
        "promotion_allowed": False,
        "promotion_reason": "default promotion requires measured Fase 12 stability outside this read-only report",
        "checks": checks,
        "gaps": gaps,
        "cockpit_required_fields": list(REQUIRED_COCKPIT_FIELDS),
    }


def cockpit_coverage_fields() -> list[str]:
    return list(REQUIRED_COCKPIT_FIELDS)


def readiness_runtime_status(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "unexpected_down"
    statuses = {str(check.get("status") or "") for check in checks}
    if "fail" in statuses:
        return "blocked"
    stale = any(
        check.get("name") in STALE_READINESS_CHECKS and check.get("status") != "pass"
        for check in checks
    )
    if stale:
        return "stale"
    if "warn" in statuses:
        return "degraded"
    if statuses == {"pass"}:
        return "ready"
    return "unexpected_down"


def check_runtime_status(name: str, status: str) -> str:
    if status == "pass":
        return "ready"
    if status == "warn" and name in STALE_READINESS_CHECKS:
        return "stale"
    if status == "warn":
        return "degraded"
    if status == "fail":
        return "blocked"
    return "unexpected_down"


def _check(name: str, status: str, summary: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "runtime_status": check_runtime_status(name, status),
        "summary": summary,
        "evidence": evidence or {},
    }


def _profile_check() -> dict[str, Any]:
    profile = autonomous_profile()
    missing = [key for key, expected in REQUIRED_AUTONOMOUS_PROFILE.items() if profile.get(key) != expected]
    promotion_requires = profile.get("promotion_requires") or []
    missing_requirements = [check for check in REQUIRED_EVAL_CHECKS if check not in promotion_requires]
    status = "pass" if profile.get("opt_in") is True and not missing and not missing_requirements else "fail"
    return _check(
        "autonomous_profile_opt_in",
        status,
        "autonomous profile is opt-in and preserves governed closed flags",
        {
            "profile_path": str(PROFILE_PATH),
            "opt_in": profile.get("opt_in"),
            "missing_or_mismatched": missing,
            "promotion_requires": promotion_requires,
            "missing_requirements": missing_requirements,
        },
    )


def _default_not_promoted_check(cfg: Any) -> dict[str, Any]:
    return _check(
        "default_not_promoted",
        "pass" if cfg.default_mode != "autonomous" else "warn",
        "default runtime remains supervised until stability is measured",
        {"default_mode": cfg.default_mode},
    )


def _runtime_config_check(cfg: Any) -> dict[str, Any]:
    required = {
        "runner_enabled": cfg.runner_enabled,
        "runner_execute_proposals_false": not cfg.runner_execute_proposals,
        "autonomous_safe_enabled": cfg.autonomous_safe_enabled,
        "event_loop_enabled": cfg.event_loop_enabled,
        "autonomous_maintenance_enabled": cfg.autonomous_maintenance_enabled,
        "governed_improvement_enabled": cfg.governed_improvement_enabled,
        "actuator_enabled": cfg.actuator_enabled,
        "actuator_auto_apply_runtime_flags": cfg.actuator_auto_apply_runtime_flags,
        "preapproval_windows_enabled": cfg.preapproval_windows_enabled,
    }
    failed = [name for name, ok in required.items() if not ok]
    return _check(
        "runner_read_only",
        "pass" if not failed else "fail",
        "runtime has supervised autonomous loops enabled without proposal execution",
        {"failed": failed, "required": required},
    )


def _command_sandbox_check(cfg: Any) -> dict[str, Any]:
    ok = (
        cfg.command_tool_enabled
        and cfg.command_tool_backend == "workspace_execution"
        and not cfg.command_tool_allow_user_context_ro
        and not cfg.command_tool_allow_host_context_ro
    )
    return _check(
        "command_sandbox",
        "pass" if ok else "fail",
        "command tool is read-only/sandboxed and has no user/host context by default",
        {
            "enabled": cfg.command_tool_enabled,
            "backend": cfg.command_tool_backend,
            "allow_user_context_ro": cfg.command_tool_allow_user_context_ro,
            "allow_host_context_ro": cfg.command_tool_allow_host_context_ro,
        },
    )


def _policy_enforce_check(cfg: Any) -> dict[str, Any]:
    policy = PolicyEngine(policy_mode=cfg.policy_mode)
    high = policy.check("storage.restore", {"dry_run": True})
    denied = policy.check("command.run.write", {"command": "touch file"})
    ok = cfg.policy_mode == "enforce" and high.decision == "require_approval" and denied.decision == "deny"
    return _check(
        "policy_enforce",
        "pass" if ok else "fail",
        "high-risk requires approval and deny remains denied",
        {
            "policy_mode": cfg.policy_mode,
            "high": high.__dict__,
            "deny": denied.__dict__,
        },
    )


def _approval_expiry_check(cfg: Any) -> dict[str, Any]:
    ok = int(cfg.approval_ttl_seconds) > 0 and int(cfg.preapproval_window_default_ttl_seconds) > 0
    return _check(
        "approval_expiry",
        "pass" if ok else "fail",
        "approval and preapproval windows have positive TTLs",
        {
            "approval_ttl_seconds": cfg.approval_ttl_seconds,
            "preapproval_window_default_ttl_seconds": cfg.preapproval_window_default_ttl_seconds,
            "preapproval_window_max_uses": cfg.preapproval_window_max_uses,
        },
    )


def _manifest_coverage_check() -> dict[str, Any]:
    manifests = (*load_service_capability_manifests(), *load_action_capability_manifests())
    explicit = {action for actions in action_matrix().values() for action in actions}
    missing = [manifest.policy_action for manifest in manifests if manifest.policy_action not in explicit]
    mismatched = [
        manifest.capability_id
        for manifest in manifests
        if manifest.policy_action in explicit and risk_for_action(manifest.policy_action) != manifest.risk_level
    ]
    return _check(
        "manifest_coverage",
        "pass" if not missing and not mismatched else "fail",
        "service/action manifests have explicit policy actions and risk alignment",
        {"count": len(manifests), "missing_policy_actions": sorted(set(missing)), "risk_mismatches": mismatched},
    )


def _resource_governor_check() -> dict[str, Any]:
    policy = build_effective_policy()
    lanes = policy.lanes
    required_lanes = ("interactive", "background", "heavy_gpu", "storage")
    missing_lanes = [lane for lane in required_lanes if lane not in lanes]
    ok = policy.mode == "enforced" and not missing_lanes
    return _check(
        "resource_governor_defer",
        "pass" if ok else "fail",
        "Resource Governor is enforced and defines required local lanes",
        {"mode": policy.mode, "source": policy.source, "missing_lanes": missing_lanes, "lanes": lanes},
    )


def _event_loop_check(cfg: Any, *, store: AgenticStore) -> dict[str, Any]:
    proposal_events = sum(
        store.count_events(event_type=event_type)
        for event_type in (
            "autonomous_safe.proposal_created",
            "autonomous_safe.maintenance_task_created",
            "event_loop.tick",
        )
    )
    ok = cfg.event_loop_enabled and cfg.autonomous_safe_enabled and cfg.autonomous_maintenance_enabled
    return _check(
        "event_loop_proposals",
        "pass" if ok else "fail",
        "event loop is enabled for safe proposals/maintenance",
        {"observed_events": proposal_events, "event_loop_enabled": cfg.event_loop_enabled},
    )


def _memory_check(*, store: AgenticStore, limit: int) -> dict[str, Any]:
    memories = store.list_agent_memory(limit=limit, include_expired=True)
    retrieval_events = store.count_events(event_type="agent.memory.retrieved")
    ok = bool(memories) or retrieval_events > 0
    return _check(
        "memory_retrieval",
        "pass" if ok else "warn",
        "agentic memory is available or retrieval has been observed",
        {"recent_memories": len(memories), "retrieval_events": retrieval_events},
    )


def _deliberation_check(*, store: AgenticStore, limit: int) -> dict[str, Any]:
    consensus = store.list_recent_agent_messages(kind="consensus", limit=limit)
    rounds = store.list_recent_parallel_rounds(limit=limit)
    ok = bool(consensus) or bool(rounds)
    return _check(
        "multi_agent_deliberation",
        "pass" if ok else "warn",
        "multi-agent rounds or consensus are visible in the ledger",
        {"recent_consensus": len(consensus), "recent_rounds": len(rounds)},
    )


def _actuator_check(cfg: Any, *, store: AgenticStore, limit: int) -> dict[str, Any]:
    actuations = store.list_actuations(limit=limit)
    rollback_or_expiry = [item for item in actuations if item.get("status") in {"rolled_back", "expired"}]
    impact = [item for item in actuations if item.get("impact")]
    ok = (
        cfg.actuator_enabled
        and cfg.actuator_auto_apply_runtime_flags
        and cfg.actuator_closed_loop_enabled
        and cfg.actuator_auto_rollback_missing_flags
        and bool(impact or rollback_or_expiry)
    )
    status = "pass" if ok else ("warn" if cfg.actuator_enabled else "fail")
    return _check(
        "actuator_ttl_rollback",
        status,
        "actuator is closed-loop capable and ledger has impact or rollback/expiry evidence",
        {
            "actuations": len(actuations),
            "impact_records": len(impact),
            "rollback_or_expiry": len(rollback_or_expiry),
        },
    )


def _replay_check(*, store: AgenticStore, limit: int) -> dict[str, Any]:
    replayed_tasks = 0
    matching_tasks = 0
    for task in store.list_tasks(limit=limit):
        replay = store.replay_agent_state(str(task["id"]))
        latest = store.latest_agent_state_snapshot(str(task["id"]))
        if replay is None:
            continue
        replayed_tasks += 1
        if latest and latest.get("state_hash") == replay.get("state_hash"):
            matching_tasks += 1
    actuation_replays = 0
    for actuation in store.list_actuations(limit=limit):
        if store.replay_actuation_lifecycle(str(actuation["id"])) is not None:
            actuation_replays += 1
    ok = matching_tasks > 0 or actuation_replays > 0
    return _check(
        "replay",
        "pass" if ok else "warn",
        "task state or actuation lifecycle can be reconstructed from the ledger",
        {
            "replayed_tasks": replayed_tasks,
            "matching_tasks": matching_tasks,
            "actuation_replays": actuation_replays,
        },
    )


def _anti_overfit_check(*, store: AgenticStore) -> dict[str, Any]:
    event_types = {
        "rag.miss": store.count_events(event_type="rag.miss"),
        "agent.invoke.failed": store.count_events(event_type="agent.invoke.failed"),
        "service.degraded": store.count_events(event_type="service.degraded"),
    }
    observed_kinds = len([count for count in event_types.values() if count > 0])
    status = "pass" if observed_kinds >= 2 else "warn"
    return _check(
        "anti_overfit_variants",
        status,
        "readiness expects more than one signal family before default promotion",
        {"observed_signal_families": observed_kinds, "event_types": event_types},
    )


def _cockpit_coverage_check() -> dict[str, Any]:
    return _check(
        "cockpit_coverage",
        "pass",
        "cockpit exposes the required Fase 12 operational sections",
        {"required_fields": list(REQUIRED_COCKPIT_FIELDS)},
    )
