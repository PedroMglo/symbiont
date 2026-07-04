"""FastAPI application for the material_builder agent."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from sharedai.servicekit.auth import service_token_dependency

from material_builder import __version__
from material_builder.config import get_settings
from material_builder.planning import (
    MaterialBuilderBlocked,
    create_plan,
    critique_repair,
    generate_files,
    propose_patch,
    repair_plan,
)
from material_builder.types import (
    CapabilitiesResponse,
    HealthResponse,
    MaterialFileGenerationRequest,
    MaterialFileGenerationResponse,
    MaterialPatchGenerationRequest,
    MaterialPatchGenerationResponse,
    MaterialPlanRepairRequest,
    MaterialPlanRepairResponse,
    MaterialPlanRequest,
    MaterialPlanResponse,
    MaterialRepairCriticRequest,
    MaterialRepairCriticResponse,
)


app = FastAPI(title="Material Builder Agent", version=__version__)
require_service_token = service_token_dependency(
    "Material Builder",
    lambda: get_settings().security.api_key,
)


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.get("/v1/material-builder/capabilities")
def capabilities() -> CapabilitiesResponse:
    response = CapabilitiesResponse()
    settings = get_settings()
    response.capabilities["llm_generation_backend"] = any(lane.configured for lane in settings.llm_lanes.values())
    response.capabilities["llm_lane_plan"] = settings.llm_plan.configured
    response.capabilities["llm_lane_file"] = settings.llm_file.configured
    response.capabilities["llm_lane_patch"] = settings.llm_patch.configured
    response.capabilities["llm_lane_repair"] = settings.llm_repair.configured
    response.capabilities["llm_lane_critic"] = settings.llm_critic.configured
    response.lane_routes = {name: lane.route for name, lane in settings.llm_lanes.items()}
    response.prewarm_lanes = [name for name, lane in settings.llm_lanes.items() if lane.configured]
    return response


@app.post("/v1/material-builder/plan", dependencies=[Depends(require_service_token)])
def plan_material(request: MaterialPlanRequest) -> MaterialPlanResponse:
    try:
        return create_plan(request)
    except MaterialBuilderBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": exc.code, "message": str(exc), "details": exc.details},
        ) from exc


@app.post("/v1/material-builder/plan/repair", dependencies=[Depends(require_service_token)])
def repair_material_plan(request: MaterialPlanRepairRequest) -> MaterialPlanRepairResponse:
    try:
        return repair_plan(request)
    except MaterialBuilderBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": exc.code, "message": str(exc), "details": exc.details},
        ) from exc


@app.post("/v1/material-builder/files", dependencies=[Depends(require_service_token)])
def generate_material_files(
    request: MaterialFileGenerationRequest,
) -> MaterialFileGenerationResponse:
    try:
        return generate_files(request)
    except MaterialBuilderBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": exc.code, "message": str(exc), "details": exc.details},
        ) from exc


@app.post("/v1/material-builder/patch", dependencies=[Depends(require_service_token)])
def propose_material_patch(
    request: MaterialPatchGenerationRequest,
) -> MaterialPatchGenerationResponse:
    try:
        return propose_patch(request)
    except MaterialBuilderBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": exc.code, "message": str(exc), "details": exc.details},
        ) from exc


@app.post("/v1/material-builder/repair/critic", dependencies=[Depends(require_service_token)])
def critique_material_repair(
    request: MaterialRepairCriticRequest,
) -> MaterialRepairCriticResponse:
    try:
        return critique_repair(request)
    except MaterialBuilderBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": exc.code, "message": str(exc), "details": exc.details},
        ) from exc
