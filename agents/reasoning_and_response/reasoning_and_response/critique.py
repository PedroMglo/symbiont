"""Quality critique provider for reasoning_and_response."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from reasoning_and_response.config import get_settings
from reasoning_and_response.synthesis import _call_governed_llm, _prompt
from reasoning_and_response.types import CritiqueResponse, LLMConfigOverride

log = logging.getLogger(__name__)

_CRITIQUE_PROMPT = _prompt("critique.md")
_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


def critique(
    *,
    output: str,
    original_query: str,
    agent_name: str = "symbiont",
    risk_level: str | None = None,
    metadata: dict[str, Any] | None = None,
    llm_config: LLMConfigOverride | None = None,
) -> CritiqueResponse:
    """Evaluate answer quality without executing any external behavior."""

    cfg = get_settings()
    metadata = metadata or {}
    if cfg.evaluation.enable_heuristics:
        heuristic = _heuristic_result(original_query, output, cfg.evaluation.heuristic_min_length)
        if heuristic is not None:
            return heuristic

    if llm_config and llm_config.model:
        model = llm_config.model
        base_url = llm_config.backend_url
        prompt = llm_config.system_prompt or _CRITIQUE_PROMPT
        temperature = llm_config.parameters.get("temperature", 0.0)
        max_tokens = llm_config.parameters.get("max_tokens", 256)
        timeout = llm_config.parameters.get("timeout", cfg.llm.timeout_seconds)
    else:
        model = cfg.llm.model
        base_url = cfg.llm.base_url
        prompt = _CRITIQUE_PROMPT
        temperature = 0.0
        max_tokens = 256
        timeout = cfg.llm.timeout_seconds

    system = prompt.format(
        query=original_query,
        output=output[: cfg.evaluation.max_eval_chars],
        agent_name=agent_name,
        risk_level=risk_level or "normal",
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "Return critique JSON."}]
    try:
        raw = _call_governed_llm(messages, model, base_url, temperature, max_tokens, timeout)
        return _parse_critique(raw, metadata)
    except Exception as exc:
        log.warning("critique: LLM failed: %s", exc)
        return CritiqueResponse(
            acceptable=False,
            confidence_score=0.0,
            issues=["Critique provider unavailable."],
            suggestions=["Retry critique after the model backend is available or keep the response blocked."],
            response="Critique provider unavailable; output was not accepted.",
            metadata={"provider_mode": "critique", "degraded_reason": "llm_unavailable", **metadata},
        )


def _heuristic_result(query: str, output: str, min_length: int) -> CritiqueResponse | None:
    stripped = output.strip()
    if len(stripped) < 20:
        return CritiqueResponse(
            acceptable=False,
            confidence_score=0.2,
            issues=["Output is too short to satisfy the request."],
            suggestions=["Provide a substantive answer grounded in the available evidence."],
            response="Output is too short to satisfy the request.",
            metadata={"provider_mode": "critique", "source": "heuristic"},
        )
    if stripped.lower().startswith(("error", "erro", "failed", "falhou")):
        return CritiqueResponse(
            acceptable=False,
            confidence_score=0.1,
            issues=["Output appears to be an error rather than an answer."],
            suggestions=["Retry or route to a more appropriate owner before responding."],
            response="Output appears to be an error rather than an answer.",
            metadata={"provider_mode": "critique", "source": "heuristic"},
        )
    if len(stripped) >= min_length and _overlap_ratio(query, stripped) >= 0.3:
        return CritiqueResponse(
            acceptable=True,
            confidence_score=0.85,
            response="Output is sufficiently detailed and overlaps with the request.",
            metadata={"provider_mode": "critique", "source": "heuristic"},
        )
    return None


def _overlap_ratio(query: str, output: str) -> float:
    query_tokens = {token.lower() for token in _TOKEN_RE.findall(query) if len(token) > 3}
    if not query_tokens:
        return 0.0
    output_tokens = {token.lower() for token in _TOKEN_RE.findall(output)}
    return len(query_tokens & output_tokens) / max(len(query_tokens), 1)


def _parse_critique(raw: str, metadata: dict[str, Any]) -> CritiqueResponse:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    elif "{" in text and "}" in text:
        text = text[text.find("{"):text.rfind("}") + 1]
    parsed = json.loads(text)
    issues = [str(item) for item in parsed.get("issues", [])]
    suggestions = [str(item) for item in parsed.get("suggestions", [])]
    acceptable = bool(parsed.get("acceptable", not issues))
    score = float(parsed.get("confidence_score", parsed.get("score", 0.7)))
    response = str(parsed.get("response") or "; ".join(issues) or "Output accepted.")
    return CritiqueResponse(
        acceptable=acceptable,
        confidence_score=max(0.0, min(score, 1.0)),
        issues=issues,
        suggestions=suggestions,
        response=response,
        metadata={"provider_mode": "critique", **metadata},
    )
