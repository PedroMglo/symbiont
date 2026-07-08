"""FastAPI application for the Research feature."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Query
from sharedai.servicekit.auth import service_token_dependency

from research import __version__
from research.autonomy import build_miss_review, build_retrieval_traces
from research.cag import get_packs
from research.config import get_settings
from research.planning import build_query_plan
from research.rag import check_health as rag_health, prepare_sources, rag_status, search_code, search_notes
from research.reasoning import build_reasoning_context
from research.types import (
    CapabilitiesResponse,
    HealthResponse,
    ReasoningEvidenceContext,
    ResearchMissReview,
    ResearchQueryPlan,
    ResearchRetrievalTrace,
    ResearchSourcePrepareRequest,
    ResearchSourcePrepareResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchStatus,
    SourceStatus,
)

app = FastAPI(title="Research Feature", version=__version__)
require_service_token = service_token_dependency(
    "Research",
    lambda: get_settings().security.api_key,
)

_PARTIAL_FAILURES = {
    SearchStatus.AUTH_ERROR,
    SearchStatus.CIRCUIT_OPEN,
    SearchStatus.DEGRADED,
    SearchStatus.SERVICE_UNAVAILABLE,
    SearchStatus.TIMEOUT,
}


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(version=__version__, rag_reachable=rag_health())


@app.get("/v1/research/capabilities")
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse()


@app.get("/v1/research/status", dependencies=[Depends(require_service_token)])
def status() -> dict[str, object]:
    """Return read-only RAG status through the research feature contract."""
    return rag_status()


@app.post("/v1/research/sources/prepare", dependencies=[Depends(require_service_token)])
def prepare_research_sources(request: ResearchSourcePrepareRequest) -> ResearchSourcePrepareResponse:
    """Prepare user-requested local sources through the RAG owner."""
    result = prepare_sources(
        [source.model_dump(mode="json") for source in request.sources],
        target=request.target,
        force=request.force,
        wait_seconds=request.wait_seconds,
        poll_interval_seconds=request.poll_interval_seconds,
    )
    return ResearchSourcePrepareResponse(**result)


@app.post("/v1/research/search", dependencies=[Depends(require_service_token)])
def search(request: SearchRequest) -> SearchResponse:
    """Perform semantic search across notes and optionally code."""
    cfg = get_settings()
    query_plan = build_query_plan(request, cfg)

    all_results: list[SearchResult] = []
    source_statuses: list[SourceStatus] = []

    # Search notes
    notes, notes_status = search_notes(
        request.query,
        top_k=query_plan.notes_top_k,
        payload=query_plan.notes_payload,
    )
    all_results.extend(notes)
    source_statuses.append(
        _source_status(
            source="notes",
            source_type="notes",
            status=notes_status,
            results=notes,
            limits={"top_k": query_plan.notes_top_k, "intent": query_plan.normalized_intent},
        )
    )

    # Search code (optional)
    if query_plan.include_code:
        code, code_status = search_code(
            request.query,
            top_k=query_plan.code_top_k,
            payload=query_plan.code_payload,
        )
        all_results.extend(code)
        source_statuses.append(
            _source_status(
                source="code",
                source_type="code",
                status=code_status,
                results=code,
                limits={"top_k": query_plan.code_top_k, "intent": query_plan.normalized_intent},
            )
        )
    else:
        source_statuses.append(
            SourceStatus(
                source="code",
                source_type="code",
                status=SearchStatus.SKIPPED,
                limits={"include_code": False, "intent": query_plan.normalized_intent},
                reason=query_plan.include_code_reason,
            )
        )

    # Add CAG packs only when the request is not restricted to explicit local sources.
    if query_plan.include_cag and query_plan.source_namespace:
        packs, packs_status = get_packs(
            intent=query_plan.pack_selection,
            budget_tokens=query_plan.cag_budget_tokens,
            scope=query_plan.source_namespace,
        )
    elif query_plan.include_cag:
        packs, packs_status = get_packs(
            intent=query_plan.pack_selection,
            budget_tokens=query_plan.cag_budget_tokens,
        )
    else:
        packs = []
        packs_status = SearchStatus.SKIPPED
    all_results.extend(packs)
    cag_limits: dict[str, str | int | float | bool | None] = {
        "budget_tokens": query_plan.cag_budget_tokens,
        "intent": query_plan.pack_selection,
    }
    if query_plan.source_namespace:
        cag_limits["scope"] = query_plan.source_namespace
    source_statuses.append(
        _source_status(
            source="cag",
            source_type="cag",
            status=packs_status,
            results=packs,
            limits=cag_limits,
            reason=query_plan.pack_selection_reason,
        )
    )

    budgeted_results, budget_status = _apply_budget(all_results, query_plan.budget_tokens)
    source_statuses.append(budget_status)

    return _response(
        results=budgeted_results,
        source_statuses=source_statuses,
        budget_tokens=query_plan.budget_tokens,
        query_plan=query_plan,
    )


@app.get("/v1/research/cag", dependencies=[Depends(require_service_token)])
def cag_packs(
    intent: str = Query(default="general"),
    budget: int = Query(default=2000),
) -> SearchResponse:
    """Fetch cached knowledge packs."""
    query_plan = build_query_plan(
        SearchRequest(query="", budget_tokens=budget, include_code=False, intent=intent),
        get_settings(),
    )
    results, status = get_packs(
        intent=query_plan.pack_selection,
        budget_tokens=query_plan.cag_budget_tokens,
    )
    budgeted_results, budget_status = _apply_budget(results, query_plan.budget_tokens)
    source_statuses = [
        _source_status(
            source="cag",
            source_type="cag",
            status=status,
            results=results,
            limits={
                "budget_tokens": query_plan.cag_budget_tokens,
                "intent": query_plan.pack_selection,
            },
            reason=query_plan.pack_selection_reason,
        ),
        budget_status,
    ]
    return _response(
        results=budgeted_results,
        source_statuses=source_statuses,
        budget_tokens=query_plan.budget_tokens,
        query_plan=query_plan,
    )


def _estimate_tokens(content: str) -> int:
    return max(1, len(content) // 4) if content else 0


def _result_token_cost(result: SearchResult) -> int:
    return result.token_cost or _estimate_tokens(result.content)


def _source_status(
    *,
    source: str,
    source_type: str,
    status: SearchStatus,
    results: list[SearchResult],
    limits: dict[str, str | int | float | bool | None],
    reason: str = "",
) -> SourceStatus:
    return SourceStatus(
        source=source,
        source_type=source_type,
        status=status,
        result_count=len(results),
        token_cost=sum(_result_token_cost(result) for result in results),
        limits=limits,
        reason=reason,
    )


def _apply_budget(
    results: list[SearchResult],
    budget_tokens: int,
) -> tuple[list[SearchResult], SourceStatus]:
    remaining = max(0, budget_tokens)
    budgeted: list[SearchResult] = []
    truncated = False

    for result in results:
        token_cost = _result_token_cost(result)
        if token_cost <= remaining:
            if result.token_cost:
                budgeted.append(result)
            else:
                budgeted.append(result.model_copy(update={"token_cost": token_cost}))
            remaining -= token_cost
            continue

        truncated = True
        if remaining > 0:
            content = result.content[: remaining * 4].rstrip()
            limits = {**result.limits, "budget_tokens": budget_tokens, "truncated": True}
            budgeted.append(
                result.model_copy(
                    update={
                        "content": content,
                        "token_cost": _estimate_tokens(content),
                        "limits": limits,
                    }
                )
            )
        break

    total_tokens = sum(_result_token_cost(result) for result in budgeted)
    return budgeted, SourceStatus(
        source="budget",
        source_type="budget",
        status=SearchStatus.DEGRADED if truncated else SearchStatus.OK,
        result_count=len(budgeted),
        token_cost=total_tokens,
        limits={
            "budget_tokens": budget_tokens,
            "used_tokens": total_tokens,
            "truncated": truncated,
        },
        reason="budget_exceeded" if truncated else "",
    )


def _aggregate_status(results: list[SearchResult], source_statuses: list[SourceStatus]) -> SearchStatus:
    statuses = [item.status for item in source_statuses]
    failures = [status for status in statuses if status in _PARTIAL_FAILURES]
    if results:
        return SearchStatus.DEGRADED if failures else SearchStatus.OK
    for status in (
        SearchStatus.AUTH_ERROR,
        SearchStatus.CIRCUIT_OPEN,
        SearchStatus.TIMEOUT,
        SearchStatus.SERVICE_UNAVAILABLE,
        SearchStatus.DEGRADED,
    ):
        if status in statuses:
            return status
    return SearchStatus.NO_RESULTS


def _response(
    *,
    results: list[SearchResult],
    source_statuses: list[SourceStatus],
    budget_tokens: int,
    query_plan: ResearchQueryPlan | None = None,
) -> SearchResponse:
    total_tokens = sum(_result_token_cost(result) for result in results)
    status = _aggregate_status(results, source_statuses)
    evidence_bundle = [result.to_evidence() for result in results]
    retrieval_traces = build_retrieval_traces(
        evidence_bundle=evidence_bundle,
        source_statuses=source_statuses,
        query_plan=query_plan,
    )
    reasoning_context = build_reasoning_context(
        evidence_bundle=evidence_bundle,
        source_statuses=source_statuses,
        budget_tokens=budget_tokens,
        query_plan=query_plan,
    )
    miss_review = build_miss_review(
        answerability=reasoning_context.answerability,
        status=status,
        retrieval_traces=retrieval_traces,
    )
    return SearchResponse(
        content=reasoning_context.content,
        results=results,
        total_tokens=total_tokens,
        status=status,
        source_statuses=source_statuses,
        degraded=status == SearchStatus.DEGRADED,
        evidence_bundle=evidence_bundle,
        limits={"budget_tokens": budget_tokens},
        query_plan=query_plan,
        reasoning_context=reasoning_context,
        retrieval_traces=retrieval_traces,
        miss_review=miss_review,
        metadata=_response_metadata(reasoning_context, retrieval_traces, miss_review),
    )


def _response_metadata(
    context: ReasoningEvidenceContext,
    retrieval_traces: list[ResearchRetrievalTrace],
    miss_review: ResearchMissReview,
) -> dict[str, object]:
    return {
        "answerability": context.answerability,
        "answerability_reason": context.answerability_reason,
        "evidence_flags": context.flags,
        "citations": [citation.model_dump(mode="json") for citation in context.citations],
        "sources_used": context.sources_used,
        "bounded_tokens": context.bounded_tokens,
        "max_tokens": context.max_tokens,
        "retrieval_traces": [trace.model_dump(mode="json") for trace in retrieval_traces],
        "miss_review": miss_review.model_dump(mode="json"),
    }
