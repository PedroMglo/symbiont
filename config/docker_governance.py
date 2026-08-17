"""Typed Docker governance taxonomies derived by Config Center."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RESOURCE_PROFILES: dict[str, dict[str, object]] = {
    "tiny": {"description": "Fixed low-footprint sidecar or proxy.", "kind": "service", "gpu_required": False},
    "small": {"description": "Lightweight API, agent, feature, or stateful service.", "kind": "service", "gpu_required": False},
    "medium": {"description": "General runtime service with moderate CPU and memory demand.", "kind": "service", "gpu_required": False},
    "large": {"description": "Heavy CPU, model, extraction, or sandbox service.", "kind": "service", "gpu_required": False},
    "gpu": {"description": "GPU-backed model-serving service.", "kind": "service", "gpu_required": True},
    "job": {"description": "Bounded one-shot initialization or maintenance job.", "kind": "job", "gpu_required": False},
}

SERVICE_CLASSES: dict[str, dict[str, object]] = {
    "gateway-public": {"host_ports": "allowed", "default_bind": "127.0.0.1", "healthcheck_required": True},
    "admin-local": {"host_ports": "allowed-with-reason", "default_bind": "127.0.0.1", "healthcheck_required": True},
    "llm-local": {"host_ports": "allowed-with-reason", "default_bind": "127.0.0.1", "healthcheck_required": True},
    "local-ui": {"host_ports": "allowed-with-reason", "default_bind": "127.0.0.1", "healthcheck_required": True},
    "internal": {"host_ports": "forbidden", "healthcheck_required": True},
    "debug-only": {"host_ports": "debug-overlay-only", "default_bind": "127.0.0.1", "healthcheck_required": True},
    "worker": {"host_ports": "forbidden", "healthcheck_required": False, "restart_policy": "no"},
    "host-native": {"host_ports": "external-to-compose", "healthcheck_required": False},
}


def generated_profile_contract(catalog: Mapping[str, Any], compose_projects: Mapping[str, Any]) -> dict[str, Any]:
    services = catalog.get("services")
    generated_env_files = catalog.get("generated_env_files")
    projects = compose_projects.get("projects")
    if not isinstance(services, Mapping) or not isinstance(generated_env_files, Mapping):
        raise ValueError("Docker service catalog is incomplete")
    project = projects.get("ai-local") if isinstance(projects, Mapping) else None
    if not isinstance(project, Mapping):
        raise ValueError("Docker project ai-local is unavailable")
    profile_names: set[str] = set()
    for key in ("required_runtime_profiles", "default_runtime_profiles", "mandatory_build_profiles", "lifecycle_only_profiles", "operator_only_runtime_profiles"):
        profile_names.update(str(item) for item in project.get(key, []) or [])
    for policy in services.values():
        if isinstance(policy, Mapping):
            profile_names.update(str(item) for item in policy.get("profiles_expected", []) or [])
    profiles: dict[str, dict[str, object]] = {}
    for profile in sorted(profile_names):
        members = [policy for policy in services.values() if isinstance(policy, Mapping) and profile in {str(item) for item in policy.get("profiles_expected", []) or []}]
        owners = {str(item.get("owner")) for item in members if item.get("owner")}
        env_keys = {str(env) for item in members for env in item.get("generated_env", []) or [] if str(env) in generated_env_files}
        generated_paths = {str(generated_env_files[key]) for key in env_keys}
        ordered_env = [str(path) for path in project.get("env_files", []) or [] if str(path) in generated_paths]
        profiles[profile] = {
            "owner": next(iter(owners)) if len(owners) == 1 else "infra/docker",
            "purpose": f"Generated operational contract for the {profile} Compose profile.",
            "health": "Health requirements are derived from the member service catalog records.",
            "secrets": "Secret requirements are the union of member service file-first contracts.",
            "resources": "Concrete CPU, memory and PID values come from Config Center capacity planning.",
            "generated_env": ordered_env,
        }
        if not profiles[profile]["generated_env"]:
            profiles[profile]["generated_env"] = list(project.get("env_files", []) or [])
    return {"meta": {"source": "config.docker_governance.generated_profile_contract"}, "profiles": profiles}
