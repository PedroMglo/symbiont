"""Import supervised autotuning proposals into the agentic ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.agentic.store import AgenticStore, get_agentic_store

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUTOTUNING_PROPOSALS_PATH = ROOT / ".local" / "generated" / "autotuning.proposals.json"


def _read_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("autotuning proposals top-level JSON value must be an object")
    return loaded


def _confidence(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    mapping = {"low": 0.35, "medium": 0.65, "high": 0.85}
    return mapping.get(str(value or "").lower(), 0.5)


def _score(proposal: dict[str, Any]) -> float:
    risk = str(proposal.get("risk") or "medium").lower()
    confidence = str(proposal.get("confidence") or "medium").lower()
    risk_score = {"low": 1.0, "medium": 2.0, "high": 3.0}.get(risk, 2.0)
    confidence_bonus = {"low": 0.0, "medium": 0.5, "high": 1.0}.get(confidence, 0.5)
    return risk_score + confidence_bonus


def _payload(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "request_human_review",
        "domain": proposal.get("domain"),
        "target": proposal.get("target"),
        "current": proposal.get("current"),
        "proposed": proposal.get("proposed"),
        "safe_actions_only": True,
        "approval_required_for": ["autotuning.apply"],
        "auto_apply": False,
    }


def _fingerprint(proposal: dict[str, Any]) -> str:
    payload = _payload(proposal)
    return ":".join(
        [
            "autotuning",
            str(proposal.get("id") or "unknown"),
            str(payload.get("domain") or "unknown"),
            str(payload.get("target") or "unknown"),
            json.dumps(payload.get("proposed"), sort_keys=True, default=str),
        ]
    )


def import_autotuning_proposals(
    *,
    path: Path | None = None,
    store: AgenticStore | None = None,
    imported_by: str = "agentic.api",
) -> dict[str, Any]:
    report_path = path or DEFAULT_AUTOTUNING_PROPOSALS_PATH
    report = _read_report(report_path)
    store = store or get_agentic_store()
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    proposals = report.get("proposals") or []
    if not isinstance(proposals, list):
        raise ValueError("autotuning proposals must be a list")

    for proposal in proposals:
        if not isinstance(proposal, dict):
            skipped.append({"reason": "invalid_proposal"})
            continue
        proposal_id = str(proposal.get("id") or "unknown")
        if proposal.get("auto_apply") is True:
            skipped.append({"id": proposal_id, "reason": "auto_apply_payload_rejected"})
            continue
        if proposal.get("requires_approval") is not True:
            skipped.append({"id": proposal_id, "reason": "missing_required_approval"})
            continue
        payload = _payload(proposal)
        created = store.create_improvement_proposal(
            kind="autotuning_supervised_proposal",
            title=str(proposal.get("title") or f"Review autotuning proposal {proposal_id}"),
            risk_level=str(proposal.get("risk") or "medium"),
            confidence=_confidence(proposal.get("confidence")),
            score=_score(proposal),
            payload=payload,
            evidence={
                **(proposal.get("evidence") if isinstance(proposal.get("evidence"), dict) else {}),
                "source_report": str(report_path),
                "report_generated_at": report.get("generated_at"),
                "autotuning_status": report.get("status"),
                "autotuning_source": report.get("source") if isinstance(report.get("source"), dict) else {},
                "safeguards": report.get("safeguards") if isinstance(report.get("safeguards"), list) else [],
            },
            metadata={
                "source": "autotuning",
                "source_report": str(report_path),
                "generated_at": report.get("generated_at"),
                "imported_by": imported_by,
                "manual_review_only": True,
                "auto_apply": False,
                "autotuning_proposal_id": proposal_id,
            },
            fingerprint=_fingerprint(proposal),
        )
        imported.append(
            {
                "id": proposal_id,
                "proposal_id": created.get("id"),
                "fingerprint": created.get("fingerprint"),
                "status": created.get("status"),
            }
        )

    if not proposals:
        skipped.append({"reason": "no_autotuning_proposals", "status": report.get("status")})

    return {
        "source": str(report_path),
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "imported": imported,
        "skipped": skipped,
        "summary": {
            "proposals": len(proposals),
            "imported": len(imported),
            "skipped": len(skipped),
        },
    }
