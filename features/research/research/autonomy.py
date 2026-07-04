"""Autonomic trace helpers for research responses."""

from __future__ import annotations

from collections.abc import Iterable

from research.types import (
    ResearchEvidenceBundle,
    ResearchMissReview,
    ResearchQueryPlan,
    ResearchRetrievalTrace,
    SearchStatus,
    SourceStatus,
)

_REQUEST_BY_SOURCE = {
    "notes": "notes_payload",
    "code": "code_payload",
    "cag": "cag_payload",
}
_MODE_BY_SOURCE = {
    "notes": "rag_notes",
    "code": "rag_code",
    "cag": "cag_pack",
    "budget": "budget",
}


def build_retrieval_traces(
    *,
    evidence_bundle: list[ResearchEvidenceBundle],
    source_statuses: list[SourceStatus],
    query_plan: ResearchQueryPlan | None,
) -> list[ResearchRetrievalTrace]:
    """Build compact traces that explain where research looked."""
    traces: list[ResearchRetrievalTrace] = []
    for status in source_statuses:
        evidence_refs = _evidence_refs_for_status(status, evidence_bundle)
        trace_ref = f"research.trace:{status.source}:{status.status.value}"
        traces.append(
            ResearchRetrievalTrace(
                trace_ref=trace_ref,
                source=status.source,
                source_type=status.source_type,
                retrieval_mode=_retrieval_mode(status),
                status=status.status,
                result_count=status.result_count,
                token_cost=status.token_cost,
                searched=status.status != SearchStatus.SKIPPED and status.source != "budget",
                request=_request_for_status(status, query_plan),
                limits=status.limits,
                miss_reasons=_miss_reasons(status),
                evidence_refs=_dedupe([trace_ref, *evidence_refs]),
            )
        )
    return traces


def build_miss_review(
    *,
    answerability: str,
    status: SearchStatus,
    retrieval_traces: list[ResearchRetrievalTrace],
) -> ResearchMissReview:
    """Build an orchestrator-owned event hint without recording the event here."""
    evidence_refs = _dedupe(ref for trace in retrieval_traces for ref in trace.evidence_refs)
    miss_reasons = _dedupe(reason for trace in retrieval_traces for reason in trace.miss_reasons)
    source_statuses = {
        trace.source: {
            "status": trace.status.value,
            "result_count": trace.result_count,
            "miss_reasons": trace.miss_reasons,
            "searched": trace.searched,
        }
        for trace in retrieval_traces
    }
    should_record = answerability == "insufficient"
    reason = (
        "research returned no usable evidence"
        if should_record
        else "usable evidence was available; no rag.miss event requested"
    )
    return ResearchMissReview(
        should_record=should_record,
        reason=reason,
        evidence_refs=evidence_refs,
        payload={
            "source": "research",
            "status": status.value,
            "answerability": answerability,
            "miss_reasons": miss_reasons,
            "retrieval_trace_refs": [trace.trace_ref for trace in retrieval_traces],
            "source_statuses": source_statuses,
            "evidence_refs": evidence_refs,
        },
    )


def _retrieval_mode(status: SourceStatus) -> str:
    return _MODE_BY_SOURCE.get(status.source, status.source_type or "unknown")


def _request_for_status(
    status: SourceStatus,
    query_plan: ResearchQueryPlan | None,
) -> dict[str, str | int | float | bool | None]:
    if query_plan is None:
        return {}
    attr = _REQUEST_BY_SOURCE.get(status.source)
    if attr is None:
        return {}
    value = getattr(query_plan, attr, {})
    return dict(value) if isinstance(value, dict) else {}


def _evidence_refs_for_status(
    status: SourceStatus,
    evidence_bundle: list[ResearchEvidenceBundle],
) -> list[str]:
    refs: list[str] = []
    for item in evidence_bundle:
        if item.source_type != status.source_type:
            continue
        citation = item.citation_ref or item.source_id or item.source
        if citation:
            refs.append(f"research.evidence:{citation}")
    return refs


def _miss_reasons(status: SourceStatus) -> list[str]:
    reasons: list[str] = []
    if status.reason:
        reasons.append(status.reason)
    if status.status == SearchStatus.NO_RESULTS:
        reasons.append("no_results_returned")
    elif status.status == SearchStatus.SKIPPED:
        reasons.append("skipped_by_plan")
    elif status.status != SearchStatus.OK:
        reasons.append(status.status.value)
    if bool(status.limits.get("truncated")):
        reasons.append("truncated")
    return _dedupe(reasons)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result
