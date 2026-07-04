#!/usr/bin/env python3
"""Local host readiness checks for ai-local."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIR = ROOT / ".local" / "generated"
DEFAULT_DOCTOR_REPORT = GENERATED_DIR / "doctor.report.json"
SECRETS = ROOT / "infra" / "docker" / "secrets"
REQUIRED_SECRETS = (
    "orc_api_key",
    "ollama_api_key",
    "rag_api_key",
    "qdrant_api_key",
    "audio_transcribe_api_key",
    "internal_api_key",
    "clickhouse_password",
    "grafana_password",
    "langfuse_db_password",
    "langfuse_nextauth_secret",
    "langfuse_salt",
)
DEFAULT_DOCKER_CONTEXT = "default"
GPU_TEST_IMAGE = "nvidia/cuda:12.4.1-runtime-ubuntu22.04"
SHAREDAI_URL = "https://github.com/PedroMglo/sharedai.git"
GENERATED_TREE_NAMES = {
    ".git",
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".venv",
    "__pycache__",
    "docs/generated",
    "graphify-out",
    "node_modules",
}
PORTABILITY_SCAN_ROOTS = (
    ROOT / "config",
    ROOT / "infra" / "docker" / "compose",
    ROOT / "scripts",
    ROOT / "Makefile",
)
PORTABILITY_EXCLUDE_NAMES = {"__pycache__", ".pytest_cache"}
BOOTSTRAP_COMMANDS = ("git", "make", "curl")
DISK_WARNING_BYTES = 25 * 1024 * 1024 * 1024


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return result.returncode, (result.stdout or result.stderr).strip()


def _docker_context() -> str:
    return os.environ.get("AI_LOCAL_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or DEFAULT_DOCKER_CONTEXT


def _docker_cmd(*args: str) -> list[str]:
    return ["docker", "--context", _docker_context(), *args]


def item(name: str, ok: bool, message: str, *, severity: str = "error", data: Any = None) -> dict[str, Any]:
    return {"name": name, "ok": ok, "severity": severity, "message": message, "data": data}


def _is_generated_path(path: Path) -> bool:
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in GENERATED_TREE_NAMES:
        return True
    return "/".join(parts[:2]) in GENERATED_TREE_NAMES


def _disk_item(name: str, path: Path, *, min_free: int = DISK_WARNING_BYTES) -> dict[str, Any]:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return item(name, False, f"{path}: {exc}", severity="warning")
    free_gb = usage.free / (1024**3)
    min_gb = min_free / (1024**3)
    return item(
        name,
        usage.free >= min_free,
        f"{path}: {free_gb:.1f} GiB free on {probe}; recommended minimum {min_gb:.0f} GiB before builds/models",
        severity="warning",
        data={"path": str(path), "free_bytes": usage.free, "recommended_min_bytes": min_free},
    )


def _parse_systemd_environment(raw: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for token in shlex.split(raw or ""):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        env[key] = value
    return env


def _ollama_gpu_config_check(gpu_visible: bool) -> dict[str, Any]:
    if not gpu_visible:
        return item(
            "ollama-gpu-config",
            True,
            "skipped because no NVIDIA GPU was detected by host or Docker checks",
            severity="info",
        )
    rc, out = _run(["systemctl", "show", "ollama", "-p", "Environment", "--value"], timeout=10)
    if rc != 0:
        return item(
            "ollama-gpu-config",
            True,
            out or "systemd Ollama service not available to inspect",
            severity="warning",
        )
    env = _parse_systemd_environment(out)
    num_gpu = env.get("OLLAMA_NUM_GPU")
    cuda_visible = env.get("CUDA_VISIBLE_DEVICES")
    cpu_only = num_gpu == "0" or ("CUDA_VISIBLE_DEVICES" in env and cuda_visible == "")
    return item(
        "ollama-gpu-config",
        not cpu_only,
        (
            "Ollama service can use GPU"
            if not cpu_only
            else "Ollama service is configured CPU-only; unset empty CUDA_VISIBLE_DEVICES and set OLLAMA_NUM_GPU=-1 or a positive layer count"
        ),
        severity="warning",
        data={
            "OLLAMA_NUM_GPU": num_gpu,
            "CUDA_VISIBLE_DEVICES": cuda_visible,
        },
    )


def check_host() -> list[dict[str, Any]]:
    checks = [
        item("platform", True, platform.platform(), severity="info"),
        item("python", sys.version_info >= (3, 11), platform.python_version()),
        item("git", shutil.which("git") is not None, _run(["git", "--version"])[1]),
    ]
    checks.extend(_monorepo_layout_checks())
    return checks


def _monorepo_layout_checks() -> list[dict[str, Any]]:
    required_dirs = ("config", "infra", "orchestrator", "agents", "features", "obsidian-rag", "storage_guardian")
    checks: list[dict[str, Any]] = []
    checks.append(item(".gitmodules", not (ROOT / ".gitmodules").exists(), "absent" if not (ROOT / ".gitmodules").exists() else "submodule metadata present"))
    for name in required_dirs:
        path = ROOT / name
        checks.append(item(f"component:{name}", path.is_dir(), "present" if path.is_dir() else f"missing {name}/"))
    nested_git = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob(".git")
        if path != ROOT / ".git" and not _is_generated_path(path)
    ]
    checks.append(item("nested-git", not nested_git, "none" if not nested_git else ", ".join(nested_git)))
    infra_dirs = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("infra")
        if path.is_dir() and path != ROOT / "infra" and not _is_generated_path(path)
    ]
    checks.append(item("single-infra", not infra_dirs, "only infra/" if not infra_dirs else ", ".join(infra_dirs)))
    return checks


def check_bootstrap() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for command in BOOTSTRAP_COMMANDS:
        found = shutil.which(command)
        checks.append(item(command, found is not None, found or f"{command} not found"))
    rc, out = _run([sys.executable, "-m", "venv", "--help"], timeout=10)
    checks.append(item("python-venv", rc == 0, "python venv module available" if rc == 0 else out))
    if shutil.which("git"):
        rc, out = _run(["git", "ls-remote", "--heads", SHAREDAI_URL, "main"], timeout=30)
        checks.append(
            item(
                "sharedai-public",
                rc == 0 and "refs/heads/main" in out,
                "public sharedai main reachable" if rc == 0 else (out or "sharedai check failed"),
            )
        )
    else:
        checks.append(item("sharedai-public", False, "git not available"))
    checks.append(item("ollama-cli", shutil.which("ollama") is not None, shutil.which("ollama") or "install Ollama before running make models", severity="warning"))
    return checks


def check_disk() -> list[dict[str, Any]]:
    checks = [
        _disk_item("repo-free-space", ROOT),
        _disk_item("local-storage-free-space", ROOT / ".local"),
        _disk_item("hf-cache-free-space", Path(os.environ.get("HF_CACHE_DIR", ROOT / ".local" / "data" / "cache" / "hf")).expanduser()),
        _disk_item("ollama-models-free-space", Path(os.environ.get("OLLAMA_MODELS", "~/.ollama/models")).expanduser()),
    ]
    if shutil.which("docker"):
        rc, out = _run(_docker_cmd("info", "--format", "{{.DockerRootDir}}"), timeout=10)
        if rc == 0 and out:
            checks.append(_disk_item("docker-root-free-space", Path(out).expanduser()))
        else:
            checks.append(item("docker-root-free-space", False, out or "docker root unavailable", severity="warning"))
    return checks


def check_docker() -> list[dict[str, Any]]:
    checks = []
    docker = shutil.which("docker")
    checks.append(item("docker-cli", docker is not None, docker or "docker not found"))
    rc, active = _run(["docker", "context", "show"], timeout=10)
    context = _docker_context()
    checks.append(item("docker-context", rc == 0, f"project={context}; active={active or 'unknown'}", severity="info"))
    if rc == 0 and active and active != context:
        checks.append(
            item(
                "docker-context-active",
                False,
                f"active context is {active}; project targets {context} for deterministic local/GPU runs",
                severity="warning",
            )
        )
    rc, out = _run(_docker_cmd("compose", "version"), timeout=10)
    checks.append(item("docker-compose", rc == 0, out or "docker compose unavailable"))
    rc, out = _run(_docker_cmd("info", "--format", "{{.ServerVersion}}"), timeout=10)
    if rc != 0:
        rc, out = _run(_docker_cmd("version", "--format", "{{.Server.Version}}"), timeout=10)
    checks.append(item("docker-daemon", rc == 0, f"running {out}" if rc == 0 else out, severity="warning"))
    return checks


def check_gpu() -> list[dict[str, Any]]:
    checks = []
    rc, out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], timeout=10)
    host_gpu_ok = rc == 0
    checks.append(item("nvidia-gpu-host", host_gpu_ok, out or "host nvidia-smi unavailable; Docker runtime may still work", severity="warning"))

    rc, info = _run(_docker_cmd("info"), timeout=10)
    has_nvidia_runtime = " nvidia " in f" {info} " or "nvidia" in info.lower()
    has_cdi_gpu = "nvidia.com/gpu" in info
    checks.append(
        item(
            "nvidia-docker-runtime",
            rc == 0 and (has_nvidia_runtime or has_cdi_gpu),
            "runtime/cdi detected" if rc == 0 and (has_nvidia_runtime or has_cdi_gpu) else (info or "docker info unavailable"),
            severity="warning",
        )
    )

    image = os.environ.get("AI_LOCAL_GPU_TEST_IMAGE", GPU_TEST_IMAGE)
    rc, out = _run(
        _docker_cmd(
            "run",
            "--rm",
            "--pull",
            "never",
            "--gpus",
            "all",
            "--entrypoint",
            "nvidia-smi",
            image,
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader",
        ),
        timeout=45,
    )
    checks.append(
        item(
            "nvidia-gpu-docker",
            rc == 0,
            out or f"Docker GPU preflight failed with {image}; ensure the image exists locally and context {_docker_context()} has NVIDIA access",
            severity="warning",
        )
    )
    checks.append(_ollama_gpu_config_check(host_gpu_ok or rc == 0))
    return checks


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"')
    return env


def check_storage() -> list[dict[str, Any]]:
    env = _read_env(ROOT / ".env.storage.generated")
    checks = [item("storage-env", bool(env), ".env.storage.generated present" if env else "run make infra", severity="warning")]
    storage_root_raw = env.get("AI_LOCAL_STORAGE_ROOT", "")
    storage_root = Path(storage_root_raw).expanduser() if storage_root_raw else None
    storage_root_exists = bool(storage_root and storage_root.exists())
    for key in ("AI_LOCAL_STORAGE_ROOT", "QDRANT_DATA_DIR", "RAG_DATA_DIR", "LLM_MODELS_DIR", "HF_CACHE_DIR"):
        value = env.get(key)
        if not value:
            checks.append(item(key, False, "not resolved", severity="warning"))
            continue
        path = Path(value).expanduser()
        if key == "AI_LOCAL_STORAGE_ROOT":
            checks.append(item(key, path.exists(), str(path), severity="warning"))
            continue
        declared_under_root = bool(storage_root and path.is_relative_to(storage_root))
        if path.exists():
            checks.append(item(key, True, str(path), severity="warning"))
        elif storage_root_exists and declared_under_root:
            checks.append(item(key, True, f"declared; created on first use: {path}", severity="warning"))
        else:
            checks.append(item(key, False, str(path), severity="warning"))
    return checks


def check_secrets() -> list[dict[str, Any]]:
    checks = [item("secrets-dir", SECRETS.exists(), str(SECRETS))]
    for name in REQUIRED_SECRETS:
        path = SECRETS / name
        ok = path.exists() and path.stat().st_size > 0 if path.exists() else False
        mode_ok = (path.stat().st_mode & 0o777) == 0o600 if path.exists() else False
        checks.append(item(name, ok and mode_ok, "present mode=600" if ok and mode_ok else "missing/empty/wrong mode"))
    return checks


def _iter_portability_files() -> list[Path]:
    files: list[Path] = []
    for root in PORTABILITY_SCAN_ROOTS:
        if root.is_file():
            files.append(root)
            continue
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if any(part in PORTABILITY_EXCLUDE_NAMES for part in path.parts):
                continue
            if path.is_file() and path.suffix in {".py", ".sh", ".yml", ".yaml", ".toml", ".env", ".example", ""}:
                files.append(path)
    return sorted(set(files))


def check_portability() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    project_path = str(ROOT)
    hardcoded_hits: list[str] = []
    for path in _iter_portability_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if project_path in text:
            hardcoded_hits.append(str(path.relative_to(ROOT)))
    checks.append(
        item(
            "project-root-hardcode",
            not hardcoded_hits,
            "none found" if not hardcoded_hits else ", ".join(hardcoded_hits[:8]),
            severity="warning",
            data=hardcoded_hits,
        )
    )
    services_env = _read_env(ROOT / ".env.services.generated")
    checks.append(
        item(
            "host-project-root-env",
            bool(services_env.get("AI_LOCAL_HOST_PROJECT_ROOT")),
            services_env.get("AI_LOCAL_HOST_PROJECT_ROOT", "run make infra"),
            severity="warning",
        )
    )
    checks.append(
        item(
            "docker-context-default",
            _docker_context() == DEFAULT_DOCKER_CONTEXT,
            f"project targets {_docker_context()}",
            severity="warning",
        )
    )
    return checks


def run(section: str) -> dict[str, Any]:
    groups = {
        "bootstrap": check_bootstrap,
        "host": check_host,
        "docker": check_docker,
        "disk": check_disk,
        "gpu": check_gpu,
        "storage": check_storage,
        "secrets": check_secrets,
        "portability": check_portability,
    }
    selected = groups if section == "all" else {section: groups[section]}
    payload = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "section": section,
        "checks": {name: fn() for name, fn in selected.items()},
    }
    payload["ok"] = all(
        check["ok"] or check["severity"] in {"warning", "info"}
        for checks in payload["checks"].values()
        for check in checks
    )
    return payload


def write_report(payload: dict[str, Any], output_path: Path = DEFAULT_DOCTOR_REPORT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--section",
        choices=("all", "bootstrap", "host", "docker", "disk", "gpu", "storage", "secrets", "portability"),
        default="all",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-report", nargs="?", const=str(DEFAULT_DOCTOR_REPORT), metavar="PATH")
    args = parser.parse_args(argv)
    payload = run(args.section)
    if args.write_report:
        write_report(payload, Path(args.write_report))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for group, checks in payload["checks"].items():
            print(f"== {group} ==")
            for check in checks:
                marker = "OK" if check["ok"] else ("WARN" if check["severity"] == "warning" else "FAIL")
                print(f"{marker:4} {check['name']}: {check['message']}")
        if args.write_report:
            print(f"Generated: {args.write_report}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
