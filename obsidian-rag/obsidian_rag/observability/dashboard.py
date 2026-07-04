"""Dashboard API — FastAPI router mounted under /dashboard/*.

Provides endpoints for RAG observability analytics (ClickHouse queries),
real-time SSE event feed, and static dashboard assets.
All read-only — never mutates any database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from obsidian_rag.config import settings

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


log = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_WEB_DIR = Path(__file__).parent.parent.parent / "web"

_event_feed: queue.Queue | None = None
_FEED_MAX = 200


def get_event_feed() -> queue.Queue:
    global _event_feed
    if _event_feed is None:
        _event_feed = queue.Queue(maxsize=_FEED_MAX)
    return _event_feed


def push_event(event_dict: dict) -> None:
    feed = get_event_feed()
    try:
        feed.put_nowait(event_dict)
    except queue.Full:
        try:
            feed.get_nowait()
        except queue.Empty:
            pass
        try:
            feed.put_nowait(event_dict)
        except queue.Full:
            pass


def _ch_url() -> str:
    return settings.observability.clickhouse_url


def _ch_db() -> str:
    return settings.observability.clickhouse_database


def _ch_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.observability.clickhouse_username:
        headers["X-ClickHouse-User"] = settings.observability.clickhouse_username
    import os
    pw = os.environ.get(settings.observability.clickhouse_password_env, "")
    if pw:
        headers["X-ClickHouse-Key"] = pw
    return headers


def _query_ch(sql: str) -> list[dict]:
    try:
        resp = httpx.post(
            _ch_url(),
            content=sql,
            params={"default_format": "JSONEachRow", "database": _ch_db()},
            headers=_ch_headers(),
            timeout=10.0,
        )
        if resp.status_code != 200:
            log.debug("ClickHouse query failed: %s", resp.text[:200])
            return []
        lines = resp.text.strip().split("\n")
        return [json.loads(line) for line in lines if line.strip()]
    except (httpx.HTTPError, OSError, json.JSONDecodeError) as exc:
        log.debug("ClickHouse query error: %s", exc)
        return []


# ------------------------------------------------------------------
# Summary / Overview
# ------------------------------------------------------------------


@router.get("/summary")
def dashboard_summary(days: int = Query(default=7, ge=1, le=365)):
    requests = _query_ch(_sql("fstring_112.sql").format(days))

    retrieval = _query_ch(_sql("fstring_123_2.sql").format(days))

    ingest = _query_ch(_sql("query_ch_121.sql"))

    resource = _query_ch(_sql("query_ch_131.sql"))

    return {
        "requests": requests[0] if requests else {},
        "retrieval": retrieval[0] if retrieval else {},
        "ingest": ingest[0] if ingest else {},
        "resources": resource[0] if resource else {},
        "days": days,
    }


# ------------------------------------------------------------------
# Timeline
# ------------------------------------------------------------------


@router.get("/timeline")
def dashboard_timeline(
    days: int = Query(default=7, ge=1, le=365),
    resolution: str = Query(default="hour", pattern="^(day|hour|minute)$"),
):
    bucket_fn = {
        "day": "toStartOfDay",
        "hour": "toStartOfHour",
        "minute": "toStartOfMinute",
    }[resolution]

    data = _query_ch(_sql("fstring_162_3.sql").format(bucket_fn, days))
    return {"data": data, "resolution": resolution}


# ------------------------------------------------------------------
# Retrieval Quality
# ------------------------------------------------------------------


@router.get("/retrieval")
def dashboard_retrieval(days: int = Query(default=7, ge=1, le=365)):
    summary = _query_ch(_sql("fstring_184_4.sql").format(days))

    timeline = _query_ch(_sql("fstring_197_5.sql").format(days))

    route_modes = _query_ch(_sql("fstring_210_6.sql").format(days))

    sources = _query_ch(_sql("fstring_217_7.sql").format(days))

    gate_reasons = _query_ch(_sql("fstring_224_8.sql").format(days))

    score_histogram = _query_ch(_sql("fstring_231_9.sql").format(days))

    return {
        "summary": summary[0] if summary else {},
        "timeline": timeline,
        "route_modes": route_modes,
        "sources": sources,
        "gate_reasons": gate_reasons,
        "score_histogram": score_histogram,
    }


# ------------------------------------------------------------------
# Ingest Pipeline
# ------------------------------------------------------------------


@router.get("/ingest")
def dashboard_ingest(days: int = Query(default=7, ge=1, le=365)):
    runs = _query_ch(_sql("fstring_258_10.sql").format(days))

    stages = _query_ch(_sql("fstring_273_11.sql").format(days))

    governor = _query_ch(_sql("fstring_282_12.sql").format(days))

    return {"runs": runs, "stages": stages, "governor": governor}


# ------------------------------------------------------------------
# CAG & Graph
# ------------------------------------------------------------------


@router.get("/cag")
def dashboard_cag(days: int = Query(default=7, ge=1, le=365)):
    summary = _query_ch(_sql("fstring_300_13.sql").format(days))

    timeline = _query_ch(_sql("fstring_314_14.sql").format(days))

    pack_types = _query_ch(_sql("fstring_324_15.sql").format(days))

    operations = _query_ch(_sql("fstring_331_16.sql").format(days))

    return {
        "summary": summary[0] if summary else {},
        "timeline": timeline,
        "pack_types": pack_types,
        "operations": operations,
    }


# ------------------------------------------------------------------
# Infrastructure / Resources
# ------------------------------------------------------------------


@router.get("/resources")
def dashboard_resources(hours: int = Query(default=6, ge=1, le=168)):
    current = _query_ch(_sql("query_ch_354.sql"))

    history = _query_ch(_sql("fstring_355_17.sql").format(hours))

    store_latency = _query_ch(_sql("fstring_367_18.sql").format(hours))

    embedding_latency = _query_ch(_sql("fstring_377_19.sql").format(hours))

    return {
        "current": current[0] if current else {},
        "history": history,
        "store_latency": store_latency,
        "embedding_latency": embedding_latency,
    }


# ------------------------------------------------------------------
# SSE Event Stream
# ------------------------------------------------------------------


@router.get("/events")
async def dashboard_events(request: Request):
    async def event_generator() -> AsyncIterator[str]:
        feed = get_event_feed()
        yield "data: {\"type\":\"connected\"}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                evt = feed.get(timeout=0.5)
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"
                await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------
# Static Files (CSS, JS)
# ------------------------------------------------------------------


@router.get("/assets/{filename}")
def dashboard_asset(filename: str):
    allowed_ext = {".css", ".js", ".svg", ".png", ".ico", ".woff2", ".woff"}
    path = _WEB_DIR / filename
    if path.suffix not in allowed_ext:
        raise HTTPException(status_code=404)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404)
    try:
        path.resolve().relative_to(_WEB_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404)

    media_types = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
    }
    return FileResponse(path, media_type=media_types.get(path.suffix, "application/octet-stream"))


# ------------------------------------------------------------------
# Dashboard UI
# ------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard_ui():
    html_path = _WEB_DIR / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse(
        "<html><body style='background:#0a0e14;color:#e2e8f0;font-family:sans-serif;padding:2rem'>"
        "<h1>RAG Dashboard</h1><p>web/dashboard.html not found. "
        "Run from the project root directory.</p></body></html>"
    )
