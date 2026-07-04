"""FastAPI app for the local translation/normalization feature."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from config import get_config
from models import (
    HealthResponse,
    LintPTPTRequest,
    LintPTPTResponse,
    NormalizeRequest,
    NormalizeResponse,
    SpellcheckRequest,
    SpellcheckResponse,
)
from normalizer import Normalizer
from ptpt_linter import PTPTLinter
from security import get_translation_api_key, install_translation_log_redaction
from sharedai.servicekit.auth import service_token_dependency


install_translation_log_redaction()

app = FastAPI(
    title="Translation Feature",
    version="0.1.0",
    description="Local non-blocking language normalization layer for ai-local.",
)

_normalizer: Normalizer | None = None
_linter: PTPTLinter | None = None
require_service_token = service_token_dependency("Translation", get_translation_api_key)


@app.middleware("http")
async def post_auth_middleware(request: Request, call_next):
    if request.method.upper() == "POST":
        try:
            require_service_token(
                authorization=request.headers.get("Authorization"),
                x_api_key=request.headers.get("X-API-Key"),
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


def get_normalizer() -> Normalizer:
    global _normalizer
    if _normalizer is None:
        _normalizer = Normalizer(get_config())
    return _normalizer


def get_linter() -> PTPTLinter:
    global _linter
    if _linter is None:
        _linter = PTPTLinter(get_config().pt_br_blocklist_path)
    return _linter


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    normalizer = get_normalizer()
    cfg = get_config()
    return HealthResponse(
        status="ok",
        dictionary_loaded=normalizer.spellchecker.loaded,
        translator_loaded=normalizer.translator.loaded,
        mode=cfg.mode if cfg.mode in {"off", "shadow", "assisted", "enforce"} else "shadow",
    )


@app.post(
    "/v1/normalize",
    response_model=NormalizeResponse,
)
def normalize(request: NormalizeRequest) -> NormalizeResponse:
    return get_normalizer().normalize(request)


@app.post(
    "/v1/lint-ptpt",
    response_model=LintPTPTResponse,
)
def lint_ptpt(request: LintPTPTRequest) -> LintPTPTResponse:
    return get_linter().lint(request.text, protect_spans_enabled=request.protect_spans)


@app.post(
    "/v1/spellcheck",
    response_model=SpellcheckResponse,
)
def spellcheck(request: SpellcheckRequest) -> SpellcheckResponse:
    text, response, _changed, _latency = get_normalizer().spellchecker.check_text(
        request.text,
        apply_autocorrect=False,
    )
    del text
    return response
