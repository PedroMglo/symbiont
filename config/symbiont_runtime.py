"""Derived symbiont runtime environment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .agentic_budgets import (
    context_timeout_seconds,
    material_decision_timeout_seconds,
    planning_timeout_seconds,
    routing_timeout_seconds,
    synthesis_timeout_seconds,
    task_default_timeout_seconds,
)


@dataclass(frozen=True)
class SymbiontRuntimeValue:
    env: str
    value: str
    origin: str
    reason: str
    formula: str
    override: str


PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS: dict[str, int] = {
    "research": 1200,
    "extrator": 1800,
    "translation": 900,
    "material_builder": 900,
    "material_execution_kernel": 900,
    "workspace_execution": 900,
}


def _decision(resolved: dict[str, Any], field: str, default: object) -> object:
    for decision in resolved.get("decisions", []):
        if decision.get("field") == field:
            return decision.get("value")
    return default


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def _fmt_float(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text}.0" if "." not in text else text


def _lifecycle_env(mode: str) -> list[SymbiontRuntimeValue]:
    prod = mode == "prod"
    idle_timeout = 180 if prod else 600
    idle_check_interval = 10 if prod else 15
    pressure_reap_min_idle = 30 if prod else 60
    pressure_reap_max_per_cycle = 4 if prod else 3
    prewarm_ttl_unused = 45 if prod else 300
    reason_suffix = "production" if prod else "local development"
    values = [
        ("ORC_LIFECYCLE_IDLE_TIMEOUT", idle_timeout, "Base lifecycle idle TTL follows the configured runtime mode."),
        ("ORC_LIFECYCLE_IDLE_CHECK_INTERVAL", idle_check_interval, "Lifecycle reaper cadence follows the configured runtime mode."),
        ("ORC_LIFECYCLE_PRESSURE_REAP_MIN_IDLE_SECONDS", pressure_reap_min_idle, "Pressure reaping can reclaim idle services sooner in production mode."),
        ("ORC_LIFECYCLE_PRESSURE_REAP_MAX_PER_CYCLE", pressure_reap_max_per_cycle, "Pressure reaping batch size follows the configured runtime mode."),
        ("ORC_PREWARMING_TTL_UNUSED_SECONDS", prewarm_ttl_unused, "Unused prewarmed services are cancelled quickly in production mode."),
        ("ORC_PREWARMING_MAX_PREWARM_PER_REQUEST", 2, "Prewarming remains bounded per request."),
        ("ORC_PREWARMING_MAX_GPU_PREWARM_PER_REQUEST", 0, "GPU prewarming stays disabled unless explicitly enabled."),
    ]
    overrides = {
        "REASONING_AND_RESPONSE": 240 if prod else 900,
        "RESEARCH": PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS["research"] if prod else 600,
        "LOCAL_EVIDENCE_OPERATOR": 180 if prod else 600,
        "EXECUTION_POLICY_OPERATOR": 120 if prod else idle_timeout,
        "PERSONAL_CONTEXT": 180 if prod else 600,
        "EXTRATOR": PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS["extrator"] if prod else 900,
        "TRANSLATION": PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS["translation"] if prod else 900,
        "AUDIO_TRANSCRIBE": 300 if prod else 900,
        "AUDIO_STREAMING": 300 if prod else 900,
        "MATERIAL_BUILDER": PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS["material_builder"] if prod else 1200,
        "MATERIAL_EXECUTION_KERNEL": PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS["material_execution_kernel"]
        if prod
        else 1200,
        "WORKSPACE_EXECUTION": PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS["workspace_execution"] if prod else 1200,
    }
    values.extend(
        (
            f"ORC_LIFECYCLE_OVERRIDES_{service}_IDLE_TIMEOUT",
            timeout,
            "Per-service lifecycle idle TTL follows the configured runtime mode.",
        )
        for service, timeout in overrides.items()
    )
    return [
        SymbiontRuntimeValue(
            env=env,
            value=str(value),
            origin="inferred",
            reason=f"{reason} ({reason_suffix}).",
            formula="prod: short idle TTLs and fast unused-prewarm cleanup; other modes: development-friendly TTLs",
            override=env,
        )
        for env, value, reason in values
    ]


def resolve_symbiont_runtime(resolved: dict[str, Any]) -> list[SymbiontRuntimeValue]:
    """Return explainable, non-secret symbiont runtime env values."""

    config = resolved["config"]
    mode = str(config.get("mode") or "dev")
    workers = int(_decision(resolved, "runtime.workers.final", 1))
    llm_timeout = int(_decision(resolved, "timeouts.llm_request_seconds", 120))
    quality_latency = config["llm"]["quality_latency"]

    context_budget_tokens = 4000 if quality_latency == "fast" else 6000
    feature_budget_tokens = max(1000, context_budget_tokens // 3)
    agent_budget_tokens = feature_budget_tokens
    context_timeout = float(context_timeout_seconds(llm_timeout))
    planning_timeout = float(planning_timeout_seconds(llm_timeout))
    routing_timeout = float(routing_timeout_seconds(llm_timeout))
    synthesis_timeout = float(synthesis_timeout_seconds(llm_timeout))
    material_timeout = material_decision_timeout_seconds(llm_timeout)
    task_timeout = task_default_timeout_seconds(llm_timeout)
    max_agents = _clamp(workers * 2, 2, 4)
    context_parallel_workers = _clamp(workers * 4, 2, 8)
    total_budget_tokens = context_budget_tokens + (max_agents * agent_budget_tokens) + 1000
    streaming_context_budget = _clamp(round(context_budget_tokens * 0.10), 512, 1200)
    streaming_max_tokens = _clamp(round(context_budget_tokens * 0.17), 768, 1536)
    reasoning_response_timeout = float(max(90, math.ceil(llm_timeout * 1.25)))
    collaboration_memory_entries = _clamp(workers * 5, 5, 10)
    collaboration_ttl = max(300, round(llm_timeout * 2.5))

    values = [
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_CONTEXT_BUDGET_TOKENS",
            value=str(context_budget_tokens),
            origin="inferred",
            reason="Context gathering budget follows the selected quality/latency profile.",
            formula="4000 for llm.quality_latency=fast, otherwise 6000",
            override="ORC_DISPATCH_CONTEXT_BUDGET_TOKENS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_CONTEXT_TIMEOUT_PER_SOURCE",
            value=_fmt_float(context_timeout),
            origin="inferred",
            reason="Each context source gets a bounded but non-trivial slice of the full local LLM timeout.",
            formula="max(8, min(20, round(timeouts.llm_request_seconds / 10)))",
            override="ORC_DISPATCH_CONTEXT_TIMEOUT_PER_SOURCE",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_FEATURE_BUDGET_TOKENS",
            value=str(feature_budget_tokens),
            origin="inferred",
            reason="Single feature queries receive about one third of the full context budget.",
            formula="max(1000, dispatch.context_budget_tokens // 3)",
            override="ORC_DISPATCH_FEATURE_BUDGET_TOKENS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_FEATURE_TIMEOUT_SECONDS",
            value=_fmt_float(context_timeout),
            origin="inferred",
            reason="Feature calls share the same watchdog as context-source gathering.",
            formula="same as ORC_DISPATCH_CONTEXT_TIMEOUT_PER_SOURCE",
            override="ORC_DISPATCH_FEATURE_TIMEOUT_SECONDS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_CONTEXT_PARALLEL_MAX_WORKERS",
            value=str(context_parallel_workers),
            origin="inferred",
            reason="Context fan-out scales with resolved workers but remains capped for local services.",
            formula="clamp(runtime.workers.final * 4, 2, 8)",
            override="ORC_DISPATCH_CONTEXT_PARALLEL_MAX_WORKERS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_AGENT_BUDGET_TOKENS",
            value=str(agent_budget_tokens),
            origin="inferred",
            reason="Agent invocations use the same token budget as a single feature query.",
            formula="dispatch.feature_budget_tokens",
            override="ORC_DISPATCH_AGENT_BUDGET_TOKENS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_AGENT_TIMEOUT_SECONDS",
            value=_fmt_float(float(llm_timeout)),
            origin="inferred",
            reason="Default agent calls follow the resolved LLM request timeout.",
            formula="timeouts.llm_request_seconds",
            override="ORC_DISPATCH_AGENT_TIMEOUT_SECONDS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_AGENT_TIMEOUT_REASONING_AND_RESPONSE",
            value=_fmt_float(synthesis_timeout),
            origin="inferred",
            reason="Reasoning and response gets enough time to consolidate agent outputs without becoming the outer watchdog.",
            formula="max(30, min(120, round(timeouts.llm_request_seconds / 4)))",
            override="ORC_DISPATCH_AGENT_TIMEOUT_REASONING_AND_RESPONSE",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_AGENT_TIMEOUT_REASONING_AND_RESPONSE_DIRECT",
            value=_fmt_float(reasoning_response_timeout),
            origin="inferred",
            reason="Direct response mode wraps a local LLM generation, so dispatch keeps headroom over the LLM timeout.",
            formula="max(90, ceil(timeouts.llm_request_seconds * 1.25))",
            override="ORC_DISPATCH_AGENT_TIMEOUT_REASONING_AND_RESPONSE_DIRECT",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_AGENT_TIMEOUT_AUDIO_TRANSCRIBE",
            value=_fmt_float(float(max(120, llm_timeout))),
            origin="inferred",
            reason="Audio transcription keeps a minimum long-running task budget.",
            formula="max(120, timeouts.llm_request_seconds)",
            override="ORC_DISPATCH_AGENT_TIMEOUT_AUDIO_TRANSCRIBE",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_STREAMING_MAX_CONTEXT_BUDGET_TOKENS",
            value=str(streaming_context_budget),
            origin="inferred",
            reason="Streaming keeps the injected context compact to protect first-token latency.",
            formula="clamp(round(dispatch.context_budget_tokens * 0.07), 320, 750)",
            override="ORC_DISPATCH_STREAMING_MAX_CONTEXT_BUDGET_TOKENS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_STREAMING_MAX_TOKENS",
            value=str(streaming_max_tokens),
            origin="inferred",
            reason="Streaming generation cap scales with context budget while staying local-latency friendly.",
            formula="clamp(round(dispatch.context_budget_tokens * 0.064), 256, 512)",
            override="ORC_DISPATCH_STREAMING_MAX_TOKENS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DISPATCH_STREAMING_TIMEOUT_SECONDS",
            value=_fmt_float(float(llm_timeout)),
            origin="inferred",
            reason="Streaming timeout follows the resolved full LLM request timeout.",
            formula="timeouts.llm_request_seconds",
            override="ORC_DISPATCH_STREAMING_TIMEOUT_SECONDS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DYNAMIC_ROUTING_ROUTING_TIMEOUT",
            value=_fmt_float(routing_timeout),
            origin="inferred",
            reason="Dynamic routing is a fast planning call with a small watchdog.",
            formula="max(5, min(12, round(timeouts.llm_request_seconds / 18)))",
            override="ORC_DYNAMIC_ROUTING_ROUTING_TIMEOUT",
        ),
        SymbiontRuntimeValue(
            env="ORC_DYNAMIC_ROUTING_MAX_AGENTS_PER_REQUEST",
            value=str(max_agents),
            origin="inferred",
            reason="The agent fan-out is bounded by runtime workers and local service pressure.",
            formula="clamp(runtime.workers.final * 2, 2, 4)",
            override="ORC_DYNAMIC_ROUTING_MAX_AGENTS_PER_REQUEST",
        ),
        SymbiontRuntimeValue(
            env="ORC_DYNAMIC_ROUTING_PER_AGENT_TIMEOUT",
            value=_fmt_float(float(llm_timeout)),
            origin="inferred",
            reason="Dynamic per-agent calls follow the resolved LLM request timeout.",
            formula="timeouts.llm_request_seconds",
            override="ORC_DYNAMIC_ROUTING_PER_AGENT_TIMEOUT",
        ),
        SymbiontRuntimeValue(
            env="ORC_DYNAMIC_ROUTING_TOTAL_BUDGET_TOKENS",
            value=str(total_budget_tokens),
            origin="inferred",
            reason="Dynamic routing total budget combines context, selected agents and synthesis slack.",
            formula="dispatch.context_budget_tokens + max_agents * dispatch.agent_budget_tokens + 1000",
            override="ORC_DYNAMIC_ROUTING_TOTAL_BUDGET_TOKENS",
        ),
        SymbiontRuntimeValue(
            env="ORC_DYNAMIC_ROUTING_DECOMPOSITION_TIMEOUT",
            value=_fmt_float(planning_timeout),
            origin="inferred",
            reason="Decomposition may need local LLM planning and should not share the tiny routing watchdog.",
            formula="max(20, min(90, round(timeouts.llm_request_seconds / 4)))",
            override="ORC_DYNAMIC_ROUTING_DECOMPOSITION_TIMEOUT",
        ),
        SymbiontRuntimeValue(
            env="ORC_DYNAMIC_ROUTING_NEGOTIATION_TIMEOUT",
            value=_fmt_float(max(1.0, round(routing_timeout / 5, 1))),
            origin="inferred",
            reason="Negotiation is a tiny coordination step after decomposition.",
            formula="max(1.0, round(dynamic_routing.routing_timeout / 5, 1))",
            override="ORC_DYNAMIC_ROUTING_NEGOTIATION_TIMEOUT",
        ),
        SymbiontRuntimeValue(
            env="ORC_DYNAMIC_ROUTING_MAX_SUBTASKS",
            value=str(_clamp(max_agents + 1, 3, 5)),
            origin="inferred",
            reason="Subtask count tracks the selected agent fan-out with one planning slot.",
            formula="clamp(dynamic_routing.max_agents_per_request + 1, 3, 5)",
            override="ORC_DYNAMIC_ROUTING_MAX_SUBTASKS",
        ),
        SymbiontRuntimeValue(
            env="ORC_AGENTS_COLLABORATION_MAX_ROUNDS",
            value="2",
            origin="inferred",
            reason="Local collaboration defaults to two rounds to avoid runaway multi-agent loops.",
            formula="2 for local balanced collaboration",
            override="ORC_AGENTS_COLLABORATION_MAX_ROUNDS",
        ),
        SymbiontRuntimeValue(
            env="ORC_AGENTS_COLLABORATION_ROUND_TIMEOUT_SECONDS",
            value=_fmt_float(planning_timeout),
            origin="inferred",
            reason="Each collaboration round may involve a local planning response, so it follows planning timeout.",
            formula="dynamic_routing.decomposition_timeout",
            override="ORC_AGENTS_COLLABORATION_ROUND_TIMEOUT_SECONDS",
        ),
        SymbiontRuntimeValue(
            env="ORC_AGENTIC_RUNTIME_TASK_DEFAULT_TIMEOUT_SECONDS",
            value=str(task_timeout),
            origin="inferred",
            reason="The supervised graph watchdog should leave room for agent deliberation and recovery loops.",
            formula="max(1200, min(3600, timeouts.llm_request_seconds * 8))",
            override="ORC_AGENTIC_RUNTIME_TASK_DEFAULT_TIMEOUT_SECONDS",
        ),
        SymbiontRuntimeValue(
            env="ORC_AGENTIC_RUNTIME_MATERIAL_DECISION_TIMEOUT_SECONDS",
            value=str(material_timeout),
            origin="inferred",
            reason="Material output generation is an adaptive multi-call workflow and should not share a short chat timeout.",
            formula="max(600, min(1800, timeouts.llm_request_seconds * 5))",
            override="ORC_AGENTIC_RUNTIME_MATERIAL_DECISION_TIMEOUT_SECONDS",
        ),
        SymbiontRuntimeValue(
            env="ORC_AGENTS_COLLABORATION_MAX_MEMORY_ENTRIES",
            value=str(collaboration_memory_entries),
            origin="inferred",
            reason="Collaboration memory is capped by resolved workers.",
            formula="clamp(runtime.workers.final * 5, 5, 10)",
            override="ORC_AGENTS_COLLABORATION_MAX_MEMORY_ENTRIES",
        ),
        SymbiontRuntimeValue(
            env="ORC_AGENTS_COLLABORATION_MEMORY_TTL_SECONDS",
            value=str(collaboration_ttl),
            origin="inferred",
            reason="Collaboration memory lives long enough for a request sequence but not indefinitely.",
            formula="max(300, round(timeouts.llm_request_seconds * 2.5))",
            override="ORC_AGENTS_COLLABORATION_MEMORY_TTL_SECONDS",
        ),
    ]
    values.extend(_lifecycle_env(mode))
    return values


def resolve_symbiont_env(resolved: dict[str, Any]) -> dict[str, str]:
    return {item.env: item.value for item in resolve_symbiont_runtime(resolved)}
