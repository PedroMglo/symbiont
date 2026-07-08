"""Task decomposition provider for reasoning_and_response."""

from __future__ import annotations

import json
import logging
from typing import Any

from reasoning_and_response.config import get_settings
from reasoning_and_response.synthesis import _call_governed_llm, _prompt
from reasoning_and_response.types import DecomposeResponse, LLMConfigOverride, SubTask

log = logging.getLogger(__name__)

_DECOMPOSE_PROMPT = _prompt("decomposition.md")


def decompose(
    query: str,
    *,
    available_agents: list[str] | None = None,
    max_subtasks: int | None = None,
    metadata: dict[str, Any] | None = None,
    llm_config: LLMConfigOverride | None = None,
) -> DecomposeResponse:
    """Decompose a query into owner-dispatchable subtasks."""

    cfg = get_settings()
    metadata = metadata or {}
    agents = available_agents or cfg.decomposition.available_capabilities
    limit = max(1, min(max_subtasks or cfg.decomposition.max_subtasks, cfg.decomposition.max_subtasks))
    if llm_config and llm_config.model:
        model = llm_config.model
        base_url = llm_config.backend_url
        prompt = llm_config.system_prompt or _DECOMPOSE_PROMPT
        temperature = llm_config.parameters.get("temperature", 0.2)
        max_tokens = llm_config.parameters.get("max_tokens", cfg.llm.max_tokens)
        timeout = llm_config.parameters.get("timeout", cfg.llm.timeout_seconds)
    else:
        model = cfg.llm.model
        base_url = cfg.llm.base_url
        prompt = _DECOMPOSE_PROMPT
        temperature = 0.2
        max_tokens = cfg.llm.max_tokens
        timeout = cfg.llm.timeout_seconds

    system = prompt.format(
        query=query,
        available_agents=", ".join(agents),
        max_subtasks=limit,
        default_budget_tokens=cfg.decomposition.default_budget_tokens,
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": query}]
    try:
        raw = _call_governed_llm(messages, model, base_url, temperature, max_tokens, timeout)
        subtasks = _parse_subtasks(raw, agents, cfg.decomposition.default_budget_tokens)[:limit]
        if subtasks:
            return DecomposeResponse(
                subtasks=subtasks,
                reasoning="LLM decomposition accepted",
                output=_subtasks_output(subtasks),
            )
        return DecomposeResponse(
            subtasks=[],
            reasoning="decomposition_provider_returned_no_valid_subtasks",
            output="[]",
        )
    except Exception as exc:
        log.warning("decomposition: LLM failed: %s", exc)
        return DecomposeResponse(
            subtasks=[],
            reasoning="decomposition_backend_unavailable: no task decomposition produced",
            output="[]",
        )


def _parse_subtasks(raw: str, available_agents: list[str], budget_tokens: int) -> list[SubTask]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    elif "[" in text and "]" in text:
        text = text[text.find("["):text.rfind("]") + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        return []
    valid = set(available_agents)
    subtasks: list[SubTask] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            continue
        selected = [
            str(agent)
            for agent in item.get("assigned_agents", item.get("agents", []))
            if str(agent) in valid
        ]
        subtasks.append(
            SubTask(
                id=str(item.get("id") or f"task_{index}"),
                objective=str(item.get("objective") or item.get("task") or "").strip() or "Complete requested work",
                assigned_agents=selected or [available_agents[0]],
                depends_on=[str(dep) for dep in item.get("depends_on", [])],
                budget_tokens=int(item.get("budget_tokens") or budget_tokens),
                parallel_group=int(item.get("parallel_group") or 0),
            )
        )
    return subtasks

def _subtasks_output(subtasks: list[SubTask]) -> str:
    return json.dumps([task.model_dump(mode="json") for task in subtasks], ensure_ascii=True)
