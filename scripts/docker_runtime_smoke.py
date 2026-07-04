#!/usr/bin/env python3
"""Runtime smoke evidence for the federated local Docker platform.

This script is intentionally read-only: it inspects Docker state and calls
health/status endpoints. It never starts, stops, restarts or mutates services.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "docs" / "generated"
REPORT_JSON = GENERATED / "docker-runtime-smoke.json"
REPORT_MD = GENERATED / "docker-runtime-smoke.md"
INVENTORY_JSON = GENERATED / "docker-inventory.json"
SERVICES_ENV = ROOT / ".env.services.generated"


def _ssl_context() -> ssl.SSLContext:
    ca_file = os.environ.get("AI_LOCAL_TLS_CA_BUNDLE_FILE") or os.environ.get("SSL_CERT_FILE")
    if not ca_file:
        services_env = _read_env(SERVICES_ENV)
        tls_dir = services_env.get("AI_LOCAL_TLS_DIR_HOST")
        candidate = Path(tls_dir) / "ca-bundle.crt" if tls_dir else ROOT / ".local" / "tls" / "ca-bundle.crt"
        if candidate.exists():
            ca_file = str(candidate)
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    return ssl.create_default_context()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _secret(name: str) -> str:
    env_value = os.environ.get(name)
    if env_value:
        return env_value
    path = ROOT / "infra" / "docker" / "secrets" / "orc_api_key"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"')
    return env


def _check(name: str, status: str, summary: str, *, detail: Any = None, action: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "status": status, "summary": summary}
    if detail is not None:
        payload["detail"] = detail
    if action:
        payload["action"] = action
    return payload


def overall_status(checks: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status")) for item in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


def expected_runtime_services(inventory: dict[str, Any] | None) -> list[dict[str, Any]]:
    def priority(item: dict[str, Any]) -> tuple[int, str]:
        profiles = set(item.get("profiles") or [])
        rank = 0 if "core" in profiles else 1 if "storage" in profiles else 2
        return rank, str(item["service"])

    services: list[dict[str, Any]] = []
    for item in (inventory or {}).get("services") or []:
        profiles = set(item.get("profiles") or [])
        if not profiles.intersection({"core", "storage"}):
            continue
        container = str(item.get("container_name") or "")
        if not container:
            continue
        services.append(
            {
                "service": item.get("service"),
                "container_name": container,
                "profiles": sorted(profiles),
                "class": item.get("class"),
            }
        )
    return sorted(services, key=priority)


def _docker_ps(context: str) -> tuple[dict[str, str], str]:
    cmd = ["docker", "--context", context, "ps", "--format", "{{.Names}}\t{{.Status}}"]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        return {}, result.stderr.strip() or result.stdout.strip()
    containers: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, _, status = line.partition("\t")
        if name:
            containers[name] = status
    return containers, ""


def _http_json(url: str, *, api_key: str = "", timeout: float = 5.0, method: str = "GET", body: dict[str, Any] | None = None) -> tuple[int, Any, str]:
    data = None
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(  # noqa: S310 - local operator smoke
            request,
            timeout=timeout,
            context=_ssl_context(),
        ) as response:
            raw = response.read(1_000_000).decode("utf-8", errors="replace")
            try:
                payload: Any = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = raw[:500]
            return int(response.status), payload, ""
    except urllib.error.HTTPError as exc:
        raw = exc.read(1000).decode("utf-8", errors="replace")
        return int(exc.code), raw, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - smoke evidence should record all failures
        return 0, None, f"{type(exc).__name__}: {exc}"


def _http_json_with_retries(
    url: str,
    *,
    api_key: str = "",
    timeout: float = 5.0,
    attempts: int = 6,
    delay: float = 2.0,
) -> tuple[int, Any, str, int]:
    last_code = 0
    last_payload: Any = None
    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        last_code, last_payload, last_error = _http_json(url, api_key=api_key, timeout=timeout)
        if 200 <= last_code < 300:
            return last_code, last_payload, last_error, attempt
        if attempt < attempts:
            time.sleep(delay)
    return last_code, last_payload, last_error, max(1, attempts)


def _container_checks(inventory: dict[str, Any] | None, *, context: str) -> list[dict[str, Any]]:
    expected = expected_runtime_services(inventory)
    containers, error = _docker_ps(context)
    if error:
        return [_check("docker.ps", "fail", "failed to inspect running containers", detail=error, action="check Docker daemon/context")]
    checks: list[dict[str, Any]] = []
    for item in expected:
        name = str(item["container_name"])
        status = containers.get(name)
        if status and status.startswith("Up"):
            checks.append(_check(f"container.{item['service']}", "pass", status, detail=item))
        else:
            checks.append(
                _check(
                    f"container.{item['service']}",
                    "fail",
                    "expected runtime container is not running",
                    detail={**item, "docker_status": status or "missing"},
                    action="start the canonical infra lifecycle with make infra",
                )
            )
    return checks


def _endpoint_checks(*, api_key: str, include_query: bool, timeout: float, services_env: dict[str, str]) -> list[dict[str, Any]]:
    def port(name: str, default: int) -> int:
        try:
            return int(services_env.get(name, default))
        except (TypeError, ValueError):
            return default

    symbiont_port = port("ORC_PORT_SYMBIONT", 8586)
    rag_port = port("ORC_PORT_RAG", 8484)
    storage_guardian_port = port("ORC_PORT_STORAGE_GUARDIAN", 8730)
    qdrant_port = port("ORC_PORT_QDRANT_HTTP", 16336)

    endpoints = [
        ("symbiont.health", f"https://127.0.0.1:{symbiont_port}/health", ""),
        ("symbiont.lifecycle", f"https://127.0.0.1:{symbiont_port}/lifecycle", api_key),
        ("symbiont.cockpit_docker", f"https://127.0.0.1:{symbiont_port}/agentic/cockpit/docker", api_key),
        ("rag.health", f"https://127.0.0.1:{rag_port}/health", ""),
        ("storage_guardian.health", f"https://127.0.0.1:{storage_guardian_port}/health", ""),
        ("qdrant.health", f"https://127.0.0.1:{qdrant_port}/healthz", ""),
    ]
    checks: list[dict[str, Any]] = []
    for name, url, key in endpoints:
        code, payload, error, attempts = _http_json_with_retries(url, api_key=key, timeout=timeout)
        if 200 <= code < 300:
            detail = payload
            if attempts > 1:
                detail = {"attempts": attempts, "response": payload}
            checks.append(_check(name, "pass", f"HTTP {code}", detail=detail))
        else:
            checks.append(
                _check(
                    name,
                    "fail",
                    error or f"HTTP {code}",
                    detail={"attempts": attempts, "response": payload},
                    action="ensure the target service is running and healthy",
                )
            )
    if include_query:
        code, payload, error = _http_json(
            f"https://127.0.0.1:{symbiont_port}/query",
            api_key=api_key,
            timeout=max(timeout, 60.0),
            method="POST",
            body={"query": "diz apenas OK", "stream": False, "agentic": False},
        )
        response = payload.get("response") if isinstance(payload, dict) else ""
        if 200 <= code < 300 and str(response).strip():
            checks.append(_check("symbiont.query", "pass", "sync query returned a response", detail={"response": str(response)[:300]}))
        else:
            checks.append(
                _check(
                    "symbiont.query",
                    "fail",
                    error or f"HTTP {code}",
                    detail=payload,
                    action="inspect symbiont logs and LLM backend health",
                )
            )
    return checks


def build_report(*, include_query: bool = False, context: str = "default", timeout: float = 5.0) -> dict[str, Any]:
    inventory = _read_json(INVENTORY_JSON)
    services_env = _read_env(SERVICES_ENV)
    checks: list[dict[str, Any]] = []
    if inventory is None:
        checks.append(
            _check(
                "inventory.present",
                "fail",
                "Docker inventory is missing or invalid",
                action="run make infra",
            )
        )
    else:
        checks.append(_check("inventory.present", "pass", "Docker inventory available", detail={"generated_at": inventory.get("generated_at")}))
    checks.extend(_container_checks(inventory, context=context))
    checks.extend(
        _endpoint_checks(
            api_key=_secret("ORC_SYMBIONT_API_KEY"),
            include_query=include_query,
            timeout=timeout,
            services_env=services_env,
        )
    )
    status = overall_status(checks)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "include_query": include_query,
        "docker_context": context,
        "summary": {
            "pass": sum(1 for item in checks if item["status"] == "pass"),
            "warn": sum(1 for item in checks if item["status"] == "warn"),
            "fail": sum(1 for item in checks if item["status"] == "fail"),
        },
        "checks": checks,
        "action_items": [item["action"] for item in checks if item.get("action")],
        "artifacts": {
            "docker_inventory": str(INVENTORY_JSON.relative_to(ROOT)),
            "report": str(REPORT_JSON.relative_to(ROOT)),
        },
    }


def _render_md(report: dict[str, Any]) -> str:
    lines = [
        "# Docker Runtime Smoke",
        "",
        f"Generated at: `{report['generated_at']}`",
        f"Status: `{report['status']}`",
        f"Docker context: `{report['docker_context']}`",
        f"Live query included: `{report['include_query']}`",
        "",
        "| Check | Status | Summary |",
        "| --- | --- | --- |",
    ]
    for item in report["checks"]:
        lines.append(f"| `{item['name']}` | `{item['status']}` | {item['summary']} |")
    if report["action_items"]:
        lines.extend(["", "## Action Items", ""])
        lines.extend(f"- {item}" for item in report["action_items"])
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any]) -> None:
    GENERATED.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    REPORT_MD.write_text(_render_md(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write docs/generated/docker-runtime-smoke artifacts")
    parser.add_argument("--include-query", action="store_true", help="also call /query with a small synchronous prompt")
    parser.add_argument("--strict", action="store_true", help="exit non-zero unless status is pass")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--context", default=os.environ.get("AI_LOCAL_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or "default")
    parser.add_argument("--json", action="store_true", help="print JSON report")
    args = parser.parse_args(argv)

    report = build_report(include_query=args.include_query, context=args.context, timeout=args.timeout)
    if args.write:
        write_report(report)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if args.write:
            print(f"Wrote {REPORT_JSON.relative_to(ROOT)} and {REPORT_MD.relative_to(ROOT)}")
        print(f"Docker runtime smoke: {report['status']} pass={report['summary']['pass']} fail={report['summary']['fail']}")
    return 1 if args.strict and report["status"] != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
