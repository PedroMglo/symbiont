"""Prepare Docker Compose bind-mount source paths before starting services."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .env_compat import read_env_file

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOCKER_CONTEXT = "default"


def _docker_context() -> str:
    return os.environ.get("AI_LOCAL_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or DEFAULT_DOCKER_CONTEXT


def _docker_cmd(*args: str) -> list[str]:
    return ["docker", "--context", _docker_context(), *args]


def _compose_config(env_files: list[str], profiles: list[str]) -> dict[str, Any]:
    cmd = _docker_cmd("compose")
    for env_file in env_files:
        cmd.extend(["--env-file", env_file])
    for profile in profiles:
        cmd.extend(["--profile", profile])
    cmd.append("config")
    proc = subprocess.run(cmd, cwd=ROOT, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "docker compose config failed")
    data = yaml.safe_load(proc.stdout) or {}
    if not isinstance(data, dict):
        raise RuntimeError("docker compose config did not return a mapping")
    return data


def _looks_like_file(path: Path) -> bool:
    return bool(path.suffix)


def _findmnt(path: Path) -> dict[str, str]:
    proc = subprocess.run(
        ["findmnt", "--json", "--target", str(path), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    filesystems = data.get("filesystems") or []
    if not filesystems:
        return {}
    entry = filesystems[-1]
    return entry if isinstance(entry, dict) else {}


def _probe_storage_root(storage_root: Path) -> list[str]:
    errors: list[str] = []
    if not storage_root.exists():
        return [f"storage root is missing: {storage_root}"]
    if not storage_root.is_dir():
        return [f"storage root is not a directory: {storage_root}"]

    mount = _findmnt(storage_root)
    options = set(str(mount.get("options", "")).split(",")) if mount else set()
    if "ro" in options or "shutdown" in options:
        source = mount.get("source", "unknown")
        errors.append(
            f"storage root mount is not writable/healthy: {storage_root} "
            f"(source={source}, options={','.join(sorted(options))})"
        )

    try:
        next(storage_root.iterdir(), None)
    except OSError as exc:
        errors.append(f"storage root is not readable: {storage_root}: {exc}")

    if errors:
        return errors

    probe_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=storage_root,
            prefix=".ai-local-write-probe-",
            delete=False,
        ) as handle:
            probe_path = handle.name
            handle.write("ok\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        errors.append(f"storage root is not writable: {storage_root}: {exc}")
    finally:
        if probe_path:
            try:
                Path(probe_path).unlink()
            except OSError as exc:
                errors.append(f"storage root write probe could not clean up {probe_path}: {exc}")
    return errors


def _prepare_bind_sources(config: dict[str, Any]) -> list[str]:
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
            try:
                source.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(f"{service_name}: cannot create bind directory {source} -> {target}: {exc}")
    return errors


def prepare_bind_sources(config: dict[str, Any], env: dict[str, str] | None = None) -> list[str]:
    errors: list[str] = []
    errors.extend(_prepare_bind_sources(config))
    if env:
        storage_root_raw = env.get("AI_LOCAL_STORAGE_ROOT")
        storage_mode = env.get("AI_LOCAL_STORAGE_MODE")
        if storage_root_raw and storage_mode == "external":
            errors.extend(_probe_storage_root(Path(storage_root_raw).expanduser()))
    return errors


def _read_env_files(paths: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for path in paths:
        env.update(read_env_file(ROOT / path if not Path(path).is_absolute() else Path(path)))
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m config.docker_bind_sources")
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--profile", action="append", default=[])
    args = parser.parse_args(argv)

    try:
        config = _compose_config(args.env_file, args.profile)
        errors = prepare_bind_sources(config, _read_env_files(args.env_file))
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
