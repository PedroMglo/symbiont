"""Derived LLM serving compatibility environment."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from .agentic_budgets import (
    material_generation_budget_seconds,
    material_generation_file_target_seconds,
    material_generation_max_files,
)


@dataclass(frozen=True)
class AgentLLMPolicy:
    env_prefix: str
    backend: str
    model_key: str
    temperature: float
    max_tokens: int
    min_timeout_seconds: int
    timeout_divisor: int


AGENT_LLM_POLICIES: dict[str, AgentLLMPolicy] = {
    "reasoning_and_response": AgentLLMPolicy(
        "REASONING_AND_RESPONSE",
        "primary",
        "primary",
        0.3,
        2048,
        120,
        1,
    ),
}


def _decision(resolved: dict[str, Any], field: str, default: object = None) -> object:
    for decision in resolved.get("decisions", []):
        if decision.get("field") == field:
            return decision.get("value")
    return default


def _port(resolved: dict[str, Any], service: str) -> int:
    for port in resolved.get("ports", []):
        if port.get("service") == service:
            return int(port["port"])
    raise KeyError(f"missing port decision for {service}")


def _vllm_context_window(resolved: dict[str, Any]) -> int:
    config = resolved["config"]
    runtime = resolved["runtime"]
    vram_total = runtime.get("vram_total_gb") or 0
    quality_latency = config["llm"]["quality_latency"]

    if not runtime.get("gpu_available"):
        return 0
    if vram_total <= 8:
        return 1024
    if quality_latency == "quality":
        return 4096
    return 2048


def _cpu_threads(resolved: dict[str, Any], fraction: float, maximum: int, minimum: int = 1) -> int:
    threads = int(resolved["runtime"].get("cpu_threads") or 1)
    return max(minimum, min(maximum, math.floor(threads * fraction)))


def _agent_timeout(base_timeout: int, policy: AgentLLMPolicy) -> int:
    return max(policy.min_timeout_seconds, math.ceil(base_timeout / policy.timeout_divisor))


def _compose_profiles() -> set[str]:
    raw = os.getenv("AI_COMPOSE_PROFILES", "core,storage")
    return {part.strip() for part in raw.replace(",", " ").split() if part.strip()}


def _ollama_proxy_upstream() -> tuple[str, str]:
    host = os.getenv("OLLAMA_PROXY_UPSTREAM_HOST", "host.docker.internal").strip() or "host.docker.internal"
    port = os.getenv("OLLAMA_PROXY_UPSTREAM_PORT", "11434").strip() or "11434"
    if not port.isdigit():
        raise ValueError(f"OLLAMA_PROXY_UPSTREAM_PORT must be numeric, got {port!r}")
    return host, port


def _ollama_accelerator(resolved: dict[str, Any]) -> str:
    ollama_host = resolved.get("ollama_host")
    if isinstance(ollama_host, dict) and ollama_host.get("gpu_enabled"):
        return "gpu"
    return "cpu"


def _material_lane_env(
    *,
    lane: str,
    base_url: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout_seconds: int,
    no_progress_timeout_seconds: int,
    wall_budget_seconds: int,
) -> dict[str, str]:
    prefix = f"MATERIAL_BUILDER_{lane.upper()}_LLM"
    return {
        f"{prefix}_BASE_URL": base_url,
        f"{prefix}_MODEL": model,
        f"{prefix}_TEMPERATURE": str(temperature),
        f"{prefix}_MAX_TOKENS": str(max_tokens),
        f"{prefix}_TIMEOUT_SECONDS": str(timeout_seconds),
        f"{prefix}_NO_PROGRESS_TIMEOUT_SECONDS": str(no_progress_timeout_seconds),
        f"{prefix}_WALL_BUDGET_SECONDS": str(wall_budget_seconds),
    }


def resolve_llm_env(resolved: dict[str, Any]) -> dict[str, str]:
    """Return non-secret env vars consumed by Compose and agent services."""

    config = resolved["config"]
    runtime = resolved["runtime"]
    quality_latency = str(config["llm"]["quality_latency"])
    effective_backend = str(_decision(resolved, "llm.backend.effective", "llama_cpp"))
    gpu_enabled = bool(runtime.get("gpu_available")) and effective_backend == "vllm"

    bind_host = config["ports"]["bind_host"]
    vllm_port = _port(resolved, "vllm")
    llama_aux_port = _port(resolved, "llama_cpp_aux")
    llama_fast_port = _port(resolved, "llama_cpp_fast")
    profiles = _compose_profiles()
    llama_profile_enabled = "llm" in profiles
    vllm_profile_enabled = "gpu" in profiles and gpu_enabled

    primary_model = "qwen3:8b"
    aux_model = "qwen3.5:4b-q4_K_M"
    critic_model = "atla/selene-mini:latest"
    classifier_model = "qwen3:1.7b"

    gpu_util = _decision(resolved, "llm.vllm.gpu_memory_utilization", 0.0)
    llm_timeout = _decision(resolved, "timeouts.llm_request_seconds", 120)
    llm_timeout_int = int(llm_timeout)
    material_session_budget = material_generation_budget_seconds(llm_timeout_int)
    material_plan_timeout = min(900, max(300, llm_timeout_int * 3))
    material_file_timeout = min(1800, max(600, llm_timeout_int * 5))
    material_patch_timeout = min(900, max(300, llm_timeout_int * 3))
    material_critic_timeout = min(600, max(180, llm_timeout_int * 2))
    material_no_progress = min(300, max(120, llm_timeout_int))
    ollama_proxy_upstream_host, ollama_proxy_upstream_port = _ollama_proxy_upstream()
    ollama_url = "https://ollama-proxy:11434"
    vllm_url = f"https://host.docker.internal:{vllm_port}/v1"
    llama_fast_url = f"https://host.docker.internal:{llama_fast_port}/v1"
    llama_aux_url = f"https://host.docker.internal:{llama_aux_port}/v1"
    primary_url = vllm_url if vllm_profile_enabled else (llama_aux_url if llama_profile_enabled else ollama_url)
    fast_url = llama_fast_url if llama_profile_enabled else ollama_url

    env = {
        "AI_LOCAL_BIND": bind_host,
        "AI_COMPOSE_PROFILES": ",".join(sorted(profiles)),
        "OLLAMA_BASE_URL": ollama_url,
        "ORC_OLLAMA_BASE_URL": ollama_url,
        "RAG_OLLAMA_BASE_URL": ollama_url,
        "OLLAMA_PROXY_UPSTREAM_HOST": ollama_proxy_upstream_host,
        "OLLAMA_PROXY_UPSTREAM_PORT": ollama_proxy_upstream_port,
        "ORC_LLM_BACKEND_OLLAMA_ENABLED": "true",
        "ORC_LLM_BACKEND_OLLAMA_ACCELERATOR": _ollama_accelerator(resolved),
        "ORC_LLM_BACKEND_LLAMA_CPP_AUX_ENABLED": str(llama_profile_enabled).lower(),
        "ORC_LLM_BACKEND_LLAMA_CPP_FAST_ENABLED": str(llama_profile_enabled).lower(),
        "ORC_LLM_BACKEND_VLLM_ENABLED": str(vllm_profile_enabled).lower(),
        "LLAMA_CPP_AUX_PORT": str(llama_aux_port),
        "LLAMA_CPP_FAST_PORT": str(llama_fast_port),
        "VLLM_PORT": str(vllm_port),
        "LLAMA_CPP_AUX_URL": llama_aux_url,
        "LLAMA_CPP_FAST_URL": llama_fast_url,
        "VLLM_URL": vllm_url,
        "REASONING_AND_RESPONSE_MODEL": primary_model,
        "AUDIO_LLM_MODEL": aux_model,
        "VLLM_MODEL": "Qwen/Qwen3-8B-AWQ",
        "VLLM_SERVED_MODEL": "qwen3:8b",
        "VLLM_MAX_MODEL_LEN": str(_vllm_context_window(resolved)),
        "VLLM_GPU_MEM_UTIL": str(gpu_util),
        "VLLM_REQUEST_TIMEOUT_SECONDS": str(llm_timeout),
        "LLAMA_CPP_AUX_MODEL_FILE": "/models/Qwen3-4B-Q4_K_M.gguf",
        "LLAMA_CPP_AUX_CTX_SIZE": "4096",
        "LLAMA_CPP_AUX_N_PREDICT": "512",
        "LLAMA_CPP_AUX_THREADS": str(_cpu_threads(resolved, 0.25, 6, 2)),
        "LLAMA_CPP_AUX_BATCH_SIZE": "512",
        "LLAMA_CPP_AUX_PARALLEL": "2",
        "LLAMA_CPP_FAST_MODEL_FILE": "/models/Qwen3-1.7B-Q8_0.gguf",
        "LLAMA_CPP_FAST_CTX_SIZE": "2048",
        "LLAMA_CPP_FAST_N_PREDICT": "256",
        "LLAMA_CPP_FAST_THREADS": str(_cpu_threads(resolved, 0.17, 4, 1)),
        "LLAMA_CPP_FAST_BATCH_SIZE": "256",
        "LLAMA_CPP_FAST_PARALLEL": "3",
        "MATERIAL_EXECUTION_KERNEL_SESSION_BUDGET_SECONDS": str(material_session_budget),
        "MATERIAL_EXECUTION_KERNEL_NO_PROGRESS_WATCHDOG_SECONDS": str(material_no_progress),
        "MATERIAL_EXECUTION_KERNEL_BUILDER_TIMEOUT_SECONDS": str(material_session_budget),
        "MATERIAL_BUILDER_MAX_FILES": str(
            material_generation_max_files(str(quality_latency))
        ),
        "MATERIAL_BUILDER_FILE_TARGET_SECONDS": str(
            material_generation_file_target_seconds(str(quality_latency))
        ),
        "MATERIAL_BUILDER_LLM_BASE_URL": primary_url,
        "MATERIAL_BUILDER_LLM_MODEL": primary_model,
        "MATERIAL_BUILDER_LLM_TEMPERATURE": "0.2",
        "MATERIAL_BUILDER_LLM_MAX_TOKENS": "4096",
        "MATERIAL_BUILDER_LLM_TIMEOUT_SECONDS": str(material_plan_timeout),
        "MATERIAL_BUILDER_LLM_CALL_MAX_TIMEOUT_SECONDS": str(min(600, max(180, llm_timeout_int * 2))),
        "MATERIAL_BUILDER_LLM_NO_PROGRESS_TIMEOUT_SECONDS": str(material_no_progress),
        "MATERIAL_BUILDER_LLM_WALL_BUDGET_SECONDS": str(material_session_budget),
    }
    env.update(
        _material_lane_env(
            lane="plan",
            base_url=primary_url,
            model=primary_model,
            temperature=0.1,
            max_tokens=4096,
            timeout_seconds=material_plan_timeout,
            no_progress_timeout_seconds=material_no_progress,
            wall_budget_seconds=material_session_budget,
        )
    )
    env.update(
        _material_lane_env(
            lane="file",
            base_url=primary_url,
            model="qwen2.5-coder:7b",
            temperature=0.15,
            max_tokens=8192,
            timeout_seconds=material_file_timeout,
            no_progress_timeout_seconds=material_no_progress,
            wall_budget_seconds=material_session_budget,
        )
    )
    env.update(
        _material_lane_env(
            lane="patch",
            base_url=primary_url,
            model="qwen2.5-coder:7b",
            temperature=0.1,
            max_tokens=4096,
            timeout_seconds=material_patch_timeout,
            no_progress_timeout_seconds=material_no_progress,
            wall_budget_seconds=material_session_budget,
        )
    )
    env.update(
        _material_lane_env(
            lane="repair",
            base_url=primary_url,
            model=primary_model,
            temperature=0.1,
            max_tokens=4096,
            timeout_seconds=material_patch_timeout,
            no_progress_timeout_seconds=material_no_progress,
            wall_budget_seconds=material_session_budget,
        )
    )
    env.update(
        _material_lane_env(
            lane="critic",
            base_url=primary_url,
            model=critic_model,
            temperature=0.0,
            max_tokens=2048,
            timeout_seconds=material_critic_timeout,
            no_progress_timeout_seconds=material_no_progress,
            wall_budget_seconds=material_session_budget,
        )
    )
    for policy in AGENT_LLM_POLICIES.values():
        if policy.backend == "primary":
            base_url = primary_url
        elif policy.backend == "fast":
            base_url = fast_url
        elif policy.backend == "ollama":
            base_url = ollama_url
        else:
            base_url = primary_url
        if policy.model_key == "primary":
            model = primary_model
        elif policy.model_key == "classifier":
            model = classifier_model
        elif policy.model_key == "critic":
            model = critic_model
        else:
            model = primary_model
        timeout = _agent_timeout(llm_timeout_int, policy)
        env[f"{policy.env_prefix}_LLM_BASE_URL"] = base_url
        env[f"{policy.env_prefix}_LLM_MODEL"] = model
        env[f"{policy.env_prefix}_LLM_TEMPERATURE"] = str(policy.temperature)
        env[f"{policy.env_prefix}_LLM_MAX_TOKENS"] = str(policy.max_tokens)
        env[f"{policy.env_prefix}_LLM_MAX_OUTPUT_TOKENS"] = str(policy.max_tokens)
        env[f"{policy.env_prefix}_LLM_TIMEOUT_SECONDS"] = str(timeout)
    return env
