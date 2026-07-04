"""OpenAI-compatible API endpoints for generic clients."""

from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from orchestrator.config import get_settings
from orchestrator.core.sanitize import sanitize_query, validate_history
from orchestrator.gateway.schemas import (
    OpenAIChatChoice,
    OpenAIChatChoiceMessage,
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAIChatUsage,
    OpenAIModelEntry,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compat"])

PROFILE_MAP = {
    "symbiont": "",
    "symbiont-code": "code",
    "symbiont-deep": "deep",
    "symbiont-fast": "fast",
}


@router.get("/models")
async def list_models():
    cfg = get_settings()
    profiles = cfg.openai_compat_profiles or tuple(PROFILE_MAP.keys())
    now = int(time.time())
    data = [
        OpenAIModelEntry(id=p, created=now).model_dump()
        for p in profiles if p in PROFILE_MAP
    ]
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions(request: Request, body: OpenAIChatRequest):
    from orchestrator.gateway.app import _llm_semaphore, _session_store

    if body.model not in PROFILE_MAP:
        raise HTTPException(status_code=404, detail=f"Model '{body.model}' not found")

    query = _extract_user_query(body)
    history = validate_history(
        [{"role": m.role, "content": m.content} for m in body.messages[:-1]] or None
    )
    session_id = str(uuid.uuid4())
    cfg = get_settings()

    if _session_store and cfg.session.enabled:
        stored = _session_store.get(session_id)
        if stored:
            history = stored + (history or [])

    await _acquire_semaphore(_llm_semaphore)

    try:
        if body.stream:
            return StreamingResponse(
                _stream(query, history, session_id, body.model, cfg),
                media_type="text/event-stream",
            )
        return await _respond(query, history, session_id, body.model, cfg)
    finally:
        if not body.stream and _llm_semaphore:
            _llm_semaphore.release()


def _extract_user_query(body: OpenAIChatRequest) -> str:
    for msg in reversed(body.messages):
        if msg.role == "user":
            clean = sanitize_query(msg.content)
            if clean:
                return clean
    raise HTTPException(status_code=422, detail="No valid user message found")


async def _acquire_semaphore(sem) -> None:
    if sem is None:
        return
    if sem.locked():
        raise HTTPException(status_code=429, detail="LLM busy", headers={"Retry-After": "5"})
    await sem.acquire()


async def _invoke_graph(query: str, history: list | None, session_id: str, model: str):
    from orchestrator.gateway.app import _get_graph
    from orchestrator.pipeline.tracer import GraphObservabilityTracer

    graph = _get_graph()
    tracer = GraphObservabilityTracer(
        request_id=uuid.uuid4().hex[:16],
        session_id=session_id,
    )
    state = {
        "query": query,
        "history": history or [],
        "session_id": session_id,
        "iterations": 0,
        "tokens_used": 0,
        "fallback_used": False,
    }
    result = await graph.ainvoke(state, {"callbacks": [tracer]})
    tracer.finalize(result)
    return result


async def _respond(query, history, session_id, model, cfg) -> JSONResponse:
    from orchestrator.gateway.app import _session_store

    result = await _invoke_graph(query, history, session_id, model)
    response_text = result.get("response", "")

    if _session_store and cfg.session.enabled:
        _session_store.append(session_id, "user", query)
        _session_store.append(session_id, "assistant", response_text)

    return JSONResponse(content=OpenAIChatResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        created=int(time.time()),
        model=model,
        choices=[OpenAIChatChoice(
            message=OpenAIChatChoiceMessage(content=response_text),
        )],
        usage=OpenAIChatUsage(total_tokens=result.get("tokens_used", 0)),
    ).model_dump())


async def _stream(query, history, session_id, model, cfg):
    from orchestrator.gateway.app import _get_graph, _llm_semaphore, _session_store
    from orchestrator.gateway.streaming import _SSE_EVENT_MARKER, stream_via_pipeline

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    try:
        yield _sse_chunk(completion_id, created, model, delta={"role": "assistant"})

        graph = _get_graph()
        full_response = []
        async for token in stream_via_pipeline(
            graph,
            query=query,
            history=history,
            session_id=session_id,
        ):
            if token.startswith(_SSE_EVENT_MARKER):
                continue
            full_response.append(token)
            yield _sse_chunk(completion_id, created, model, delta={"content": token})

        yield _sse_chunk(completion_id, created, model, delta={}, finish_reason="stop")
        yield "data: [DONE]\n\n"

        if _session_store and cfg.session.enabled:
            response_text = "".join(full_response)
            _session_store.append(session_id, "user", query)
            _session_store.append(session_id, "assistant", response_text)
    finally:
        if _llm_semaphore:
            _llm_semaphore.release()


def _sse_chunk(cid: str, created: int, model: str, delta: dict, finish_reason=None) -> str:
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk)}\n\n"
