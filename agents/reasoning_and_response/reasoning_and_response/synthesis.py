"""Synthesis provider for reasoning_and_response."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx
from context_governor import govern_chat_completion
from sharedai.llm.utils import strip_think as _strip_think

from reasoning_and_response.config import get_settings
from reasoning_and_response.types import (
    LLMConfigOverride,
    PolishResponse,
    SourceResult,
    SynthesizeResponse,
)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE: dict[str, str] = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


log = logging.getLogger(__name__)

_SYNTHESIS_PROMPT = _prompt("synthesis.md")
_POLISH_PROMPT = _prompt("polish.md")


def _max_output_tokens() -> int:
    try:
        configured = int(os.environ.get("REASONING_AND_RESPONSE_LLM_MAX_OUTPUT_TOKENS", "2048"))
    except ValueError:
        configured = 2048
    return max(64, min(configured, 4096))


def _bounded_token_limit(requested: int | str | float) -> int:
    try:
        value = int(requested)
    except (TypeError, ValueError):
        value = 768
    return max(64, min(value, _max_output_tokens()))


def _language_policy(metadata: dict[str, Any] | None, query: str) -> tuple[str, str]:
    metadata = metadata or {}
    raw_context = metadata.get("language_context")
    context = raw_context if isinstance(raw_context, dict) else {}
    original_query = str(metadata.get("original_query") or context.get("original_text") or query)
    response_language = str(
        metadata.get("response_language") or context.get("response_language") or "same_as_user"
    )
    if response_language and response_language != "same_as_user":
        instruction = f"User-facing final responses must use this language: {response_language}."
    else:
        instruction = "User-facing final responses must match the original user's language."
    instruction += " Agent-to-agent summaries and structured internal contracts should remain in English."
    return original_query, instruction


def _template_with_required_fields(template: str, required_fields: tuple[str, ...], suffix: str) -> str:
    if all(f"{{{field}}}" in template for field in required_fields):
        return template
    return f"{template.rstrip()}\n\n{suffix}"


def synthesize(
    query: str,
    sources: list[SourceResult],
    metadata: dict[str, Any] | None = None,
    llm_config: LLMConfigOverride | None = None,
) -> SynthesizeResponse:
    """Synthesize multiple owner outputs into a coherent response."""

    cfg = get_settings()
    if llm_config and llm_config.model:
        model = llm_config.model
        base_url = llm_config.backend_url
        system_prompt = llm_config.system_prompt or _SYNTHESIS_PROMPT
        temperature = llm_config.parameters.get("temperature", cfg.llm.temperature)
        max_tokens = llm_config.parameters.get("max_tokens", cfg.llm.max_tokens)
        timeout = llm_config.parameters.get("timeout", cfg.llm.timeout_seconds)
    else:
        model = cfg.llm.model
        base_url = cfg.llm.base_url
        system_prompt = _SYNTHESIS_PROMPT
        temperature = cfg.llm.temperature
        max_tokens = cfg.llm.max_tokens
        timeout = cfg.llm.timeout_seconds

    successful = [source for source in sources if source.output.strip()]
    if not successful:
        return SynthesizeResponse(
            response="Nao foi possivel obter informacao relevante para a sua pergunta.",
            sources_used=[],
        )
    if len(successful) == 1:
        return SynthesizeResponse(
            response=_strip_think(successful[0].output),
            sources_used=[successful[0].agent_name],
        )

    max_chars = cfg.synthesis.max_source_chars
    sources_text = "\n\n".join(
        f"[{source.agent_name}]\n{_strip_think(source.output)[:max_chars]}" for source in successful
    )

    original_query, language_instruction = _language_policy(metadata, query)
    system_prompt = _template_with_required_fields(
        system_prompt,
        ("query", "sources"),
        (
            "User query: {query}\nOriginal user query: {original_query}\n"
            "Language policy: {language_instruction}\n\nSources:\n{sources}"
        ),
    )
    prompt = system_prompt.format(
        query=query,
        original_query=original_query,
        language_instruction=language_instruction,
        sources=sources_text,
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": query},
    ]

    try:
        content = _call_governed_llm(messages, model, base_url, temperature, max_tokens, timeout)
        return SynthesizeResponse(
            response=_strip_think(content),
            sources_used=[source.agent_name for source in successful],
        )
    except Exception as exc:
        log.warning("synthesis: LLM failed (%s), falling back to concat", exc)
        concat = "\n\n---\n\n".join(
            f"**{source.agent_name}**:\n{_strip_think(source.output)}" for source in successful
        )
        return SynthesizeResponse(
            response=concat,
            sources_used=[source.agent_name for source in successful],
        )


def polish(
    query: str,
    draft: str,
    issues: list[str],
    metadata: dict[str, Any] | None = None,
    llm_config: LLMConfigOverride | None = None,
) -> PolishResponse:
    """Refine a draft response using critic feedback."""

    cfg = get_settings()
    if llm_config and llm_config.model:
        model = llm_config.model
        base_url = llm_config.backend_url
        polish_prompt = llm_config.extra_prompts.get("polish_prompt", _POLISH_PROMPT)
        temperature = llm_config.parameters.get("temperature", cfg.llm.temperature)
        max_tokens = llm_config.parameters.get("max_tokens", cfg.llm.max_tokens)
        timeout = llm_config.parameters.get("timeout", cfg.llm.timeout_seconds)
    else:
        model = cfg.llm.model
        base_url = cfg.llm.base_url
        polish_prompt = _POLISH_PROMPT
        temperature = cfg.llm.temperature
        max_tokens = cfg.llm.max_tokens
        timeout = cfg.llm.timeout_seconds

    issues_text = "\n".join(f"- {issue}" for issue in issues) if issues else "- General quality improvements needed"
    original_query, language_instruction = _language_policy(metadata, query)
    polish_prompt = _template_with_required_fields(
        polish_prompt,
        ("query", "draft", "issues"),
        (
            "User query: {query}\nOriginal user query: {original_query}\n"
            "Language policy: {language_instruction}\n\nDraft response:\n{draft}\n\n"
            "Issues found by reviewer:\n{issues}"
        ),
    )
    prompt = polish_prompt.format(
        query=query,
        original_query=original_query,
        language_instruction=language_instruction,
        draft=_strip_think(draft),
        issues=issues_text,
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": query},
    ]

    try:
        content = _call_governed_llm(messages, model, base_url, temperature, max_tokens, timeout)
        return PolishResponse(response=_strip_think(content), refinement_applied=True)
    except Exception as exc:
        log.warning("polish: LLM failed (%s), keeping draft", exc)
        return PolishResponse(response=_strip_think(draft), refinement_applied=False)


def _call_governed_llm(
    messages: list[dict],
    model: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """Call LLM and return content string."""

    token_limit = _bounded_token_limit(max_tokens)
    return govern_chat_completion(
        model=model,
        messages=messages,
        base_url=base_url,
        temperature=temperature,
        max_tokens=token_limit,
        timeout=timeout,
        phase="reasoning_and_response",
        post=httpx.post,
    )
