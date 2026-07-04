#!/usr/bin/env python3
"""Bootstrap preflight for a new ai-local Linux user.

The script is intentionally conservative: it diagnoses host prerequisites,
prints distro-specific install hints, and validates the mono-repo layout.
It does not install system packages by itself.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = ROOT / ".local" / "generated" / "bootstrap.report.json"
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
DISK_WARNING_BYTES = 25 * 1024 * 1024 * 1024


def _run(cmd: list[str], *, cwd: Path = ROOT, timeout: int = 20) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return result.returncode, (result.stdout or result.stderr).strip()


def item(name: str, ok: bool, message: str, *, severity: str = "error", data: Any = None) -> dict[str, Any]:
    return {"name": name, "ok": ok, "message": message, "severity": severity, "data": data}


def _os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip('"')
    return data


def _install_hint(os_release: dict[str, str]) -> str:
    distro_id = os_release.get("ID", "").lower()
    like = os_release.get("ID_LIKE", "").lower()
    if distro_id in {"ubuntu", "debian"} or "debian" in like:
        return (
            "sudo apt-get update && sudo apt-get install -y "
            "git make curl python3 python3-venv python3-pip docker.io docker-compose-plugin"
        )
    if distro_id in {"fedora"} or "fedora" in like:
        return "sudo dnf install -y git make curl python3 python3-pip docker docker-compose-plugin"
    if distro_id in {"rhel", "centos", "rocky", "almalinux"} or "rhel" in like:
        return "sudo dnf install -y git make curl python3 python3-pip docker docker-compose-plugin"
    if distro_id in {"arch", "manjaro"} or "arch" in like:
        return "sudo pacman -S --needed git make curl python python-pip docker docker-compose"
    if distro_id in {"opensuse-leap", "opensuse-tumbleweed", "sles"} or "suse" in like:
        return "sudo zypper install -y git make curl python3 python3-pip docker docker-compose"
    return "Install git, make, curl, Python 3.11+, Docker Engine and the Docker Compose plugin for your distro."


def _ollama_install_hint() -> str:
    return "Install Ollama from https://ollama.com/download/linux or your distro/package manager before running make models."


def _system_install_plan(os_release: dict[str, str]) -> str:
    return f"{_install_hint(os_release)}\n# Then: {_ollama_install_hint()}"


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


def check_host_prereqs() -> list[dict[str, Any]]:
    checks = [item("platform", sys.platform.startswith("linux"), platform.platform(), severity="error")]
    for command in ("git", "make", "curl"):
        found = shutil.which(command)
        checks.append(item(command, found is not None, found or f"{command} not found"))
    python_ok = sys.version_info >= (3, 11)
    checks.append(item("python", python_ok, platform.python_version()))
    rc, out = _run([sys.executable, "-m", "venv", "--help"], timeout=10)
    checks.append(item("python-venv", rc == 0, "python venv module available" if rc == 0 else out))
    docker = shutil.which("docker")
    checks.append(item("docker-cli", docker is not None, docker or "docker not found"))
    if docker:
        rc, out = _run(["docker", "compose", "version"], timeout=10)
        checks.append(item("docker-compose", rc == 0, out or "docker compose unavailable"))
        rc, out = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=10)
        checks.append(item("docker-daemon", rc == 0, f"running {out}" if rc == 0 else out))
    checks.append(item("ollama-cli", shutil.which("ollama") is not None, shutil.which("ollama") or _ollama_install_hint(), severity="warning"))
    return checks


def check_disk_space() -> list[dict[str, Any]]:
    checks = [
        _disk_item("repo-free-space", ROOT),
        _disk_item("local-storage-free-space", ROOT / ".local"),
        _disk_item("hf-cache-free-space", Path(os.environ.get("HF_CACHE_DIR", ROOT / ".local" / "data" / "cache" / "hf")).expanduser()),
        _disk_item("ollama-models-free-space", Path(os.environ.get("OLLAMA_MODELS", "~/.ollama/models")).expanduser()),
    ]
    docker = shutil.which("docker")
    if docker:
        rc, out = _run(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=10)
        if rc == 0 and out:
            checks.append(_disk_item("docker-root-free-space", Path(out).expanduser()))
        else:
            checks.append(item("docker-root-free-space", False, out or "docker root unavailable", severity="warning"))
    return checks


def check_monorepo_layout() -> list[dict[str, Any]]:
    required_dirs = ("config", "infra", "orchestrator", "agents", "features", "obsidian-rag", "storage_guardian")
    checks: list[dict[str, Any]] = [
        item(".gitmodules", not (ROOT / ".gitmodules").exists(), "absent" if not (ROOT / ".gitmodules").exists() else "remove submodule metadata"),
    ]
    for name in required_dirs:
        path = ROOT / name
        checks.append(item(f"component:{name}", path.is_dir(), "present" if path.is_dir() else f"missing {name}/"))
    nested_git = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob(".git")
        if path != ROOT / ".git" and not _is_generated_path(path)
    ]
    checks.append(
        item(
            "nested-git",
            not nested_git,
            "none" if not nested_git else "nested Git metadata found: " + ", ".join(nested_git),
        )
    )
    infra_dirs = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("infra")
        if path.is_dir() and path != ROOT / "infra" and not _is_generated_path(path)
    ]
    checks.append(
        item(
            "single-infra",
            not infra_dirs,
            "only infra/" if not infra_dirs else "extra infra dirs found: " + ", ".join(infra_dirs),
        )
    )
    return checks


def check_sharedai() -> list[dict[str, Any]]:
    if not shutil.which("git"):
        return [item("sharedai-public", False, "git not available")]
    rc, out = _run(["git", "ls-remote", "--heads", SHAREDAI_URL, "main"], timeout=30)
    return [
        item(
            "sharedai-public",
            rc == 0 and "refs/heads/main" in out,
            "public sharedai main reachable" if rc == 0 else (out or "sharedai check failed"),
        )
    ]


def build_payload() -> dict[str, Any]:
    os_release = _os_release()
    checks = {
        "host": check_host_prereqs(),
        "workspace": check_monorepo_layout(),
        "disk": check_disk_space(),
        "sharedai": check_sharedai(),
    }
    actions: list[dict[str, Any]] = []
    all_checks = [check for group in checks.values() for check in group]
    payload = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "distro": os_release,
        "install_hint": _install_hint(os_release),
        "ollama_install_hint": _ollama_install_hint(),
        "system_install_plan": _system_install_plan(os_release),
        "actions": actions,
        "checks": checks,
    }
    payload["ok"] = all(check["ok"] or check["severity"] in {"warning", "info"} for check in all_checks + actions)
    return payload


def write_report(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_text(payload: dict[str, Any]) -> None:
    print("== ai-local bootstrap preflight ==")
    print(f"Platform: {payload['platform']}")
    print(f"Install hint: {payload['install_hint']}")
    print(f"Ollama hint: {payload['ollama_install_hint']}")
    for group, checks in payload["checks"].items():
        print(f"\n== {group} ==")
        for check in checks:
            marker = "OK" if check["ok"] else ("WARN" if check["severity"] == "warning" else "FAIL")
            print(f"{marker:4} {check['name']}: {check['message']}")
    if payload["actions"]:
        print("\n== actions ==")
        for action in payload["actions"]:
            marker = "OK" if action["ok"] else "FAIL"
            print(f"{marker:4} {action['name']}: {action['message']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print-install-command", action="store_true", help="Print distro-specific system prerequisite commands and exit.")
    parser.add_argument("--write-report", nargs="?", const=str(DEFAULT_REPORT), metavar="PATH")
    args = parser.parse_args(argv)

    payload = build_payload()
    if args.print_install_command:
        print(payload["system_install_plan"])
        return 0
    if args.write_report:
        write_report(payload, Path(args.write_report))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_text(payload)
        if args.write_report:
            print(f"\nGenerated: {args.write_report}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
