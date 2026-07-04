#!/usr/bin/env python3
"""End-to-end readiness summary for a new ai-local install."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = ROOT / ".local" / "generated" / "verify.report.json"
_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE: dict[str, str] = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text

REQUIRED_USER_CONTAINERS = ("orc-symbiont", "orc-rag", "orc-qdrant", "orc-storage-guardian", "orc-ollama-proxy")
MAX_PROFILE_CONTAINERS = {
    "agents": (
        "orc-reasoning-and-response",
        "orc-material-builder",
        "orc-local-evidence-operator",
        "orc-execution-policy-operator",
    ),
    "features": (
        "orc-research",
        "orc-workspace-execution",
        "orc-material-execution-kernel",
        "orc-personal-context",
        "orc-extrator",
    ),
    "material": (
        "orc-material-builder",
        "orc-material-execution-kernel",
        "orc-workspace-execution",
        "orc-execution-policy-operator",
    ),
    "llm": ("orc-llama-cpp-aux", "orc-llama-cpp-fast"),
    "gpu": ("orc-vllm",),
    "observability": ("orc-clickhouse", "orc-grafana", "orc-otel-collector", "orc-langfuse"),
}


def _run(cmd: list[str], *, timeout: int = 20) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return result.returncode, (result.stdout or result.stderr).strip()


def _http_get(url: str, *, timeout: int = 10) -> tuple[int, str]:
    if not shutil.which("curl"):
        return 0, "curl not found"
    try:
        result = subprocess.run(
            ["curl", "-ksS", "--max-time", str(timeout), "-w", "\n__HTTP_STATUS__:%{http_code}", url],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except subprocess.TimeoutExpired:
        return 0, "timeout"
    output = (result.stdout or result.stderr).strip()
    marker = "\n__HTTP_STATUS__:"
    if marker not in output:
        return 0, output
    body, raw_status = output.rsplit(marker, 1)
    try:
        status = int(raw_status.strip())
    except ValueError:
        status = 0
    return status, body.strip()[:2048]


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


def item(name: str, ok: bool, message: str, *, severity: str = "error", data: Any = None) -> dict[str, Any]:
    return {"name": name, "ok": ok, "message": message, "severity": severity, "data": data}


def check_generated_env() -> list[dict[str, Any]]:
    checks = []
    for name in (".env.storage.generated", ".env.llm.generated", ".env.services.generated", ".env.docker.resources.generated"):
        path = ROOT / name
        checks.append(item(name, path.exists() and path.stat().st_size > 0, "present" if path.exists() else "run make infra"))
    return checks


def check_alias() -> list[dict[str, Any]]:
    path = shutil.which("@") or str(Path.home() / ".local" / "bin" / "@")
    alias_path = Path(path)
    return [item("alias-@", alias_path.exists(), str(alias_path) if alias_path.exists() else "run make aliases")]


def check_alias_smoke() -> list[dict[str, Any]]:
    path = shutil.which("@") or str(Path.home() / ".local" / "bin" / "@")
    alias_path = Path(path)
    if not alias_path.exists():
        return [item("alias-smoke", False, "alias @ not installed; run make setup")]
    rc, out = _run(
        [
            str(alias_path),
            "--raw",
            "--no-stream",
            "--no-agentic",
            "--new-session",
            _prompt("alias_smoke.md"),
        ],
        timeout=180,
    )
    clean = out.strip()
    return [
        item(
            "alias-smoke",
            rc == 0 and bool(clean),
            clean[:240] if clean else "empty alias response",
            severity="error",
        )
    ]


def _container_statuses() -> dict[str, str]:
    if not shutil.which("docker"):
        return {}
    context = os.environ.get("AI_LOCAL_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or "default"
    rc, out = _run(
        ["docker", "--context", context, "ps", "-a", "--filter", "name=orc-", "--format", "{{.Names}}|{{.Status}}"],
        timeout=20,
    )
    if rc != 0:
        return {}
    statuses: dict[str, str] = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, status = line.split("|", 1)
        statuses[name] = status
    return statuses


def check_containers(mode: str) -> list[dict[str, Any]]:
    statuses = _container_statuses()
    if not statuses:
        return [item("docker-containers", False, "no ai-local containers found; run make infra")]
    checks: list[dict[str, Any]] = []
    for name in REQUIRED_USER_CONTAINERS:
        status = statuses.get(name)
        checks.append(item(name, bool(status and "Up" in status), status or "missing"))
    if mode == "max":
        profiles = _active_profiles()
        for profile, names in MAX_PROFILE_CONTAINERS.items():
            if profile not in profiles:
                checks.append(item(f"profile:{profile}", True, "not requested on this machine", severity="info"))
                continue
            for name in names:
                status = statuses.get(name)
                checks.append(item(name, bool(status and "Up" in status), status or "missing", severity="warning"))
    return checks


def _active_profiles() -> set[str]:
    raw = os.environ.get("AI_COMPOSE_PROFILES")
    if not raw:
        raw = _read_env(ROOT / ".env.llm.generated").get("AI_COMPOSE_PROFILES", "core,storage")
    return {part.strip() for part in raw.replace(",", " ").split() if part.strip()}


def check_rag_sources() -> list[dict[str, Any]]:
    path = ROOT / "config" / "rag" / "user.toml"
    if tomllib is None or not path.exists():
        return [item("rag-config", False, "config/rag/user.toml unavailable", severity="warning")]
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    vaults = data.get("paths", {}).get("vault_dirs") or []
    repos = data.get("repos", {}).get("paths") or []
    source_count = len(vaults) + len(repos)
    existing = 0
    for raw in [*vaults, *repos]:
        if Path(str(raw)).expanduser().exists():
            existing += 1
    if source_count == 0:
        return [item("rag-sources", True, "no personal RAG sources configured yet", severity="info")]
    return [item("rag-sources", existing > 0, f"{existing}/{source_count} configured sources exist", severity="warning")]


def check_models_summary(mode: str) -> list[dict[str, Any]]:
    rc, out = _run(["python", "scripts/models_prepare.py", "--json"], timeout=60)
    if rc not in {0, 1}:
        return [item("models", False, out or "models_prepare failed", severity="warning")]
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return [item("models", False, "models_prepare returned invalid JSON", severity="warning")]
    warnings = [
        check
        for group in payload.get("checks", {}).values()
        for check in group
        if not check.get("ok") and check.get("severity") == "warning"
    ]
    profiles = _active_profiles()
    strict = mode == "max" or bool({"llm", "gpu", "agents", "material"} & profiles)
    blocking = warnings if strict else []
    suggestions = []
    for warning in warnings:
        name = str(warning.get("name") or "model")
        suggestions.append({"model_check": name, "command": "make models", "detail": warning.get("message")})
    if blocking:
        return [
            item(
                "models",
                False,
                f"{len(blocking)} blocking model warning(s) for profiles {','.join(sorted(profiles))}; run make models",
                severity="error",
                data={"warnings": warnings, "suggestions": suggestions},
            )
        ]
    return [
        item(
            "models",
            True,
            f"{len(warnings)} model warning(s); run make models for detail",
            severity="info",
            data={"warnings": warnings, "suggestions": suggestions},
        )
    ]


def _local_https_url(port_key: str, path: str = "") -> str | None:
    env = _read_env(ROOT / ".env.services.generated")
    port = env.get(port_key)
    if not port:
        return None
    suffix = path if path.startswith("/") or not path else f"/{path}"
    return f"https://127.0.0.1:{port}{suffix}"


def _http_check(name: str, url: str | None, *, severity: str = "warning") -> dict[str, Any]:
    if not url:
        return item(name, False, "missing generated service port", severity=severity)
    status, body = _http_get(url)
    ok = 200 <= status < 500
    detail = f"HTTP {status} {url}" if status else f"{url}: {body}"
    return item(name, ok, detail, severity=severity, data={"url": url, "body": body[:500]})


def check_live_http(mode: str) -> list[dict[str, Any]]:
    checks = [
        _http_check("symbiont-live", _local_https_url("ORC_PORT_SYMBIONT", "/live"), severity="error"),
        _http_check("rag-health", _local_https_url("ORC_PORT_RAG", "/health"), severity="error"),
    ]
    if mode == "max" and "observability" in _active_profiles():
        checks.extend(
            [
                _http_check("grafana-health", _local_https_url("ORC_PORT_GRAFANA", "/api/health")),
                _http_check("clickhouse-ping", _local_https_url("ORC_PORT_CLICKHOUSE_HTTP", "/ping")),
                _http_check("langfuse-health", _local_https_url("ORC_PORT_LANGFUSE", "/api/public/health")),
                _http_check("otel-metrics", _local_https_url("ORC_PORT_OTEL_METRICS", "/metrics")),
            ]
        )
    return checks


def build_payload(mode: str, *, live: bool = False) -> dict[str, Any]:
    checks = {
        "generated-env": check_generated_env(),
        "alias": check_alias(),
        "containers": check_containers(mode),
        "rag": check_rag_sources(),
        "models": check_models_summary(mode),
    }
    if live:
        checks["live-http"] = check_live_http(mode)
        checks["alias-smoke"] = check_alias_smoke()
    all_checks = [check for group in checks.values() for check in group]
    payload = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": mode,
        "live": live,
        "active_profiles": sorted(_active_profiles()),
        "checks": checks,
    }
    payload["ok"] = all(check["ok"] or check["severity"] in {"warning", "info"} for check in all_checks)
    return payload


def write_report(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_text(payload: dict[str, Any]) -> None:
    print(f"== ai-local verify ({payload['mode']}) ==")
    print("Profiles: " + ", ".join(payload["active_profiles"]))
    for group, checks in payload["checks"].items():
        print(f"\n== {group} ==")
        for check in checks:
            marker = "OK" if check["ok"] else ("WARN" if check["severity"] == "warning" else "FAIL")
            print(f"{marker:4} {check['name']}: {check['message']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("user", "max"), default="user")
    parser.add_argument("--live", action="store_true", help="Run HTTP and alias smoke checks against the live stack.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-report", nargs="?", const=str(DEFAULT_REPORT), metavar="PATH")
    args = parser.parse_args(argv)

    payload = build_payload(args.mode, live=args.live)
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
