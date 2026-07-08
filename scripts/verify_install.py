#!/usr/bin/env python3
"""End-to-end readiness summary for a new ai-local install."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.symbiont_runtime import PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS  # noqa: E402

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
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "not found"
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        return 124, (stdout or stderr or "timeout").strip()
    return proc.returncode, (stdout or stderr).strip()


def _read_secret(path: Path | str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _configured_api_key() -> str:
    token = os.environ.get("ORC_SYMBIONT_API_KEY", "").strip()
    if token:
        return token
    return _read_secret(ROOT / "infra" / "docker" / "secrets" / "orc_api_key")


def _configured_internal_api_key() -> str:
    for env_name in ("AI_RESOURCE_GOVERNOR_TOKEN", "INTERNAL_API_KEY", "ORC_INTERNAL_API_KEY"):
        token = os.environ.get(env_name, "").strip()
        if token:
            return token
    for env_name in ("AI_RESOURCE_GOVERNOR_TOKEN_FILE", "INTERNAL_API_KEY_FILE", "ORC_INTERNAL_API_KEY_FILE"):
        token = _read_secret(os.environ.get(env_name, ""))
        if token:
            return token
    return _read_secret(ROOT / "infra" / "docker" / "secrets" / "internal_api_key")


def _http_get(
    url: str,
    *,
    timeout: int = 10,
    api_key: str | None = None,
    internal_key: str | None = None,
) -> tuple[int, str]:
    if not shutil.which("curl"):
        return 0, "curl not found"
    headers: list[str] = []
    if api_key:
        headers.extend([f"X-API-Key: {api_key}", f"Authorization: Bearer {api_key}"])
    if internal_key:
        headers.append(f"X-Internal-API-Key: {internal_key}")
    header_file = None
    try:
        cmd = ["curl", "-ksS", "--max-time", str(timeout), "-w", "\n__HTTP_STATUS__:%{http_code}"]
        if headers:
            header_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
            header_file.write("\n".join(headers) + "\n")
            header_file.close()
            cmd.extend(["-H", f"@{header_file.name}"])
        cmd.append(url)
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except subprocess.TimeoutExpired:
        return 0, "timeout"
    finally:
        if header_file is not None:
            try:
                Path(header_file.name).unlink()
            except OSError:
                pass
    output = (result.stdout or result.stderr).strip()
    marker = "\n__HTTP_STATUS__:"
    if marker not in output:
        return 0, output
    body, raw_status = output.rsplit(marker, 1)
    try:
        status = int(raw_status.strip())
    except ValueError:
        status = 0
    return status, body.strip()


def _http_get_json(
    url: str | None,
    *,
    timeout: int = 10,
    api_key: str | None = None,
    internal_key: str | None = None,
) -> tuple[int, dict[str, Any] | list[Any] | None, str]:
    if not url:
        return 0, None, "missing generated service port"
    status, body = _http_get(url, timeout=timeout, api_key=api_key, internal_key=internal_key)
    if not body:
        return status, None, ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return status, None, body[:500]
    return status, payload, body


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


def check_alias_argv_safety() -> list[dict[str, Any]]:
    path = shutil.which("@") or str(Path.home() / ".local" / "bin" / "@")
    alias_path = Path(path)
    if not alias_path.exists():
        return [item("alias-argv-safety", False, "alias @ not installed; run make setup")]
    try:
        script = alias_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [item("alias-argv-safety", False, f"cannot read alias @: {exc}")]
    safe = (
        '-H "@$HEADERFILE"' in script
        and '--data-binary "@$PAYLOADFILE"' in script
        and '-H "X-API-Key: $ORC_SYMBIONT_API_KEY"' not in script
        and '--data "$PAYLOAD"' not in script
    )
    return [
        item(
            "alias-argv-safety",
            safe,
            "headers and payload are passed through temp files"
            if safe
            else "alias @ may expose secret or payload in process argv; run make aliases",
        )
    ]


def _alias_path() -> Path | None:
    path = shutil.which("@") or str(Path.home() / ".local" / "bin" / "@")
    alias_path = Path(path)
    return alias_path if alias_path.exists() else None


def _run_alias_prompt(prompt_name: str, *, timeout: int) -> tuple[int, str]:
    alias_path = _alias_path()
    if alias_path is None:
        return 127, "alias @ not installed; run make setup"
    rc, out = _run(
        [
            str(alias_path),
            "--raw",
            "--no-stream",
            "--no-agentic",
            "--new-session",
            _prompt(prompt_name),
        ],
        timeout=timeout,
    )
    return rc, out.strip()


def check_alias_transport_smoke() -> list[dict[str, Any]]:
    rc, out = _run_alias_prompt("alias_transport_smoke.md", timeout=45)
    clean = out.strip()
    return [
        item(
            "alias-transport-smoke",
            rc == 0 and bool(clean),
            clean[:240] if clean else "empty alias transport response",
            severity="error",
            data={"exit_code": rc},
        )
    ]


def _classify_alias_llm_failure(rc: int, output: str, pressure_level: str | None) -> str:
    text = output.lower()
    if rc == 124 or "timeout" in text:
        return "timeout_under_low_pressure" if pressure_level in {None, "low", "moderate"} else "timeout_under_pressure"
    if "401" in text or "invalid api key" in text or "missing api key" in text:
        return "auth_failed"
    if "connection refused" in text or "could not connect" in text or "couldn't connect" in text:
        return "transport_broken"
    if "model" in text and any(term in text for term in ("unavailable", "not found", "missing", "no such")):
        return "model_unavailable"
    return "unexpected_error"


def _resources_snapshot() -> tuple[int, dict[str, Any] | None, str]:
    return _http_get_json(
        _local_https_url("ORC_PORT_SYMBIONT", "/resources/snapshot"),
        api_key=None,
        internal_key=_configured_internal_api_key(),
    )


def check_alias_llm_smoke() -> list[dict[str, Any]]:
    pressure_status, snapshot, pressure_error = _resources_snapshot()
    pressure_level = None
    pressure_reasons: list[str] = []
    if isinstance(snapshot, dict):
        pressure_level = str(snapshot.get("pressure_level") or "").lower() or None
        pressure_reasons = [str(reason) for reason in snapshot.get("pressure_reasons") or []]
    blocking_reasons = {"gpu_saturated", "thermal_high"}
    if pressure_status == 200 and (
        pressure_level == "critical" or blocking_reasons.intersection(pressure_reasons)
    ):
        return [
            item(
                "alias-llm-smoke",
                True,
                f"deferred_by_pressure: {pressure_level or 'unknown'} {','.join(pressure_reasons)}",
                severity="info",
                data={"status": "deferred_by_pressure", "resources": snapshot},
            )
        ]

    rc, out = _run_alias_prompt("alias_smoke.md", timeout=180)
    clean = out.strip()
    if rc == 0 and bool(clean):
        return [
            item(
                "alias-llm-smoke",
                True,
                clean[:240],
                data={"exit_code": rc, "resource_snapshot_status": pressure_status},
            )
        ]
    classification = _classify_alias_llm_failure(rc, clean or pressure_error, pressure_level)
    return [
        item(
            "alias-llm-smoke",
            False,
            f"{classification}: {(clean or pressure_error or 'empty alias response')[:220]}",
            severity="error",
            data={
                "status": classification,
                "exit_code": rc,
                "pressure_level": pressure_level,
                "pressure_reasons": pressure_reasons,
                "resource_snapshot_status": pressure_status,
            },
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


def check_runtime_identity() -> list[dict[str, Any]]:
    api_key = _configured_api_key()
    if not api_key:
        return [item("runtime-identity", False, "missing ORC_SYMBIONT_API_KEY or infra/docker/secrets/orc_api_key")]
    base = _local_https_url("ORC_PORT_SYMBIONT")
    checks: list[dict[str, Any]] = []
    payloads: dict[str, dict[str, Any]] = {}
    for endpoint in ("image-info", "runtime-info", "config-effective"):
        status, payload, body = _http_get_json(
            f"{base}/{endpoint}" if base else None,
            api_key=api_key,
        )
        ok = status == 200 and isinstance(payload, dict)
        checks.append(
            item(
                endpoint,
                ok,
                f"HTTP {status}" if status else (body or "request failed"),
                severity="error",
                data=payload if isinstance(payload, dict) else {"body": body[:500]},
            )
        )
        if isinstance(payload, dict):
            payloads[endpoint] = payload

    config_payload = payloads.get("config-effective") or payloads.get("runtime-info") or {}
    generated_hash = config_payload.get("generated_env_hash")
    effective_hash = config_payload.get("effective_env_hash")
    drift = bool(config_payload.get("runtime_config_drift"))
    hashes_ok = bool(generated_hash and effective_hash and generated_hash == effective_hash and not drift)
    checks.append(
        item(
            "runtime-config-hash",
            hashes_ok,
            "generated_env_hash == effective_env_hash"
            if hashes_ok
            else "generated/effective runtime env hash mismatch or unavailable",
            severity="error",
            data={
                "generated_env_hash": generated_hash,
                "effective_env_hash": effective_hash,
                "runtime_config_drift": drift,
            },
        )
    )
    return checks


def check_live_lifecycle_prod() -> list[dict[str, Any]]:
    api_key = _configured_api_key()
    if not api_key:
        return [item("lifecycle-production-ttls", False, "missing ORC_SYMBIONT_API_KEY or infra/docker/secrets/orc_api_key")]
    status, payload, body = _http_get_json(
        _local_https_url("ORC_PORT_SYMBIONT", "/lifecycle"),
        api_key=api_key,
    )
    if status != 200 or not isinstance(payload, dict):
        return [
            item(
                "lifecycle-production-ttls",
                False,
                f"HTTP {status}: {body[:220]}" if status else (body or "request failed"),
                data={"status": status},
            )
        ]
    if not payload.get("enabled"):
        return [
            item(
                "lifecycle-production-ttls",
                False,
                str(payload.get("message") or "lifecycle management is not active"),
                data=payload,
            )
        ]
    services = payload.get("services") or []
    by_name = {str(entry.get("name")): entry for entry in services if isinstance(entry, dict)}
    failures: list[dict[str, Any]] = []
    checked: dict[str, int] = {}
    for service_name, minimum in PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS.items():
        entry = by_name.get(service_name)
        if not entry:
            failures.append({"service": service_name, "reason": "missing from /lifecycle"})
            continue
        idle_timeout = entry.get("idle_timeout")
        try:
            actual = int(idle_timeout)
        except (TypeError, ValueError):
            failures.append({"service": service_name, "idle_timeout": idle_timeout, "minimum": minimum})
            continue
        checked[service_name] = actual
        if actual < minimum:
            failures.append({"service": service_name, "idle_timeout": actual, "minimum": minimum})
    return [
        item(
            "lifecycle-production-ttls",
            not failures,
            "production lifecycle TTL floors are active" if not failures else f"{len(failures)} lifecycle TTL floor violation(s)",
            severity="error",
            data={"checked": checked, "failures": failures},
        )
    ]


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


def build_payload(mode: str, *, live: bool = False, lifecycle_prod_only: bool = False) -> dict[str, Any]:
    if lifecycle_prod_only:
        checks = {"lifecycle-production": check_live_lifecycle_prod()}
    else:
        checks = {
            "generated-env": check_generated_env(),
            "alias": check_alias(),
            "containers": check_containers(mode),
            "rag": check_rag_sources(),
            "models": check_models_summary(mode),
        }
        if live:
            checks["live-http"] = check_live_http(mode)
            checks["runtime-identity"] = check_runtime_identity()
            checks["lifecycle-production"] = check_live_lifecycle_prod()
            checks["alias-argv-safety"] = check_alias_argv_safety()
            checks["alias-transport-smoke"] = check_alias_transport_smoke()
            checks["alias-llm-smoke"] = check_alias_llm_smoke()
    all_checks = [check for group in checks.values() for check in group]
    payload = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": mode,
        "live": live,
        "lifecycle_prod_only": lifecycle_prod_only,
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
    parser.add_argument(
        "--lifecycle-prod-only",
        action="store_true",
        help="Run only production lifecycle TTL validation against the live orchestrator.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-report", nargs="?", const=str(DEFAULT_REPORT), metavar="PATH")
    args = parser.parse_args(argv)

    payload = build_payload(args.mode, live=args.live, lifecycle_prod_only=args.lifecycle_prod_only)
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
