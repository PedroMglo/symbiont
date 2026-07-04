"""RAG provider — semantic search via obsidian-rag HTTP API."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from research.config import get_settings
from research.types import SearchResult, SearchStatus

log = logging.getLogger(__name__)

# Circuit breaker state
_lock = threading.Lock()
_failures: dict[str, int] = {}
_open_until: dict[str, float] = {}


def _item_content(item: dict) -> str:
    """RAG currently returns both fields; keep a fallback for older deployments."""
    return str(item.get("content") or item.get("text") or "")


def _estimate_tokens(content: str) -> int:
    return max(1, len(content) // 4) if content else 0


def _is_circuit_open(endpoint: str) -> bool:
    cfg = get_settings()
    with _lock:
        failures = _failures.get(endpoint, 0)
        if failures >= cfg.rag.circuit_breaker_threshold:
            if time.monotonic() < _open_until.get(endpoint, 0.0):
                return True
            _failures[endpoint] = cfg.rag.circuit_breaker_threshold - 1
        return False


def _record_failure(endpoint: str) -> None:
    cfg = get_settings()
    with _lock:
        _failures[endpoint] = _failures.get(endpoint, 0) + 1
        if _failures[endpoint] >= cfg.rag.circuit_breaker_threshold:
            _open_until[endpoint] = time.monotonic() + cfg.rag.circuit_breaker_reset_seconds
            log.warning(
                "RAG circuit breaker OPEN for %s (will retry in %ds)",
                endpoint,
                cfg.rag.circuit_breaker_reset_seconds,
            )


def _record_success(endpoint: str) -> None:
    with _lock:
        _failures[endpoint] = 0


def _citation_ref(item: dict, *, fallback: str) -> str:
    path = str(item.get("source_path") or item.get("path") or "")
    section = str(item.get("section_header") or item.get("symbol_type") or "")
    if path and section:
        return f"{path}#{section}"
    return path or str(item.get("source_id") or item.get("repo_name") or fallback)


def _result_from_item(
    item: dict,
    *,
    source: str,
    source_type: str,
    retrieval_mode: str,
    top_k: int,
) -> SearchResult:
    content = _item_content(item)
    path = str(item.get("source_path") or item.get("path") or "")
    upstream_source_type = str(item.get("source_type") or "")
    metadata = {
        "upstream_source_type": upstream_source_type or None,
        "note_title": item.get("note_title"),
        "section_header": item.get("section_header"),
        "repo_name": item.get("repo_name"),
        "symbol_type": item.get("symbol_type"),
    }
    return SearchResult(
        source=source,
        source_type=source_type,
        content=content,
        score=float(item.get("score") or 0.0),
        citation_ref=_citation_ref(item, fallback=source),
        retrieval_mode=retrieval_mode,
        token_cost=_estimate_tokens(content),
        freshness=str(item.get("freshness") or item.get("fresh") or "unknown"),
        limits={"top_k": top_k},
        source_id=str(item.get("source_id") or item.get("repo_name") or ""),
        path=path,
        timestamp=str(item.get("timestamp") or ""),
        metadata={k: v for k, v in metadata.items() if v is not None},
    )


def check_health() -> bool:
    """Quick health check for the RAG service."""
    cfg = get_settings()
    try:
        resp = httpx.get(f"{cfg.rag.url}/health", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def rag_status() -> dict[str, Any]:
    """Return read-only RAG status details through the research owner."""

    cfg = get_settings()
    headers: dict[str, str] = {}
    if cfg.rag.api_key:
        headers["Authorization"] = f"Bearer {cfg.rag.api_key}"

    health: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    retrieval: dict[str, Any] = {}
    errors: list[str] = []
    for path, target in (
        ("/health", health),
        ("/stats", stats),
        ("/status/retrieval", retrieval),
    ):
        try:
            response = httpx.get(f"{cfg.rag.url}{path}", headers=headers, timeout=3.0)
            if response.status_code >= 400:
                errors.append(f"{path}: HTTP {response.status_code}")
                continue
            data = response.json()
            if isinstance(data, dict):
                target.update(data)
        except Exception as exc:
            errors.append(f"{path}: {str(exc)[:120]}")

    total_chunks = _as_int(stats.get("total_chunks")) or 0
    code_chunks = _as_int(stats.get("code_chunks")) or 0
    bm25 = retrieval.get("bm25") if isinstance(retrieval, dict) else None
    bm25_available = [
        str(item.get("collection"))
        for item in (bm25 or [])
        if isinstance(item, dict) and item.get("available")
    ]
    content = format_rag_status_context(
        base_url=cfg.rag.url,
        health=health,
        total_chunks=total_chunks,
        code_chunks=code_chunks,
        bm25_available=bm25_available,
        errors=errors,
    )
    return {
        "content": content,
        "source": "research",
        "success": True,
        "token_estimate": max(1, len(content) // 4),
        "metadata": {
            "operation": "rag_status",
            "health": health,
            "stats": stats,
            "retrieval": retrieval,
            "total_chunks": total_chunks,
            "code_chunks": code_chunks,
            "errors": errors,
        },
    }


def format_rag_status_context(
    *,
    base_url: str,
    health: dict[str, Any],
    total_chunks: int,
    code_chunks: int,
    bm25_available: list[str],
    errors: list[str],
) -> str:
    lines = [
        f"Endpoint: `{base_url}`; health: `{health.get('status', 'unknown')}`.",
        f"Índice pesquisável: {total_chunks} chunks Obsidian, {code_chunks} chunks de código.",
    ]
    if bm25_available:
        lines.append(f"BM25 disponível para: {', '.join(bm25_available)}.")
    else:
        lines.append("BM25 não está disponível para as coleções ativas.")
    if total_chunks == 0 and code_chunks == 0:
        lines.append("Conclusão: RAG está acessível, mas o índice desta stack está vazio; perguntas de conhecimento devem declarar ausência de fontes.")
    if errors:
        lines.append(f"Avisos: {'; '.join(errors)}.")
    lines.append("Método usado: `rag.status` read-only via research feature.")
    return " ".join(lines)


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def search_notes(
    query: str,
    top_k: int = 5,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[list[SearchResult], SearchStatus]:
    """Search notes via RAG /query endpoint."""
    endpoint = "/query"
    if _is_circuit_open(endpoint):
        return [], SearchStatus.CIRCUIT_OPEN

    cfg = get_settings()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.rag.api_key:
        headers["Authorization"] = f"Bearer {cfg.rag.api_key}"

    request_payload = payload or {"query": query, "top_k": top_k}
    effective_top_k = _as_int(request_payload.get("top_k")) or top_k

    try:
        resp = httpx.post(
            f"{cfg.rag.url}/query",
            json=request_payload,
            headers=headers,
            timeout=cfg.rag.timeout_seconds,
        )
        if resp.status_code in {401, 403}:
            _record_failure(endpoint)
            return [], SearchStatus.AUTH_ERROR
        if resp.status_code != 200:
            _record_failure(endpoint)
            return [], SearchStatus.SERVICE_UNAVAILABLE

        _record_success(endpoint)
        data = resp.json()
        results = [
            _result_from_item(
                item,
                source="rag",
                source_type="notes",
                retrieval_mode="rag_notes",
                top_k=effective_top_k,
            )
            for item in data.get("results", [])
        ]
        return results, SearchStatus.OK if results else SearchStatus.NO_RESULTS

    except httpx.TimeoutException:
        _record_failure(endpoint)
        return [], SearchStatus.TIMEOUT
    except Exception:
        _record_failure(endpoint)
        return [], SearchStatus.SERVICE_UNAVAILABLE


def search_code(
    query: str,
    top_k: int = 5,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[list[SearchResult], SearchStatus]:
    """Search code via RAG /query/code endpoint."""
    endpoint = "/query/code"
    if _is_circuit_open(endpoint):
        return [], SearchStatus.CIRCUIT_OPEN

    cfg = get_settings()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.rag.api_key:
        headers["Authorization"] = f"Bearer {cfg.rag.api_key}"

    request_payload = payload or {"query": query, "top_k": top_k}
    effective_top_k = _as_int(request_payload.get("top_k")) or top_k

    try:
        resp = httpx.post(
            f"{cfg.rag.url}/query/code",
            json=request_payload,
            headers=headers,
            timeout=cfg.rag.timeout_seconds,
        )
        if resp.status_code in {401, 403}:
            _record_failure(endpoint)
            return [], SearchStatus.AUTH_ERROR
        if resp.status_code != 200:
            _record_failure(endpoint)
            return [], SearchStatus.SERVICE_UNAVAILABLE

        _record_success(endpoint)
        data = resp.json()
        results = [
            _result_from_item(
                item,
                source="rag_code",
                source_type="code",
                retrieval_mode="rag_code",
                top_k=effective_top_k,
            )
            for item in data.get("results", [])
        ]
        return results, SearchStatus.OK if results else SearchStatus.NO_RESULTS

    except httpx.TimeoutException:
        _record_failure(endpoint)
        return [], SearchStatus.TIMEOUT
    except Exception:
        _record_failure(endpoint)
        return [], SearchStatus.SERVICE_UNAVAILABLE
