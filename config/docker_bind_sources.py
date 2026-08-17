"""Validate Docker Compose bind-mount source paths before starting services."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from config import docker_projects
from config.env_files import EnvFileError, read_env_file_strict

ROOT = Path(__file__).resolve().parent.parent


def _docker_cmd(*args: str) -> list[str]:
    return ["docker", "--context", docker_projects.resolve_docker_context(), *args]


def _compose_config(env_files: list[str], profiles: list[str]) -> dict[str, Any]:
    project = docker_projects.load_docker_project_config()
    workdir = (ROOT / project.workdir).resolve()
    expected_env_files = tuple((ROOT / path).resolve() for path in project.env_files)
    requested_env_files = tuple(
        (ROOT / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        for path in env_files
    )
    if requested_env_files and requested_env_files != expected_env_files:
        raise RuntimeError(
            "Compose bind validation env files must exactly match the typed "
            "Docker project order"
        )
    selected_env_files = requested_env_files or expected_env_files
    try:
        selected_profiles = (
            docker_projects.normalize_runtime_profiles(profiles, project=project)
            if profiles
            else docker_projects.resolve_compose_profiles(project=project)
        )
        runtime_env = docker_projects.sanitize_compose_operator_env(os.environ)
    except docker_projects.DockerProjectConfigError as exc:
        raise RuntimeError(str(exc)) from exc
    cmd = [
        *_docker_cmd("compose"),
        "--project-name",
        project.name,
        "--project-directory",
        str(workdir),
    ]
    for compose_file in project.files:
        cmd.extend(["--file", str((workdir / compose_file).resolve())])
    try:
        for env_file in selected_env_files:
            cmd.extend(["--env-file", str(env_file)])
            runtime_env.update(read_env_file_strict(env_file))
    except EnvFileError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        runtime_env = docker_projects.sanitize_compose_operator_env(runtime_env)
    except docker_projects.DockerProjectConfigError as exc:
        raise RuntimeError(str(exc)) from exc
    for profile in selected_profiles:
        cmd.extend(["--profile", profile])
    cmd.append("config")
    proc = subprocess.run(
        cmd,
        cwd=workdir,
        env=runtime_env,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "docker compose config failed")
    data = yaml.safe_load(proc.stdout) or {}
    if not isinstance(data, dict):
        raise RuntimeError("docker compose config did not return a mapping")
    return data


def _looks_like_file(path: Path) -> bool:
    return bool(path.suffix)


def _validate_bind_sources(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    services = config.get("services", {})
    if not isinstance(services, dict):
        return errors

    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        volumes = service.get("volumes") or []
        if not isinstance(volumes, list):
            continue
        for volume in volumes:
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                continue
            source_raw = volume.get("source")
            target = str(volume.get("target") or "")
            if not source_raw:
                continue
            source = Path(str(source_raw)).expanduser()
            if not source.is_absolute():
                source = (ROOT / source).resolve()
            if source.exists():
                continue
            if _looks_like_file(source):
                errors.append(f"{service_name}: missing bind file {source} -> {target}")
                continue
            errors.append(f"{service_name}: missing bind directory {source} -> {target}")
    return errors


def validate_bind_sources(config: dict[str, Any]) -> list[str]:
    """Report missing sources without materializing or probe-writing storage."""
    return _validate_bind_sources(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m config.docker_bind_sources")
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--profile", action="append", default=[])
    args = parser.parse_args(argv)

    try:
        config = _compose_config(args.env_file, args.profile)
        errors = validate_bind_sources(config)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("OK: Docker bind sources are present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
