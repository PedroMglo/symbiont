"""Derived command-tool runtime environment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandRuntimeValue:
    env: str
    value: str
    origin: str
    reason: str
    formula: str
    override: str


def _decision(resolved: dict[str, Any], field: str, default: object) -> object:
    for decision in resolved.get("decisions", []):
        if decision.get("field") == field:
            return decision.get("value")
    return default


def resolve_command_runtime(resolved: dict[str, Any]) -> list[CommandRuntimeValue]:
    """Return explainable non-secret env values for agentic command sessions."""

    config = resolved["config"]
    quality_latency = config["llm"]["quality_latency"]
    workers = int(_decision(resolved, "runtime.workers.final", 1))
    llm_timeout = int(_decision(resolved, "timeouts.llm_request_seconds", 180))
    timeout = max(60, min(240, round(llm_timeout * 2 / 3)))
    max_output = 48000 if quality_latency == "fast" else 128000 if quality_latency == "quality" else 64000
    session_ttl = max(1800, min(7200, llm_timeout * 12))
    max_commands = max(20, min(80, workers * 20))
    memory_limit = 256 if quality_latency == "fast" else 768 if quality_latency == "quality" else 512
    pids_limit = 128 if quality_latency == "fast" else 256 if quality_latency == "quality" else 192
    return [
        CommandRuntimeValue(
            env="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_TIMEOUT_SECONDS",
            value=str(timeout),
            origin="inferred",
            reason="Agentic commands need room for tests and recovery probes while still bounded by the selected LLM profile.",
            formula="clamp(round(timeouts.llm_request_seconds * 2 / 3), 60, 240)",
            override="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_TIMEOUT_SECONDS",
        ),
        CommandRuntimeValue(
            env="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_MAX_OUTPUT_BYTES",
            value=str(max_output),
            origin="inferred",
            reason="Command observations must carry enough logs for autonomous repair without flooding the ledger.",
            formula="48000 fast, 64000 balanced, 128000 quality",
            override="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_MAX_OUTPUT_BYTES",
        ),
        CommandRuntimeValue(
            env="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_SESSION_TTL_SECONDS",
            value=str(session_ttl),
            origin="inferred",
            reason="Complex sandbox sessions may need multiple validation and repair commands before cleanup.",
            formula="clamp(timeouts.llm_request_seconds * 12, 1800, 7200)",
            override="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_SESSION_TTL_SECONDS",
        ),
        CommandRuntimeValue(
            env="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_MAX_COMMANDS_PER_SESSION",
            value=str(max_commands),
            origin="inferred",
            reason="Session length scales lightly with local worker capacity while staying bounded.",
            formula="clamp(runtime.workers.final * 20, 20, 80)",
            override="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_MAX_COMMANDS_PER_SESSION",
        ),
        CommandRuntimeValue(
            env="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_DOCKER_MEMORY_LIMIT_MB",
            value=str(memory_limit),
            origin="inferred",
            reason="Ephemeral command sandboxes need enough memory for Python test and build probes.",
            formula="256 fast, 512 balanced, 768 quality",
            override="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_DOCKER_MEMORY_LIMIT_MB",
        ),
        CommandRuntimeValue(
            env="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_DOCKER_PIDS_LIMIT",
            value=str(pids_limit),
            origin="inferred",
            reason="Pids are bounded to reduce fork/DoS risk in command investigations.",
            formula="128 fast, 192 balanced, 256 quality",
            override="ORC_AGENTIC_RUNTIME_COMMAND_TOOL_DOCKER_PIDS_LIMIT",
        ),
    ]


def resolve_command_env(resolved: dict[str, Any]) -> dict[str, str]:
    return {item.env: item.value for item in resolve_command_runtime(resolved)}
