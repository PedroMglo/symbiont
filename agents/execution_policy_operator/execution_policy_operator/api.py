"""Combined API for deterministic execution policy providers."""

from __future__ import annotations

from fastapi import FastAPI

from bash_safety.api import app as bash_safety_app
from execution_policy_operator import __version__

app = FastAPI(title="Execution Policy Operator", version=__version__)

_SKIP_PROVIDER_PATHS = {"/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "healthy",
        "version": __version__,
        "providers": ["bash_safety"],
    }


@app.get("/v1/execution-policy/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "name": "execution_policy_operator",
        "capabilities": [
            "execution_policy_operator",
            "bash_safety",
            "shell_static_analysis",
            "command_risk_classification",
            "destructive_command_detection",
            "dry_run_planning",
            "portable_shell_review",
        ],
        "policy": {
            "read_only": True,
            "executes_commands": False,
            "mutates_inputs": False,
            "writes_final_outputs": False,
            "policy_owner": True,
        },
        "providers": {
            "bash_safety": "/v1/bash/static-safety",
            "command_risk": "/v1/bash/command-risk",
        },
    }


def _attach_provider_routes(provider_app: FastAPI) -> None:
    for route in provider_app.routes:
        path = getattr(route, "path", "")
        if path in _SKIP_PROVIDER_PATHS:
            continue
        app.router.routes.append(route)


_attach_provider_routes(bash_safety_app)
