"""Supervised autotuning proposals and generated overlays."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from .calibration import CALIBRATION_REPORT_PATH, CALIBRATION_TRENDS_PATH, GENERATED_DIR, STATE_DIR

AUTOTUNING_PROPOSALS_PATH = GENERATED_DIR / "autotuning.proposals.json"
AUTOTUNING_SIMULATION_PATH = GENERATED_DIR / "autotuning.simulation.json"
AUTOTUNING_APPROVALS_PATH = GENERATED_DIR / "autotuning.approvals.json"
AUTOTUNING_EFFECTIVE_PATH = GENERATED_DIR / "autotuning.effective.json"
AUTOTUNING_DECISION_HISTORY_PATH = STATE_DIR / "autotuning-decision-history.json"
MAX_DECISION_HISTORY_ENTRIES = 100

AUTOTUNING_PROPOSALS_CONTRACT = "ai-local.autotuning-proposals.v1"
AUTOTUNING_SIMULATION_CONTRACT = "ai-local.autotuning-simulation.v1"
AUTOTUNING_APPROVAL_CONTRACT = "ai-local.autotuning-approval.v1"
AUTOTUNING_EFFECTIVE_CONTRACT = "ai-local.autotuning-effective.v1"
AUTOTUNING_DECISION_HISTORY_CONTRACT = "ai-local.autotuning-decision-history.v1"

SUPPORTED_OVERLAY_TARGETS = {
    "resource_governor.limits.storage_workers",
    "resource_governor.operational_authority.pressure_gates.thermal",
}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(payload: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_history(path: Path = AUTOTUNING_DECISION_HISTORY_PATH) -> list[dict[str, Any]]:
    payload = _load_json(path)
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    return [item for item in entries if isinstance(item, dict)]


def _append_history(
    entry: dict[str, Any],
    *,
    path: Path = AUTOTUNING_DECISION_HISTORY_PATH,
    max_entries: int = MAX_DECISION_HISTORY_ENTRIES,
) -> list[dict[str, Any]]:
    entries = _load_history(path)
    entries.append(entry)
    entries = entries[-max_entries:]
    _write_json(
        {
            "schema_version": 1,
            "contract": AUTOTUNING_DECISION_HISTORY_CONTRACT,
            "updated_at": _utc_now(),
            "max_entries": max_entries,
            "entries": entries,
        },
        path,
    )
    return entries


def _metric(payload: dict[str, Any], path: tuple[str, ...]) -> float | None:
    current: Any = payload
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    if isinstance(current, bool) or current is None:
        return None
    try:
        return float(current)
    except (TypeError, ValueError):
        return None


def _hint_ids(trends: dict[str, Any]) -> set[str]:
    hints = trends.get("hints") if isinstance(trends.get("hints"), list) else []
    return {item.get("id") for item in hints if isinstance(item, dict) and item.get("id")}


def _current_limit(report: dict[str, Any], key: str) -> Any:
    resource_governor = report.get("resource_governor") if isinstance(report.get("resource_governor"), dict) else {}
    limits = resource_governor.get("limits") if isinstance(resource_governor.get("limits"), dict) else {}
    return limits.get(key)


def _proposal(
    *,
    proposal_id: str,
    title: str,
    domain: str,
    target: str,
    current: Any,
    proposed: Any,
    rationale: str,
    evidence: dict[str, Any],
    risk: str = "medium",
    confidence: str = "medium",
) -> dict[str, Any]:
    return {
        "id": proposal_id,
        "title": title,
        "domain": domain,
        "status": "proposed",
        "apply_mode": "manual_review_only",
        "risk": risk,
        "confidence": confidence,
        "target": target,
        "current": current,
        "proposed": proposed,
        "rationale": rationale,
        "evidence": evidence,
        "requires_approval": True,
        "auto_apply": False,
    }


def build_autotuning_proposals(
    *,
    report: dict[str, Any] | None = None,
    trends: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    report = report or {}
    trends = trends or {}
    hints = _hint_ids(trends)
    proposals: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []

    sample_count = int(trends.get("sample_count") or 0)
    report_status = str(report.get("status") or "unknown")
    trend_status = str(trends.get("status") or "unknown")
    storage_mode = report.get("storage_mode")

    if storage_mode == "local_fallback":
        observations.append(
            {
                "id": "storage-local-fallback-accepted",
                "severity": "info",
                "message": "External storage is absent but local_fallback is accepted as operational.",
            }
        )

    if sample_count < 3:
        observations.append(
            {
                "id": "insufficient-history-for-aggressive-tuning",
                "severity": "info",
                "message": "Fewer than three calibration samples are available; only conservative proposals are allowed.",
            }
        )

    if report_status == "blocked" or trend_status == "blocked" or "trend-blockers-present" in hints:
        proposals.append(
            _proposal(
                proposal_id="review-blocked-calibration-before-apply",
                title="Review blocked calibration before any tuning apply",
                domain="safety",
                target="autotuning.apply_gate",
                current="manual_review_only",
                proposed="keep_manual_review_only",
                rationale="A blocked calibration or trend was observed; supervised review must remain mandatory.",
                evidence={
                    "report_status": report_status,
                    "trend_status": trend_status,
                    "hint_ids": sorted(hints),
                },
                risk="high",
                confidence="high",
            )
        )

    storage_avg = _metric(trends, ("averages", "storage_write_mib_s"))
    storage_current = _metric(report, ("benchmarks", "storage_write", "throughput_mib_s"))
    storage_workers = _current_limit(report, "storage_workers")
    if "trend-storage-slow" in hints or (storage_avg is not None and storage_avg < 10) or (
        storage_current is not None and storage_current < 10
    ):
        if storage_workers != 1:
            proposals.append(
                _proposal(
                    proposal_id="limit-storage-workers-under-slow-storage",
                    title="Limit storage workers while storage throughput is low",
                    domain="resource_governor",
                    target="resource_governor.limits.storage_workers",
                    current=storage_workers,
                    proposed=1,
                    rationale="Low storage throughput should keep archive/indexing work serialized.",
                    evidence={
                        "storage_write_mib_s": storage_current,
                        "storage_write_avg_mib_s": storage_avg,
                        "hint_ids": sorted(hints),
                    },
                    risk="low",
                    confidence="high",
                )
            )
        else:
            observations.append(
                {
                    "id": "storage-workers-already-conservative",
                    "severity": "info",
                    "message": "Storage throughput is constrained, but storage_workers is already set to 1.",
                }
            )

    docker_avg = _metric(trends, ("averages", "docker_ps_elapsed_ms"))
    if "trend-docker-latency-high" in hints or (docker_avg is not None and docker_avg > 1000):
        proposals.append(
            _proposal(
                proposal_id="batch-docker-reconcile-under-control-plane-latency",
                title="Batch Docker reconcile actions when control plane latency is high",
                domain="lifecycle",
                target="docker.reconcile.strategy",
                current="immediate_when_requested",
                proposed="batch_and_require_approval",
                rationale="High Docker control plane latency makes lifecycle churn visible to foreground work.",
                evidence={"docker_ps_avg_ms": docker_avg, "hint_ids": sorted(hints)},
                risk="medium",
                confidence="medium",
            )
        )

    thermal_avg = _metric(trends, ("averages", "thermal_max_celsius"))
    if "trend-thermal-high" in hints or (thermal_avg is not None and thermal_avg >= 85):
        proposals.append(
            _proposal(
                proposal_id="defer-background-gpu-under-high-thermal-trend",
                title="Defer background GPU and storage work under high thermal trend",
                domain="resource_governor",
                target="resource_governor.operational_authority.pressure_gates.thermal",
                current="defer_background_storage_gpu",
                proposed="defer_background_storage_gpu",
                rationale="Thermal readings are high enough to keep foreground work protected.",
                evidence={"thermal_avg_celsius": thermal_avg, "hint_ids": sorted(hints)},
                risk="low",
                confidence="medium",
            )
        )

    if not proposals:
        observations.append(
            {
                "id": "no-supervised-tuning-change-proposed",
                "severity": "info",
                "message": "Current calibration and trends do not justify a supervised tuning change.",
            }
        )

    return {
        "schema_version": 1,
        "contract": AUTOTUNING_PROPOSALS_CONTRACT,
        "generated_at": generated_at or _utc_now(),
        "status": "proposals_ready" if proposals else "no_changes",
        "apply_mode": "manual_review_only",
        "source": {
            "calibration_status": report_status,
            "trend_status": trend_status,
            "sample_count": sample_count,
            "storage_mode": storage_mode,
            "profile": report.get("profile"),
            "llm_backend": report.get("llm_backend"),
        },
        "safeguards": [
            "no_auto_apply",
            "requires_human_approval",
            "local_fallback_is_not_blocker",
            "recommendations_are_advisory",
        ],
        "proposals": proposals,
        "observations": observations,
    }


def write_autotuning_proposals(payload: dict[str, Any], output_path: Path = AUTOTUNING_PROPOSALS_PATH) -> None:
    _write_json(payload, output_path)


def _change_from_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    proposal_id = str(proposal.get("id") or proposal.get("target") or "unknown")
    target = str(proposal.get("target") or "")
    current = proposal.get("current")
    proposed = proposal.get("proposed")
    supported_target = target in SUPPORTED_OVERLAY_TARGETS
    changes_value = current != proposed
    applyable = supported_target and changes_value
    if not supported_target:
        blocked_reason = "target is not an approved generated-overlay path"
    elif not changes_value:
        blocked_reason = "proposal does not change the current value"
    else:
        blocked_reason = None
    return {
        "proposal_id": proposal_id,
        "domain": proposal.get("domain"),
        "target": target,
        "current": current,
        "proposed": proposed,
        "diff": {
            "target": target,
            "before": current,
            "after": proposed,
        },
        "reason": proposal.get("rationale"),
        "evidence": proposal.get("evidence") or {},
        "risk": proposal.get("risk"),
        "confidence": proposal.get("confidence"),
        "requires_approval": True,
        "supported": supported_target,
        "applyable": applyable,
        "blocked_reason": blocked_reason,
        "rollback": {
            "target": target,
            "restore": current,
        },
    }


def build_autotuning_simulation(
    proposals_payload: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a reviewable simulation without mutating source config."""

    proposals = proposals_payload.get("proposals") if isinstance(proposals_payload.get("proposals"), list) else []
    changes = [_change_from_proposal(item) for item in proposals if isinstance(item, dict)]
    applyable_count = sum(1 for item in changes if item["applyable"])
    if not changes:
        status = "no_changes"
    elif applyable_count:
        status = "approval_required"
    else:
        status = "review_only"
    return {
        "schema_version": 1,
        "contract": AUTOTUNING_SIMULATION_CONTRACT,
        "generated_at": generated_at or _utc_now(),
        "status": status,
        "apply_mode": "manual_review_only",
        "source": {
            "proposal_contract": proposals_payload.get("contract"),
            "proposal_generated_at": proposals_payload.get("generated_at"),
            "proposal_status": proposals_payload.get("status"),
        },
        "gates": [
            {
                "id": "human-approval-required",
                "status": "required" if applyable_count else "not_applicable",
                "reason": "Autotuning never applies automatically.",
            },
            {
                "id": "generated-overlay-only",
                "status": "passed",
                "reason": "Apply writes .local/generated/autotuning.effective.json and does not edit source config.",
            },
            {
                "id": "supported-targets-only",
                "status": "passed" if all(item["supported"] for item in changes if item["applyable"]) else "review",
                "reason": "Only config-owned Resource Governor overlay targets can be applied.",
            },
        ],
        "changes": changes,
        "summary": {
            "proposal_count": len(changes),
            "applyable_count": applyable_count,
            "blocked_count": sum(1 for item in changes if not item["applyable"]),
        },
    }


