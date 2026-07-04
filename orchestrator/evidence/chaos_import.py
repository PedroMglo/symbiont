"""Import simulation-only chaos proposals into the agentic ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.agentic.store import AgenticStore, get_agentic_store
from orchestrator.evidence.local_resilience import local_resilience_report_path


def _read_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("chaos report top-level JSON value must be an object")
    return loaded


def _proposal_payload(simulation: dict[str, Any]) -> dict[str, Any] | None:
    proposal = simulation.get("proposal")
    if not isinstance(proposal, dict):
        return None
    payload = proposal.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("operation") != "set_runtime_flag":
        return None
    if not payload.get("key") or not isinstance(payload.get("value") or {}, dict):
        return None
    return proposal


def _fingerprint(simulation_name: str, payload: dict[str, Any]) -> str:
    runtime_flag = str(payload.get("key") or "")
    value = payload.get("value") or {}
    safe_action = str(value.get("safe_action") or "")
    return f"chaos-local:{simulation_name}:{runtime_flag}:{safe_action}"


def import_chaos_proposals(
    *,
    include_pass: bool = False,
    path: Path | None = None,
    store: AgenticStore | None = None,
    imported_by: str = "agentic.api",
) -> dict[str, Any]:
    report_path = path or local_resilience_report_path("chaos")
    report = _read_report(report_path)
    store = store or get_agentic_store()
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    simulations = report.get("simulations") or []
    if not isinstance(simulations, list):
        raise ValueError("chaos report simulations must be a list")

    for simulation in simulations:
        if not isinstance(simulation, dict):
            skipped.append({"reason": "invalid_simulation"})
            continue
        name = str(simulation.get("name") or "unknown")
        scenario_status = str(simulation.get("current_scenario_status") or "unknown")
        if scenario_status == "pass" and not include_pass:
            skipped.append({"name": name, "status": scenario_status, "reason": "scenario_pass"})
            continue
        proposal = _proposal_payload(simulation)
        if proposal is None:
            skipped.append({"name": name, "status": scenario_status, "reason": "invalid_proposal_payload"})
            continue
        payload = dict(proposal["payload"])
        fp = _fingerprint(name, payload)
        created = store.create_improvement_proposal(
            kind=str(proposal.get("kind") or "runtime_resilience_guardrail"),
            title=str(proposal.get("title") or f"Import chaos proposal {name}"),
            risk_level=str(proposal.get("risk_level") or "medium"),
            confidence=float(proposal.get("confidence") or 0.0),
            score=float(proposal.get("score") or 0.0),
            payload=payload,
            evidence={
                **(proposal.get("evidence") if isinstance(proposal.get("evidence"), dict) else {}),
                "source_report": str(report_path),
                "report_generated_at": report.get("generated_at"),
                "simulation_name": name,
                "current_scenario_status": scenario_status,
                "simulation_only": True,
            },
            ttl_seconds=float(payload.get("ttl_seconds") or 300),
            metadata={
                "source": "chaos-local",
                "source_report": str(report_path),
                "generated_at": report.get("generated_at"),
                "simulation_only": True,
                "imported_by": imported_by,
                "include_pass": include_pass,
            },
            fingerprint=fp,
        )
        imported.append(
            {
                "name": name,
                "status": scenario_status,
                "proposal_id": created.get("id"),
                "fingerprint": created.get("fingerprint") or fp,
            }
        )

    return {
        "source": str(report_path),
        "generated_at": report.get("generated_at"),
        "include_pass": include_pass,
        "imported": imported,
        "skipped": skipped,
        "summary": {
            "simulations": len(simulations),
            "imported": len(imported),
            "skipped": len(skipped),
        },
    }
