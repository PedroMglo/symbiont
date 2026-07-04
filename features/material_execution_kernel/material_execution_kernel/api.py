"""FastAPI API for the material execution kernel feature."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from sharedai.servicekit.auth import service_token_dependency

from material_execution_kernel import __version__
from material_execution_kernel.config import get_settings
from material_execution_kernel.events import events_to_jsonl
from material_execution_kernel.material_builder_client import material_builder_client_from_env
from material_execution_kernel.sessions import MaterialSessionNotFound, MaterialSessionStore
from material_execution_kernel.types import (
    CapabilitiesResponse,
    MaterialSessionRequest,
    MaterialSessionResponse,
)
from material_execution_kernel.workspace_client import workspace_client_from_env


app = FastAPI(title="Material Execution Kernel", version=__version__)
require_service_token = service_token_dependency(
    "Material Execution Kernel",
    lambda: get_settings().security.api_key,
)


def get_store() -> MaterialSessionStore:
    if not hasattr(app.state, "session_store"):
        app.state.session_store = MaterialSessionStore(
            material_builder=material_builder_client_from_env(),
            workspace_client=workspace_client_from_env(),
        )
    return app.state.session_store


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy", "version": __version__}


@app.get("/v1/material-execution/capabilities", dependencies=[Depends(require_service_token)])
def capabilities() -> CapabilitiesResponse:
    settings = get_settings()
    return CapabilitiesResponse(
        active_sandbox_owner=settings.active_sandbox_owner,
        runtime_limits=settings.runtime_limits,
        model_lane_policy=settings.model_lane_policy,
    )


@app.post("/v1/material-execution/sessions", dependencies=[Depends(require_service_token)])
def create_session(
    request: MaterialSessionRequest,
    store: MaterialSessionStore = Depends(get_store),
) -> MaterialSessionResponse:
    return store.create_or_resume(request)


@app.get("/v1/material-execution/sessions/{session_id}", dependencies=[Depends(require_service_token)])
def get_session(
    session_id: str,
    store: MaterialSessionStore = Depends(get_store),
) -> MaterialSessionResponse:
    try:
        return store.get(session_id)
    except MaterialSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="Material session not found") from exc


@app.post("/v1/material-execution/sessions/{session_id}/step", dependencies=[Depends(require_service_token)])
def step_session(
    session_id: str,
    store: MaterialSessionStore = Depends(get_store),
) -> MaterialSessionResponse:
    try:
        return store.step(session_id)
    except MaterialSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="Material session not found") from exc


@app.get("/v1/material-execution/sessions/{session_id}/events", dependencies=[Depends(require_service_token)])
def get_session_events(
    session_id: str,
    store: MaterialSessionStore = Depends(get_store),
) -> PlainTextResponse:
    try:
        body = events_to_jsonl(store.events(session_id))
    except MaterialSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="Material session not found") from exc
    return PlainTextResponse(body, media_type="application/x-ndjson")


@app.get("/v1/material-execution/sessions/{session_id}/events/json", dependencies=[Depends(require_service_token)])
def get_session_events_json(
    session_id: str,
    store: MaterialSessionStore = Depends(get_store),
) -> dict[str, object]:
    try:
        events = store.events(session_id)
    except MaterialSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="Material session not found") from exc
    return {"session_id": session_id, "events": [event.model_dump(mode="json") for event in events]}


@app.get("/v1/material-execution/sessions/{session_id}/manifest", dependencies=[Depends(require_service_token)])
def get_session_manifest(
    session_id: str,
    store: MaterialSessionStore = Depends(get_store),
) -> dict[str, object]:
    try:
        manifest = store.manifest(session_id)
    except MaterialSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="Material session not found") from exc
    return manifest.model_dump(mode="json")


@app.get("/v1/material-execution/sessions/{session_id}/diagnostics", dependencies=[Depends(require_service_token)])
def get_session_diagnostics(
    session_id: str,
    store: MaterialSessionStore = Depends(get_store),
) -> dict[str, object]:
    try:
        return store.diagnostics(session_id)
    except MaterialSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="Material session not found") from exc


@app.post("/v1/material-execution/sessions/{session_id}/cancel", dependencies=[Depends(require_service_token)])
def cancel_session(
    session_id: str,
    store: MaterialSessionStore = Depends(get_store),
) -> MaterialSessionResponse:
    try:
        return store.cancel(session_id)
    except MaterialSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="Material session not found") from exc
