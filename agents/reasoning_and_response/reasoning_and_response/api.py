"""FastAPI application for the Reasoning And Response agent."""

from __future__ import annotations

from fastapi import Depends, FastAPI
from sharedai.servicekit.auth import service_token_dependency

from reasoning_and_response import __version__
from reasoning_and_response.classification import classify
from reasoning_and_response.config import get_settings
from reasoning_and_response.critique import critique
from reasoning_and_response.decomposition import decompose
from reasoning_and_response.direct_response import respond
from reasoning_and_response.synthesis import polish, synthesize
from reasoning_and_response.types import (
    CapabilitiesResponse,
    ClassifyRequest,
    ClassifyResponse,
    CritiqueRequest,
    CritiqueResponse,
    DecomposeRequest,
    DecomposeResponse,
    HealthResponse,
    PolishRequest,
    PolishResponse,
    RespondRequest,
    RespondResponse,
    SynthesizeRequest,
    SynthesizeResponse,
)

app = FastAPI(title="Reasoning And Response Agent", version=__version__)
require_service_token = service_token_dependency(
    "Reasoning And Response",
    lambda: get_settings().security.api_key,
)


def _metadata_with_language_context(metadata: dict, language_context: dict) -> dict:
    if not language_context:
        return metadata
    return {**metadata, "language_context": language_context}


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.get("/v1/reasoning/capabilities")
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse()


@app.post("/v1/reasoning/respond", dependencies=[Depends(require_service_token)])
def respond_directly(request: RespondRequest) -> RespondResponse:
    return respond(
        query=request.query,
        history=request.history,
        context=request.context,
        budget_tokens=request.budget_tokens,
        metadata=_metadata_with_language_context(request.metadata, request.language_context),
        llm_config=request.llm_config,
    )


@app.post("/v1/reasoning/decompose", dependencies=[Depends(require_service_token)])
def decompose_query(request: DecomposeRequest) -> DecomposeResponse:
    return decompose(
        query=request.query,
        available_agents=request.available_agents,
        max_subtasks=request.max_subtasks,
        metadata=_metadata_with_language_context(request.metadata, request.language_context),
        llm_config=request.llm_config,
    )


@app.post("/v1/reasoning/synthesize", dependencies=[Depends(require_service_token)])
def synthesize_sources(request: SynthesizeRequest) -> SynthesizeResponse:
    return synthesize(
        query=request.query,
        sources=request.sources,
        metadata=_metadata_with_language_context(request.metadata, request.language_context),
        llm_config=request.llm_config,
    )


@app.post("/v1/reasoning/polish", dependencies=[Depends(require_service_token)])
def polish_draft(request: PolishRequest) -> PolishResponse:
    return polish(
        query=request.query,
        draft=request.draft,
        issues=request.issues,
        metadata=_metadata_with_language_context(request.metadata, request.language_context),
        llm_config=request.llm_config,
    )


@app.post("/v1/reasoning/critique", dependencies=[Depends(require_service_token)])
def critique_output(request: CritiqueRequest) -> CritiqueResponse:
    return critique(
        output=request.output,
        original_query=request.original_query,
        agent_name=request.agent_name,
        risk_level=request.risk_level,
        metadata=_metadata_with_language_context(request.metadata, request.language_context),
        llm_config=request.llm_config,
    )


@app.post("/v1/reasoning/classify", dependencies=[Depends(require_service_token)])
def classify_query(request: ClassifyRequest) -> ClassifyResponse:
    return classify(
        query=request.query,
        available_agents=request.available_agents,
        metadata=_metadata_with_language_context(request.metadata, request.language_context),
        llm_config=request.llm_config,
    )
