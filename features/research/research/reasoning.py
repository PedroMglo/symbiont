"""Reasoning-ready context builder for research evidence."""

from __future__ import annotations

from research.types import (
    ReasoningEvidenceContext,
    ResearchCitation,
    ResearchEvidenceBundle,
    ResearchQueryPlan,
    SearchStatus,
    SourceStatus,
)

_MAX_CONTEXT_TOKENS = 1200
_HEADER_RESERVE_TOKENS = 80
_DEGRADED_STATUSES = {
    SearchStatus.AUTH_ERROR,
    SearchStatus.CIRCUIT_OPEN,
    SearchStatus.DEGRADED,
    SearchStatus.SERVICE_UNAVAILABLE,
    SearchStatus.TIMEOUT,
}


def build_reasoning_context(
    *,
    evidence_bundle: list[ResearchEvidenceBundle],
    source_statuses: list[SourceStatus],
    budget_tokens: int,
    query_plan: ResearchQueryPlan | None = None,
) -> ReasoningEvidenceContext:
    """Build a bounded, citation-aware context for reasoning agents."""
    max_tokens = min(max(0, budget_tokens), _MAX_CONTEXT_TOKENS)
    usable_evidence = [item for item in evidence_bundle if item.content.strip()]
    flags = _evidence_flags(usable_evidence, source_statuses)

    if not usable_evidence or max_tokens <= 0:
        flags = _dedupe([*flags, "insufficient_evidence"])
        content = _header(
            answerability="insufficient",
            reason="no usable evidence was returned",
            flags=flags,
            query_plan=query_plan,
        )
        return ReasoningEvidenceContext(
            content=content,
            answerability="insufficient",
            answerability_reason="no usable evidence was returned",
            flags=flags,
            bounded_tokens=_estimate_tokens(content),
            max_tokens=max_tokens,
        )

    citations = [
        ResearchCitation(
            ref=f"R{index}",
            citation_ref=item.citation_ref or item.source,
            source=item.source,
            source_type=item.source_type,
            retrieval_mode=item.retrieval_mode,
            score=item.score,
            freshness=item.freshness,
            path=item.path,
            source_id=item.source_id,
            metadata=item.metadata,
        )
        for index, item in enumerate(usable_evidence, start=1)
    ]
    body, included_indexes, truncated = _bounded_body(
        evidence_bundle=usable_evidence,
        citations=citations,
        max_tokens=max(0, max_tokens - _HEADER_RESERVE_TOKENS),
    )
    if truncated:
        flags.append("truncated_evidence")
    flags = _dedupe(flags)
    answerability, reason = _answerability(flags)
    included_citations = [citations[index] for index in included_indexes]
    header = _header(
        answerability=answerability,
        reason=reason,
        flags=flags,
        query_plan=query_plan,
    )
    content = "\n\n".join(part for part in (header, body) if part).strip()
    return ReasoningEvidenceContext(
        content=content,
        answerability=answerability,
        answerability_reason=reason,
        citations=included_citations,
        flags=flags,
        bounded_tokens=_estimate_tokens(content),
        max_tokens=max_tokens,
        sources_used=_dedupe([citation.source_type for citation in included_citations]),
    )


def _evidence_flags(
    evidence_bundle: list[ResearchEvidenceBundle],
    source_statuses: list[SourceStatus],
) -> list[str]:
    flags: list[str] = []
    if any(item.freshness.strip().lower() == "stale" for item in evidence_bundle):
        flags.append("stale_evidence")
    if any(bool(item.limits.get("truncated")) for item in evidence_bundle):
        flags.append("truncated_evidence")
    if any(bool(status.limits.get("truncated")) for status in source_statuses):
        flags.append("truncated_evidence")
    if any(status.status in _DEGRADED_STATUSES for status in source_statuses):
        flags.append("degraded_sources")
    return flags


def _answerability(flags: list[str]) -> tuple[str, str]:
    if "insufficient_evidence" in flags:
        return "insufficient", "no usable evidence was returned"
    if "stale_evidence" in flags:
        return "partial", "usable evidence was returned but at least one source is stale"
    if "truncated_evidence" in flags:
        return "partial", "usable evidence was returned but context was truncated"
    if "degraded_sources" in flags:
        return "partial", "usable evidence was returned while at least one source degraded"
    return "answerable", "usable evidence returned within current context bounds"


def _bounded_body(
    *,
    evidence_bundle: list[ResearchEvidenceBundle],
    citations: list[ResearchCitation],
    max_tokens: int,
) -> tuple[str, list[int], bool]:
    char_limit = max(0, max_tokens * 4)
    if char_limit <= 0:
        return "", [], bool(evidence_bundle)

    chunks: list[str] = []
    included_indexes: list[int] = []
    used = 0
    truncated = False
    for index, (item, citation) in enumerate(zip(evidence_bundle, citations, strict=True)):
        header = _citation_header(item, citation)
        clean_content = _clean_text(item.content)
        section = f"{header}\n{clean_content}".strip()
        separator = "\n\n" if chunks else ""
        addition = f"{separator}{section}"
        remaining = char_limit - used

        if len(addition) <= remaining:
            chunks.append(addition)
            included_indexes.append(index)
            used += len(addition)
            continue

        truncated = True
        trimmed = _trim_section(
            header=header,
            content=clean_content,
            remaining=remaining - len(separator),
        )
        if trimmed:
            chunks.append(f"{separator}{trimmed}")
            included_indexes.append(index)
        break

    return "".join(chunks).strip(), included_indexes, truncated


def _citation_header(item: ResearchEvidenceBundle, citation: ResearchCitation) -> str:
    details = [
        citation.ref,
        item.source_type,
        item.retrieval_mode,
        f"score={item.score:.2f}",
        f"freshness={item.freshness}",
        item.citation_ref or item.source,
    ]
    return "[" + "] ".join([details[0], " | ".join(details[1:])])


def _trim_section(*, header: str, content: str, remaining: int) -> str:
    if remaining <= len(header) + 8:
        return ""
    available = remaining - len(header) - 1
    return f"{header}\n{_trim_text(content, available)}".strip()


def _trim_text(content: str, char_limit: int) -> str:
    if len(content) <= char_limit:
        return content
    if char_limit <= 16:
        return content[:char_limit].rstrip()
    return f"{content[: char_limit - 14].rstrip()} [truncated]"


def _header(
    *,
    answerability: str,
    reason: str,
    flags: list[str],
    query_plan: ResearchQueryPlan | None,
) -> str:
    parts = [
        f"Research evidence answerability: {answerability}.",
        f"Reason: {reason}.",
        f"Flags: {', '.join(flags) if flags else 'none'}.",
    ]
    if query_plan is not None:
        parts.append(
            "Plan: "
            f"intent={query_plan.normalized_intent}; "
            f"retrieval_modes={', '.join(query_plan.retrieval_modes)}; "
            f"budget_tokens={query_plan.budget_tokens}."
        )
    return "\n".join(parts)


def _clean_text(content: str) -> str:
    return " ".join(content.split())


def _estimate_tokens(content: str) -> int:
    return max(1, len(content) // 4) if content else 0


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result
