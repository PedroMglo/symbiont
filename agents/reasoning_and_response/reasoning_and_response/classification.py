"""Intent classification provider for reasoning_and_response."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from reasoning_and_response.config import get_settings
from reasoning_and_response.synthesis import _call_governed_llm, _prompt
from reasoning_and_response.types import ClassifyResponse, LLMConfigOverride

log = logging.getLogger(__name__)

_CLASSIFY_PROMPT = _prompt("classification.md")


def classify(
    query: str,
    *,
    available_agents: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    llm_config: LLMConfigOverride | None = None,
) -> ClassifyResponse:
    """Classify intent and choose available downstream owners."""

    cfg = get_settings()
    _ = metadata
    agents = available_agents or cfg.classification.available_agents
    max_agents = cfg.classification.max_agents_per_query
    if llm_config and llm_config.model:
        model = llm_config.model
        base_url = llm_config.backend_url
        prompt = llm_config.system_prompt or _CLASSIFY_PROMPT
        temperature = llm_config.parameters.get("temperature", 0.1)
        max_tokens = llm_config.parameters.get("max_tokens", 256)
        timeout = llm_config.parameters.get("timeout", cfg.llm.timeout_seconds)
    else:
        model = cfg.llm.model
        base_url = cfg.llm.base_url
        prompt = _CLASSIFY_PROMPT
        temperature = 0.1
        max_tokens = 256
        timeout = cfg.llm.timeout_seconds

    system = prompt.format(available_agents=", ".join(agents), max_agents=max_agents)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": query}]
    try:
        raw = _call_governed_llm(messages, model, base_url, temperature, max_tokens, timeout)
        parsed = _parse_classification(raw, agents, max_agents)
        if parsed.agents:
            return parsed
    except Exception as exc:
        log.warning("classification: LLM failed: %s", exc)

    selected = _heuristic_route(query, agents)[:max_agents]
    return ClassifyResponse(
        agents=selected,
        reasoning="Fallback routing after classification provider failure",
        response=json.dumps({"agents": selected, "reasoning": "fallback"}, ensure_ascii=True),
    )


def _parse_classification(raw: str, available_agents: list[str], max_agents: int) -> ClassifyResponse:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    elif "{" in text and "}" in text:
        text = text[text.find("{"):text.rfind("}") + 1]
    parsed = json.loads(text)
    valid = set(available_agents)
    agents = [str(agent) for agent in parsed.get("agents", []) if str(agent) in valid]
    agents = agents[:max_agents]
    reasoning = str(parsed.get("reasoning") or "")
    return ClassifyResponse(
        agents=agents,
        reasoning=reasoning,
        response=json.dumps({"agents": agents, "reasoning": reasoning}, ensure_ascii=True),
    )


def _heuristic_route(query: str, available_agents: list[str]) -> list[str]:
    q = query.lower()
    available = set(available_agents)
    selected: list[str] = []

    def add(*names: str) -> None:
        for name in names:
            if name in available and name not in selected:
                selected.append(name)

    if re.search(r"\b(code|codigo|c[oó]digo|repo|ficheiro|arquivo|fun[cç][aã]o|bug)\b", q):
        add("code", "local_evidence_operator")
    if re.search(r"\b(email|mail|calend[aá]rio|rss|agenda|evento)\b", q):
        add("personal", "personal_context")
    if re.search(r"\b(rag|cag|pesquisa|research|nota|documento|conhecimento)\b", q):
        add("research")
    if re.search(r"\b(cr[ií]tica|critic|qualidade|avaliar|risco|precis[aã]o)\b", q):
        add("reasoning_and_response")
    if re.search(r"\b(sintetiza|s[ií]ntese|combina|resume|resumo)\b", q):
        add("reasoning_and_response")
    if re.search(r"\b(decomp[oõ]e|decompose|plano|passos|tarefas)\b", q):
        add("reasoning_and_response")

    if not selected:
        add("reasoning_and_response", "research")
    return selected
