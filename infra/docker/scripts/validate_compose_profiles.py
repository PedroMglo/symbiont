#!/usr/bin/env python3
"""Validate the declarative Docker Compose profile contract.

The contract lives in infra because it documents operational wiring. Service
policy remains in config/docker/service-catalog.toml and is treated here as the
source of truth for service owners, expected profiles and health requirements.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PROFILE_CONTRACT = ROOT / "infra" / "docker" / "compose" / "profile-contract.toml"
SERVICE_CATALOG = ROOT / "config" / "docker" / "service-catalog.toml"
COMPOSE_PROJECTS = ROOT / "config" / "docker" / "compose-projects.toml"

PHASE_1_PROFILES = {
    "core",
    "storage",
    "features",
    "agents",
    "heavy",
    "material",
    "observability",
    "llm",
    "gpu",
}
REQUIRED_TEXT_FIELDS = ("owner", "purpose", "health", "secrets", "resources")
SERVICE_TEXT_FIELDS = ("owner", "secret_profile", "resource_profile")
RUNNER_SERVICE_MARKERS = ("runner", "sandbox", "workspace-execution")
URL_ENV_MARKERS = ("URL", "ENDPOINT", "BASE_URL")
RUNNER_RW_BIND_TARGET_ALLOWLIST = {
    "/run/ai-local-tls",
}


@dataclass(frozen=True)
class Finding:
    rule: str
    subject: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "subject": self.subject, "message": self.message}


@dataclass
class ValidationResult:
    errors: list[Finding]
    warnings: list[Finding]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, rule: str, subject: str, message: str) -> None:
        self.errors.append(Finding(rule, subject, message))

    def warning(self, rule: str, subject: str, message: str) -> None:
        self.warnings.append(Finding(rule, subject, message))


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def catalog_profiles(catalog: dict[str, Any]) -> set[str]:
    profiles: set[str] = set()
    for service in (catalog.get("services") or {}).values():
        for profile in service.get("profiles_expected") or []:
            profiles.add(str(profile))
    return profiles


def compose_project_env_files(projects: dict[str, Any]) -> set[str]:
    env_files: set[str] = set()
    for project in (projects.get("projects") or {}).values():
        for env_file in project.get("env_files") or []:
            env_files.add(str(env_file))
    return env_files


def labels_for(service: dict[str, Any]) -> dict[str, str]:
    labels = service.get("labels") or {}
    if isinstance(labels, dict):
        return {str(key): str(value) for key, value in labels.items()}
    parsed: dict[str, str] = {}
    if isinstance(labels, list):
        for label in labels:
            key, _, value = str(label).partition("=")
            parsed[key] = value
    return parsed


def _non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def environment_for(service: dict[str, Any]) -> dict[str, str]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return {str(key): str(value) for key, value in environment.items()}
    parsed: dict[str, str] = {}
    if isinstance(environment, list):
        for item in environment:
            key, _, value = str(item).partition("=")
            if key:
                parsed[key] = value
    return parsed


def volume_items(service: dict[str, Any]) -> list[dict[str, str | bool]]:
    volumes: list[dict[str, str | bool]] = []
    for item in service.get("volumes") or []:
        if isinstance(item, str):
            parts = item.split(":")
            source = parts[0] if len(parts) >= 2 else ""
            target = parts[1] if len(parts) >= 2 else parts[0]
            mode = parts[2] if len(parts) >= 3 else "rw"
            read_only = "ro" in {part.strip() for part in mode.split(",")}
            volumes.append(
                {
                    "type": "bind",
                    "source": source,
                    "target": target,
                    "mode": mode,
                    "read_only": read_only,
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        mode = str(item.get("mode") or "")
        read_only = bool(item.get("read_only") or item.get("readOnly") or mode == "ro")
        volumes.append(
            {
                "type": str(item.get("type") or "volume"),
                "source": str(item.get("source") or item.get("Source") or ""),
                "target": str(item.get("target") or item.get("Target") or ""),
                "mode": mode or ("ro" if read_only else "rw"),
                "read_only": read_only,
            }
        )
    return volumes


def _volume_is_rw(volume: dict[str, str | bool]) -> bool:
    if bool(volume.get("read_only")):
        return False
    mode = str(volume.get("mode") or "")
    if mode and "ro" in {part.strip() for part in mode.split(",")}:
        return False
    return True


def _has_docker_socket_mount(service: dict[str, Any]) -> bool:
    return any("docker.sock" in str(volume.get("source")) or "docker.sock" in str(volume.get("target")) for volume in volume_items(service))


def _is_runner_or_sandbox(name: str, service: dict[str, Any]) -> bool:
    lowered = name.lower()
    if any(marker in lowered for marker in RUNNER_SERVICE_MARKERS):
        return True
    env = environment_for(service)
    return any(key.startswith("WORKSPACE_EXECUTION_RUNNER_") for key in env)


def _url_env_items(service: dict[str, Any]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for key, value in environment_for(service).items():
        key_upper = key.upper()
        if key_upper == "DOCKER_HOST" or any(marker in key_upper for marker in URL_ENV_MARKERS):
            items.append((key, value))
    return items


def validate_contract(
    contract: dict[str, Any],
    catalog: dict[str, Any],
    compose_projects: dict[str, Any],
    *,
    compose: dict[str, Any] | None = None,
) -> ValidationResult:
    result = ValidationResult([], [])
    profiles = contract.get("profiles") or {}
    services = catalog.get("services") or {}
    generated_env_files = set((catalog.get("generated_env_files") or {}).keys())
    env_files = compose_project_env_files(compose_projects)

    if not isinstance(profiles, dict) or not profiles:
        result.error("profile_contract.empty", "profiles", "profile contract has no [profiles.*] entries")
        return result

    required_profiles = catalog_profiles(catalog) | PHASE_1_PROFILES
    for profile in sorted(required_profiles):
        entry = profiles.get(profile)
        if not isinstance(entry, dict):
            result.error("profile.missing", profile, "profile is used by catalog or Phase 1 but is not documented")
            continue
        for field in REQUIRED_TEXT_FIELDS:
            if not _non_empty_text(entry.get(field)):
                result.error("profile.field_missing", profile, f"profile is missing non-empty {field}")
        generated_env = entry.get("generated_env")
        if not isinstance(generated_env, list) or not generated_env:
            result.error("profile.generated_env_missing", profile, "profile must declare generated_env inputs")
            continue
        for env_file in generated_env:
            env_name = str(env_file)
            if env_name not in env_files:
                result.error(
                    "profile.generated_env_uncataloged",
                    profile,
                    f"{env_name} is not listed in config/docker/compose-projects.toml",
                )

    for profile in sorted(set(profiles) - required_profiles):
        result.warning("profile.unused", profile, "profile is documented but not referenced by service catalog")

    for name, policy in sorted(services.items()):
        for field in SERVICE_TEXT_FIELDS:
            if not _non_empty_text(policy.get(field)):
                result.error("service.field_missing", name, f"service catalog entry must declare non-empty {field}")
        expected_profiles = policy.get("profiles_expected")
        if not isinstance(expected_profiles, list) or not expected_profiles:
            result.error("service.profiles_missing", name, "service catalog entry must declare profiles_expected")
            continue
        for profile in expected_profiles:
            if str(profile) not in profiles:
                result.error("service.profile_undocumented", name, f"profile {profile} is not documented")
        generated_env = policy.get("generated_env")
        if not isinstance(generated_env, list) or not generated_env:
            result.error("service.generated_env_missing", name, "service catalog entry must declare generated_env")
        else:
            for env_key in generated_env:
                if str(env_key) not in generated_env_files:
                    result.error(
                        "service.generated_env_unknown",
                        name,
                        f"generated_env {env_key} is not declared in [generated_env_files]",
                    )
        secrets_required = policy.get("secrets_required")
        if not isinstance(secrets_required, list):
            result.error("service.secrets_required_missing", name, "service catalog entry must declare secrets_required")
        if "compose_healthcheck_required" not in policy:
            result.error("service.health_policy_missing", name, "service catalog entry must declare compose_healthcheck_required")

    if compose is None:
        return result

    compose_services = compose.get("services") or {}
    for name, service in sorted(compose_services.items()):
        policy = services.get(name)
        if not isinstance(policy, dict):
            result.error("compose.owner_missing", name, "compose service is missing from service-catalog.toml")
            continue

        actual_profiles = {str(profile) for profile in service.get("profiles") or []}
        expected_profiles = {str(profile) for profile in policy.get("profiles_expected") or []}
        if actual_profiles != expected_profiles:
            result.error(
                "compose.profiles_mismatch",
                name,
                "compose profiles "
                f"{sorted(actual_profiles)} do not match catalog profiles_expected {sorted(expected_profiles)}",
            )

        labels = labels_for(service)
        label_owner = labels.get("ai.local.owner")
        if label_owner != policy.get("owner"):
            result.error(
                "compose.owner_label_mismatch",
                name,
                f"ai.local.owner={label_owner!r} does not match catalog owner {policy.get('owner')!r}",
            )
        label_profile = labels.get("ai.local.profile")
        if label_profile not in actual_profiles:
            result.error(
                "compose.profile_label_mismatch",
                name,
                f"ai.local.profile={label_profile!r} is not one of compose profiles {sorted(actual_profiles)}",
            )
        if policy.get("compose_healthcheck_required", True) and not service.get("healthcheck"):
            result.error("compose.healthcheck_missing", name, "catalog requires a Compose healthcheck")

        env = environment_for(service)
        docker_host = env.get("DOCKER_HOST", "")
        if _has_docker_socket_mount(service):
            if name != "docker-proxy":
                result.error("compose.docker_socket_mount", name, "only docker-proxy may mount docker.sock")
            for volume in volume_items(service):
                if "docker.sock" in str(volume.get("source")) or "docker.sock" in str(volume.get("target")):
                    if _volume_is_rw(volume):
                        result.error("compose.docker_socket_rw", name, "docker.sock mount must be read-only")
        if docker_host:
            if docker_host != "tcp://docker-proxy:2375":
                result.error("compose.docker_host_unapproved", name, f"DOCKER_HOST must point at docker-proxy, got {docker_host!r}")
            if env.get("DOCKER_TLS_VERIFY") != "1" or not env.get("DOCKER_CERT_PATH"):
                result.error(
                    "compose.docker_host_tls_missing",
                    name,
                    "DOCKER_HOST over tcp requires DOCKER_TLS_VERIFY=1 and DOCKER_CERT_PATH",
                )
        for key, value in _url_env_items(service):
            if value.startswith("http://"):
                result.error("compose.insecure_http_url", name, f"{key} must use https, got {value!r}")
        if _is_runner_or_sandbox(name, service):
            for volume in volume_items(service):
                if (
                    volume.get("type") == "bind"
                    and _volume_is_rw(volume)
                    and str(volume.get("target")) not in RUNNER_RW_BIND_TARGET_ALLOWLIST
                ):
                    result.error(
                        "compose.runner_rw_host_bind",
                        name,
                        "runner/sandbox service must not receive RW host binds outside approved operational mounts",
                    )

    return result


def load_compose_config(catalog: dict[str, Any]) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from scripts import docker_policy  # pylint: disable=import-outside-toplevel

    return docker_policy.compose_config(catalog)


def result_payload(result: ValidationResult) -> dict[str, Any]:
    return {
        "status": "pass" if result.ok else "fail",
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "errors": [finding.to_dict() for finding in result.errors],
        "warnings": [finding.to_dict() for finding in result.warnings],
    }


def print_human(result: ValidationResult) -> None:
    print(
        "Compose profile contract: "
        f"{'pass' if result.ok else 'fail'} "
        f"({len(result.errors)} error(s), {len(result.warnings)} warning(s))"
    )
    for finding in result.errors:
        print(f"ERROR {finding.rule} {finding.subject}: {finding.message}")
    for finding in result.warnings:
        print(f"WARN {finding.rule} {finding.subject}: {finding.message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-compose", action="store_true", help="validate static TOML contracts only")
    parser.add_argument("--json", action="store_true", help="print JSON result")
    args = parser.parse_args(argv)

    contract = load_toml(PROFILE_CONTRACT)
    catalog = load_toml(SERVICE_CATALOG)
    compose_projects = load_toml(COMPOSE_PROJECTS)

    compose = None
    result = ValidationResult([], [])
    if not args.skip_compose:
        try:
            compose = load_compose_config(catalog)
        except SystemExit as exc:
            result.error("compose.config_failed", "docker compose config", str(exc))

    contract_result = validate_contract(contract, catalog, compose_projects, compose=compose)
    result.errors.extend(contract_result.errors)
    result.warnings.extend(contract_result.warnings)

    if args.json:
        print(json.dumps(result_payload(result), indent=2, sort_keys=True))
    else:
        print_human(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