def write_autotuning_simulation(payload: dict[str, Any], output_path: Path = AUTOTUNING_SIMULATION_PATH) -> None:
    _write_json(payload, output_path)


def approve_autotuning_simulation(
    simulation: dict[str, Any],
    proposal_ids: list[str],
    *,
    approver: str = "manual",
    reason: str = "",
    generated_at: str | None = None,
) -> dict[str, Any]:
    changes = simulation.get("changes") if isinstance(simulation.get("changes"), list) else []
    available = {str(item.get("proposal_id")): item for item in changes if isinstance(item, dict)}
    requested = [item for item in proposal_ids if item]
    invalid = sorted(item for item in requested if item not in available)
    blocked = sorted(item for item in requested if item in available and not available[item].get("applyable"))
    approved = sorted(item for item in requested if item in available and available[item].get("applyable"))
    status = "approved" if approved and not invalid and not blocked else "blocked"
    return {
        "schema_version": 1,
        "contract": AUTOTUNING_APPROVAL_CONTRACT,
        "generated_at": generated_at or _utc_now(),
        "status": status,
        "apply_mode": "manual_review_only",
        "approver": approver,
        "reason": reason,
        "simulation_generated_at": simulation.get("generated_at"),
        "approved_proposal_ids": approved,
        "invalid_proposal_ids": invalid,
        "blocked_proposal_ids": blocked,
    }


