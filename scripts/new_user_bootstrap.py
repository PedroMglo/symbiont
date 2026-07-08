#!/usr/bin/env python3
"""Bootstrap preflight for a new ai-local Linux user.

The script diagnoses host prerequisites, can install distro-specific system
packages when explicitly requested, and validates the mono-repo layout.
"""

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
from typing import Any, Dict, List, Optional, Tuple

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
DOCKER_COMPOSE_VERSION = "v2.40.3"
SUPPORTED_PYTHON_COMMANDS = ("python3.13", "python3.12", "python3.11", "python3", "python")


def _run(cmd: List[str], *, cwd: Path = ROOT, timeout: int = 20) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return result.returncode, (result.stdout or result.stderr).strip()


def _format_command(cmd: List[str], env: Optional[Dict[str, str]] = None) -> str:
    prefix = ""
    if env:
        prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(env.items())) + " "
    return prefix + " ".join(shlex.quote(part) for part in cmd)


def _run_live(cmd: List[str], env: Optional[Dict[str, str]] = None) -> None:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    print("+ " + _format_command(cmd, env))
    result = subprocess.run(cmd, env=merged_env, check=False)
    if result.returncode != 0:
        raise SystemExit(f"system install command failed ({result.returncode}): {_format_command(cmd, env)}")


def _sudo_prefix() -> List[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo]
    raise SystemExit("Installing system prerequisites requires root or sudo.")


def _systemd_available() -> bool:
    return bool(shutil.which("systemctl")) and Path("/run/systemd/system").exists()


def _docker_info_ok() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    rc, _ = _run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=10)
    return rc == 0


def _docker_compose_ok() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    rc, _ = _run([docker, "compose", "version"], timeout=10)
    return rc == 0


