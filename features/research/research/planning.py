"""Query planning for the Research feature."""

from __future__ import annotations

from typing import Any

from research.types import ResearchQueryPlan, SearchRequest

_SUPPORTED_INTENTS = {
    "general",
    "factual",
    "code",
    "graph",
    "historical",
    "broad",
    "narrow",
    "local",
    "system",
}

_CAG_INTENT_BY_QUERY_INTENT = {
    "general": "local",
    "factual": "local",
    "code": "code",
    "graph": "graph",
    "historical": "local",
    "broad": "local",
    "narrow": "local",
    "local": "local",
    "system": "system",
}


def build_query_plan(request: SearchRequest, settings: Any) -> ResearchQueryPlan:
    requested_intent = (request.intent or "general").strip().lower().replace("-", "_")
    warnings: list[str] = []
    if requested_intent not in _SUPPORTED_INTENTS:
        warnings.append(f"unknown intent '{request.intent}' normalized to general")
        normalized_intent = "general"
    else:
        normalized_intent = requested_intent

    budget_tokens = max(0, int(request.budget_tokens))
    notes_top_k = _notes_top_k(normalized_intent, budget_tokens, settings.search.max_top_k)
    include_code, include_code_reason = _include_code(normalized_intent, request.include_code)
    code_top_k = _code_top_k(normalized_intent, budget_tokens, settings.search.max_top_k)
    cag_intent = _CAG_INTENT_BY_QUERY_INTENT[normalized_intent]
    cag_budget_tokens = _cag_budget(normalized_intent, budget_tokens)
    namespace = _namespace_from_request(request)
    source_scoped = bool(namespace or request.source_paths)
    include_cag = not source_scoped
    include_cag_reason = (
        "skipped because caller restricted retrieval to explicit local sources"
        if source_scoped
        else _pack_selection_reason(normalized_intent, cag_intent)
    )

    notes_payload = {"query": request.query, "top_k": notes_top_k}
    code_payload = {"query": request.query, "top_k": code_top_k} if include_code else {}
    if namespace:
        notes_payload["vault"] = namespace
        if include_code:
            code_payload["repo"] = namespace
    cag_payload = {"intent": cag_intent, "budget": cag_budget_tokens}

    retrieval_modes = ["rag_notes"]
    if include_code:
        retrieval_modes.append("rag_code")
    if include_cag:
        retrieval_modes.append("cag_pack")

    return ResearchQueryPlan(
        requested_intent=request.intent,
        normalized_intent=normalized_intent,
        source_namespace=namespace,
        source_scoped=source_scoped,
        include_code=include_code,
        include_code_reason=include_code_reason,
        include_cag=include_cag,
        include_cag_reason=include_cag_reason,
        budget_tokens=budget_tokens,
        budget_reason=_budget_reason(normalized_intent, budget_tokens, notes_top_k, code_top_k),
        pack_selection=cag_intent,
        pack_selection_reason=include_cag_reason,
        notes_top_k=notes_top_k,
        code_top_k=code_top_k,
        cag_budget_tokens=cag_budget_tokens,
        notes_payload=notes_payload,
        code_payload=code_payload,
        cag_payload=cag_payload,
        retrieval_modes=retrieval_modes,
        warnings=warnings,
    )


def _namespace_from_request(request: SearchRequest) -> str:
    explicit = str(getattr(request, "namespace", "") or "").strip()
    if explicit:
        return explicit[:120]
    metadata = getattr(request, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("namespace", "source_namespace", "source_name", "repo_name", "vault"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value[:120]
    return ""


def _base_top_k(budget_tokens: int, max_top_k: int) -> int:
    return min(max_top_k, max(1, budget_tokens // 400))


def _notes_top_k(intent: str, budget_tokens: int, max_top_k: int) -> int:
    base = _base_top_k(budget_tokens, max_top_k)
    if intent in {"broad", "historical"}:
        return min(max_top_k, max(1, base * 2))
    if intent == "narrow":
        return min(base, 2)
    return base


def _code_top_k(intent: str, budget_tokens: int, max_top_k: int) -> int:
    base = _base_top_k(budget_tokens, max_top_k)
    if intent == "code":
        return min(max_top_k, max(base, min(5, max_top_k)))
    if intent == "broad":
        return min(max_top_k, max(1, base * 2))
    return base


def _include_code(intent: str, requested: bool) -> tuple[bool, str]:
    if not requested:
        return False, "disabled by request include_code=false"
    if intent == "code":
        return True, "code intent requires code retrieval when the caller permits it"
    if intent in {"general", "broad", "system"}:
        return True, f"{intent} intent benefits from code evidence when available"
    return False, f"{intent} intent prioritizes notes/CAG over code retrieval"


def _cag_budget(intent: str, budget_tokens: int) -> int:
    if budget_tokens <= 0:
        return 0
    if intent in {"factual", "narrow"}:
        return max(1, budget_tokens // 3)
    if intent == "historical":
        return max(1, budget_tokens // 2)
    return budget_tokens


def _budget_reason(intent: str, budget_tokens: int, notes_top_k: int, code_top_k: int) -> str:
    if intent in {"broad", "historical"}:
        strategy = "expanded recall"
    elif intent in {"factual", "narrow"}:
        strategy = "compact precision"
    elif intent == "code":
        strategy = "code-focused recall"
    else:
        strategy = "balanced retrieval"
    return (
        f"{strategy}: budget_tokens={budget_tokens}, notes_top_k={notes_top_k}, "
        f"code_top_k={code_top_k}"
    )


def _pack_selection_reason(intent: str, cag_intent: str) -> str:
    if intent == "code":
        return "code intent selects code CAG packs"
    if intent == "graph":
        return "graph intent selects graph CAG packs"
    if intent == "system":
        return "system intent selects system CAG packs"
    return f"{intent} intent selects {cag_intent} CAG packs"
