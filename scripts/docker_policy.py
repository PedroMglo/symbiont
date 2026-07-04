#!/usr/bin/env python3
"""Docker governance inventory and policy checks for ai-local.

Observed truth comes from `docker compose config --format json`. Intended policy
comes from config/docker/service-catalog.toml.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ required by repo
    print("ERROR: Python 3.11+ with tomllib is required", file=sys.stderr)
    raise


ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "config" / "docker" / "service-catalog.toml"
VOLUMES_CATALOG_PATH = ROOT / "config" / "docker" / "volumes-catalog.toml"
COMPOSE_PROJECTS_PATH = ROOT / "config" / "docker" / "compose-projects.toml"
DEBUG_COMPOSE = ROOT / "infra" / "docker" / "compose" / "debug.yml"
GENERATED_DIR = ROOT / "docs" / "generated"
INVENTORY_JSON = GENERATED_DIR / "docker-inventory.json"
INVENTORY_MD = GENERATED_DIR / "docker-inventory.md"
OPTIMIZATION_JSON = GENERATED_DIR / "docker-optimization.json"
OPTIMIZATION_MD = GENERATED_DIR / "docker-optimization.md"

LOCAL_BIND_VALUES = {"127.0.0.1", "localhost", "::1"}
STRICT_LABELS = (
    "ai.local.service",
    "ai.local.service_class",
    "ai.local.exposure_class",
    "ai.local.profile",
    "ai.local.owner",
)
CURRENT_DOCKERFILES = (
    ROOT / "infra" / "docker" / "images" / "command-sandbox" / "Dockerfile",
    *sorted((ROOT / "infra" / "docker" / "images").glob("**/*Dockerfile*")),
)
SENSITIVE_OUTPUT_MARKERS = ("secret", "password", "token", "api_key", "auth")
SENSITIVE_TOKEN_RE = re.compile(r"(?i)\b[A-Z0-9_]*(?:SECRET|PASSWORD|TOKEN|API_KEY|AUTH)[A-Z0-9_]*\b")
DEFAULT_SELECTED_PROFILES = ("core", "storage")
SECRET_PROFILES = {"none", "internal", "gateway", "rag", "llm", "audio", "observability"}
RESOURCE_PROFILES = {"tiny", "small", "medium", "large", "gpu", "job"}
HOST_HOME_ACCESS_MODES = {"none", "read_only", "storage_write"}
PUBLIC_RULE_IDS = {
    "catalog.required_fields": "catalog.required_fields",
    "catalog.class": "catalog.class",
    "catalog.profile_unknown": "catalog.profile_unknown",
    "catalog.generated_env": "catalog.generated_env",
    "catalog.secret_profile": "catalog.secret_profile",
    "catalog.resource_profile": "catalog.resource_profile",
    "catalog.host_port_reason": "catalog.host_port_reason",
    "catalog.exception_reason": "catalog.exception_reason",
    "catalog.latest_reason": "catalog.latest_reason",
    "catalog.host_home_access": "catalog.host_home_access",
    "catalog.host_home_access_reason": "catalog.host_home_access_reason",
    "catalog.missing": "catalog.missing",
    "profiles.mismatch": "profiles.mismatch",
    "secrets.tracked": "private_files.tracked",
    "secrets.mismatch": "secrets.mismatch",
    "ports.internal_published": "ports.internal_published",
    "ports.uncataloged": "ports.uncataloged",
    "ports.bind": "ports.bind",
    "healthcheck.missing": "healthcheck.missing",
    "security.privileged": "container.privileged",
    "security.host_network": "container.host_network",
    "security.docker_socket": "container.docker_socket",
    "security.capabilities": "container.capabilities",
    "storage.direct_credentials": "storage.external_env",
    "storage.direct_secret": "storage.external_mount",
    "storage.rw_agent_feature_persistent": "storage.rw_agent_feature_persistent",
    "storage.rw_managed_volume": "storage.rw_managed_volume",
    "storage.rw_managed_root": "storage.rw_managed_root",
    "host_mount.broad_home": "host_mount.broad_home",
    "host_mount.broad_home_mode": "host_mount.broad_home_mode",
    "host_mount.broad_home_owner": "host_mount.broad_home_owner",
    "labels.missing": "labels.missing",
    "image.latest": "image.latest",
    "resources.missing": "resources.missing",
    "compose.project_invalid": "compose.project_invalid",
}
PUBLIC_RULE_MESSAGES = {
    "catalog.required_fields": "catalog entry is missing required governance fields",
    "catalog.class": "catalog entry references an unknown service class",
    "catalog.profile_unknown": "service references an undocumented compose profile",
    "catalog.generated_env": "service references an unknown generated env surface",
    "catalog.secret_profile": "service references an unknown secret profile",
    "catalog.resource_profile": "service references an unknown resource profile",
    "catalog.host_port_reason": "host ports require a catalog reason",
    "catalog.exception_reason": "approved exceptions require an exception reason",
    "catalog.latest_reason": "latest image exceptions require a reason",
    "catalog.host_home_access": "service declares an unknown host HOME access mode",
    "catalog.host_home_access_reason": "host HOME access requires a catalog reason",
    "catalog.missing": "service is present in compose but missing from the catalog",
    "profiles.mismatch": "compose profiles differ from the catalog",
    "private_files.tracked": "private runtime files must not be tracked",
    "secrets.mismatch": "compose secrets differ from the catalog",
    "ports.internal_published": "internal or worker service publishes host ports",
    "ports.uncataloged": "host port is not cataloged",
    "ports.bind": "host port does not bind to an approved interface",
    "healthcheck.missing": "compose healthcheck is required by the catalog",
    "container.privileged": "privileged containers are not allowed",
    "container.host_network": "host network mode is not allowed",
    "container.docker_socket": "direct docker socket mounts are not allowed",
    "container.capabilities": "service requests blocked Linux capabilities",
    "storage.external_env": "external storage environment values must stay in storage_guardian",
    "storage.external_mount": "external storage private mounts must stay in storage_guardian",
    "storage.rw_agent_feature_persistent": "agents and features must not mount persistent storage read-write",
    "storage.rw_managed_volume": "read-write mount overlaps a storage_guardian managed volume",
    "storage.rw_managed_root": "read-write mount overlaps a storage_guardian managed root",
    "host_mount.broad_home": "broad HOME mounts require explicit catalog approval",
    "host_mount.broad_home_mode": "broad HOME mount mode violates catalog approval",
    "host_mount.broad_home_owner": "read-write HOME access is restricted to storage_guardian",
    "labels.missing": "governance labels are missing",
    "image.latest": "image tag uses latest",
    "resources.missing": "resource limits are missing",
    "compose.project_invalid": "compose project config is invalid",
}


@dataclass
class PolicyResult:
    violations: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    def add_violation(self, service: str, rule: str, message: str, **extra: Any) -> None:
        self.violations.append({"service": service, "rule": rule, "message": message, **extra})

    def add_warning(self, service: str, rule: str, message: str, **extra: Any) -> None:
        self.warnings.append({"service": service, "rule": rule, "message": message, **extra})


def _public_payload(value: Any) -> Any:
    if isinstance(value, dict):
        public: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in SENSITIVE_OUTPUT_MARKERS):
                public[key] = "<redacted>"
            else:
                public[key] = _public_payload(item)
        return public
    if isinstance(value, list):
        return [_public_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_public_payload(item) for item in value)
    if isinstance(value, str):
        return _public_text(value)
    return value


def _public_text(value: str) -> str:
    redacted = SENSITIVE_TOKEN_RE.sub("<sensitive-field>", value)
    return redacted.replace("infra/docker/secrets", "infra/docker/<sensitive-dir>")


def _public_json(value: Any) -> str:
    return json.dumps(_public_payload(value), indent=2, sort_keys=True)


def _public_label(value: Any, *, fallback: str = "-") -> str:
    text = _public_text(str(value or "").strip())
    if not text:
        return fallback
    if SENSITIVE_TOKEN_RE.search(text):
        return "<sensitive-field>"
    lowered = text.lower()
    if any(marker in lowered for marker in SENSITIVE_OUTPUT_MARKERS):
        return "<redacted>"
    return text[:240]


def _public_labels(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return [_public_label(value) for value in values]


def _public_port(port: dict[str, Any]) -> dict[str, Any]:
    return {
        "host_ip": _public_label(port.get("host_ip", ""), fallback=""),
        "published": _as_int(port.get("published")),
        "target": _as_int(port.get("target")),
        "protocol": _public_label(port.get("protocol", "tcp")),
    }


def _public_policy_item(item: dict[str, Any]) -> dict[str, str]:
    rule = PUBLIC_RULE_IDS.get(str(item.get("rule", "")), "policy.review")
    return {
        "service": _public_label(item.get("service", "unknown")),
        "rule": rule,
        "message": PUBLIC_RULE_MESSAGES.get(rule, "policy condition requires review"),
    }


def _public_compose_project(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _public_label(item.get("name", "unknown")),
        "role": _public_label(item.get("role", "service-owned")),
        "status": _public_label(item.get("status", "unknown")),
        "workdir": _public_label(item.get("workdir", "")),
        "files": _public_labels(item.get("files") or []),
        "profiles": _public_labels(item.get("profiles") or []),
        "manifest": _public_label(item.get("manifest", "")),
    }


def _public_service(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "service": _public_label(item.get("service", "unknown")),
        "class": _public_label(item.get("class", "uncataloged")),
        "owner": _public_label(item.get("owner", "")),
        "profiles": _public_labels(item.get("profiles") or []),
        "generated_env": _public_labels(item.get("generated_env") or []),
        "secret_profile": _public_label(item.get("secret_profile", "")),
        "resource_profile": _public_label(item.get("resource_profile", "")),
        "container_name": _public_label(item.get("container_name", "")),
        "host_ports": [_public_port(port) for port in item.get("host_ports", []) if isinstance(port, dict)],
        "expose": _public_labels(item.get("expose") or []),
        "healthcheck": bool(item.get("healthcheck")),
        "image": _public_label(item.get("image", "")),
        "restart": _public_label(item.get("restart", "")),
        "mem_limit": _public_label(item.get("mem_limit", "")),
        "cpus": _public_label(item.get("cpus", "")),
        "pids": _public_label(item.get("pids", "")),
        "networks": _public_labels(item.get("networks") or []),
        "secrets": _public_labels(item.get("secrets") or []),
        "stateful": bool(item.get("stateful")),
        "backup_required": bool(item.get("backup_required")),
        "policy_reason": _public_label(item.get("policy_reason", "")),
    }


def _public_inventory_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": _public_label(payload.get("generated_at", "")),
        "policy_mode": _public_label(payload.get("policy_mode", "")),
        "profiles": _public_labels(payload.get("profiles") or []),
        "compose_projects": [
            _public_compose_project(item)
            for item in payload.get("compose_projects", [])
            if isinstance(item, dict)
        ],
        "services": [_public_service(item) for item in payload.get("services", []) if isinstance(item, dict)],
        "violations": [_public_policy_item(item) for item in payload.get("violations", []) if isinstance(item, dict)],
        "warnings": [_public_policy_item(item) for item in payload.get("warnings", []) if isinstance(item, dict)],
    }


def _public_optimization_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": _public_label(payload.get("generated_at", "")),
        "dockerfiles": [
            {
                "dockerfile": _public_label(item.get("dockerfile", "")),
                "from": _public_labels(item.get("from") or []),
                "warnings": _public_labels(item.get("warnings") or []),
            }
            for item in payload.get("dockerfiles", [])
            if isinstance(item, dict)
        ],
        "compose_warnings": [
            _public_policy_item(item)
            for item in payload.get("compose_warnings", [])
            if isinstance(item, dict)
        ],
        "dockerignore_warnings": _public_labels(payload.get("dockerignore_warnings") or []),
    }


def _inventory_artifact_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact": "docker-inventory",
        "status": "generated",
        "violation_count": len(payload.get("violations", [])),
        "warning_count": len(payload.get("warnings", [])),
        "service_count": len(payload.get("services", [])),
    }


def _optimization_artifact_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact": "docker-optimization",
        "status": "generated",
        "dockerfile_count": len(payload.get("dockerfiles", [])),
        "warning_count": len(payload.get("compose_warnings", [])) + len(payload.get("dockerignore_warnings", [])),
    }


def _summary_doc(summary: dict[str, Any]) -> str:
    lines = [
        f"# {summary['artifact']}",
        "",
        f"Status: `{summary['status']}`",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    for key, value in summary.items():
        if key in {"artifact", "status"}:
            continue
        lines.append(f"| `{key}` | `{value}` |")
    lines.append("")
    return "\n".join(lines)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_catalog() -> dict[str, Any]:
    catalog = _load_toml(CATALOG_PATH)
    services = catalog.get("services", {})
    if not isinstance(services, dict) or not services:
        raise SystemExit(f"ERROR: no [services.*] entries found in {CATALOG_PATH}")
    return catalog


def load_compose_projects() -> list[dict[str, Any]]:
    raw = _load_toml(COMPOSE_PROJECTS_PATH)
    projects = raw.get("projects", {})
    if not isinstance(projects, dict) or not projects:
        raise SystemExit(f"ERROR: no [projects.*] entries found in {COMPOSE_PROJECTS_PATH}")

    items: list[dict[str, Any]] = []
    for name, project in sorted(projects.items(), key=lambda item: int(item[1].get("order", 1000))):
        workdir = ROOT / str(project.get("workdir", "."))
        files = tuple(str(item) for item in (project.get("files") or ["compose.yml"]))
        profiles = tuple(str(item) for item in (project.get("profiles") or []))
        env_files = tuple(str(item) for item in (project.get("env_files") or []))
        secrets_dir = str(project.get("secrets_dir") or "")
        items.append(
            {
                "name": str(name),
                "role": str(project.get("role") or "service-owned"),
                "workdir": workdir,
                "files": files,
                "profiles": profiles,
                "use_catalog_profiles": bool(project.get("use_catalog_profiles", False)),
                "env_files": env_files,
                "secrets_dir": secrets_dir,
            }
        )
    return items


def _env_for_compose() -> dict[str, str]:
    env = os.environ.copy()
    defaults = {
        "AI_LOCAL_BIND": "127.0.0.1",
        "LLM_MODELS_DIR": str(ROOT / ".local" / "data" / "models" / "gguf"),
        "HF_CACHE_DIR": str(ROOT / ".local" / "data" / "cache" / "hf"),
        "AI_STORAGE_HOST_BIND_ROOT": str(ROOT / ".local"),
        "AI_STORAGE_CONTAINER_BIND_ROOT": "/workspace/ai-local/.local",
        "STORAGE_GUARDIAN_DATA_DIR": str(ROOT / ".local" / "data" / "storage_guardian"),
        "ORC_SECRETS_DIR": str(ROOT / "infra" / "docker" / "secrets"),
        "ORC_PORT_SYMBIONT": "8586",
        "ORC_SYMBIONT_PORT": "8586",
        "ORC_PORT_RAG": "8486",
        "LANGFUSE_DB_PASSWORD": "policy-placeholder",
        "LANGFUSE_NEXTAUTH_SECRET": "policy-placeholder",
        "LANGFUSE_SALT": "policy-placeholder",
    }
    for key, value in defaults.items():
        env.setdefault(key, value)
    return env


def _catalog_profiles(catalog: dict[str, Any]) -> list[str]:
    profiles: set[str] = set()
    for service in catalog.get("services", {}).values():
        for profile in service.get("profiles_expected", []):
            profiles.add(str(profile))
    return sorted(profiles)


def _selected_profiles(raw: str | None = None) -> set[str]:
    value = raw if raw is not None else os.environ.get("AI_COMPOSE_PROFILES")
    if not value:
        return set(DEFAULT_SELECTED_PROFILES)
    selected = {item.strip() for item in value.split(",") if item.strip()}
    return selected or set(DEFAULT_SELECTED_PROFILES)


def _service_selected(policy: dict[str, Any], selected_profiles: set[str]) -> bool:
    expected = {str(item) for item in policy.get("profiles_expected", [])}
    return bool(expected & selected_profiles)


def _compose_secret_files(compose: dict[str, Any]) -> dict[str, dict[str, str]]:
    files: dict[str, dict[str, str]] = {}
    for source, metadata in (compose.get("secrets") or {}).items():
        if not isinstance(metadata, dict):
            continue
        raw_path = str(metadata.get("file") or "")
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = ROOT / path
        files[str(source)] = {"secret": path.name, "path": _path_for_report(path)}
    return files


def required_secret_files(
    catalog: dict[str, Any],
    selected_profiles: set[str],
    *,
    compose: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    observed = compose if compose is not None else compose_config(catalog)
    source_files = _compose_secret_files(observed)
    by_secret: dict[str, dict[str, Any]] = {}
    for service_name, service in sorted((observed.get("services") or {}).items()):
        policy = catalog.get("services", {}).get(service_name)
        if not isinstance(policy, dict) or not _service_selected(policy, selected_profiles):
            continue
        profiles = {str(item) for item in policy.get("profiles_expected", []) or []}
        for item in service.get("secrets", []) or []:
            source = str(item.get("source", item) if isinstance(item, dict) else item)
            file_info = source_files.get(source, {"secret": source, "path": f"infra/docker/secrets/{source}"})
            secret = str(file_info["secret"])
            entry = by_secret.setdefault(
                secret,
                {"secret": secret, "path": str(file_info["path"]), "profiles": set(), "sources": set()},
            )
            entry["profiles"].update(profiles)
            entry["sources"].add(source)
    return [
        {
            "secret": secret,
            "path": str(item["path"]),
            "profiles": sorted(item["profiles"]),
            "sources": sorted(item["sources"]),
        }
        for secret, item in sorted(by_secret.items())
    ]


def selected_host_ports(catalog: dict[str, Any], selected_profiles: set[str]) -> list[dict[str, Any]]:
    defaults = catalog.get("defaults", {})
    ports: list[dict[str, Any]] = []
    for service, policy in sorted(catalog.get("services", {}).items()):
        if not _service_selected(policy, selected_profiles):
            continue
        for port in policy.get("host_ports_allowed", []) or []:
            ports.append(
                {
                    "service": str(service),
                    "port": int(port),
                    "bind": str(policy.get("bind_required") or defaults.get("bind_required") or ""),
                    "profiles": [str(item) for item in policy.get("profiles_expected", [])],
                }
            )
    return ports


def _compose_cmd(catalog: dict[str, Any], *, debug: bool = False) -> list[str]:
    context = os.environ.get("AI_LOCAL_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or "default"
    cmd = ["docker", "--context", context, "compose"]
    if debug:
        cmd.extend(["-f", "compose.yml", "-f", str(DEBUG_COMPOSE.relative_to(ROOT))])
    for env_file in (".env.storage.generated", ".env.llm.generated", ".env.services.generated", "infra/docker/.env.observability"):
        if (ROOT / env_file).exists():
            cmd.extend(["--env-file", env_file])
    for profile in _catalog_profiles(catalog):
        cmd.extend(["--profile", profile])
    cmd.extend(["config", "--format", "json"])
    return cmd


def _compose_project_env(project: dict[str, Any]) -> dict[str, str]:
    env = _env_for_compose()
    workdir = Path(project["workdir"]).resolve()
    secrets_dir = str(project.get("secrets_dir") or "")
    if secrets_dir:
        secrets_path = Path(secrets_dir)
        if not secrets_path.is_absolute():
            secrets_path = ROOT / secrets_path
        env["ORC_SECRETS_DIR"] = str(secrets_path.resolve(strict=False))
    elif workdir != ROOT:
        env["ORC_SECRETS_DIR"] = str(workdir / "infra" / "docker" / "secrets")
    return env


def _compose_project_cmd(project: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    context = os.environ.get("AI_LOCAL_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or "default"
    cmd = ["docker", "--context", context, "compose"]
    workdir = Path(project["workdir"])
    for compose_file in project["files"]:
        path = (workdir / str(compose_file)).resolve()
        cmd.extend(["-f", str(path)])
    for env_file in project.get("env_files", ()):
        path = (ROOT / str(env_file)).resolve()
        if path.exists():
            cmd.extend(["--env-file", str(path)])
    profiles = project.get("profiles") or (tuple(_catalog_profiles(catalog)) if project.get("use_catalog_profiles") else ())
    for profile in profiles:
        cmd.extend(["--profile", str(profile)])
    cmd.extend(["config", "--quiet"])
    return cmd


def compose_project_checks(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for project in load_compose_projects():
        workdir = Path(project["workdir"]).resolve()
        cmd = _compose_project_cmd(project, catalog)
        if not workdir.exists():
            checks.append(
                {
                    "name": project["name"],
                    "role": project["role"],
                    "status": "fail",
                    "workdir": _path_for_report(workdir),
                    "files": list(project["files"]),
                    "profiles": list(project.get("profiles") or (_catalog_profiles(catalog) if project.get("use_catalog_profiles") else [])),
                    "manifest": _path_for_report(COMPOSE_PROJECTS_PATH),
                    "error": f"missing compose project directory: {workdir}",
                }
            )
            continue
        result = subprocess.run(
            cmd,
            cwd=workdir,
            env=_compose_project_env(project),
            capture_output=True,
            text=True,
            timeout=60,
        )
        checks.append(
            {
                "name": project["name"],
                "role": project["role"],
                "status": "pass" if result.returncode == 0 else "fail",
                "workdir": _path_for_report(workdir),
                "files": list(project["files"]),
                "profiles": list(project.get("profiles") or (_catalog_profiles(catalog) if project.get("use_catalog_profiles") else [])),
                "manifest": _path_for_report(COMPOSE_PROJECTS_PATH),
                "command": " ".join(cmd),
                "error": result.stderr.strip() or result.stdout.strip(),
            }
        )
    return checks


def _docker_context() -> str:
    return os.environ.get("AI_LOCAL_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or "default"


def compose_config(catalog: dict[str, Any], *, debug: bool = False) -> dict[str, Any]:
    cmd = _compose_cmd(catalog, debug=debug)
    result = subprocess.run(cmd, cwd=ROOT, env=_env_for_compose(), capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        target = "debug overlay" if debug else "base compose"
        raise SystemExit(
            "ERROR: docker compose config failed for "
            f"{target}.\nCommand: {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "ERROR: Docker Compose config JSON unavailable or invalid.\n"
            f"Run: docker --context {_docker_context()} compose version\n"
            f"Expected: docker --context {_docker_context()} compose config --format json\n"
            f"Parser error: {exc}"
        ) from exc


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _parse_string_port(value: str) -> dict[str, Any]:
    parts = value.split(":")
    host_ip = ""
    published = None
    target = None
    if len(parts) == 3:
        host_ip, published, target = parts
    elif len(parts) == 2:
        published, target = parts
    elif len(parts) == 1:
        target = parts[0]
    return {"host_ip": host_ip, "published": _as_int(published), "target": _as_int(target), "raw": value}


def _ports(service: dict[str, Any]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for item in service.get("ports", []) or []:
        if isinstance(item, str):
            parsed.append(_parse_string_port(item))
            continue
        if not isinstance(item, dict):
            continue
        published = _as_int(item.get("published") or item.get("Published"))
        target = _as_int(item.get("target") or item.get("Target"))
        parsed.append(
            {
                "host_ip": str(item.get("host_ip") or item.get("HostIp") or ""),
                "published": published,
                "target": target,
                "protocol": item.get("protocol") or item.get("Protocol") or "tcp",
                "raw": item,
            }
        )
    return parsed


def _labels(service: dict[str, Any]) -> dict[str, str]:
    labels = service.get("labels") or {}
    if isinstance(labels, dict):
        return {str(k): str(v) for k, v in labels.items()}
    parsed: dict[str, str] = {}
    if isinstance(labels, list):
        for label in labels:
            key, _, value = str(label).partition("=")
            parsed[key] = value
    return parsed


def _has_docker_socket_mount(service: dict[str, Any]) -> bool:
    for volume in service.get("volumes", []) or []:
        raw = json.dumps(volume, sort_keys=True) if isinstance(volume, dict) else str(volume)
        if "docker.sock" in raw:
            return True
    return False


def _service_volumes(service: dict[str, Any]) -> list[dict[str, Any]]:
    volumes: list[dict[str, Any]] = []
    for volume in service.get("volumes", []) or []:
        if isinstance(volume, str):
            parts = volume.split(":")
            source = parts[0] if len(parts) >= 2 else ""
            target = parts[1] if len(parts) >= 2 else parts[0]
            mode = parts[2] if len(parts) >= 3 else "rw"
            volumes.append({"type": "bind", "source": source, "target": target, "mode": mode, "read_only": "ro" in mode.split(",")})
            continue
        if not isinstance(volume, dict):
            continue
        mode = str(volume.get("mode") or "")
        read_only = bool(volume.get("read_only") or volume.get("readOnly") or mode == "ro")
        volumes.append(
            {
                "type": str(volume.get("type") or "volume"),
                "source": str(volume.get("source") or volume.get("Source") or ""),
                "target": str(volume.get("target") or volume.get("Target") or ""),
                "mode": mode or ("ro" if read_only else "rw"),
                "read_only": read_only,
            }
        )
    return volumes


def _volume_is_rw(volume: dict[str, Any]) -> bool:
    if volume.get("read_only"):
        return False
    mode = str(volume.get("mode") or "")
    if mode and "ro" in {part.strip() for part in mode.split(",")}:
        return False
    return True


def _managed_storage_policy_roots(catalog: dict[str, Any]) -> tuple[set[Path], set[str]]:
    roots: set[Path] = set()
    named_volumes: set[str] = set()

    storage_policy = catalog.get("storage_guardian", {})
    if isinstance(storage_policy, dict):
        for raw in storage_policy.get("exclusive_write_paths", []) or []:
            path = Path(str(raw))
            roots.add((ROOT / path if not path.is_absolute() else path).resolve(strict=False))
        for raw in storage_policy.get("exclusive_write_volumes", []) or []:
            named_volumes.add(str(raw))

    if VOLUMES_CATALOG_PATH.exists():
        volumes_catalog = _load_toml(VOLUMES_CATALOG_PATH)
        env = _env_for_compose()
        for name, policy in (volumes_catalog.get("volumes") or {}).items():
            if policy.get("exclusive_writer") != "storage_guardian":
                continue
            if policy.get("compose_volume"):
                named_volumes.add(str(policy["compose_volume"]))
            env_key = policy.get("env_path")
            if env_key:
                raw = env.get(str(env_key)) or os.environ.get(str(env_key))
                if raw:
                    path = Path(str(raw))
                    roots.add((ROOT / path if not path.is_absolute() else path).resolve(strict=False))
            if policy.get("path"):
                path = Path(str(policy["path"]))
                roots.add((ROOT / path if not path.is_absolute() else path).resolve(strict=False))
    return roots, named_volumes


def _path_from_volume_source(source: str) -> Path | None:
    if not source or source.startswith("${"):
        return None
    path = Path(source)
    if not path.is_absolute() and (source.startswith(".") or "/" in source):
        path = ROOT / path
    if not path.is_absolute():
        return None
    return path.resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _agent_feature_storage_subject(name: str, service: dict[str, Any], policy: dict[str, Any]) -> bool:
    if name == "storage_guardian":
        return False
    labels = _labels(service)
    component = labels.get("ai.local.component", "")
    if component in {"agent", "feature"}:
        return True
    owner = str(policy.get("owner") or "")
    if owner in {"agents", "features"}:
        return True
    return name in {"audio-transcribe", "audio-streaming"}


def _agent_feature_rw_exception(volume: dict[str, Any], policy: dict[str, Any]) -> bool:
    target = str(volume.get("target") or "").rstrip("/")
    built_in_targets = {
        "/run/ai-local-tls",
        "/models/huggingface",
    }
    if target in built_in_targets:
        return True
    allowed_targets = {str(item).rstrip("/") for item in policy.get("rw_persistent_mount_targets_allowed", []) or []}
    return target in allowed_targets


def _environment_mapping(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return {str(key): str(value) for key, value in env.items()}
    parsed: dict[str, str] = {}
    if isinstance(env, list):
        for item in env:
            key, _, value = str(item).partition("=")
            parsed[key] = value
    return parsed


def _path_for_report(path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _normalized_build(build: Any) -> Any:
    if not isinstance(build, dict):
        return build

    normalized = dict(build)
    context_raw = build.get("context")
    dockerfile_raw = build.get("dockerfile")

    context_path: Path | None = None
    if context_raw:
        context_path = Path(str(context_raw))
        if not context_path.is_absolute():
            context_path = ROOT / context_path
        normalized["context"] = _path_for_report(context_path)

    if dockerfile_raw:
        dockerfile_path = Path(str(dockerfile_raw))
        if not dockerfile_path.is_absolute() and context_path is not None:
            dockerfile_path = context_path / dockerfile_path
        normalized["dockerfile"] = _path_for_report(dockerfile_path)

    return normalized


def observed_inventory(compose: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    catalog_services = catalog.get("services", {})
    items: list[dict[str, Any]] = []
    for name, service in sorted((compose.get("services") or {}).items()):
        policy = catalog_services.get(name, {})
        ports = _ports(service)
        deploy = service.get("deploy") or {}
        resources = deploy.get("resources") or {}
        limits = resources.get("limits") or {}
        items.append(
            {
                "service": name,
                "class": policy.get("class", "uncataloged"),
                "owner": policy.get("owner", ""),
                "profiles": service.get("profiles", []),
                "generated_env": policy.get("generated_env", []),
                "secret_profile": policy.get("secret_profile", ""),
                "resource_profile": policy.get("resource_profile", ""),
                "container_name": service.get("container_name", ""),
                "host_ports": [port for port in ports if port.get("published")],
                "expose": service.get("expose", []),
                "healthcheck": bool(service.get("healthcheck")),
                "image": service.get("image", ""),
                "build": _normalized_build(service.get("build")),
                "labels": _labels(service),
                "restart": service.get("restart", ""),
                "mem_limit": service.get("mem_limit") or limits.get("memory"),
                "cpus": limits.get("cpus") or service.get("cpus"),
                "pids": limits.get("pids") or service.get("pids_limit"),
                "networks": sorted((service.get("networks") or {}).keys()) if isinstance(service.get("networks"), dict) else service.get("networks", []),
                "secrets": [s.get("source", s) if isinstance(s, dict) else s for s in service.get("secrets", []) or []],
                "stateful": bool(policy.get("stateful", False)),
                "backup_required": bool(policy.get("backup_required", False)),
                "policy_reason": policy.get("reason", ""),
            }
        )
    return items


def _allowed_ports(policy: dict[str, Any], *, include_debug: bool = False) -> set[int]:
    ports = set(int(p) for p in policy.get("host_ports_allowed", []) or [])
    if include_debug:
        ports.update(int(p) for p in policy.get("debug_ports_allowed", []) or [])
    return ports


def _git_tracked(paths: list[str]) -> list[str]:
    result = subprocess.run(["git", "ls-files", *paths], cwd=ROOT, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def validate_catalog(catalog: dict[str, Any], result: PolicyResult) -> None:
    service_classes = set(_load_toml(ROOT / "config" / "docker" / "governance" / "service-classes.toml").get("classes", {}))
    generated_env_files = set((catalog.get("generated_env_files") or {}).keys())
    documented_profiles = set(_catalog_profiles(catalog))
    required = {
        "class",
        "owner",
        "risk",
        "reason",
        "review_after",
        "profiles_expected",
        "host_ports_allowed",
        "compose_healthcheck_required",
        "generated_env",
        "secrets_required",
        "secret_profile",
        "resource_profile",
    }
    for name, policy in sorted(catalog.get("services", {}).items()):
        missing = sorted(required - set(policy))
        if missing:
            result.add_violation(name, "catalog.required_fields", f"catalog missing required fields: {', '.join(missing)}")
        if policy.get("class") not in service_classes:
            result.add_violation(name, "catalog.class", f"unknown service class: {policy.get('class')!r}")
        unknown_profiles = sorted(
            str(profile)
            for profile in policy.get("profiles_expected", []) or []
            if str(profile) not in documented_profiles
        )
        if unknown_profiles:
            result.add_violation(
                name,
                "catalog.profile_unknown",
                "profiles_expected references undocumented profiles: " + ", ".join(unknown_profiles),
            )
        unknown_env = sorted(str(item) for item in policy.get("generated_env", []) or [] if str(item) not in generated_env_files)
        if unknown_env:
            result.add_violation(name, "catalog.generated_env", "generated_env references unknown entries: " + ", ".join(unknown_env))
        if policy.get("secret_profile") not in SECRET_PROFILES:
            result.add_violation(name, "catalog.secret_profile", f"unknown secret_profile: {policy.get('secret_profile')!r}")
        if policy.get("resource_profile") not in RESOURCE_PROFILES:
            result.add_violation(name, "catalog.resource_profile", f"unknown resource_profile: {policy.get('resource_profile')!r}")
        if policy.get("host_ports_allowed") and not policy.get("reason"):
            result.add_violation(name, "catalog.host_port_reason", "host ports require a reason")
        if policy.get("exception_approved") and not policy.get("exception_reason"):
            result.add_violation(name, "catalog.exception_reason", "approved exceptions require exception_reason")
        if policy.get("rw_persistent_mount_targets_allowed") and not policy.get("exception_reason"):
            result.add_violation(
                name,
                "catalog.exception_reason",
                "rw_persistent_mount_targets_allowed requires exception_reason",
            )
        if policy.get("latest_allowed") and not policy.get("latest_reason"):
            result.add_violation(name, "catalog.latest_reason", "latest_allowed requires latest_reason")
        host_home_access = str(policy.get("host_home_access") or "none")
        if host_home_access not in HOST_HOME_ACCESS_MODES:
            result.add_violation(
                name,
                "catalog.host_home_access",
                f"unknown host_home_access: {host_home_access!r}",
            )
        if host_home_access != "none" and not str(policy.get("host_home_access_reason") or "").strip():
            result.add_violation(
                name,
                "catalog.host_home_access_reason",
                "host_home_access requires host_home_access_reason",
            )


def validate_observed(base: dict[str, Any], debug: dict[str, Any], catalog: dict[str, Any], *, mode: str) -> PolicyResult:
    result = PolicyResult([], [])
    validate_catalog(catalog, result)
    catalog_services = catalog.get("services", {})
    base_services = base.get("services") or {}
    debug_services = debug.get("services") or {}
    managed_roots, managed_named_volumes = _managed_storage_policy_roots(catalog)

    for tracked in _git_tracked(["infra/docker/secrets", "infra/docker/.env.observability"]):
        if Path(tracked).name == ".gitignore":
            continue
        result.add_violation("repo", "secrets.tracked", f"sensitive local runtime file is tracked: {tracked}")

    for name, service in sorted(base_services.items()):
        policy = catalog_services.get(name)
        if policy is None:
            result.add_warning(name, "catalog.missing", "service is present in compose but missing from service-catalog.toml")
            continue

        expected_profiles = {str(item) for item in policy.get("profiles_expected", []) or []}
        observed_profiles = {str(item) for item in service.get("profiles", []) or []}
        if observed_profiles != expected_profiles:
            result.add_violation(
                name,
                "profiles.mismatch",
                "compose profiles "
                + ", ".join(sorted(observed_profiles or {"-"}))
                + " differ from catalog "
                + ", ".join(sorted(expected_profiles or {"-"})),
            )

        expected_secrets = {str(item) for item in policy.get("secrets_required", []) or []}
        observed_secrets = {
            str(secret.get("source", secret) if isinstance(secret, dict) else secret)
            for secret in service.get("secrets", []) or []
        }
        if observed_secrets != expected_secrets:
            missing = sorted(expected_secrets - observed_secrets)
            unexpected = sorted(observed_secrets - expected_secrets)
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if unexpected:
                details.append("unexpected: " + ", ".join(unexpected))
            result.add_violation(name, "secrets.mismatch", "; ".join(details))

        ports = _ports(service)
        allowed = _allowed_ports(policy)
        service_class = policy.get("class")
        if service_class in {"internal", "worker"} and ports:
            result.add_violation(name, "ports.internal_published", f"{service_class} service publishes host ports")

        for port in ports:
            published = port.get("published")
            if not published:
                continue
            if published not in allowed:
                result.add_violation(name, "ports.uncataloged", f"host port {published} is not cataloged")
            host_ip = port.get("host_ip") or ""
            allow_public = bool(policy.get("allow_public_bind", False))
            if host_ip and host_ip not in LOCAL_BIND_VALUES and not allow_public:
                result.add_violation(name, "ports.bind", f"host port {published} binds to {host_ip}, expected localhost")

        if policy.get("compose_healthcheck_required", True) and not service.get("healthcheck"):
            result.add_violation(name, "healthcheck.missing", "Compose healthcheck is required by catalog")

        if service.get("privileged"):
            result.add_violation(name, "security.privileged", "privileged containers are not allowed")
        if service.get("network_mode") == "host":
            result.add_violation(name, "security.host_network", "network_mode=host is not allowed")
        if _has_docker_socket_mount(service) and name != "docker-proxy":
            result.add_violation(name, "security.docker_socket", "direct docker.sock mount is not allowed")
        dangerous_caps = {"SYS_ADMIN", "DAC_READ_SEARCH", "SYS_PTRACE", "NET_ADMIN", "MKNOD"}
        cap_add = {str(item).upper() for item in service.get("cap_add", []) or []}
        blocked_caps = sorted(cap_add & dangerous_caps)
        if blocked_caps:
            result.add_violation(name, "security.capabilities", "dangerous capabilities are not allowed: " + ", ".join(blocked_caps))

        env = _environment_mapping(service)
        if name != "storage_guardian":
            direct_storage_creds = sorted(
                key
                for key in env
                if key.startswith(("AWS_", "S3_", "GCS_", "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_STORAGE"))
            )
            if direct_storage_creds:
                result.add_violation(
                    name,
                    "storage.direct_credentials",
                    "direct object-storage credentials must be owned by storage_guardian: " + ", ".join(direct_storage_creds),
                )
            storage_secrets = [
                str(secret)
                for secret in (service.get("secrets") or [])
                if "storage" in str(secret).lower() or "s3" in str(secret).lower() or "gcs" in str(secret).lower()
            ]
            if storage_secrets:
                result.add_violation(
                    name,
                    "storage.direct_secret",
                    "storage backend secrets must not be mounted outside storage_guardian: " + ", ".join(storage_secrets),
                )

        for volume in _service_volumes(service):
            source = str(volume.get("source") or "")
            source_path = _path_from_volume_source(source)
            target = str(volume.get("target") or "")
            if target in {"/host_home", "/app/sources/host_home"} and source_path is not None:
                try:
                    if source_path.resolve(strict=False) == Path.home().resolve(strict=False):
                        host_home_access = str(policy.get("host_home_access") or "none")
                        if host_home_access == "none":
                            result.add_violation(
                                name,
                                "host_mount.broad_home",
                                "broad HOME mounts require explicit catalog approval",
                                target=target,
                                source=source,
                            )
                        elif host_home_access == "read_only" and _volume_is_rw(volume):
                            result.add_violation(
                                name,
                                "host_mount.broad_home_mode",
                                "catalog allows read-only HOME access but observed mount is read-write",
                                target=target,
                                source=source,
                            )
                        elif host_home_access == "storage_write" and (
                            name != "storage_guardian" or str(policy.get("owner") or "") != "storage"
                        ):
                            result.add_violation(
                                name,
                                "host_mount.broad_home_owner",
                                "read-write HOME access is restricted to the storage_guardian storage owner",
                                target=target,
                                source=source,
                            )
                except OSError:
                    pass
            if (
                _agent_feature_storage_subject(name, service, policy)
                and _volume_is_rw(volume)
                and not _agent_feature_rw_exception(volume, policy)
            ):
                result.add_violation(
                    name,
                    "storage.rw_agent_feature_persistent",
                    "agent/feature service has a persistent RW mount; use tmpfs or storage_guardian",
                    target=volume.get("target"),
                    source=volume.get("source"),
                )
            if not _volume_is_rw(volume) or name == "storage_guardian":
                continue
            if volume.get("type") == "volume" and source in managed_named_volumes:
                result.add_violation(
                    name,
                    "storage.rw_managed_volume",
                    f"RW named volume {source!r} is exclusive to storage_guardian",
                    target=volume.get("target"),
                )
            if source_path is not None:
                for root in sorted(managed_roots):
                    if _paths_overlap(source_path, root):
                        result.add_violation(
                            name,
                            "storage.rw_managed_root",
                            f"RW mount {source_path} overlaps storage_guardian exclusive root {root}",
                            target=volume.get("target"),
                        )

        labels = _labels(service)
        missing_labels = [key for key in STRICT_LABELS if key not in labels]
        if missing_labels:
            message = "missing governance labels: " + ", ".join(missing_labels)
            if mode == "strict":
                result.add_violation(name, "labels.missing", message)
            else:
                result.add_warning(name, "labels.missing", message)

        image = str(service.get("image", ""))
        if image.endswith(":latest") and not policy.get("latest_allowed", False):
            result.add_warning(name, "image.latest", f"image uses latest tag: {image}")

        deploy = service.get("deploy") or {}
        limits = (deploy.get("resources") or {}).get("limits") or {}
        missing_resources = []
        if not (limits.get("memory") or service.get("mem_limit")):
            missing_resources.append("memory")
        if not (limits.get("cpus") or service.get("cpus")):
            missing_resources.append("cpus")
        if not (limits.get("pids") or service.get("pids_limit")):
            missing_resources.append("pids")
        if missing_resources:
            result.add_warning(name, "resources.missing", "missing resource limits: " + ", ".join(missing_resources))

    for name, service in sorted(debug_services.items()):
        policy = catalog_services.get(name)
        if policy is None:
            continue
        allowed = _allowed_ports(policy, include_debug=True)
        for port in _ports(service):
            published = port.get("published")
            if published and published not in allowed:
                result.add_violation(name, "debug.uncataloged_port", f"debug/base overlay publishes uncataloged port {published}")

    return result


def _inventory_doc(payload: dict[str, Any]) -> str:
    lines = [
        "# Docker Inventory",
        "",
        f"Generated at: `{payload['generated_at']}`",
        f"Policy mode: `{payload['policy_mode']}`",
        "",
        "| Service | Class | Profiles | Host ports | Health | Owner | Secret profile | Resource profile | Generated env |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in payload["services"]:
        ports = ", ".join(str(p["published"]) for p in item["host_ports"]) or "-"
        profiles = ", ".join(item.get("profiles") or []) or "-"
        generated_env = ", ".join(item.get("generated_env") or []) or "-"
        health = "yes" if item.get("healthcheck") else "no"
        lines.append(
            f"| `{item['service']}` | {item['class']} | {profiles} | {ports} | {health} | "
            f"{item.get('owner') or '-'} | {item.get('secret_profile') or '-'} | "
            f"{item.get('resource_profile') or '-'} | {generated_env} |"
        )
    if payload.get("compose_projects"):
        lines.extend([
            "",
            "## Compose Projects",
            "",
            "| Project | Role | Workdir | Profiles | Status |",
            "| --- | --- | --- | --- | --- |",
        ])
        for item in payload["compose_projects"]:
            profiles = ", ".join(item.get("profiles") or []) or "-"
            lines.append(
                f"| `{item['name']}` | {item['role']} | `{item['workdir']}` | {profiles} | `{item['status']}` |"
            )
    if payload["violations"]:
        lines.extend(["", "## Violations", ""])
        for violation in payload["violations"]:
            lines.append(f"- `{violation['service']}` `{violation['rule']}`: {violation['message']}")
    if payload["warnings"]:
        lines.extend(["", "## Warnings", ""])
        for warning in payload["warnings"]:
            lines.append(f"- `{warning['service']}` `{warning['rule']}`: {warning['message']}")
    lines.append("")
    return "\n".join(lines)


def write_inventory(payload: dict[str, Any]) -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    summary = _inventory_artifact_summary(payload)
    INVENTORY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    INVENTORY_MD.write_text(_summary_doc(summary), encoding="utf-8")


def build_inventory(mode: str) -> dict[str, Any]:
    catalog = load_catalog()
    base = compose_config(catalog)
    debug = compose_config(catalog, debug=True)
    result = validate_observed(base, debug, catalog, mode=mode)
    project_checks = compose_project_checks(catalog)
    for check in project_checks:
        if check["status"] != "pass":
            result.add_violation(
                f"compose:{check['name']}",
                "compose.project_invalid",
                f"{check['name']} compose config is invalid",
                error=check.get("error", ""),
            )
    return _public_inventory_payload({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy_mode": mode,
        "profiles": _catalog_profiles(catalog),
        "compose_projects": project_checks,
        "services": observed_inventory(base, catalog),
        "violations": result.violations,
        "warnings": result.warnings,
    })


def _dockerfile_report() -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for path in CURRENT_DOCKERFILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(ROOT))
        warnings: list[str] = []
        froms = re.findall(r"(?im)^FROM\s+([^\s]+)", text)
        if any(ref.endswith(":latest") or ":" not in ref for ref in froms):
            warnings.append("base image is unpinned or uses latest")
        if "USER " not in text:
            warnings.append("no explicit non-root USER")
        if "LABEL org.opencontainers.image" not in text:
            warnings.append("missing OCI image labels")
        if "apt-get install" in text and "rm -rf /var/lib/apt/lists" not in text:
            warnings.append("apt cache cleanup not detected")
        report.append({"dockerfile": rel, "from": froms, "warnings": warnings})
    return report


def build_optimization_report() -> dict[str, Any]:
    inventory = build_inventory("warn")
    compose_warnings = [
        warning for warning in inventory["warnings"]
        if warning["rule"] in {"image.latest", "resources.missing", "labels.missing"}
    ]
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8", errors="replace") if (ROOT / ".dockerignore").exists() else ""
    dockerignore_warnings = []
    for required in ("infra/docker/secrets/", ".env.storage.generated", ".env.llm.generated", ".env.services.generated", "**/*.gguf"):
        if required not in dockerignore:
            dockerignore_warnings.append(".dockerignore missing required private-file exclusion")
    return _public_optimization_payload({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dockerfiles": _dockerfile_report(),
        "compose_warnings": compose_warnings,
        "dockerignore_warnings": dockerignore_warnings,
    })


def write_optimization_report(payload: dict[str, Any]) -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    summary = _optimization_artifact_summary(payload)
    OPTIMIZATION_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    OPTIMIZATION_MD.write_text(_summary_doc(summary), encoding="utf-8")


def _print_compose_project_checks(checks: list[dict[str, Any]], *, lines: bool = False) -> None:
    failed = sum(1 for check in checks if check.get("status") != "pass")
    if lines:
        for index, check in enumerate(checks, start=1):
            status = "pass" if check.get("status") == "pass" else "fail"
            print(f"{status}\tproject-{index:03d}\tcompose\t.")
        return

    print(f"Compose projects: {len(checks)} checked, {failed} failed")


def _print_required_secrets(catalog: dict[str, Any], *, lines: bool = False) -> None:
    selected = _selected_profiles()
    secrets = required_secret_files(catalog, selected)
    if lines:
        for item in secrets:
            print(f"{item['secret']}\t{item['path']}\t{','.join(item['profiles'])}")
        return
    print("Required secrets for profiles " + ",".join(sorted(selected)) + f": {len(secrets)}")
    for item in secrets:
        print(f"- {item['secret']} ({item['path']})")


def _print_host_ports(catalog: dict[str, Any], *, lines: bool = False) -> None:
    selected = _selected_profiles()
    ports = selected_host_ports(catalog, selected)
    if lines:
        for item in ports:
            print(f"{item['service']}\t{item['port']}\t{item['bind']}\t{','.join(item['profiles'])}")
        return
    print("Host ports for profiles " + ",".join(sorted(selected)) + f": {len(ports)}")
    for item in ports:
        print(f"- {item['service']}: {item['bind']}:{item['port']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("inventory", "validate", "optimize", "compose-projects", "required-secrets", "host-ports"),
        nargs="?",
        default="validate",
    )
    parser.add_argument("--mode", choices=("warn", "baseline", "strict"), default=os.environ.get("DOCKER_POLICY_MODE", "baseline"))
    parser.add_argument("--write", action="store_true", help="write docs/generated artifacts")
    parser.add_argument("--json", action="store_true", help="print JSON payload")
    parser.add_argument("--lines", action="store_true", help="print compact tab-separated output")
    args = parser.parse_args(argv)

    if args.command == "required-secrets":
        _print_required_secrets(load_catalog(), lines=args.lines)
        return 0

    if args.command == "host-ports":
        _print_host_ports(load_catalog(), lines=args.lines)
        return 0

    if args.command == "compose-projects":
        catalog = load_catalog()
        checks = compose_project_checks(catalog)
        if args.json:
            print(json.dumps({"status": "generated", "project_count": len(checks)}, indent=2, sort_keys=True))
        else:
            _print_compose_project_checks(checks, lines=args.lines)
        return 1 if any(check.get("status") != "pass" for check in checks) else 0

    if args.command == "optimize":
        payload = build_optimization_report()
        if args.write:
            write_optimization_report(payload)
        if args.json:
            print(json.dumps(_optimization_artifact_summary(payload), indent=2, sort_keys=True))
        else:
            write_optimization_report(payload)
            print(f"Wrote {OPTIMIZATION_MD.relative_to(ROOT)}")
        return 0

    payload = build_inventory(args.mode)
    if args.write or args.command == "inventory":
        write_inventory(payload)
    if args.json:
        print(json.dumps(_inventory_artifact_summary(payload), indent=2, sort_keys=True))
    else:
        print(
            f"Docker policy {args.mode}: "
            f"{len(payload['violations'])} violation(s), {len(payload['warnings'])} warning(s)"
        )
        if payload["violations"]:
            print("ERROR docker policy violations detected")
        elif args.command == "inventory":
            print(f"Wrote {INVENTORY_JSON.relative_to(ROOT)} and {INVENTORY_MD.relative_to(ROOT)}")

    return 1 if payload["violations"] and args.mode in {"baseline", "strict"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
