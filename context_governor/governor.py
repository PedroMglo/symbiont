"""Config-backed Context Governor implementation."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sharedai.llm.http_client import call_chat_completion
from sharedai.llm.tokens import estimate_tokens as _estimate_tokens

from context_governor.contracts import (
    ContextBudget,
    ContextGovernorPolicy,
    ContextItem,
    ContextPackage,
    ContextRequest,
)


class ContextGovernorBlocked(RuntimeError):
    """Raised when enforce mode refuses an oversized prompt."""


def estimate_chat_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate chat prompt tokens without retaining prompt content."""

    total = 0
    for message in messages:
        role = str(message.get("role") or "")
        content = message.get("content") or ""
        total += 6 + _estimate_tokens(role) + _estimate_tokens(str(content))
    return total


def load_context_governor_policy(config_dir: Path | None = None) -> ContextGovernorPolicy:
    """Load Context Governor policy from config/orc/*.toml plus env overrides."""

    raw = _load_orc_toml(config_dir)
    section = raw.get("context_governor", {})
    return ContextGovernorPolicy(
        enabled=_env_bool("ORC_CONTEXT_GOVERNOR_ENABLED", bool(section.get("enabled", True))),
        mode=_env_mode("ORC_CONTEXT_GOVERNOR_MODE", str(section.get("mode", "observe"))),
        default_context_window_tokens=_env_int(
            "ORC_CONTEXT_GOVERNOR_DEFAULT_CONTEXT_WINDOW_TOKENS",
            int(section.get("default_context_window_tokens", 8192)),
        ),
        prompt_budget_ratio=_env_float(
            "ORC_CONTEXT_GOVERNOR_PROMPT_BUDGET_RATIO",
            float(section.get("prompt_budget_ratio", 0.75)),
        ),
        reserved_response_ratio=_env_float(
            "ORC_CONTEXT_GOVERNOR_RESERVED_RESPONSE_RATIO",
            float(section.get("reserved_response_ratio", 0.15)),
        ),
        minimum_reserved_response_tokens=_env_int(
            "ORC_CONTEXT_GOVERNOR_MINIMUM_RESERVED_RESPONSE_TOKENS",
            int(section.get("minimum_reserved_response_tokens", 256)),
        ),
        max_reserved_response_tokens=_env_int(
            "ORC_CONTEXT_GOVERNOR_MAX_RESERVED_RESPONSE_TOKENS",
            int(section.get("max_reserved_response_tokens", 2048)),
        ),
        warning_pressure_threshold=_env_float(
            "ORC_CONTEXT_GOVERNOR_WARNING_PRESSURE_THRESHOLD",
            float(section.get("warning_pressure_threshold", 0.75)),
        ),
        block_pressure_threshold=_env_float(
            "ORC_CONTEXT_GOVERNOR_BLOCK_PRESSURE_THRESHOLD",
            float(section.get("block_pressure_threshold", 1.0)),
        ),
        model_context_windows=_int_mapping(section.get("model_context_windows", {})),
        phase_overrides=_phase_overrides(section.get("phase", {})),
    )


def govern_messages_for_llm_call(
    messages: list[dict[str, Any]],
    *,
    model: str,
    phase: str,
    reserved_response_tokens: int | None = None,
    context_window_tokens: int | None = None,
    policy: ContextGovernorPolicy | None = None,
) -> ContextPackage:
    """Build a package for an existing chat-message LLM call."""

    request = ContextRequest(
        phase=phase,
        model=model,
        messages=messages,
        context_window_tokens=context_window_tokens,
        reserved_response_tokens=reserved_response_tokens,
    )
    return build_context_package(request, policy=policy)


def govern_chat_completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    phase: str,
    context_window_tokens: int | None = None,
    post: Callable[..., Any] | None = None,
    policy: ContextGovernorPolicy | None = None,
) -> str:
    """Govern and dispatch one chat-completion call through the shared transport."""

    package = govern_messages_for_llm_call(
        messages,
        model=model,
        phase=phase,
        reserved_response_tokens=max_tokens,
        context_window_tokens=context_window_tokens,
        policy=policy,
    )
    if _use_ollama_native_no_think(base_url, model):
        return _call_ollama_native_chat_completion(
            model=model,
            messages=package.messages,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            post=post,
        )
    return call_chat_completion(
        model=model,
        messages=package.messages,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        post=post,
    )


def _use_ollama_native_no_think(base_url: str, model: str) -> bool:
    raw = os.environ.get("ORC_OLLAMA_NATIVE_NO_THINK_ENABLED", "true").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    model_key = str(model or "").strip().lower()
    if not model_key.startswith("qwen3"):
        return False
    url = str(base_url or "").strip().lower()
    return "ollama" in url or ":11434" in url


