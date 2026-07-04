"""Resolve, validate and explain ai-local central configuration."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from .command_runtime import resolve_command_runtime
from .docker_resources import resolve_docker_resources
from .env_compat import read_env_file, sanitized_env
from .intelligence import infer_all
from .llm import resolve_llm_env
from .ollama_host import format_ollama_apply_script, format_ollama_systemd_override, resolve_ollama_host_config
from .operational_self_model import OPERATIONAL_SELF_MODEL_CONTRACT, build_operational_self_model
from .ports import port_conflicts, resolve_ports, resolve_service_endpoints
from .rag import resolve_rag_runtime
from .resource_governor_policy import build_effective_policy_payload
from .runtime import RuntimeInfo, probe_runtime
from .schema import AppConfig, ConfigError, parse_app_config, to_plain
from .symbiont_runtime import resolve_symbiont_runtime

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "main.yaml"
PROFILES_PATH = ROOT / "config" / "profiles.yaml"
RESOURCE_GOVERNOR_CONFIG_PATH = ROOT / "config" / "resource_governor.yaml"
RESOURCE_GOVERNOR_POLICY_SNAPSHOT_PATH = ROOT / ".local" / "generated" / "resource_governor_policy.json"
AUTOTUNING_EFFECTIVE_PATH = ROOT / ".local" / "generated" / "autotuning.effective.json"
OPERATIONAL_SELF_MODEL_SNAPSHOT_PATH = ROOT / ".local" / "generated" / "operational-self-model.json"
ENV_STORAGE_PATH = ROOT / ".env.storage.generated"
ENV_LLM_PATH = ROOT / ".env.llm.generated"
ENV_SERVICES_PATH = ROOT / ".env.services.generated"
ENV_DOCKER_RESOURCES_PATH = ROOT / ".env.docker.resources.generated"
OLLAMA_HOST_CONFIG_DIR = ROOT / ".local" / "generated" / "ollama-host"
RESOLVER_CONTRACT_VERSION = "ai-local.config-resolver.v1"
CONFIG_HEALTH_CONTRACT_VERSION = "ai-local.config-health.v1"
GENERATED_ENV_CONTRACTS: dict[str, dict[str, str]] = {
    "storage": {
        "contract": "ai-local.storage-env.v1",
        "version": "1",
        "artifact": ".env.storage.generated",
        "command": "python -m config.resolver --write-storage-env",
        "consumer": "Docker Compose storage binds; storage_guardian",
        "status": "compatibility env until storage consumers read typed resolver output",
        "contract_env": "AI_LOCAL_STORAGE_ENV_CONTRACT",
        "version_env": "AI_LOCAL_STORAGE_ENV_CONTRACT_VERSION",
    },
    "llm": {
        "contract": "ai-local.llm-env.v1",
        "version": "1",
        "artifact": ".env.llm.generated",
        "command": "python -m config.resolver --write-llm-env",
        "consumer": "LLM serving Compose services; orchestrator and RAG model loaders",
        "status": "compatibility env until LLM consumers read typed resolver output",
        "contract_env": "AI_LOCAL_LLM_ENV_CONTRACT",
        "version_env": "AI_LOCAL_LLM_ENV_CONTRACT_VERSION",
    },
    "services": {
        "contract": "ai-local.services-env.v1",
        "version": "1",
        "artifact": ".env.services.generated",
        "command": "python -m config.resolver --write-services-env",
        "consumer": "Docker Compose service wiring; orchestrator, agents, features and RAG service loaders",
        "status": "compatibility env until service consumers read typed resolver output",
        "contract_env": "AI_LOCAL_SERVICES_ENV_CONTRACT",
        "version_env": "AI_LOCAL_SERVICES_ENV_CONTRACT_VERSION",
    },
    "docker_resources": {
        "contract": "ai-local.docker-resources-env.v1",
        "version": "1",
        "artifact": ".env.docker.resources.generated",
        "command": "python -m config.resolver --write-docker-resources-env",
        "consumer": "Docker Compose resource limits, lifecycle parallelism, and cache caps generated from central config",
        "status": "compatibility env until Compose resource policy reads typed resolver output",
        "contract_env": "AI_LOCAL_DOCKER_RESOURCES_ENV_CONTRACT",
        "version_env": "AI_LOCAL_DOCKER_RESOURCES_ENV_CONTRACT_VERSION",
    },
}
RUNTIME_OUTPUT_CONTRACTS: dict[str, dict[str, str]] = {
    "resolver": {
        "contract": RESOLVER_CONTRACT_VERSION,
        "version": "1",
        "artifact": "python -m config.resolver --print",
        "consumer": "automation, UI, ledger and diagnostics consumers that need typed resolved config",
        "status": "source-of-truth typed resolver output",
    },
    "rag_runtime": {
        "contract": "ai-local.rag-runtime.v1",
        "version": "1",
        "artifact": "resolved.rag_runtime",
        "consumer": "obsidian-rag service runtime and Compose env generation",
        "status": "typed runtime values mirrored into .env.services.generated for compatibility",
    },
    "symbiont_runtime": {
        "contract": "ai-local.symbiont-runtime.v1",
        "version": "1",
        "artifact": "resolved.symbiont_runtime",
        "consumer": "orchestrator runtime, dispatch and agentic service wiring",
        "status": "typed runtime values mirrored into .env.services.generated for compatibility",
    },
    "command_runtime": {
        "contract": "ai-local.command-runtime.v1",
        "version": "1",
        "artifact": "resolved.command_runtime",
        "consumer": "agentic command sandbox/runtime tooling",
        "status": "typed runtime values mirrored into .env.services.generated for compatibility",
    },
    "docker_resources": {
        "contract": "ai-local.docker-resources.v1",
        "version": "1",
        "artifact": "resolved.docker_resources",
        "consumer": "Docker Compose resource env generation and governance checks",
        "status": "typed resource envelope mirrored into .env.docker.resources.generated for compatibility",
    },
    "resource_governor_policy": {
        "contract": "resource-governor.v1",
        "version": "1",
        "artifact": ".local/generated/resource_governor_policy.json",
        "consumer": "orchestrator Resource Governor and runtime pressure gates",
        "status": "typed policy snapshot generated from central config and Resource Governor rules",
    },
    "autotuning_effective": {
        "contract": "ai-local.autotuning-effective.v1",
        "version": "1",
        "artifact": ".local/generated/autotuning.effective.json",
        "consumer": "config.resolver Resource Governor overlay loader",
        "status": "manual-approval generated overlay; absent by default and never auto-applied",
    },
    "operational_self_model": {
        "contract": OPERATIONAL_SELF_MODEL_CONTRACT,
        "version": "1",
        "artifact": "resolved.operational_self_model",
        "consumer": "Resource Governor, prewarming, routing, UI and diagnostics surfaces",
        "status": "derived status/capacity model; consumers must not treat it as behavior ownership",
    },
    "config_health": {
        "contract": CONFIG_HEALTH_CONTRACT_VERSION,
        "version": "1",
        "artifact": "python -m config.resolver --health-report",
        "consumer": "orchestrator /health, UI and ledger diagnostics",
        "status": "short typed status report derived from resolver validation and runtime probes",
    },
}
COMPATIBILITY_SURFACES: dict[str, dict[str, str]] = {
    "config/orc/*.toml": {
        "status": "compatibility input",
        "consumer": "orchestrator runtime loaders while they migrate to typed resolver output",
        "sunset": "reduce as orchestrator loaders consume resolved contracts directly",
    },
    "config/rag/*.toml": {
        "status": "compatibility input",
        "consumer": "RAG runtime loaders while they migrate to typed resolver output",
        "sunset": "reduce as RAG loaders consume resolved contracts directly",
    },
    "config/models/*.json": {
        "status": "compatibility input",
        "consumer": "model role/prompt loaders; runtime URLs stay in generated resolver env",
        "sunset": "keep model intent here, but remove runtime wiring once all consumers read typed resolver output",
    },
}
STORAGE_ENV_KEYS = (
    "AI_LOCAL_UID",
    "AI_LOCAL_GID",
    "AI_LOCAL_STORAGE_MODE",
    "AI_LOCAL_PROJECT_SCRATCH_ROOT",
    "AI_LOCAL_AGENT_TEMP_ROOT",
    "AI_LOCAL_AGENT_TEMP_ROOTS",
    "AI_LOCAL_OUTPUT_ROOT",
    "AI_LOCAL_HOST_OUTPUT_ROOT",
    "AI_LOCAL_STORAGE_ROOT",
    "AI_LOCAL_LOGS_ROOT",
    "AI_STORAGE_EXTERNAL_ROOT",
    "AI_STORAGE_EXTERNAL_MOUNT_PARENT",
    "AI_STORAGE_HOST_BIND_ROOT",
    "AI_STORAGE_CONTAINER_BIND_ROOT",
    "AI_STORAGE_GUARDIAN_ROOT",
    "AI_STORAGE_GUARDIAN_EXTERNAL_ROOT",
    "AI_STORAGE_REQUIRE_EXTERNAL",
    "AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK",
    "LLM_MODELS_DIR",
    "HF_CACHE_DIR",
    "HF_HOME",
    "OLLAMA_MODELS",
    "SYMBIONT_DATA_DIR",
    "EXTRATOR_DATA_DIR",
    "LOCAL_EVIDENCE_OPERATOR_DATA_DIR",
    "EXECUTION_POLICY_OPERATOR_DATA_DIR",
    "PERSONAL_CONTEXT_DATA_DIR",
    "TRANSLATION_CACHE_DIR",
    "TRANSLATION_MODELS_DIR",
    "AUDIO_TRANSCRIBE_DATA_DIR",
    "GRAPHIFY_OUT_DIR",
    "RAG_DATA_DIR",
    "QDRANT_DATA_DIR",
    "CLICKHOUSE_DATA_DIR",
    "CLICKHOUSE_LOGS_DIR",
    "GRAFANA_DATA_DIR",
    "LANGFUSE_DB_DATA_DIR",
    "REDIS_DATA_DIR",
    "STORAGE_GUARDIAN_DATA_DIR",
    "STORAGE_GUARDIAN_STATE_DIR",
    "STORAGE_GUARDIAN_CACHE_DIR",
)
STORAGE_DIRECTORY_KEYS = {
    "AI_LOCAL_PROJECT_SCRATCH_ROOT",
    "AI_LOCAL_OUTPUT_ROOT",
    "AI_LOCAL_LOGS_ROOT",
    "LLM_MODELS_DIR",
    "HF_CACHE_DIR",
    "HF_HOME",
    "OLLAMA_MODELS",
    "SYMBIONT_DATA_DIR",
    "EXTRATOR_DATA_DIR",
    "LOCAL_EVIDENCE_OPERATOR_DATA_DIR",
    "EXECUTION_POLICY_OPERATOR_DATA_DIR",
    "PERSONAL_CONTEXT_DATA_DIR",
    "TRANSLATION_CACHE_DIR",
    "TRANSLATION_MODELS_DIR",
    "AUDIO_TRANSCRIBE_DATA_DIR",
    "GRAPHIFY_OUT_DIR",
    "RAG_DATA_DIR",
    "QDRANT_DATA_DIR",
    "CLICKHOUSE_DATA_DIR",
    "CLICKHOUSE_LOGS_DIR",
    "GRAFANA_DATA_DIR",
    "LANGFUSE_DB_DATA_DIR",
    "REDIS_DATA_DIR",
    "STORAGE_GUARDIAN_DATA_DIR",
    "STORAGE_GUARDIAN_STATE_DIR",
    "STORAGE_GUARDIAN_CACHE_DIR",
}

KVM_MAJOR = 10
KVM_MINOR = 232
RAG_DATA_SUBDIRS = (
    "qdrant",
)
AUDIO_TRANSCRIBE_DATA_SUBDIRS = (
    "input",
    "output",
)

DEFAULT_CONTAINER_STORAGE_ROOT = "/storage/ai-local"


def _storage_layout_paths(base: str, *, namespace: str = "", project_root: Path | None = None) -> dict[str, str]:
    data_base = f"{base}/data"
    logs_base = f"{base}/logs"
    storage_guardian_scratch = f"{data_base}/storage_guardian/scratch"
    if namespace:
        data_base = f"{data_base}/{namespace}"
        logs_base = f"{logs_base}/{namespace}"
        storage_guardian_scratch = f"{data_base}/storage_guardian/scratch"
    scratch_root = f"{storage_guardian_scratch}/project"
    return {
        "AI_LOCAL_PROJECT_SCRATCH_ROOT": scratch_root,
        "AI_LOCAL_AGENT_TEMP_ROOT": scratch_root,
        "AI_LOCAL_AGENT_TEMP_ROOTS": f"{scratch_root}:{storage_guardian_scratch}",
        "AI_LOCAL_OUTPUT_ROOT": f"{scratch_root}/agentic-command-output",
        "AI_LOCAL_HOST_OUTPUT_ROOT": f"{scratch_root}/agentic-command-output",
        "AI_LOCAL_LOGS_ROOT": logs_base,
        "SYMBIONT_LOGS_DIR": f"{logs_base}/symbiont",
        "LLM_MODELS_DIR": f"{data_base}/models/gguf",
        "HF_CACHE_DIR": f"{data_base}/cache/hf",
        "HF_HOME": f"{data_base}/cache/hf",
        "OLLAMA_MODELS": f"{data_base}/models/ollama",
        "SYMBIONT_DATA_DIR": f"{data_base}/symbiont",
        "EXTRATOR_DATA_DIR": f"{data_base}/extrator",
        "LOCAL_EVIDENCE_OPERATOR_DATA_DIR": f"{data_base}/local_evidence_operator",
        "EXECUTION_POLICY_OPERATOR_DATA_DIR": f"{data_base}/execution_policy_operator",
        "PERSONAL_CONTEXT_DATA_DIR": f"{data_base}/personal_context",
        "TRANSLATION_CACHE_DIR": f"{data_base}/cache/translation",
        "TRANSLATION_MODELS_DIR": f"{data_base}/models/translation",
        "AUDIO_TRANSCRIBE_DATA_DIR": f"{data_base}/audio",
        "GRAPHIFY_OUT_DIR": f"{data_base}/graphify",
        "RAG_DATA_DIR": f"{data_base}/rag",
        "QDRANT_DATA_DIR": f"{data_base}/docker-volumes/qdrant",
        "CLICKHOUSE_DATA_DIR": f"{data_base}/docker-volumes/clickhouse/data",
        "CLICKHOUSE_LOGS_DIR": f"{logs_base}/clickhouse",
        "GRAFANA_DATA_DIR": f"{data_base}/docker-volumes/grafana",
        "LANGFUSE_DB_DATA_DIR": f"{data_base}/docker-volumes/langfuse-db",
        "REDIS_DATA_DIR": f"{data_base}/docker-volumes/redis",
        "STORAGE_GUARDIAN_DATA_DIR": f"{data_base}/storage_guardian",
        "STORAGE_GUARDIAN_STATE_DIR": f"{base}/state/storage_guardian",
        "STORAGE_GUARDIAN_CACHE_DIR": f"{base}/cache/storage_guardian",
    }


def _generated_env_contract_header(kind: str) -> list[str]:
    contract = GENERATED_ENV_CONTRACTS[kind]
    return [
        f"# Auto-generated by {contract['command']}",
        "# Do not put secrets in this file.",
        f"# Contract: {contract['contract']}",
        f"# Expected consumer: {contract['consumer']}",
        f"# Compatibility status: {contract['status']}; sunset before contract v2.",
    ]


def _generated_env_contract_values(kind: str) -> list[str]:
    contract = GENERATED_ENV_CONTRACTS[kind]
    return [
        f"{contract['contract_env']}={contract['contract']}",
        f"{contract['version_env']}={contract['version']}",
    ]


def _generated_env_contract_summary() -> dict[str, dict[str, str]]:
    summary_keys = ("contract", "version", "artifact", "consumer", "status")
    return {
        kind: {key: contract[key] for key in summary_keys}
        for kind, contract in GENERATED_ENV_CONTRACTS.items()
    }


def _runtime_output_contract_summary() -> dict[str, dict[str, str]]:
    summary_keys = ("contract", "version", "artifact", "consumer", "status")
    return {
        kind: {key: contract[key] for key in summary_keys}
        for kind, contract in RUNTIME_OUTPUT_CONTRACTS.items()
    }


def _compatibility_surface_summary() -> dict[str, dict[str, str]]:
    return {name: dict(surface) for name, surface in COMPATIBILITY_SURFACES.items()}


def _decision_id(field: str) -> str:
    return field if field.startswith("config.") else f"config.{field}"


def _decision_inputs(field: str) -> list[str]:
    if field.startswith("storage."):
        return ["config.storage", "runtime.storage_*", "env:AI_STORAGE_*"]
    if field.startswith("llm."):
        return ["config.llm", "runtime.gpu_*", "env:AI_LLM_*"]
    if field.startswith("hardware."):
        return ["runtime.gpu_*", "env:AI_RUNTIME_FORCE_GPU"]
    if field.startswith("runtime."):
        return ["config.limits", "config.inference", "runtime.cpu_threads", "runtime.ram_available_gb"]
    if field.startswith("timeouts."):
        return ["config.llm.quality_latency", "llm.backend.effective"]
    return ["config/main.yaml", "runtime probes", "AI_* overrides"]


def _decision_probes(field: str) -> list[str]:
    if field.startswith("storage."):
        return ["external storage root", "findmnt filesystem", "writability", "Docker context"]
    if field.startswith("llm.") or field.startswith("hardware."):
        return ["nvidia-smi", "optional Docker GPU probe"]
    if field.startswith("runtime."):
        return ["/proc/meminfo", "os.cpu_count", "GPU/VRAM probe"]
    if field.startswith("timeouts."):
        return ["effective backend decision"]
    return ["resolver inputs"]


def _decision_impact(field: str) -> list[str]:
    if field.startswith("storage."):
        return [
            "storage env paths",
            "Docker bind roots",
            "storage_guardian runtime",
            "Resource Governor storage policy",
        ]
    if field.startswith("llm."):
        return ["LLM env generation", "model backend enablement", "agent/RAG timeout budgets"]
    if field.startswith("hardware."):
        return ["GPU service profile", "vLLM enablement", "Resource Governor machine profile"]
    if field == "runtime.workers.final":
        return ["service workers", "RAG runtime", "Symbiont runtime", "Resource Governor limits"]
    if field == "runtime.batch_size":
        return ["RAG embedding batch", "Resource Governor embedding batch"]
    if field.startswith("timeouts."):
        return ["LLM request budget", "agentic watchdogs", "RAG/query timeouts"]
    return ["resolved config consumers"]


def _decision_confidence(decision: dict[str, Any]) -> str:
    if decision.get("origin") == "manual":
        return "high"
    if decision.get("warning") and decision.get("field") != "hardware.gpu.available":
        return "medium"
    return "high"


def _decision_status(decision: dict[str, Any]) -> str:
    field = str(decision.get("field") or "")
    value = str(decision.get("value") or "")
    if value in {"external_missing", "local_fallback"}:
        return value
    if decision.get("warning") and field != "hardware.gpu.available":
        return "degraded"
    return "ready"


def _explainable_decision(decision: dict[str, Any]) -> dict[str, Any]:
    field = str(decision.get("field") or "unknown")
    enriched = dict(decision)
    enriched["decision_id"] = _decision_id(field)
    enriched["inputs"] = _decision_inputs(field)
    enriched["probes"] = _decision_probes(field)
    enriched["confidence"] = _decision_confidence(decision)
    enriched["status"] = _decision_status(decision)
    enriched["downstream_impact"] = _decision_impact(field)
    return enriched


def _artifact_state(artifact: str, config_path: Path) -> dict[str, Any]:
    path = ROOT / artifact
    state = "not_generated"
    if path.exists():
        state = "ready"
        try:
            if path.stat().st_mtime < config_path.stat().st_mtime:
                state = "stale"
        except OSError:
            state = "degraded"
    return {
        "artifact": artifact,
        "path": str(path),
        "status": state,
    }


def build_config_health_report(resolved: dict[str, Any], errors: list[str] | None = None) -> dict[str, Any]:
    """Return a short typed status object suitable for orchestrator health surfaces."""

    errors = errors or []
    config_path = Path(resolved.get("compatibility", {}).get("config_path") or DEFAULT_CONFIG_PATH)
    storage_mode = str(resolved.get("storage_paths", {}).get("AI_LOCAL_STORAGE_MODE") or "unknown")
    outputs = {
        kind: {
            **_artifact_state(contract["artifact"], config_path),
            "contract": contract["contract"],
            "version": contract["version"],
            "consumer": contract["consumer"],
        }
        for kind, contract in GENERATED_ENV_CONTRACTS.items()
    }
    stale_outputs = sorted(kind for kind, item in outputs.items() if item["status"] == "stale")
    warning_items = [
        warning
        for warning in resolved.get("warnings", [])
        if warning and "GPU services should stay disabled" not in warning
    ]
    if errors:
        status = "blocked"
    elif storage_mode == "external_missing":
        status = "external_missing"
    elif stale_outputs:
        status = "stale"
    elif storage_mode == "local_fallback":
        status = "local_fallback"
    elif warning_items:
        status = "degraded"
    else:
        status = "ready"
    return {
        "contract": CONFIG_HEALTH_CONTRACT_VERSION,
        "version": 1,
        "status": status,
        "storage_mode": storage_mode,
        "errors": errors,
        "warnings": warning_items,
        "outputs": outputs,
        "stale_outputs": stale_outputs,
        "runtime": {
            "docker_available": resolved.get("runtime", {}).get("docker_available"),
            "docker_context": resolved.get("runtime", {}).get("docker_context"),
            "gpu_available": resolved.get("runtime", {}).get("gpu_available"),
            "cpu_threads": resolved.get("runtime", {}).get("cpu_threads"),
            "ram_available_gb": resolved.get("runtime", {}).get("ram_available_gb"),
        },
        "downstream": {
            "resource_governor_policy": (resolved.get("resource_governor_policy") or {}).get("contract_version"),
            "orchestrator_health": "embed this object as the config component status; do not reinterpret service behavior here",
        },
    }


def _bool_env(value: bool) -> str:
    return "true" if value else "false"


def _host_identity_env() -> dict[str, str]:
    uid = os.environ.get("AI_LOCAL_UID")
    gid = os.environ.get("AI_LOCAL_GID")
    if uid is None:
        uid = str(os.getuid()) if hasattr(os, "getuid") else "1000"
    if gid is None:
        gid = str(os.getgid()) if hasattr(os, "getgid") else "1000"
    return {"AI_LOCAL_UID": uid, "AI_LOCAL_GID": gid}


def _external_mount_parent(root: Path | None) -> str:
    if root is None:
        return "/mnt"
    root_path = root.expanduser()
    parts = root_path.parts
    if len(parts) > 1 and parts[1] == "mnt":
        return "/mnt"
    return str(root_path.parent)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return data


def _load_generated_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _set_nested(raw: dict[str, Any], dotted: str, value: Any) -> None:
    current = raw
    parts = dotted.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


ENV_OVERRIDES: dict[str, str] = {
    "AI_MODE": "mode",
    "AI_HARDWARE_PROFILE": "hardware.profile",
    "AI_STORAGE_EXTERNAL_ROOT": "storage.external_root",
    "AI_STORAGE_EXPECTED_FILESYSTEM": "storage.expected_filesystem",
    "AI_STORAGE_REQUIRE_EXTERNAL": "storage.require_external",
    "AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK": "storage.allow_local_heavy_fallback",
    "AI_LLM_PREFERRED_BACKEND": "llm.preferred_backend",
    "AI_LLM_QUALITY_LATENCY": "llm.quality_latency",
    "AI_LIMITS_MAX_WORKERS": "limits.max_workers",
    "AI_LIMITS_CPU_BUDGET_FRACTION": "limits.cpu_budget_fraction",
    "AI_LIMITS_MEMORY_BUDGET_FRACTION": "limits.memory_budget_fraction",
    "AI_PORTS_BIND_HOST": "ports.bind_host",
    "AI_PORTS_BASE_PORT": "ports.base_port",
    "AI_RUNTIME_PROBE": "runtime.probe",
    "AI_RUNTIME_DOCKER_PROBE": "runtime.docker_probe",
    "AI_RUNTIME_FORCE_GPU": "runtime.force_gpu",
    "AI_LLM_VLLM_GPU_MEMORY_UTILIZATION": "inference.vllm_gpu_memory_utilization_cap",
    "AI_LOCAL_COMPOSE_PARALLEL_LIMIT": "docker.compose_parallel_limit",
    "COMPOSE_PARALLEL_LIMIT": "docker.compose_parallel_limit",
    "DOCKER_BUILDKIT": "docker.buildkit",
    "AI_LOCAL_DOCKER_BUILD_CACHE_MAX": "docker.build_cache_max",
    "AI_LOCAL_DOCKER_UP_NO_BUILD": "docker.up_no_build",
    "AI_LOCAL_DOCKER_UP_WAIT": "docker.up_wait",
    "AI_LOCAL_DOCKER_UP_WAIT_TIMEOUT": "docker.up_wait_timeout_seconds",
    "AI_LOCAL_DOCKER_REMOVE_ORPHANS": "docker.remove_orphans",
}


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    result = dict(raw)
    for env_key, dotted in ENV_OVERRIDES.items():
        if env_key in os.environ:
            _set_nested(result, dotted, os.environ[env_key])
    return result


def _profile_values(config_raw: dict[str, Any]) -> dict[str, Any]:
    profile = str(config_raw.get("hardware", {}).get("profile", "auto"))
    profiles_doc = _load_yaml(PROFILES_PATH)
    profiles = profiles_doc.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("profiles.yaml: profiles must be a mapping")
    entry = profiles.get(profile, {})
    if not isinstance(entry, dict):
        raise ConfigError(f"profile {profile!r} must be a mapping")
    values = entry.get("values", {})
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ConfigError(f"profile {profile!r} values must be a mapping")
    return values


def _runtime_for(config: AppConfig) -> RuntimeInfo:
    if not config.runtime.probe:
        return RuntimeInfo(
            cpu_threads=os.cpu_count() or 1,
            ram_total_gb=None,
            ram_available_gb=None,
            gpu_available=False,
            gpu_name=None,
            vram_total_gb=None,
            vram_used_gb=None,
            vram_free_gb=None,
            storage_root=config.storage.external_root,
            storage_exists=False,
            storage_mounted=False,
            storage_filesystem=None,
            storage_writable=False,
            docker_available=False,
        )
    return probe_runtime(
        config.storage.external_root,
        docker_probe=config.runtime.docker_probe,
        force_gpu=config.runtime.force_gpu,
    )


def _build_resource_governor_policy(resolved: dict[str, Any]) -> dict[str, Any]:
    return build_effective_policy_payload(
        resolved_config=resolved,
        policy_path=RESOURCE_GOVERNOR_CONFIG_PATH,
    )


def _set_policy_path(policy: dict[str, Any], parts: list[str], value: Any) -> bool:
    if not parts:
        return False
    current: dict[str, Any] = policy
    for part in parts[:-1]:
        node = current.get(part)
        if node is None:
            node = {}
            current[part] = node
        if not isinstance(node, dict):
            return False
        current = node
    current[parts[-1]] = value
    return True


def _apply_autotuning_effective(policy: dict[str, Any]) -> dict[str, Any]:
    effective = _load_generated_json(AUTOTUNING_EFFECTIVE_PATH)
    cloned = json.loads(json.dumps(policy))
    metadata: dict[str, Any] = {
        "contract": "ai-local.autotuning-effective.v1",
        "status": "not_applied",
        "source_path": str(AUTOTUNING_EFFECTIVE_PATH),
    }
    if not effective:
        cloned["autotuning"] = metadata
        return cloned
    metadata.update(
        {
            "status": str(effective.get("status") or "unknown"),
            "generated_at": effective.get("generated_at"),
            "approved_by": effective.get("approved_by"),
            "approval_reason": effective.get("approval_reason"),
        }
    )
    if effective.get("status") != "applied":
        cloned["autotuning"] = metadata
        return cloned

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    overrides = effective.get("overrides") if isinstance(effective.get("overrides"), list) else []
    for override in overrides:
        if not isinstance(override, dict):
            continue
        target = str(override.get("target") or "")
        if not target.startswith("resource_governor."):
            skipped.append({"target": target, "reason": "outside resource_governor overlay scope"})
            continue
        parts = target.removeprefix("resource_governor.").split(".")
        if _set_policy_path(cloned, parts, override.get("value")):
            applied.append(
                {
                    "proposal_id": override.get("proposal_id"),
                    "target": target,
                    "value": override.get("value"),
                    "reason": override.get("reason"),
                    "rollback": override.get("rollback") or {},
                }
            )
        else:
            skipped.append({"target": target, "reason": "target path is not mergeable"})
    metadata["status"] = "applied" if applied else "no_applicable_overrides"
    metadata["overrides"] = applied
    metadata["skipped"] = skipped
    cloned["autotuning"] = metadata
    return cloned


def _storage_paths(config: AppConfig, storage_mode: str) -> dict[str, str]:
    root = config.storage.external_root
    container_root = os.environ.get("AI_STORAGE_CONTAINER_BIND_ROOT") or DEFAULT_CONTAINER_STORAGE_ROOT
    if storage_mode == "external" and root is not None:
        base = str(root)
        return {
            **_host_identity_env(),
            **_storage_layout_paths(base),
            "AI_LOCAL_STORAGE_MODE": storage_mode,
            "AI_LOCAL_STORAGE_ROOT": base,
            "AI_STORAGE_EXTERNAL_ROOT": base,
            "AI_STORAGE_EXTERNAL_MOUNT_PARENT": _external_mount_parent(root),
            "AI_STORAGE_HOST_BIND_ROOT": base,
            "AI_STORAGE_CONTAINER_BIND_ROOT": container_root,
            "AI_STORAGE_GUARDIAN_ROOT": container_root,
            "AI_STORAGE_GUARDIAN_EXTERNAL_ROOT": container_root,
            "AI_STORAGE_REQUIRE_EXTERNAL": _bool_env(config.storage.require_external),
            "AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK": _bool_env(config.storage.allow_local_heavy_fallback),
        }
    if storage_mode == "external_missing":
        base = str(ROOT / ".local")
        external = str(root) if root is not None else ""
        return {
            **_host_identity_env(),
            **_storage_layout_paths(base, namespace="external-missing"),
            "AI_LOCAL_STORAGE_MODE": "external_missing",
            "AI_LOCAL_STORAGE_ROOT": base,
            "AI_STORAGE_EXTERNAL_ROOT": external,
            "AI_STORAGE_EXTERNAL_MOUNT_PARENT": _external_mount_parent(root),
            "AI_STORAGE_HOST_BIND_ROOT": base,
            "AI_STORAGE_CONTAINER_BIND_ROOT": container_root,
            "AI_STORAGE_GUARDIAN_ROOT": container_root,
            "AI_STORAGE_GUARDIAN_EXTERNAL_ROOT": "",
            "AI_STORAGE_REQUIRE_EXTERNAL": _bool_env(config.storage.require_external),
            "AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK": _bool_env(config.storage.allow_local_heavy_fallback),
        }
    if storage_mode in {"local", "local_fallback", "local_fallback_explicit"}:
        base = str(ROOT / ".local")
        return {
            **_host_identity_env(),
            **_storage_layout_paths(base),
            "AI_LOCAL_STORAGE_MODE": "local" if storage_mode == "local" else "local_fallback",
            "AI_LOCAL_STORAGE_ROOT": base,
            "AI_STORAGE_EXTERNAL_ROOT": str(root) if root is not None else "",
            "AI_STORAGE_EXTERNAL_MOUNT_PARENT": _external_mount_parent(root),
            "AI_STORAGE_HOST_BIND_ROOT": base,
            "AI_STORAGE_CONTAINER_BIND_ROOT": container_root,
            "AI_STORAGE_GUARDIAN_ROOT": container_root,
            "AI_STORAGE_GUARDIAN_EXTERNAL_ROOT": "",
            "AI_STORAGE_REQUIRE_EXTERNAL": _bool_env(config.storage.require_external),
            "AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK": _bool_env(config.storage.allow_local_heavy_fallback),
        }
    return {"AI_LOCAL_STORAGE_MODE": storage_mode}


def resolve_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    raw = _load_yaml(config_path)
    raw = _deep_merge(raw, _profile_values(raw))
    raw = _apply_env_overrides(raw)
    config = parse_app_config(raw)
    runtime = _runtime_for(config)
    decisions = infer_all(config, runtime)
    plain_config = to_plain(config)
    plain_runtime = to_plain(runtime)
    plain_decisions = [_explainable_decision(asdict(d)) for d in decisions]
    ports = resolve_ports(config.ports.base_port, preserve_existing=True)
    runtime_workers = int(next((d.value for d in decisions if d.field == "runtime.workers.final"), 1))
    service_endpoints = resolve_service_endpoints(
        config.ports.base_port,
        runtime_workers=runtime_workers,
        context="docker",
        preserve_existing=True,
    )
    conflicts = port_conflicts(ports)
    storage_mode = next((str(d.value) for d in decisions if d.field == "storage.mode"), "unknown")
    compat_storage = read_env_file(ENV_STORAGE_PATH) if config.compatibility.read_env_storage_generated else {}

    warnings = [d.warning for d in decisions if d.warning]
    warnings.extend(p.warning for p in ports if p.warning)
    if conflicts:
        warnings.append(f"port conflicts: {conflicts}")
    if storage_mode == "local_fallback" and config.storage.require_external:
        if config.storage.external_root is None:
            warnings.append("no external storage root configured or discovered; using local fallback")
        else:
            warnings.append("external storage is configured but unavailable; using local fallback and will reconcile when it returns")
    if storage_mode == "external_missing" and config.storage.require_external:
        warnings.append(
            "external storage is configured but unavailable; using isolated local bind paths because local fallback is disabled"
        )

    resolved = {
        "contract_version": RESOLVER_CONTRACT_VERSION,
        "config": plain_config,
        "contracts": {
            "generated_env": _generated_env_contract_summary(),
            "runtime_outputs": _runtime_output_contract_summary(),
            "compatibility_surfaces": _compatibility_surface_summary(),
        },
        "runtime": plain_runtime,
        "decisions": plain_decisions,
        "ports": [asdict(p) for p in ports],
        "service_endpoints": [asdict(s) for s in service_endpoints],
        "symbiont_runtime": [asdict(r) for r in resolve_symbiont_runtime({
            "config": plain_config,
            "runtime": plain_runtime,
            "decisions": plain_decisions,
        })],
        "rag_runtime": [asdict(r) for r in resolve_rag_runtime({
            "config": plain_config,
            "runtime": plain_runtime,
            "decisions": plain_decisions,
        })],
        "command_runtime": [asdict(r) for r in resolve_command_runtime({
            "config": plain_config,
            "runtime": plain_runtime,
            "decisions": plain_decisions,
        })],
        "docker_resources": [asdict(r) for r in resolve_docker_resources({
            "config": plain_config,
            "runtime": plain_runtime,
            "decisions": plain_decisions,
        })],
        "storage_paths": _storage_paths(config, storage_mode),
        "compatibility": {
            "env_storage_generated": sanitized_env(compat_storage),
            "config_path": str(config_path),
            "profiles_path": str(PROFILES_PATH),
            "resource_governor_config_path": str(RESOURCE_GOVERNOR_CONFIG_PATH),
            "resource_governor_policy_snapshot_path": str(RESOURCE_GOVERNOR_POLICY_SNAPSHOT_PATH),
            "autotuning_effective_path": str(AUTOTUNING_EFFECTIVE_PATH),
            "operational_self_model_snapshot_path": str(OPERATIONAL_SELF_MODEL_SNAPSHOT_PATH),
        },
        "warnings": [w for w in warnings if w],
    }
    resolved["ollama_host"] = resolve_ollama_host_config(resolved)
    try:
        resolved["resource_governor_policy"] = _apply_autotuning_effective(_build_resource_governor_policy(resolved))
    except Exception as exc:
        resolved["resource_governor_policy"] = {}
        resolved["warnings"].append(f"resource_governor_policy could not be generated: {exc}")
    resolved["config_health"] = build_config_health_report(resolved, validate_resolved(resolved))
    resolved["operational_self_model"] = build_operational_self_model(
        resolved,
        autotuning_effective=_load_generated_json(AUTOTUNING_EFFECTIVE_PATH),
    )
    return resolved


def validate_resolved(resolved: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    config = resolved["config"]
    storage = config["storage"]
    storage_mode = resolved["storage_paths"].get("AI_LOCAL_STORAGE_MODE")
    if storage["require_external"] and storage_mode not in {"external", "external_missing", "local_fallback"}:
        errors.append(
            "storage.require_external=true but resolved storage mode is neither external, external_missing nor local_fallback"
        )
    conflicts = port_conflicts([type("P", (), p)() for p in resolved["ports"]])  # type: ignore[arg-type]
    if conflicts:
        errors.append(f"port conflicts detected: {conflicts}")
    for endpoint in resolved.get("service_endpoints", []):
        url = str(endpoint.get("url", ""))
        if url.startswith("http://"):
            errors.append(f"plain HTTP service endpoint is forbidden: {endpoint.get('service')}={url}")
        elif url and not url.startswith("https://"):
            errors.append(f"service endpoint must use https: {endpoint.get('service')}={url}")
    for key, value in resolve_llm_env(resolved).items():
        if not key.endswith(("_URL", "_BASE_URL")):
            continue
        if value.startswith("http://"):
            errors.append(f"plain HTTP LLM endpoint is forbidden: {key}={value}")
        elif value and not value.startswith("https://"):
            errors.append(f"LLM endpoint must use https: {key}={value}")
    return errors


def _is_kvm_device(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == KVM_MAJOR and os.minor(info.st_rdev) == KVM_MINOR


def _workspace_execution_vm_runtime() -> dict[str, str]:
    release = platform.uname().release
    host_boot = Path("/boot")
    kernel = host_boot / f"vmlinuz-{release}"
    qemu_available = shutil.which("qemu-system-x86_64") is not None
    kernel_available = kernel.exists() and kernel.is_file()
    kvm_available = _is_kvm_device(Path("/dev/kvm"))
    backend = "microvm" if qemu_available and kernel_available and kvm_available else "unavailable"
    return {
        "backend": backend,
        "container_kernel_path": f"/host-boot/vmlinuz-{release}" if kernel_available else "",
        "host_boot_dir": str(host_boot if host_boot.exists() else Path("/tmp")),
        "host_kvm_device": "/dev/kvm" if kvm_available else "/dev/null",
        "require_kvm": "true" if kvm_available else "false",
    }


def _format_storage_env(resolved: dict[str, Any]) -> str:
    storage_paths = resolved["storage_paths"]
    mode = storage_paths.get("AI_LOCAL_STORAGE_MODE", "unknown")
    lines = [
        *_generated_env_contract_header("storage"),
        f"# Mode: {mode}",
        "",
        *_generated_env_contract_values("storage"),
    ]
    for key in STORAGE_ENV_KEYS:
        value = storage_paths.get(key)
        if value is not None:
            lines.append(f"{key}={value}")
    lines.append("")
    return "\n".join(lines)


def _format_llm_env(resolved: dict[str, Any]) -> str:
    llm_env = resolve_llm_env(resolved)
    lines = [
        *_generated_env_contract_header("llm"),
        "",
        *_generated_env_contract_values("llm"),
    ]
    for key, value in llm_env.items():
        lines.append(f"{key}={value}")
    lines.append("")
    return "\n".join(lines)


def _format_services_env(resolved: dict[str, Any]) -> str:
    host_projects_dir = ROOT.parent
    host_allowed_root = Path.home()
    storage_paths = resolved.get("storage_paths", {})
    llm_request_timeout = next(
        (
            int(decision["value"])
            for decision in resolved.get("decisions", [])
            if decision.get("field") == "timeouts.llm_request_seconds"
        ),
        180,
    )
    lines = [
        *_generated_env_contract_header("services"),
        "",
        *_generated_env_contract_values("services"),
        f"AI_LOCAL_HOST_ALLOWED_ROOT={host_allowed_root}",
        f"AI_LOCAL_HOST_HOME={host_allowed_root}",
        f"AI_LOCAL_HOST_PROJECTS_DIR={host_projects_dir}",
        f"PROJECTS_DIR={host_projects_dir}",
        f"AI_RAG_HOST_SOURCE_ROOT={host_allowed_root}",
        f"AI_STORAGE_HOST_ACCESS_ROOT={host_allowed_root}",
        f"AI_LOCAL_TLS_DIR_HOST={ROOT / '.local' / 'tls'}",
        f"AI_LOCAL_HOST_PROJECT_ROOT={ROOT}",
        f"ORC_LIFECYCLE_HOST_PROJECT_ROOT={ROOT}",
        f"ORC_LIFECYCLE_PROJECT_DIR={ROOT}",
        f"ORC_LIFECYCLE_COMPOSE_PROJECT_DIR={ROOT}",
        f"ORC_SECRETS_DIR={ROOT / 'infra' / 'docker' / 'secrets'}",
        f"AI_ORC_SETTINGS_DIR_HOST={ROOT / 'config' / 'orc'}",
        f"ORC_MODELS_CONFIG={ROOT / 'config' / 'models' / 'orc.config.json'}",
        f"AI_RAG_SETTINGS_DIR_HOST={ROOT / 'config' / 'rag'}",
        f"RAG_MODELS_CONFIG={ROOT / 'config' / 'models' / 'rag.config.json'}",
        "",
    ]
    for key in (
        "AI_LOCAL_PROJECT_SCRATCH_ROOT",
        "AI_LOCAL_AGENT_TEMP_ROOT",
        "AI_LOCAL_AGENT_TEMP_ROOTS",
        "AI_LOCAL_OUTPUT_ROOT",
        "AI_LOCAL_HOST_OUTPUT_ROOT",
    ):
        value = storage_paths.get(key)
        if value:
            lines.append(f"{key}={value}")
    lines.append("")
    if resolved.get("ports"):
        lines.append("# Published host ports inferred by config.ports")
        for port in resolved["ports"]:
            service_key = str(port["service"]).upper()
            lines.append(f"ORC_PORT_{service_key}={port['port']}")
        lines.append("")
    orc_service_aliases = {
        "clickhouse_http": {"CLICKHOUSE_URL"},
        "otel_http": {"OTEL_ENDPOINT"},
    }
    native_service_aliases = {
        "rag": {"ORC_RAG_URL", "RESEARCH_RAG_URL", "LOCAL_EVIDENCE_GRAPH_RAG_URL"},
        "qdrant_http": {"RAG_STORE_QDRANT_URL"},
        "clickhouse_http": {"RAG_OBSERVABILITY_CLICKHOUSE_URL"},
    }
    for endpoint in resolved.get("service_endpoints", []):
        service_key = endpoint["service"].upper()
        env_prefix = endpoint.get("env_prefix", service_key)
        lines.append(f"ORC_SERVICES_{service_key}_URL={endpoint['url']}")
        lines.append(f"ORC_SERVICES_{service_key}_HOST={endpoint['host']}")
        lines.append(f"ORC_SERVICES_{service_key}_PORT={endpoint['port']}")
        lines.append(f"ORC_SERVICES_{service_key}_WORKERS={endpoint['workers']}")
        lines.append(f"{env_prefix}_SERVER_HOST=0.0.0.0")
        lines.append(f"{env_prefix}_SERVER_PORT={endpoint['port']}")
        lines.append(f"{env_prefix}_SERVER_WORKERS={endpoint['workers']}")
        lines.append(f"{env_prefix}_HEALTHCHECK_PATH={endpoint['healthcheck_path']}")
        lines.append(f"{env_prefix}_HEALTHCHECK_TIMEOUT={endpoint['healthcheck_timeout_seconds']}s")
        if endpoint["service"] == "workspace_execution":
            vm_runtime = _workspace_execution_vm_runtime()
            lines.append("WORKSPACE_EXECUTION_SCRATCH_ROOT=/temp/workspace_execution")
            lines.append('WORKSPACE_EXECUTION_SOURCE_ROOTS={"ai-local":"/projects/ai-local"}')
            lines.append("WORKSPACE_EXECUTION_SESSION_TTL_SECONDS=3600")
            lines.append("WORKSPACE_EXECUTION_COMMAND_TIMEOUT_SECONDS=120")
            lines.append("WORKSPACE_EXECUTION_MAX_OUTPUT_BYTES=20000")
            lines.append("WORKSPACE_EXECUTION_RUNNER_BACKEND=docker_ephemeral")
            lines.append("WORKSPACE_EXECUTION_RUNNER_IMAGE=ai-local-command-sandbox:latest")
            lines.append("WORKSPACE_EXECUTION_RUNNER_NETWORK_DEFAULT=disabled")
            lines.append("WORKSPACE_EXECUTION_RUNNER_PROFILES=standard,test,destructive")
            lines.append("WORKSPACE_EXECUTION_RUNNER_CPU_LIMIT=1.0")
            lines.append("WORKSPACE_EXECUTION_RUNNER_MEMORY_LIMIT=512m")
            lines.append("WORKSPACE_EXECUTION_RUNNER_PIDS_LIMIT=256")
            lines.append(f"WORKSPACE_EXECUTION_VM_BACKEND={vm_runtime['backend']}")
            lines.append("WORKSPACE_EXECUTION_VM_CONTROL_URL=")
            lines.append("WORKSPACE_EXECUTION_VM_CONTROL_TOKEN_FILE=")
            lines.append("WORKSPACE_EXECUTION_VM_IMAGE_REF=ai-local-command-sandbox:latest")
            lines.append("WORKSPACE_EXECUTION_VM_PROFILE=material-default")
            lines.append("WORKSPACE_EXECUTION_VM_QEMU_BINARY=qemu-system-x86_64")
            lines.append(f"WORKSPACE_EXECUTION_VM_KERNEL_PATH={vm_runtime['container_kernel_path']}")
            lines.append(f"WORKSPACE_EXECUTION_VM_BOOT_DIR={vm_runtime['host_boot_dir']}")
            lines.append(f"WORKSPACE_EXECUTION_VM_KVM_DEVICE={vm_runtime['host_kvm_device']}")
            lines.append("WORKSPACE_EXECUTION_VM_KVM_DEVICE_CONTAINER=/dev/kvm")
            lines.append(f"WORKSPACE_EXECUTION_VM_REQUIRE_KVM={vm_runtime['require_kvm']}")
            lines.append("WORKSPACE_EXECUTION_VM_CACHE_ROOT=/temp/workspace_execution_microvm")
            lines.append("WORKSPACE_EXECUTION_VM_BOOT_TIMEOUT_SECONDS=45")
            lines.append("WORKSPACE_EXECUTION_VM_TTL_SECONDS=3600")
            lines.append("WORKSPACE_EXECUTION_VM_CPU_LIMIT=2.0")
            lines.append("WORKSPACE_EXECUTION_VM_MEMORY_LIMIT=4g")
            lines.append("WORKSPACE_EXECUTION_VM_DISK_LIMIT=20g")
            lines.append("WORKSPACE_EXECUTION_COMPOSE_RUNTIME_URL=https://workspace-execution:8000")
            lines.append("WORKSPACE_EXECUTION_COMPOSE_RUNTIME_TOKEN_FILE=/run/secrets/internal_api_key")
            lines.append("WORKSPACE_EXECUTION_COMPOSE_RUNTIME_TIMEOUT_SECONDS=300")
            lines.append("WORKSPACE_EXECUTION_COMPOSE_RUNTIME_BACKEND=dedicated-dind")
            lines.append("WORKSPACE_EXECUTION_COMPOSE_RUNTIME_DIND_IMAGE=docker:27-dind")
            lines.append("WORKSPACE_EXECUTION_COMPOSE_RUNTIME_RUNNER_IMAGE=ai-local-command-sandbox:latest")
            lines.append("STORAGE_GUARDIAN_URL=https://storage-guardian:8730")
            lines.append("WORKSPACE_EXECUTION_STORAGE_GUARDIAN_AGENT=symbiont")
            lines.append("WORKSPACE_EXECUTION_STORAGE_GUARDIAN_STORE=agent_outputs")
            lines.append("WORKSPACE_EXECUTION_STORAGE_GUARDIAN_ZONE=ingest")
        if endpoint["service"] == "rag":
            lines.append("RAG_API_HOST=0.0.0.0")
            lines.append(f"RAG_API_PORT={endpoint['port']}")
        for alias in sorted(orc_service_aliases.get(endpoint["service"], set())):
            lines.append(f"ORC_SERVICES_{alias}={endpoint['url']}")
        for alias in sorted(native_service_aliases.get(endpoint["service"], set())):
            lines.append(f"{alias}={endpoint['url']}")
        lines.append("")
    if resolved.get("rag_runtime"):
        lines.append("# RAG runtime values inferred by config.rag")
        for item in resolved["rag_runtime"]:
            lines.append(f"{item['env']}={item['value']}")
        lines.append("")
    if resolved.get("symbiont_runtime"):
        lines.append("# Symbiont runtime values inferred by config.symbiont_runtime")
        for item in resolved["symbiont_runtime"]:
            lines.append(f"{item['env']}={item['value']}")
        lines.append("")
    if resolved.get("command_runtime"):
        lines.append("# Agentic command runtime values inferred by config.command_runtime")
        for item in resolved["command_runtime"]:
            lines.append(f"{item['env']}={item['value']}")
        lines.append("")
    lines.append("# Translation runtime values inferred by central LLM timeout policy")
    lines.append(f"TRANSLATION_OLLAMA_TIMEOUT_SECONDS={llm_request_timeout}")
    lines.append("")
    return "\n".join(lines)


def _format_docker_resources_env(resolved: dict[str, Any]) -> str:
    lines = [
        *_generated_env_contract_header("docker_resources"),
        "",
        *_generated_env_contract_values("docker_resources"),
    ]
    for item in resolved.get("docker_resources", []):
        lines.append(f"{item['env']}={item['value']}")
    lines.append("")
    return "\n".join(lines)


def write_storage_env(resolved: dict[str, Any], output_path: Path) -> None:
    errors = validate_resolved(resolved)
    if errors:
        raise ConfigError("; ".join(errors))

    storage_paths = resolved["storage_paths"]
    mode = storage_paths.get("AI_LOCAL_STORAGE_MODE")
    required = {"AI_LOCAL_STORAGE_ROOT", "LLM_MODELS_DIR", "HF_CACHE_DIR", "AUDIO_TRANSCRIBE_DATA_DIR"}
    missing = sorted(required - set(storage_paths))
    if missing:
        raise ConfigError(
            f"storage mode {mode!r} does not provide complete env paths; missing: {', '.join(missing)}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_format_storage_env(resolved), encoding="utf-8")
    _ensure_storage_directories(storage_paths)
    _write_storage_warning(resolved)


def _ensure_storage_directories(storage_paths: dict[str, str]) -> None:
    for key in sorted(STORAGE_DIRECTORY_KEYS):
        raw = storage_paths.get(key)
        if not raw:
            continue
        try:
            Path(raw).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"could not create storage directory {key}={raw}: {exc}") from exc
    rag_data_dir = storage_paths.get("RAG_DATA_DIR")
    if rag_data_dir:
        for subdir in RAG_DATA_SUBDIRS:
            try:
                (Path(rag_data_dir).expanduser() / subdir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(f"could not create RAG data directory {rag_data_dir}/{subdir}: {exc}") from exc
    audio_data_dir = storage_paths.get("AUDIO_TRANSCRIBE_DATA_DIR")
    if audio_data_dir:
        for subdir in AUDIO_TRANSCRIBE_DATA_SUBDIRS:
            try:
                (Path(audio_data_dir).expanduser() / subdir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(f"could not create audio data directory {audio_data_dir}/{subdir}: {exc}") from exc


def write_llm_env(resolved: dict[str, Any], output_path: Path) -> None:
    errors = validate_resolved(resolved)
    if errors:
        raise ConfigError("; ".join(errors))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_format_llm_env(resolved), encoding="utf-8")


def write_services_env(resolved: dict[str, Any], output_path: Path) -> None:
    errors = validate_resolved(resolved)
    if errors:
        raise ConfigError("; ".join(errors))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_format_services_env(resolved), encoding="utf-8")


def write_docker_resources_env(resolved: dict[str, Any], output_path: Path) -> None:
    errors = validate_resolved(resolved)
    if errors:
        raise ConfigError("; ".join(errors))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_format_docker_resources_env(resolved), encoding="utf-8")


def write_ollama_host_config(resolved: dict[str, Any], output_dir: Path) -> None:
    errors = validate_resolved(resolved)
    if errors:
        raise ConfigError("; ".join(errors))
    ollama_host = resolved.get("ollama_host")
    if not isinstance(ollama_host, dict) or not ollama_host:
        raise ConfigError("ollama_host config is empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    dropin_path = output_dir / str(ollama_host.get("dropin_name", "90-ai-local.conf"))
    script_path = output_dir / "apply-ollama-systemd.sh"
    dropin_path.write_text(format_ollama_systemd_override(ollama_host), encoding="utf-8")
    script_path.write_text(format_ollama_apply_script(), encoding="utf-8")
    script_path.chmod(0o755)


def write_resource_governor_policy(resolved: dict[str, Any], output_path: Path) -> None:
    errors = validate_resolved(resolved)
    if errors:
        raise ConfigError("; ".join(errors))
    policy = resolved.get("resource_governor_policy")
    if not isinstance(policy, dict) or not policy:
        raise ConfigError("resource_governor_policy is empty")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(policy, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def write_operational_self_model(resolved: dict[str, Any], output_path: Path) -> None:
    model = resolved.get("operational_self_model")
    if not isinstance(model, dict) or not model:
        raise ConfigError("operational_self_model is empty")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(model, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _write_storage_warning(resolved: dict[str, Any]) -> None:
    storage_mode = resolved["storage_paths"].get("AI_LOCAL_STORAGE_MODE")
    logs_root = resolved["storage_paths"].get("AI_LOCAL_LOGS_ROOT") or str(ROOT / ".local" / "logs")
    warning_dir = Path(logs_root).expanduser() / "storage"
    warning_dir.mkdir(parents=True, exist_ok=True)
    warning_path = warning_dir / "storage-warning.txt"
    if storage_mode == "local_fallback":
        warning_path.write_text(
            "\n".join(
                [
                    "External SSD storage is configured but unavailable.",
                    "ai-local is running with local fallback storage.",
                    "When the SSD returns, run `make infra` or start the project normally to move local deltas to the SSD.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return
    if storage_mode == "external_missing":
        warning_path.write_text(
            "\n".join(
                [
                    "External SSD storage is configured but unavailable.",
                    "ai-local generated isolated local bind paths because local fallback is disabled.",
                    "Mount/share the SSD or set AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK=true to use .local/data as the normal fallback.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return
    if warning_path.exists():
        warning_path.unlink()


def _print_explain(resolved: dict[str, Any]) -> None:
    print("Resolved ai-local configuration explanation")
    print(f"contract_version = {resolved.get('contract_version')}")
    health = resolved.get("config_health") or {}
    if health:
        print(f"config_health = {health.get('status')}")
        print(f"  storage_mode = {health.get('storage_mode')}")
        stale_outputs = health.get("stale_outputs") or []
        if stale_outputs:
            print(f"  stale_outputs = {', '.join(stale_outputs)}")
    print("")
    for decision in resolved["decisions"]:
        print(f"decision_id = {decision['decision_id']}")
        print(f"{decision['field']} = {decision['value']}")
        print(f"  status = {decision['status']}")
        print(f"  confidence = {decision['confidence']}")
        print(f"  origin = {decision['origin']}")
        print(f"  inputs = {', '.join(decision['inputs'])}")
        print(f"  probes = {', '.join(decision['probes'])}")
        print(f"  reason = {decision['reason']}")
        if decision.get("formula"):
            print(f"  formula = {decision['formula']}")
        if decision.get("override"):
            print(f"  override = {decision['override']}")
        if decision.get("warning"):
            print(f"  warning = {decision['warning']}")
        print(f"  downstream_impact = {', '.join(decision['downstream_impact'])}")
        print("")
    print("ports")
    for port in resolved["ports"]:
        suffix = f" warning={port['warning']}" if port.get("warning") else ""
        print(f"  {port['service']} = {port['port']} ({port['origin']}){suffix}")
    print("")
    print("service_endpoints")
    for endpoint in resolved["service_endpoints"]:
        suffix = f" warning={endpoint['warning']}" if endpoint.get("warning") else ""
        print(
            f"  {endpoint['service']} = {endpoint['url']} "
            f"workers={endpoint['workers']} ({endpoint['origin']}){suffix}"
        )
    if resolved.get("rag_runtime"):
        print("")
        print("rag_runtime")
        for item in resolved["rag_runtime"]:
            print(f"  {item['env']} = {item['value']} ({item['origin']})")
            print(f"    reason = {item['reason']}")
            print(f"    formula = {item['formula']}")
            print(f"    override = {item['override']}")
    if resolved.get("symbiont_runtime"):
        print("")
        print("symbiont_runtime")
        for item in resolved["symbiont_runtime"]:
            print(f"  {item['env']} = {item['value']} ({item['origin']})")
            print(f"    reason = {item['reason']}")
            print(f"    formula = {item['formula']}")
            print(f"    override = {item['override']}")
    if resolved.get("command_runtime"):
        print("")
        print("command_runtime")
        for item in resolved["command_runtime"]:
            print(f"  {item['env']} = {item['value']} ({item['origin']})")
            print(f"    reason = {item['reason']}")
            print(f"    formula = {item['formula']}")
            print(f"    override = {item['override']}")
    if resolved.get("docker_resources"):
        print("")
        print("docker_resources")
        for item in resolved["docker_resources"]:
            print(f"  {item['env']} = {item['value']} ({item['origin']})")
            print(f"    reason = {item['reason']}")
            print(f"    formula = {item['formula']}")
            print(f"    override = {item['override']}")
    if resolved.get("ollama_host"):
        ollama_host = resolved["ollama_host"]
        print("")
        print("ollama_host")
        print(f"  service = {ollama_host['service']}")
        print(f"  dropin = {ollama_host['dropin_name']}")
        print(f"  gpu_enabled = {ollama_host['gpu_enabled']}")
        print(f"  override = {ollama_host['override_env']}")
        for key, value in ollama_host["env"].items():
            print(f"  env.{key} = {value}")
        for key, value in ollama_host["service_settings"].items():
            print(f"  service.{key} = {value}")
    rg_policy = resolved.get("resource_governor_policy") or {}
    if rg_policy:
        print("")
        print("resource_governor")
        print(f"  mode = {rg_policy.get('mode')}")
        print(f"  machine_profile = {rg_policy.get('machine_profile')}")
        print(f"  thin_but_capable = {rg_policy.get('thin_but_capable')}")
        limits = rg_policy.get("limits") or {}
        if limits:
            print(
                "  limits = "
                f"workers:{limits.get('max_workers')} "
                f"batch:{limits.get('embedding_batch')} "
                f"heavy_gpu:{limits.get('heavy_gpu_concurrency')}"
            )
        autotuning = rg_policy.get("autotuning") or {}
        if autotuning:
            print(f"  autotuning = {autotuning.get('status')}")
    self_model = resolved.get("operational_self_model") or {}
    if self_model:
        print("")
        print("operational_self_model")
        print(f"  status = {self_model.get('status')}")
        capacity = self_model.get("execution_capacity") or {}
        print(f"  foreground_interaction = {capacity.get('foreground_interaction')}")
        print(f"  background_storage = {capacity.get('background_storage')}")
        print(f"  heavy_gpu = {capacity.get('heavy_gpu')}")
    if resolved["warnings"]:
        print("")
        print("warnings")
        for warning in resolved["warnings"]:
            print(f"  - {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m config.resolver")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--print", action="store_true", dest="print_config")
    group.add_argument("--explain", action="store_true")
    group.add_argument("--health-report", action="store_true")
    group.add_argument("--self-model", action="store_true")
    group.add_argument("--validate", action="store_true")
    group.add_argument("--write-storage-env", nargs="?", const=str(ENV_STORAGE_PATH), metavar="PATH")
    group.add_argument("--write-llm-env", nargs="?", const=str(ENV_LLM_PATH), metavar="PATH")
    group.add_argument("--write-services-env", nargs="?", const=str(ENV_SERVICES_PATH), metavar="PATH")
    group.add_argument(
        "--write-docker-resources-env",
        nargs="?",
        const=str(ENV_DOCKER_RESOURCES_PATH),
        metavar="PATH",
    )
    group.add_argument(
        "--write-ollama-host-config",
        nargs="?",
        const=str(OLLAMA_HOST_CONFIG_DIR),
        metavar="DIR",
    )
    group.add_argument(
        "--write-resource-governor-policy",
        nargs="?",
        const=str(RESOURCE_GOVERNOR_POLICY_SNAPSHOT_PATH),
        metavar="PATH",
    )
    group.add_argument(
        "--write-operational-self-model",
        nargs="?",
        const=str(OPERATIONAL_SELF_MODEL_SNAPSHOT_PATH),
        metavar="PATH",
    )
    args = parser.parse_args(argv)

    try:
        resolved = resolve_config(Path(args.config))
        errors = validate_resolved(resolved)
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.validate:
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        for warning in resolved["warnings"]:
            print(f"WARNING: {warning}")
        print("OK: configuration is valid")
        return 0

    if args.self_model:
        print(json.dumps(resolved.get("operational_self_model") or {}, indent=2, sort_keys=True))
        return 0

    if args.write_storage_env:
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        try:
            output_path = Path(args.write_storage_env)
            write_storage_env(resolved, output_path)
        except ConfigError as exc:
            print(f"ERROR: {exc}")
            return 1
        for warning in resolved["warnings"]:
            print(f"WARNING: {warning}")
        print(f"Generated: {output_path}")
        return 0

    if args.write_llm_env:
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        try:
            output_path = Path(args.write_llm_env)
            write_llm_env(resolved, output_path)
        except ConfigError as exc:
            print(f"ERROR: {exc}")
            return 1
        for warning in resolved["warnings"]:
            print(f"WARNING: {warning}")
        print(f"Generated: {output_path}")
        return 0

    if args.write_services_env:
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        try:
            output_path = Path(args.write_services_env)
            write_services_env(resolved, output_path)
        except ConfigError as exc:
            print(f"ERROR: {exc}")
            return 1
        for warning in resolved["warnings"]:
            print(f"WARNING: {warning}")
        print(f"Generated: {output_path}")
        return 0

    if args.write_docker_resources_env:
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        try:
            output_path = Path(args.write_docker_resources_env)
            write_docker_resources_env(resolved, output_path)
        except ConfigError as exc:
            print(f"ERROR: {exc}")
            return 1
        for warning in resolved["warnings"]:
            print(f"WARNING: {warning}")
        print(f"Generated: {output_path}")
        return 0

    if args.write_ollama_host_config:
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        try:
            output_dir = Path(args.write_ollama_host_config)
            write_ollama_host_config(resolved, output_dir)
        except ConfigError as exc:
            print(f"ERROR: {exc}")
            return 1
        for warning in resolved["warnings"]:
            print(f"WARNING: {warning}")
        print(f"Generated: {output_dir}")
        return 0

    if args.write_resource_governor_policy:
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        try:
            output_path = Path(args.write_resource_governor_policy)
            write_resource_governor_policy(resolved, output_path)
        except ConfigError as exc:
            print(f"ERROR: {exc}")
            return 1
        for warning in resolved["warnings"]:
            print(f"WARNING: {warning}")
        print(f"Generated: {output_path}")
        return 0

    if args.write_operational_self_model:
        try:
            output_path = Path(args.write_operational_self_model)
            write_operational_self_model(resolved, output_path)
        except ConfigError as exc:
            print(f"ERROR: {exc}")
            return 1
        for warning in resolved["warnings"]:
            print(f"WARNING: {warning}")
        print(f"Generated: {output_path}")
        return 0

    if args.health_report:
        print(json.dumps(resolved["config_health"], indent=2, ensure_ascii=False))
        return 1 if errors else 0

    if args.explain:
        _print_explain(resolved)
        return 1 if errors else 0

    print(json.dumps(resolved, indent=2, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