def _find_supported_python() -> Optional[Tuple[str, str]]:
    script = (
        "import sys; "
        "print('%d.%d' % (sys.version_info[0], sys.version_info[1])); "
        "raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    )
    for candidate in SUPPORTED_PYTHON_COMMANDS:
        path = shutil.which(candidate)
        if not path:
            continue
        rc, out = _run([path, "-c", script], timeout=10)
        if rc == 0:
            return path, out.splitlines()[0] if out else candidate
    return None


def _required_host_ready() -> bool:
    if not _find_supported_python():
        return False
    for command in ("git", "make", "curl"):
        if not shutil.which(command):
            return False
    python = _find_supported_python()
    if not python:
        return False
    rc, _ = _run([python[0], "-m", "venv", "--help"], timeout=10)
    return rc == 0 and _docker_compose_ok() and _docker_info_ok()


def item(name: str, ok: bool, message: str, *, severity: str = "error", data: Any = None) -> Dict[str, Any]:
    return {"name": name, "ok": ok, "message": message, "severity": severity, "data": data}


def _os_release() -> Dict[str, str]:
    path = Path("/etc/os-release")
    if not path.exists():
        return {}
    data = {}  # type: Dict[str, str]
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip('"')
    return data


def _compose_plugin_install_plan() -> str:
    return "\n".join(
        [
            'compose_arch="$(uname -m)"',
            'case "$compose_arch" in x86_64|amd64) compose_arch="x86_64" ;; aarch64|arm64) compose_arch="aarch64" ;; *) echo "Unsupported Docker Compose architecture: $compose_arch" >&2; exit 2 ;; esac',
            "sudo mkdir -p /usr/local/lib/docker/cli-plugins",
            f'sudo curl -fsSL "https://github.com/docker/compose/releases/download/{DOCKER_COMPOSE_VERSION}/docker-compose-linux-${{compose_arch}}" -o /usr/local/lib/docker/cli-plugins/docker-compose',
            "sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose",
        ]
    )


def _install_compose_plugin(prefix: List[str]) -> None:
    if _docker_compose_ok():
        print("Docker Compose v2 already available.")
        return
    arch = platform.machine().lower()
    if arch in {"x86_64", "amd64"}:
        compose_arch = "x86_64"
    elif arch in {"aarch64", "arm64"}:
        compose_arch = "aarch64"
    else:
        raise SystemExit(f"Unsupported Docker Compose architecture: {arch}")
    destination = "/usr/local/lib/docker/cli-plugins/docker-compose"
    url = f"https://github.com/docker/compose/releases/download/{DOCKER_COMPOSE_VERSION}/docker-compose-linux-{compose_arch}"
    _run_live(prefix + ["mkdir", "-p", "/usr/local/lib/docker/cli-plugins"])
    _run_live(prefix + ["curl", "-fsSL", url, "-o", destination])
    _run_live(prefix + ["chmod", "+x", destination])


def _enable_docker(prefix: List[str]) -> None:
    if _docker_info_ok():
        print("Docker daemon already reachable.")
        return
    if _systemd_available():
        _run_live(prefix + ["systemctl", "enable", "--now", "docker"])
        return
    print("Docker daemon is not reachable and systemd is not active; start Docker for this distro before running make up.")


def _dnf_add_repo(prefix: List[str], repo_url: str) -> None:
    dnf = shutil.which("dnf")
    if not dnf:
        raise SystemExit("dnf is required for this distro.")
    rc, help_text = _run([dnf, "config-manager", "--help"], timeout=20)
    if rc == 0 and "addrepo" in help_text:
        _run_live(prefix + [dnf, "config-manager", "addrepo", f"--from-repofile={repo_url}"])
        return
    _run_live(prefix + [dnf, "install", "-y", "dnf-plugins-core"])
    _run_live(prefix + [dnf, "config-manager", "--add-repo", repo_url])


def _install_hint(os_release: Dict[str, str]) -> str:
    distro_id = os_release.get("ID", "").lower()
    like = os_release.get("ID_LIKE", "").lower()
    if distro_id in {"ubuntu", "debian"} or "debian" in like:
        return "\n".join(
            [
                "sudo apt-get update && sudo apt-get install -y git make curl ca-certificates python3 python3-venv python3-pip docker.io",
                _compose_plugin_install_plan(),
                "sudo systemctl enable --now docker",
            ]
        )
    if distro_id in {"fedora"} or "fedora" in like:
        return "\n".join(
            [
                "sudo dnf install -y git make curl ca-certificates python3 python3-pip docker-cli",
                _compose_plugin_install_plan(),
                "# Install/start Docker Engine for your Fedora version if docker info still fails: https://docs.docker.com/engine/install/fedora/",
            ]
        )
    if distro_id in {"rhel", "centos", "rocky", "almalinux"} or "rhel" in like:
        return "\n".join(
            [
                "sudo dnf install -y git make curl ca-certificates python3.11 python3.11-pip",
                "sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo",
                "sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
                "sudo systemctl enable --now docker",
            ]
        )
    if distro_id in {"arch", "manjaro"} or "arch" in like:
        return "sudo pacman -S --needed git make curl python python-pip docker docker-compose"
    if distro_id in {"opensuse-leap", "opensuse-tumbleweed", "sles"} or "suse" in like:
        return "openSUSE/SLES are not supported by ai-local setup-system because their distro Compose packages lag behind the required modern Docker Compose stack. Use Ubuntu, Debian, Fedora or Arch."
    return "Install git, make, curl, Python 3.11+, Docker Engine and modern Docker Compose v2 for your distro."


def install_system_prereqs(os_release: Dict[str, str]) -> List[Dict[str, Any]]:
    print("== ai-local system prerequisite install ==")
    print(_system_install_plan(os_release))
    distro_id = os_release.get("ID", "").lower()
    like = os_release.get("ID_LIKE", "").lower()
    if distro_id in {"opensuse-leap", "opensuse-tumbleweed", "sles"} or "suse" in like:
        raise SystemExit(_install_hint(os_release))
    if _required_host_ready():
        print("System prerequisites already satisfied; no package install needed.")
        return [item("system-prereqs", True, "already satisfied", severity="info")]

    prefix = _sudo_prefix()
    if distro_id in {"ubuntu", "debian"} or "debian" in like:
        env = {"DEBIAN_FRONTEND": "noninteractive"}
        _run_live(prefix + ["apt-get", "update"], env=env)
        _run_live(
            prefix
            + [
                "apt-get",
                "install",
                "-y",
                "git",
                "make",
                "curl",
                "ca-certificates",
                "python3",
                "python3-venv",
                "python3-pip",
                "docker.io",
            ],
            env=env,
        )
        _install_compose_plugin(prefix)
        _enable_docker(prefix)
    elif distro_id in {"fedora"} or "fedora" in like:
        dnf = shutil.which("dnf") or "dnf"
        _run_live(prefix + [dnf, "install", "-y", "git", "make", "curl", "ca-certificates", "python3", "python3-pip"])
        if not _docker_info_ok():
            _dnf_add_repo(prefix, "https://download.docker.com/linux/fedora/docker-ce.repo")
            _run_live(prefix + [dnf, "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin"])
        elif not shutil.which("docker"):
            _run_live(prefix + [dnf, "install", "-y", "docker-cli"])
        _install_compose_plugin(prefix)
        _enable_docker(prefix)
    elif distro_id in {"rhel", "centos", "rocky", "almalinux"} or "rhel" in like:
        dnf = shutil.which("dnf") or "dnf"
        _run_live(prefix + [dnf, "install", "-y", "git", "make", "curl", "ca-certificates", "python3.11", "python3.11-pip"])
        if not _docker_info_ok():
            _dnf_add_repo(prefix, "https://download.docker.com/linux/centos/docker-ce.repo")
            _run_live(prefix + [dnf, "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin"])
        _install_compose_plugin(prefix)
        _enable_docker(prefix)
    elif distro_id in {"arch", "manjaro"} or "arch" in like:
        _run_live(prefix + ["pacman", "-Sy", "--noconfirm", "--needed", "git", "make", "curl", "python", "python-pip", "docker", "docker-compose"])
        _enable_docker(prefix)
    else:
        raise SystemExit("Unsupported Linux distro for automatic setup-system install. Install these manually:\n" + _system_install_plan(os_release))

    return [item("system-prereqs", True, "install command completed", severity="info")]


def _ollama_install_hint() -> str:
    return "Install Ollama from https://ollama.com/download/linux or your distro/package manager before running make models."


def _system_install_plan(os_release: Dict[str, str]) -> str:
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


def _disk_item(name: str, path: Path, *, min_free: int = DISK_WARNING_BYTES) -> Dict[str, Any]:
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


def check_host_prereqs() -> List[Dict[str, Any]]:
    checks = [item("platform", sys.platform.startswith("linux"), platform.platform(), severity="error")]
    for command in ("git", "make", "curl"):
        found = shutil.which(command)
        checks.append(item(command, found is not None, found or f"{command} not found"))
    python = _find_supported_python()
    if python:
        checks.append(item("python", True, f"{python[1]} ({python[0]})"))
        rc, out = _run([python[0], "-m", "venv", "--help"], timeout=10)
        checks.append(item("python-venv", rc == 0, "python venv module available" if rc == 0 else out))
    else:
        checks.append(item("python", False, f"Python 3.11+ not found; current interpreter is {platform.python_version()}"))
        checks.append(item("python-venv", False, "Python 3.11+ venv module unavailable"))
    docker = shutil.which("docker")
    checks.append(item("docker-cli", docker is not None, docker or "docker not found"))
    if docker:
        rc, out = _run(["docker", "compose", "version"], timeout=10)
        checks.append(item("docker-compose", rc == 0, out or "docker compose unavailable"))
        rc, out = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=10)
        checks.append(item("docker-daemon", rc == 0, f"running {out}" if rc == 0 else out))
    checks.append(item("ollama-cli", shutil.which("ollama") is not None, shutil.which("ollama") or _ollama_install_hint(), severity="warning"))
    return checks