def write_autotuning_approval(payload: dict[str, Any], output_path: Path = AUTOTUNING_APPROVALS_PATH) -> None:
    _write_json(payload, output_path)


def apply_autotuning_approval(
    simulation: dict[str, Any],
    approval: dict[str, Any],
    *,
    output_path: Path = AUTOTUNING_EFFECTIVE_PATH,
    history_path: Path = AUTOTUNING_DECISION_HISTORY_PATH,
    generated_at: str | None = None,
) -> dict[str, Any]:
    changes = simulation.get("changes") if isinstance(simulation.get("changes"), list) else []
    available = {str(item.get("proposal_id")): item for item in changes if isinstance(item, dict)}
    approved_ids = [str(item) for item in approval.get("approved_proposal_ids", []) if item]
    selected = [available[item] for item in approved_ids if item in available]
    invalid = sorted(item for item in approved_ids if item not in available)
    blocked = sorted(item["proposal_id"] for item in selected if not item.get("applyable"))
    approval_status = str(approval.get("status") or "unknown")
    timestamp = generated_at or _utc_now()

    if approval_status != "approved" or invalid or blocked or not selected:
        result = {
            "schema_version": 1,
            "contract": AUTOTUNING_EFFECTIVE_CONTRACT,
            "generated_at": timestamp,
            "status": "blocked",
            "reason": "approval is not applyable",
            "approval_status": approval_status,
            "invalid_proposal_ids": invalid,
            "blocked_proposal_ids": blocked,
            "approved_proposal_ids": approved_ids,
        }
        _append_history({"action": "apply", **result}, path=history_path)
        return result

    effective = {
        "schema_version": 1,
        "contract": AUTOTUNING_EFFECTIVE_CONTRACT,
        "generated_at": timestamp,
        "status": "applied",
        "apply_mode": "manual_review_only",
        "approved_by": approval.get("approver") or "manual",
        "approval_reason": approval.get("reason") or "",
        "source_simulation_generated_at": simulation.get("generated_at"),
        "overrides": [
            {
                "proposal_id": item["proposal_id"],
                "target": item["target"],
                "value": item["proposed"],
                "reason": item.get("reason"),
                "risk": item.get("risk"),
                "confidence": item.get("confidence"),
                "rollback": item.get("rollback") or {},
            }
            for item in selected
        ],
    }
    _write_json(effective, output_path)
    _append_history({"action": "apply", **effective}, path=history_path)
    return effective


