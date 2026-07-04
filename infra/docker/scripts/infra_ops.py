#!/usr/bin/env python3
"""Canonical ai-local infrastructure operations.

The Makefile is the human-facing sequence. This script owns the mechanical
steps so the Makefile can stay small and readable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ROLLBACK_ROOT = ROOT / ".local" / "infra" / "rollback"
STORAGE_ENV = ROOT / ".env.storage.generated"
LLM_ENV = ROOT / ".env.llm.generated"
SERVICES_ENV = ROOT / ".env.services.generated"
DOCKER_RESOURCES_ENV = ROOT / ".env.docker.resources.generated"
IMAGE_BUILD_CATALOG = ROOT / "config" / "docker" / "image-build-catalog.toml"
OBS_ENV = ROOT / "infra" / "docker" / ".env.observability"
OLLAMA_HOST_CONFIG_DIR = ROOT / ".local" / "generated" / "ollama-host"
TLS_DIR = ROOT / ".local" / "tls"
ENV_FILES = (STORAGE_ENV, LLM_ENV, SERVICES_ENV, DOCKER_RESOURCES_ENV)
ALL_ENV_FILES = (*ENV_FILES, OBS_ENV)
PROFILE_ALL = (
    "core",
    "storage",
    "agents",
    "features",
    "material",
    "heavy",
    "observability",
    "llm",
    "gpu",
    "i18n",
    "qdrant",
    "temporal",
    "rag-graph",
)
DEFAULT_DOCKER_CACHE_MAX = "30gb"
DEFAULT_COMPOSE_PARALLEL_LIMIT = "4"
GENERATED_ARTIFACTS = (
    STORAGE_ENV,
    LLM_ENV,
    SERVICES_ENV,
    DOCKER_RESOURCES_ENV,
    OBS_ENV,
    ROOT / "docs" / "generated" / "docker-inventory.json",
)


@dataclass(frozen=True)
class Step:
    name: str
    command: tuple[str, ...]
    suggestion: str
    extra_env: dict[str, str] | None = None


class InfraFailure(RuntimeError):
    def __init__(self, step: Step, code: int) -> None:
        super().__init__(f"{step.name} failed with exit code {code}")
        self.step = step
        self.code = code


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    docker_context = env.get("AI_LOCAL_DOCKER_CONTEXT") or env.get("DOCKER_CONTEXT") or "default"
    env["DOCKER_CONTEXT"] = docker_context
    env["AI_LOCAL_DOCKER_CONTEXT"] = docker_context
    env.setdefault("AI_COMPOSE_PROFILES", "core,storage")
    env.setdefault("DOCKER_BUILDKIT", "1")
    return env


def _bool_setting(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_setting(value: str | None, *, default: str) -> str:
    raw = (value or default).strip()
    try:
        parsed = int(raw)
    except ValueError:
        return default
    if parsed < 1:
        return default
    return str(parsed)


def _docker_resource_env() -> dict[str, str]:
    return _read_env_file(DOCKER_RESOURCES_ENV)


def _operator_env(env: dict[str, str]) -> dict[str, str]:
    generated = _docker_resource_env()
    merged = env.copy()
    for key, value in generated.items():
        merged.setdefault(key, value)
    merged.setdefault("DOCKER_BUILDKIT", "1")
    parallel = env.get("AI_LOCAL_COMPOSE_PARALLEL_LIMIT") or env.get("COMPOSE_PARALLEL_LIMIT")
    if not parallel:
        parallel = generated.get("AI_LOCAL_COMPOSE_PARALLEL_LIMIT") or generated.get("COMPOSE_PARALLEL_LIMIT")
    if not parallel:
        parallel = DEFAULT_COMPOSE_PARALLEL_LIMIT
    parallel = _positive_int_setting(parallel, default=DEFAULT_COMPOSE_PARALLEL_LIMIT)
    merged["AI_LOCAL_COMPOSE_PARALLEL_LIMIT"] = parallel
    merged["COMPOSE_PARALLEL_LIMIT"] = parallel
    return merged


def _docker(env: dict[str, str]) -> tuple[str, ...]:
    return ("docker", "--context", env["AI_LOCAL_DOCKER_CONTEXT"])


def _compose_base(env: dict[str, str], env_files: tuple[Path, ...] = ENV_FILES) -> list[str]:
    effective_env = _operator_env(env)
    cmd = [*_docker(effective_env), "compose", "--parallel", effective_env["COMPOSE_PARALLEL_LIMIT"]]
    for env_file in env_files:
        cmd.extend(("--env-file", str(env_file.relative_to(ROOT))))
    return cmd


def _selected_profiles(env: dict[str, str]) -> list[str]:
    raw = env.get("AI_COMPOSE_PROFILES", "core,storage")
    return [profile.strip() for profile in raw.split(",") if profile.strip()]


def _image_tag(env: dict[str, str]) -> str:
    return env.get("AI_LOCAL_IMAGE_TAG", "dev") or "dev"


def _profile_args(profiles: list[str]) -> list[str]:
    args: list[str] = []
    for profile in profiles:
        args.extend(("--profile", profile))
    return args


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def _read_image_build_catalog() -> dict[str, object]:
    if not IMAGE_BUILD_CATALOG.exists():
        return {}
    return tomllib.loads(IMAGE_BUILD_CATALOG.read_text(encoding="utf-8"))


def _catalog_build_arg_value(env: dict[str, str], name: str) -> str:
    if name == "AI_LOCAL_BASE_TAG":
        return env.get(name) or _image_tag(env)
    return env.get(name, "")


def _direct_build_steps(env: dict[str, str]) -> list[Step]:
    catalog = _read_image_build_catalog()
    targets = catalog.get("direct_targets", [])
    if not isinstance(targets, list):
        return []

    steps: list[Step] = []
    for target in targets:
        if not isinstance(target, dict) or not target.get("mandatory", False):
            continue
        name = str(target.get("name") or "unnamed-image")
        image_template = str(target.get("image") or "")
        dockerfile = str(target.get("dockerfile") or "")
        context = str(target.get("context") or ".")
        if not image_template or not dockerfile:
            continue
        command = [
            *_docker(env),
            "build",
            "-t",
            image_template.format(tag=_image_tag(env)),
            "-f",
            dockerfile,
        ]
        target_stage = str(target.get("target") or "")
        if target_stage:
            command.extend(("--target", target_stage))
        build_args = target.get("build_args", [])
        if isinstance(build_args, list):
            for build_arg in build_args:
                arg_name = str(build_arg)
                command.extend(("--build-arg", f"{arg_name}={_catalog_build_arg_value(env, arg_name)}"))
        command.append(context)
        steps.append(
            Step(
                f"build mandatory direct image {name}",
                tuple(command),
                "Fix config/docker/image-build-catalog.toml or the referenced Dockerfile.",
            )
        )
    return steps


def _storage_guardian_env(env: dict[str, str]) -> dict[str, str]:
    storage_env = _read_env_file(STORAGE_ENV)
    merged = env.copy()
    root = storage_env.get("AI_STORAGE_HOST_BIND_ROOT", "")
    external_root = storage_env.get("AI_STORAGE_EXTERNAL_ROOT", "")
    if root:
        merged["AI_STORAGE_GUARDIAN_ROOT"] = root
    if external_root:
        merged["AI_STORAGE_GUARDIAN_EXTERNAL_ROOT"] = external_root
    merged["PYTHONPATH"] = "storage_guardian/src"
    return merged


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _run(step: Step, env: dict[str, str]) -> None:
    print(f"\n==> {step.name}")
    step_env = _operator_env(env)
    if step.extra_env:
        step_env.update(step.extra_env)
    completed = subprocess.run(step.command, cwd=ROOT, env=step_env, check=False)
    if completed.returncode != 0:
        raise InfraFailure(step, completed.returncode)


def _run_steps(steps: list[Step], env: dict[str, str]) -> int:
    try:
        for step in steps:
            _run(step, env)
    except InfraFailure as exc:
        print("\nInfra operation failed.")
        print(f"Step: {exc.step.name}")
        print(f"Suggestion: {exc.step.suggestion}")
        return exc.code
    return 0


def _snapshot_name() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_snapshot(label: str) -> Path:
    snapshot = ROLLBACK_ROOT / f"{_snapshot_name()}-{label}"
    snapshot.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "artifacts": [],
    }
    artifacts: list[dict[str, str]] = []
    for artifact in GENERATED_ARTIFACTS:
        if not artifact.exists():
            continue
        rel = artifact.relative_to(ROOT)
        target = snapshot / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, target)
        artifacts.append({"path": rel.as_posix(), "status": "captured"})
    manifest["artifacts"] = artifacts
    (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return snapshot


def latest_snapshot() -> Path | None:
    if not ROLLBACK_ROOT.exists():
        return None
    snapshots = sorted(path for path in ROLLBACK_ROOT.iterdir() if path.is_dir())
    return snapshots[-1] if snapshots else None


def restore_snapshot(snapshot: Path) -> None:
    manifest_path = snapshot / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Snapshot manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored: list[str] = []
    for entry in manifest.get("artifacts", []):
        rel = Path(str(entry["path"]))
        source = snapshot / rel
        target = ROOT / rel
        if not source.exists():
            raise SystemExit(f"Snapshot artifact missing: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored.append(rel.as_posix())
    print(f"Restored {len(restored)} generated artifact(s) from {_display_path(snapshot)}")
    for rel in restored:
        print(f"  - {rel}")


def _write_observability_env() -> None:
    secrets_dir = ROOT / "infra" / "docker" / "secrets"
    values = {
        "LANGFUSE_DB_PASSWORD": (secrets_dir / "langfuse_db_password").read_text(encoding="utf-8").strip(),
        "LANGFUSE_NEXTAUTH_SECRET": (secrets_dir / "langfuse_nextauth_secret").read_text(encoding="utf-8").strip(),
        "LANGFUSE_SALT": (secrets_dir / "langfuse_salt").read_text(encoding="utf-8").strip(),
    }
    OBS_ENV.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    print(f"Generated {_display_path(OBS_ENV)} from infra/docker/secrets/")


def _config_steps(env: dict[str, str]) -> list[Step]:
    return [
        Step("ensure Docker secrets", ("python", "-m", "config.docker_secrets", "--ensure"), "Check infra/docker/secrets permissions."),
        Step(
            "write storage env",
            ("python", "-m", "config.resolver", "--write-storage-env", _display_path(STORAGE_ENV)),
            "Fix config/main.yaml storage settings.",
        ),
        Step(
            "reconcile storage guardian structure",
            ("python", "-m", "storage_guardian.cli", "--config", "config/storage_guardian.yaml", "reconcile-structure", "--apply"),
            "Mount storage or inspect storage_guardian structure reconciliation.",
            extra_env=_storage_guardian_env(env),
        ),
        Step(
            "sync storage guardian pending external",
            ("python", "-m", "storage_guardian.cli", "--config", "config/storage_guardian.yaml", "sync-pending"),
            "Mount storage or inspect storage_guardian pending external sync.",
            extra_env=_storage_guardian_env(env),
        ),
        Step(
            "write LLM env",
            ("python", "-m", "config.resolver", "--write-llm-env", _display_path(LLM_ENV)),
            "Fix config/main.yaml LLM settings.",
            extra_env={"AI_COMPOSE_PROFILES": env.get("AI_COMPOSE_PROFILES", "core,storage")},
        ),
        Step(
            "write services env",
            ("python", "-m", "config.resolver", "--write-services-env", _display_path(SERVICES_ENV)),
            "Fix service port/config settings.",
        ),
        Step(
            "write Docker resources env",
            ("python", "-m", "config.resolver", "--write-docker-resources-env", _display_path(DOCKER_RESOURCES_ENV)),
            "Fix Docker resource settings.",
        ),
        Step(
            "write Ollama host config",
            ("python", "-m", "config.resolver", "--write-ollama-host-config", _display_path(OLLAMA_HOST_CONFIG_DIR)),
            "Inspect generated Ollama host config.",
        ),
    ]


def _validation_steps(env: dict[str, str]) -> list[Step]:
    compose_all = [*_compose_base(env, ALL_ENV_FILES), *_profile_args(list(PROFILE_ALL))]
    return [
        Step(
            "validate storage boundary",
            ("python", "-m", "storage_guardian.cli", "--config", "config/storage_guardian.yaml", "status"),
            "Mount storage or inspect storage_guardian status.",
            extra_env=_storage_guardian_env(env),
        ),
        Step(
            "validate storage policies",
            ("python", "-m", "storage_guardian.cli", "--config", "config/storage_guardian.yaml", "storage-policies"),
            "Inspect storage_guardian policy config.",
            extra_env=_storage_guardian_env(env),
        ),
        Step("validate Compose graph", (*compose_all, "config", "--quiet"), "Fix compose fragments or generated env files."),
        Step(
            "validate Docker policy",
            ("python", "scripts/docker_policy.py", "validate", "--mode", "baseline", "--write"),
            "Fix config/docker policy or Compose labels.",
        ),
        Step(
            "validate Docker host contract",
            ("infra/docker/scripts/validate-docker.sh",),
            "Fix Docker daemon/secrets/profile issues.",
        ),
    ]


def _build_steps(env: dict[str, str], *, profiles: tuple[str, ...] | list[str] | None = None) -> list[Step]:
    selected_profiles = list(profiles) if profiles is not None else _selected_profiles(env)
    compose_selected = [*_compose_base(env, ALL_ENV_FILES), *_profile_args(selected_profiles)]
    all_profiles_selected = tuple(selected_profiles) == PROFILE_ALL
    steps = _direct_build_steps(env) if all_profiles_selected else []
    steps.append(
        Step(
            "build mandatory Compose image catalog" if all_profiles_selected else "build selected Docker images",
            (*compose_selected, "build"),
            "Fix Dockerfiles, package dependencies, or compose profiles.",
        )
    )
    if "features" in selected_profiles or "material" in selected_profiles:
        steps.append(
            Step(
                "build command sandbox image",
                (
                    *_docker(env),
                    "build",
                    "-t",
                    "ai-local-command-sandbox:latest",
                    "-f",
                    "infra/docker/images/command-sandbox/Dockerfile",
                    "infra/docker/images/command-sandbox",
                ),
                "Fix command sandbox Dockerfile.",
            )
        )
    return steps


def _start_steps(env: dict[str, str]) -> list[Step]:
    profiles = _selected_profiles(env)
    compose_selected = [*_compose_base(env), *_profile_args(profiles)]
    operator_env = _operator_env(env)
    up_command = [*compose_selected, "up", "-d"]
    if _bool_setting(operator_env.get("AI_LOCAL_DOCKER_UP_NO_BUILD"), default=True):
        up_command.append("--no-build")
    if _bool_setting(operator_env.get("AI_LOCAL_DOCKER_REMOVE_ORPHANS"), default=False):
        up_command.append("--remove-orphans")
    if _bool_setting(operator_env.get("AI_LOCAL_DOCKER_UP_WAIT"), default=True):
        up_command.extend(
            (
                "--wait",
                "--wait-timeout",
                _positive_int_setting(operator_env.get("AI_LOCAL_DOCKER_UP_WAIT_TIMEOUT"), default="120"),
            )
        )
    steps: list[Step] = [
        Step(
            "generate Docker TLS bind sources",
            (
                "sh",
                "infra/docker/images/symbiont/base/tls-cert.sh",
            ),
            "Regenerate .local/tls or inspect TLS permissions.",
            extra_env={
                "AI_LOCAL_TLS_DIR": str(TLS_DIR),
                "AI_LOCAL_TLS_SERVICE_NAME": "docker-proxy",
                "AI_LOCAL_TLS_DNS_NAMES": "docker-proxy,orc-docker-proxy,localhost",
            },
        ),
        Step(
            "validate Docker bind sources",
            (
                "python",
                "-m",
                "config.docker_bind_sources",
                "--env-file",
                _display_path(STORAGE_ENV),
                "--env-file",
                _display_path(LLM_ENV),
                "--env-file",
                _display_path(SERVICES_ENV),
                *_profile_args(profiles),
            ),
            "Fix generated env files or bind-source config.",
        ),
    ]
    steps.extend(
        [
            Step("start selected stack", tuple(up_command), "Inspect Docker logs or rollback generated env."),
            Step("check container health", (*_docker(env), "ps", "--filter", "name=orc-"), "Inspect unhealthy container logs."),
            Step(
                "write Docker inventory",
                ("python", "scripts/docker_policy.py", "inventory", "--mode", "warn", "--write"),
                "Fix Docker inventory warnings.",
            ),
            Step(
                "run runtime smoke",
                ("python", "scripts/docker_runtime_smoke.py", "--write", "--strict"),
                "Keep the stack up, inspect docs/generated/docker-runtime-smoke.md, then rerun `make up`.",
            ),
            Step(
                "report degraded states",
                ("python", "scripts/resilience_report.py", "slo-report"),
                "Inspect docs/generated/slo-report.md and owner logs.",
            ),
        ]
    )
    return steps


def command_config(_: argparse.Namespace) -> int:
    env = _base_env()
    code = _run_steps(_config_steps(env), env)
    if code == 0:
        _write_observability_env()
    return code


def command_validate(_: argparse.Namespace) -> int:
    env = _base_env()
    return _run_steps(_validation_steps(env), env)


def command_build(_: argparse.Namespace) -> int:
    env = _base_env()
    return _run_steps(_build_steps(env, profiles=PROFILE_ALL), env)


def command_prepare(_: argparse.Namespace) -> int:
    env = _base_env()
    code = _run_steps(_config_steps(env), env)
    if code != 0:
        return code
    _write_observability_env()
    code = _run_steps(_validation_steps(env), env)
    if code == 0:
        code = _run_steps(_build_steps(env, profiles=PROFILE_ALL), env)
    if code == 0:
        print("\nInfra prepared. Next: make up")
    return code


def command_run(args: argparse.Namespace) -> int:
    env = _base_env()
    snapshot = None if args.no_snapshot else create_snapshot("pre-up")
    if snapshot is not None:
        print(f"Rollback snapshot: {_display_path(snapshot)}")
    code = _run_steps(_config_steps(env), env)
    if code == 0:
        _write_observability_env()
        code = _run_steps([*_validation_steps(env), *_start_steps(env)], env)
    if code != 0:
        if snapshot is not None:
            print(f"Rollback available: make rollback SNAPSHOT={_display_path(snapshot)}")
        return code
    print("\nStack ready.")
    print("Generated config, validated infra, started services, ran smoke, and wrote degraded-state reports.")
    if snapshot is not None:
        print(f"Rollback snapshot retained: {_display_path(snapshot)}")
    return 0


def command_status(_: argparse.Namespace) -> int:
    env = _base_env()
    return _run_steps([Step("show ai-local containers", (*_docker(env), "ps", "--filter", "name=orc-"), "Check Docker daemon.")], env)


def command_logs(args: argparse.Namespace) -> int:
    env = _base_env()
    profiles = _selected_profiles(env)
    command = [*_compose_base(env), *_profile_args(profiles), "logs", "--tail", str(args.tail)]
    if args.follow:
        command.append("-f")
    return _run_steps([Step("show stack logs", tuple(command), "Check Docker daemon and compose profiles.")], env)


def command_down(_: argparse.Namespace) -> int:
    env = _base_env()
    command = [*_compose_base(env), *_profile_args(list(PROFILE_ALL)), "down"]
    return _run_steps([Step("stop ai-local stack", tuple(command), "Check Docker daemon and compose profiles.")], env)


def command_disk_report(_: argparse.Namespace) -> int:
    env = _base_env()
    steps = [
        Step("show Docker disk usage", (*_docker(env), "system", "df", "-v"), "Check Docker daemon access."),
        Step("show BuildKit cache usage", (*_docker(env), "buildx", "du"), "Check Docker BuildKit/buildx support."),
        Step(
            "list local Docker images",
            (
                *_docker(env),
                "image",
                "ls",
                "--format",
                "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}",
            ),
            "Check Docker daemon access.",
        ),
        Step("list containers with writable sizes", (*_docker(env), "ps", "-a", "--size"), "Check Docker daemon access."),
    ]
    return _run_steps(steps, env)


def command_safe_prune(args: argparse.Namespace) -> int:
    env = _base_env()
    operator_env = _operator_env(env)
    cache_max = args.cache_max or operator_env.get("AI_LOCAL_DOCKER_BUILD_CACHE_MAX") or DEFAULT_DOCKER_CACHE_MAX
    print("Safe Docker prune: preserving named and anonymous volumes.")
    steps = [
        Step("remove stopped containers", (*_docker(env), "container", "prune", "-f"), "Check Docker daemon access."),
        Step("remove dangling images", (*_docker(env), "image", "prune", "-f"), "Check Docker daemon access."),
        Step(
            "cap BuildKit cache",
            (*_docker(env), "buildx", "prune", "--all", "--max-used-space", cache_max, "-f"),
            "Check Docker BuildKit/buildx support or rerun with a larger DOCKER_CACHE_MAX.",
        ),
        Step("show Docker disk usage after prune", (*_docker(env), "system", "df"), "Check Docker daemon access."),
    ]
    return _run_steps(steps, env)


def command_rollback(args: argparse.Namespace) -> int:
    snapshot = ROOT / args.snapshot if args.snapshot else latest_snapshot()
    if snapshot is None:
        print("No infra rollback snapshot found.", file=sys.stderr)
        return 2
    if not snapshot.exists():
        print(f"Infra rollback snapshot does not exist: {snapshot}", file=sys.stderr)
        return 2
    restore_snapshot(snapshot)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canonical ai-local infra operations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("config", help="generate secrets and generated env/config artifacts")
    subparsers.add_parser("validate", help="validate storage, compose, Docker policy, and Docker host contracts")
    subparsers.add_parser("build", help="build the mandatory Docker image catalog")
    subparsers.add_parser("prepare", help="generate config, validate infra, and build Docker images without starting services")
    run_parser = subparsers.add_parser("run", help="prepare, start, smoke-test, and report degraded states")
    run_parser.add_argument("--no-snapshot", action="store_true", help="skip generated-artifact rollback snapshot")
    subparsers.add_parser("status", help="show ai-local containers")
    logs_parser = subparsers.add_parser("logs", help="show compose logs for selected profiles")
    logs_parser.add_argument("--follow", action="store_true", help="follow logs")
    logs_parser.add_argument("--tail", type=int, default=80, help="number of log lines")
    subparsers.add_parser("down", help="stop the full ai-local compose stack")
    subparsers.add_parser("disk-report", help="report Docker image, volume, container, and BuildKit disk usage")
    safe_prune_parser = subparsers.add_parser(
        "safe-prune",
        help="prune stopped containers, dangling images, and BuildKit cache without pruning volumes",
    )
    safe_prune_parser.add_argument("--cache-max", default="", help="BuildKit cache cap, e.g. 30gb")
    rollback_parser = subparsers.add_parser("rollback", help="restore generated artifacts from a snapshot")
    rollback_parser.add_argument("--snapshot", default="", help="snapshot path relative to repo root")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    commands = {
        "config": command_config,
        "validate": command_validate,
        "build": command_build,
        "prepare": command_prepare,
        "run": command_run,
        "status": command_status,
        "logs": command_logs,
        "down": command_down,
        "disk-report": command_disk_report,
        "safe-prune": command_safe_prune,
        "rollback": command_rollback,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
