"""CAG provider — cached knowledge packs via RAG API."""

from __future__ import annotations

import logging

import httpx

from research.config import get_settings
from research.types import SearchResult, SearchStatus

log = logging.getLogger(__name__)

_INTENT_MAP: dict[str, str] = {
    "general": "local",
    "local": "local",
    "code": "code",
    "system": "system",
    "graph": "graph",
}


def _estimate_tokens(content: str) -> int:
    return max(1, len(content) // 4) if content else 0


def get_packs(intent: str = "general", budget_tokens: int = 2000) -> tuple[list[SearchResult], SearchStatus]:
    """Fetch CAG packs from the RAG API."""
    cfg = get_settings()
    url = f"{cfg.rag.url}/cag/packs"

    params: dict[str, str | int] = {"budget": budget_tokens}
    intent_key = _INTENT_MAP.get(intent)
    if intent_key:
        params["intent"] = intent_key

    headers: dict[str, str] = {}
    if cfg.rag.api_key:
        headers["Authorization"] = f"Bearer {cfg.rag.api_key}"

    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=5.0)
        if resp.status_code in {401, 403}:
            log.debug("CAG API: auth failed with HTTP %d", resp.status_code)
            return [], SearchStatus.AUTH_ERROR
        if resp.status_code != 200:
            log.debug("CAG API: HTTP %d", resp.status_code)
            return [], SearchStatus.SERVICE_UNAVAILABLE

        data = resp.json()
        packs = data.get("packs", [])
        if not packs:
            return [], SearchStatus.NO_RESULTS

        results = []
        for pack in packs:
            content = str(pack.get("content", ""))
            pack_type = str(pack.get("pack_type") or pack.get("type") or "")
            scope = str(pack.get("scope") or "global")
            token_cost = int(pack.get("tokens") or _estimate_tokens(content))
            fresh = bool(pack.get("fresh", True))
            results.append(SearchResult(
                source="cag",
                source_type="cag",
                content=content,
                pack_type=pack_type,
                score=1.0,
                citation_ref=f"cag:{pack_type}:{scope}" if pack_type else f"cag:{scope}",
                retrieval_mode="cag_pack",
                token_cost=token_cost,
                freshness="fresh" if fresh else "stale",
                limits={"budget_tokens": budget_tokens, "intent": intent_key or intent, "scope": scope},
                source_id=scope,
                path=pack_type,
                metadata={"pack_type": pack_type, "scope": scope},
            ))
        return results, SearchStatus.OK

    except httpx.TimeoutException:
        return [], SearchStatus.TIMEOUT
    except Exception as exc:
        log.debug("CAG API: request failed: %s", exc)
        return [], SearchStatus.SERVICE_UNAVAILABLE