def rollback_autotuning_effective(
    *,
    effective_path: Path = AUTOTUNING_EFFECTIVE_PATH,
    history_path: Path = AUTOTUNING_DECISION_HISTORY_PATH,
    generated_at: str | None = None,
) -> dict[str, Any]:
    timestamp = generated_at or _utc_now()
    if not effective_path.exists():
        result = {
            "schema_version": 1,
            "contract": AUTOTUNING_EFFECTIVE_CONTRACT,
            "generated_at": timestamp,
            "status": "no_effective_overlay",
            "path": str(effective_path),
        }
        _append_history({"action": "rollback", **result}, path=history_path)
        return result
    rolled_back_overlay = _load_json(effective_path)
    effective_path.unlink()
    result = {
        "schema_version": 1,
        "contract": AUTOTUNING_EFFECTIVE_CONTRACT,
        "generated_at": timestamp,
        "status": "rolled_back",
        "path": str(effective_path),
        "rolled_back_overrides": (
            rolled_back_overlay.get("overrides")
            if isinstance(rolled_back_overlay.get("overrides"), list)
            else []
        ),
    }
    _append_history({"action": "rollback", **result}, path=history_path)
    return result


def _ids_from_arg(raw: str, simulation: dict[str, Any]) -> list[str]:
    if raw.strip().lower() == "all":
        changes = simulation.get("changes") if isinstance(simulation.get("changes"), list) else []
        return [str(item.get("proposal_id")) for item in changes if isinstance(item, dict) and item.get("applyable")]
    return [item.strip() for item in raw.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m config.autotuning")
    parser.add_argument("--report", default=str(CALIBRATION_REPORT_PATH), metavar="PATH")
    parser.add_argument("--trends", default=str(CALIBRATION_TRENDS_PATH), metavar="PATH")
    parser.add_argument("--write", nargs="?", const=str(AUTOTUNING_PROPOSALS_PATH), metavar="PATH")
    parser.add_argument("--simulate", nargs="?", const=str(AUTOTUNING_SIMULATION_PATH), metavar="PATH")
    parser.add_argument("--simulation", default=str(AUTOTUNING_SIMULATION_PATH), metavar="PATH")
    parser.add_argument("--approve", metavar="PROPOSAL_IDS")
    parser.add_argument("--approver", default="manual")
    parser.add_argument("--approval-reason", default="")
    parser.add_argument("--approval-output", default=str(AUTOTUNING_APPROVALS_PATH), metavar="PATH")
    parser.add_argument("--apply-approved", nargs="?", const=str(AUTOTUNING_APPROVALS_PATH), metavar="PATH")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.rollback:
        action_payload = rollback_autotuning_effective()
        if args.json:
            print(json.dumps(action_payload, indent=2, sort_keys=True))
        else:
            print(f"autotuning rollback: {action_payload['status']}")
        return 0 if action_payload["status"] in {"rolled_back", "no_effective_overlay"} else 1

    report = _load_json(Path(args.report))
    trends = _load_json(Path(args.trends))
    payload = build_autotuning_proposals(report=report, trends=trends)
    action_payload: dict[str, Any] | None = None
    simulation = build_autotuning_simulation(payload)

    if args.write:
        write_autotuning_proposals(payload, Path(args.write))
    if args.simulate:
        write_autotuning_simulation(simulation, Path(args.simulate))
        action_payload = simulation
    if args.approve:
        proposal_ids = _ids_from_arg(args.approve, simulation)
        approval = approve_autotuning_simulation(
            simulation,
            proposal_ids,
            approver=args.approver,
            reason=args.approval_reason,
        )
        write_autotuning_approval(approval, Path(args.approval_output))
        action_payload = approval
    if args.apply_approved:
        approval = _load_json(Path(args.apply_approved))
        simulation_path = Path(args.simulation)
        simulation_for_apply = _load_json(simulation_path) if simulation_path.exists() else simulation
        action_payload = apply_autotuning_approval(simulation_for_apply, approval)

    if args.json:
        print(json.dumps(action_payload or payload, indent=2, sort_keys=True))
    else:
        active = action_payload or payload
        print(f"autotuning: {active['status']}")
        print(f"apply_mode: {payload['apply_mode']}")
        print(f"sample_count: {payload['source']['sample_count']}")
        print(f"proposals: {len(payload['proposals'])}")
        print(f"observations: {len(payload['observations'])}")
        if args.write:
            print(f"Generated: {args.write}")
        if args.simulate:
            print(f"Generated: {args.simulate}")
        if args.approve:
            print(f"Generated: {args.approval_output}")
        if args.apply_approved and action_payload and action_payload.get("status") == "applied":
            print(f"Generated: {AUTOTUNING_EFFECTIVE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