def _call_ollama_native_chat_completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    post: Callable[..., Any] | None,
) -> str:
    transport = post
    if transport is None:
        import httpx

        transport = httpx.post
    native_base = str(base_url).rstrip("/")
    if native_base.endswith("/v1"):
        native_base = native_base[:-3].rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    response = transport(
        f"{native_base}/api/chat",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return str((data.get("message") or {}).get("content") or "")


def build_context_package(
    request: ContextRequest,
    *,
    policy: ContextGovernorPolicy | None = None,
) -> ContextPackage:
    """Resolve budget, measure pressure and pack structured context items."""

    policy = policy or load_context_governor_policy()
    budget = _resolve_budget(request, policy)
    base_messages = [dict(message) for message in request.messages]
    base_tokens = estimate_chat_tokens(base_messages)
    included, dropped, item_tokens = _pack_items(request.items, budget.max_prompt_tokens - base_tokens)
    original_tokens = base_tokens + sum(_item_tokens(item) for item in request.items)
    prompt_tokens = base_tokens + item_tokens
    pressure = _pressure(prompt_tokens, budget.max_prompt_tokens)
    warnings: list[str] = []
    decision = "allow"

    if not policy.enabled or policy.mode == "off":
        decision = "allow"
        included = list(request.items)
        dropped = []
        prompt_tokens = original_tokens
        pressure = _pressure(prompt_tokens, budget.max_prompt_tokens)
    elif pressure >= budget.block_pressure_threshold:
        decision = "block"
        warnings.append("prompt_exceeds_context_budget")
    elif pressure >= budget.warning_pressure_threshold:
        decision = "warn"
        warnings.append("context_pressure_high")
    elif dropped:
        decision = "trim"
        warnings.append("optional_context_dropped")

    if policy.enabled and policy.mode == "observe":
        included = list(request.items)
        dropped = []
        prompt_tokens = original_tokens
        pressure = _pressure(prompt_tokens, budget.max_prompt_tokens)
        if pressure >= budget.block_pressure_threshold:
            decision = "block"
            warnings = ["prompt_would_exceed_context_budget"]
        elif pressure >= budget.warning_pressure_threshold:
            decision = "warn"
            warnings = ["context_pressure_high"]
        else:
            decision = "allow"
            warnings = []

    if policy.enabled and policy.mode == "enforce" and decision == "block":
        raise ContextGovernorBlocked(
            f"context budget exceeded for phase={request.phase!r} model={request.model!r}: "
            f"estimated_prompt_tokens={prompt_tokens} max_prompt_tokens={budget.max_prompt_tokens}"
        )

    return ContextPackage(
        phase=request.phase,
        model=request.model,
        mode=policy.mode if policy.enabled else "off",
        decision=decision,
        budget=budget,
        messages=base_messages,
        included_items=included,
        dropped_items=dropped,
        prompt_tokens_estimate=prompt_tokens,
        original_prompt_tokens_estimate=original_tokens,
        context_pressure=pressure,
        warnings=tuple(warnings),
        metadata=dict(request.metadata),
    )


def _resolve_budget(request: ContextRequest, policy: ContextGovernorPolicy) -> ContextBudget:
    override = policy.phase_overrides.get(request.phase, {})
    context_window = (
        request.context_window_tokens
        or policy.model_context_windows.get(request.model)
        or policy.default_context_window_tokens
    )
    prompt_ratio = float(override.get("prompt_budget_ratio", policy.prompt_budget_ratio))
    warning_threshold = float(override.get("warning_pressure_threshold", policy.warning_pressure_threshold))
    block_threshold = float(override.get("block_pressure_threshold", policy.block_pressure_threshold))
    reserved = request.reserved_response_tokens
    if reserved is None:
        reserved = round(context_window * float(override.get("reserved_response_ratio", policy.reserved_response_ratio)))
        reserved = max(policy.minimum_reserved_response_tokens, reserved)
        reserved = min(policy.max_reserved_response_tokens, reserved)
    reserved = max(1, min(int(reserved), max(1, context_window - 1)))
    max_prompt_tokens = min(round(context_window * prompt_ratio), context_window - reserved)
    max_prompt_tokens = max(1, int(max_prompt_tokens))
    return ContextBudget(
        phase=request.phase,
        model=request.model,
        context_window_tokens=int(context_window),
        max_prompt_tokens=max_prompt_tokens,
        reserved_response_tokens=reserved,
        prompt_budget_ratio=prompt_ratio,
        warning_pressure_threshold=warning_threshold,
        block_pressure_threshold=block_threshold,
    )


def _pack_items(items: list[ContextItem], remaining_tokens: int) -> tuple[list[ContextItem], list[ContextItem], int]:
    if not items:
        return [], [], 0
    included: list[ContextItem] = []
    dropped: list[ContextItem] = []
    used = 0
    ordered = sorted(enumerate(items), key=lambda entry: (not entry[1].required, entry[1].priority, entry[0]))
    for _, item in ordered:
        tokens = _item_tokens(item)
        if item.required or used + tokens <= remaining_tokens:
            included.append(item)
            used += tokens
            continue
        dropped.append(item)
    return included, dropped, used


def _item_tokens(item: ContextItem) -> int:
    if item.token_estimate is not None:
        return max(0, int(item.token_estimate))
    return _estimate_tokens(item.content)


def _pressure(tokens: int, max_prompt_tokens: int) -> float:
    if max_prompt_tokens <= 0:
        return 1.0
    return tokens / max_prompt_tokens


def _load_orc_toml(config_dir: Path | None) -> dict[str, Any]:
    if config_dir is None:
        config_dir = _default_config_dir()
    merged: dict[str, Any] = {}
    if not config_dir.exists():
        return merged
    for path in sorted(config_dir.glob("*.toml")):
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        _deep_merge(merged, data)
    return merged


def _default_config_dir() -> Path:
    candidates = [
        Path.cwd() / "config" / "orc",
        Path(__file__).resolve().parents[1] / "config" / "orc",
        Path("/app/config/orc"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[1]


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _int_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, item in value.items():
        try:
            result[str(key)] = int(item)
        except (TypeError, ValueError):
            continue
    return result


def _phase_overrides(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for phase, override in value.items():
        if isinstance(override, dict):
            result[str(phase)] = dict(override)
    return result


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_mode(key: str, default: str) -> str:
    raw = os.getenv(key, default).strip().lower()
    if raw in {"off", "observe", "enforce"}:
        return raw
    return "observe"