def check_disk_space() -> List[Dict[str, Any]]:
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


def check_monorepo_layout() -> List[Dict[str, Any]]:
    required_dirs = ("config", "infra", "orchestrator", "agents", "features", "obsidian-rag", "storage_guardian")
    checks = [
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


def check_sharedai() -> List[Dict[str, Any]]:
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


def build_payload() -> Dict[str, Any]:
    os_release = _os_release()
    checks = {
        "host": check_host_prereqs(),
        "workspace": check_monorepo_layout(),
        "disk": check_disk_space(),
        "sharedai": check_sharedai(),
    }
    actions = []  # type: List[Dict[str, Any]]
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


def write_report(payload: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_text(payload: Dict[str, Any]) -> None:
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print-install-command", action="store_true", help="Print distro-specific system prerequisite commands and exit.")
    parser.add_argument("--install-system", action="store_true", help="Print and execute distro-specific system prerequisite installation.")
    parser.add_argument("--write-report", nargs="?", const=str(DEFAULT_REPORT), metavar="PATH")
    args = parser.parse_args(argv)

    payload = build_payload()
    if args.print_install_command:
        print(payload["system_install_plan"])
        return 0
    if args.install_system:
        actions = install_system_prereqs(payload["distro"])
        payload = build_payload()
        payload["actions"] = actions
        all_checks = [check for group in payload["checks"].values() for check in group]
        payload["ok"] = all(check["ok"] or check["severity"] in {"warning", "info"} for check in all_checks + actions)
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
