"""Combined API for local read-only evidence providers."""

from __future__ import annotations

from fastapi import FastAPI

from code_analysis.api import app as code_analysis_app
from data_analysis.api import app as data_analysis_app
from local_evidence_operator import __version__
from ops_diagnostics.api import app as ops_diagnostics_app
from security_analysis.api import app as security_analysis_app

app = FastAPI(title="Local Evidence Operator", version=__version__)

_SKIP_PROVIDER_PATHS = {"/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "healthy",
        "version": __version__,
        "providers": ["code_analysis", "data_analysis", "ops_diagnostics", "security_analysis"],
    }


@app.get("/v1/local-evidence/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "name": "local_evidence_operator",
        "capabilities": [
            "local_evidence_operator",
            "code_analysis",
            "data_analysis",
            "ops_diagnostics",
            "security_analysis",
            "repo",
            "graph",
            "data_profile",
            "schema_drift",
            "sqlite_reconcile",
            "compose_diagnostics",
            "incident_timeline",
            "log_performance",
            "local_security_evidence",
        ],
        "policy": {
            "read_only": True,
            "executes_commands": False,
            "mutates_inputs": False,
            "writes_final_outputs": False,
        },
        "providers": {
            "code_analysis": "/v1/code/analyze",
            "data_analysis": "/v1/data/profile",
            "ops_diagnostics": "/v1/ops/diagnose",
            "security_analysis": "/v1/security/cache-correlation",
        },
    }


def _attach_provider_routes(provider_app: FastAPI) -> None:
    for route in provider_app.routes:
        path = getattr(route, "path", "")
        if path in _SKIP_PROVIDER_PATHS:
            continue
        app.router.routes.append(route)


for _provider_app in (code_analysis_app, data_analysis_app, ops_diagnostics_app, security_analysis_app):
    _attach_provider_routes(_provider_app)
