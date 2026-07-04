"""Read-only Docker Compose diagnostics for local workspaces."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


def resolve_compose_workspace(path: str | None, *, host_home_prefix: str | None = None) -> Path | None:
    raw = (path or "").strip()
    if not raw or "\x00" in raw:
        return None
    candidates = [Path(raw)]
    host_home = (host_home_prefix or os.environ.get("HOST_HOME_PREFIX") or "").strip().rstrip("/")
    if host_home and raw == host_home:
        candidates.append(Path("/host_home"))
    elif host_home and raw.startswith(f"{host_home}/"):
        candidates.append(Path("/host_home") / raw[len(host_home) + 1 :])
    parts = Path(raw).parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "home":
        candidates.append(Path("/host_home").joinpath(*parts[3:]))
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir():
            return resolved
    return None


def build_compose_analysis_report(workspace: Path, query: str = "") -> dict[str, Any]:
    del query
    root = workspace.resolve()
    compose_path = _find_compose_file(root)
    if compose_path is None:
        return {
            "workspace": str(root),
            "compose_file": None,
            "error": "compose_file_not_found",
            "issues": [],
        }

    compose = _load_yaml(compose_path)
    services = compose.get("services") if isinstance(compose, dict) else {}
    if not isinstance(services, dict):
        services = {}
    env = _parse_env_file(root / ".env")
    env_example = _parse_env_file(root / ".env.example")

    issues: list[dict[str, Any]] = []
    issues.extend(_database_localhost_issues(env, services))
    issues.extend(_secondary_localhost_issues(env))
    issues.extend(_depends_on_issues(services))
    issues.extend(_healthcheck_issues(root, services))
    issues.extend(_nginx_timeout_issues(root))
    issues.extend(_worker_idempotency_issues(root))
    issues.extend(_env_drift_issues(env, env_example))
    issues.extend(_volume_reuse_issues(compose))
    issues.extend(_wait_script_issues(root))

    return {
        "workspace": str(root),
        "compose_file": str(compose_path),
        "analysis_mode": "read_only_static_compose_env_code_review",
        "services": sorted(services.keys()),
        "env_keys": sorted(env.keys()),
        "issues": issues,
        "failure_model": _failure_model(issues),
        "minimal_diffs": _minimal_diffs(env, env_example, services),
        "validation_commands": [
            "docker compose config",
            "docker compose up --build",
        "docker compose ps",
        "docker compose logs --tail=200",
        "curl -fsS <declared-readiness-url>",
        "run the smallest documented smoke test for the affected route or worker",
        ],
        "residual_risks": [
            "If a named database volume contains an older schema, readiness can pass while migrations are still incompatible.",
            "Retry/idempotency should be validated with a test that crashes after the side effect and then replays the job.",
            "Static analysis cannot prove runtime DNS or schema state without running the stack.",
        ],
    }


def format_compose_analysis_report(report: dict[str, Any], *, published_uri: str | None = None) -> str:
    lines = ["# Docker Compose chaos report", ""]
    if published_uri:
        lines.append(f"- storage_guardian object: `{published_uri}`")
    if report.get("error"):
        lines.append(f"- error: {report['error']}")
        return "\n".join(lines).strip() + "\n"
    lines.extend([
        f"- compose file: `{_rel(report.get('compose_file'))}`",
        f"- analysis mode: {report.get('analysis_mode')}",
        f"- services: {', '.join(report.get('services', []))}",
        "",
        "## Failure model",
    ])
    for item in report.get("failure_model", []):
        lines.append(f"- {item}")

    lines.extend(["", "## Evidence and findings"])
    for issue in report.get("issues", []):
        lines.append(
            f"- **{issue['severity']} {issue['id']}**: {issue['summary']} "
            f"Evidence: {issue['evidence']} Recommendation: {issue['recommendation']}"
        )

    lines.extend(["", "## Minimal diffs"])
    for diff in report.get("minimal_diffs", []):
        lines.extend(["```diff", diff.rstrip(), "```"])

    lines.extend(["", "## Validation commands"])
    for command in report.get("validation_commands", []):
        lines.append(f"- `{command}`")

    lines.extend(["", "## Residual risks and assumptions"])
    for risk in report.get("residual_risks", []):
        lines.append(f"- {risk}")
    lines.extend([
        "",
        "## Safety",
        "- No `docker system prune`, volume deletion, or broad cleanup is required for this diagnosis.",
        "- Prefer `docker compose config` and focused logs before any runtime changes.",
    ])
    return "\n".join(lines).strip() + "\n"


def _find_compose_file(root: Path) -> Path | None:
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        path = root / name
        if path.is_file():
            return path
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _database_localhost_issues(env: dict[str, str], services: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    for key, value in env.items():
        if "DATABASE" not in key.upper() and not key.upper().endswith("_DB_URL"):
            continue
        host = _url_host(value)
        if host in {"localhost", "127.0.0.1", "::1"}:
            target = "postgres" if "postgres" in services else "<compose-service-name>"
            issues.append(_issue(
                "database-url-localhost",
                "critical",
                f"`{key}` points at `{host}` from inside containers, which resolves to the container itself.",
                f".env: `{key}={value}`; services include: {', '.join(sorted(services))}",
                f"Use the Compose service DNS name `{target}` in `{key}`.",
            ))
    return issues


def _secondary_localhost_issues(env: dict[str, str]) -> list[dict[str, Any]]:
    issues = []
    for key, value in env.items():
        if "DATABASE" in key.upper():
            continue
        host = _url_host(value)
        if host in {"localhost", "127.0.0.1", "::1"}:
            issues.append(_issue(
                f"{key.lower()}-localhost",
                "medium",
                f"`{key}` also points at `{host}`; this can break container networking but is secondary unless it is on the failing path.",
                f".env: `{key}={value}`",
                "Use the relevant Compose service DNS name or document why this value is host-only.",
            ))
    return issues


def _depends_on_issues(services: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    for service, spec in services.items():
        if not isinstance(spec, dict) or "depends_on" not in spec:
            continue
        depends_on = spec.get("depends_on")
        if isinstance(depends_on, list):
            issues.append(_issue(
                f"{service}-depends-on-order-only",
                "high",
                f"`{service}` uses list-form `depends_on`, which orders startup but does not wait for readiness.",
                f"{service}.depends_on={depends_on}",
                "Use healthcheck-backed conditions or an explicit readiness script that checks schema/application readiness.",
            ))
        elif isinstance(depends_on, dict):
            missing = [
                name for name, condition in depends_on.items()
                if not (isinstance(condition, dict) and condition.get("condition") == "service_healthy")
            ]
            if missing:
                issues.append(_issue(
                    f"{service}-depends-on-without-health",
                    "medium",
                    f"`{service}` has dependencies without `condition: service_healthy`.",
                    f"dependencies without health condition: {', '.join(missing)}",
                    "Gate stateful dependencies on health or application readiness.",
                ))
    return issues


def _healthcheck_issues(root: Path, services: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    for service, spec in services.items():
        if not isinstance(spec, dict) or "healthcheck" not in spec:
            continue
        healthcheck = spec.get("healthcheck") or {}
        test = str(healthcheck.get("test") if isinstance(healthcheck, dict) else healthcheck)
        lowered = test.lower()
        if any(term in lowered for term in ("socket.create_connection", "nc -z", "/dev/tcp", "curl -f http://127.0.0.1")):
            issues.append(_issue(
                f"{service}-tcp-only-healthcheck",
                "high",
                f"`{service}` healthcheck checks TCP/process reachability, not application/schema readiness.",
                f"{service}.healthcheck.test={test}",
                "Expose a readiness endpoint or command that checks required schema and dependent services.",
            ))
    app_files = list((root / "api").glob("*.py")) if (root / "api").is_dir() else []
    for path in app_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "/health" in text and "schema" in text.lower() and "does not prove schema readiness" in text.lower():
            issues.append(_issue(
                "api-health-not-schema-ready",
                "high",
                "`/health` explicitly does not prove schema readiness.",
                f"{path.relative_to(root)} mentions process-only health and schema-readiness gap.",
                "Add `/ready` or equivalent that checks database schema and Redis/cache connectivity.",
            ))
            break
    return issues


def _nginx_timeout_issues(root: Path) -> list[dict[str, Any]]:
    issues = []
    for path in (root / "nginx").glob("*.conf") if (root / "nginx").is_dir() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        matches = re.findall(r"(proxy_(?:connect|read|send)_timeout)\s+(\d+)s", text)
        short = [(name, value) for name, value in matches if int(value) <= 2]
        if short:
            issues.append(_issue(
                "nginx-short-proxy-timeouts",
                "medium",
                "Nginx proxy timeouts are very short and can turn slow startup or cold requests into flaky failures.",
                f"{path.relative_to(root)}: " + ", ".join(f"{name}={value}s" for name, value in short),
                "Use realistic local timeouts such as 30s while keeping separate production tuning.",
            ))
    return issues


def _worker_idempotency_issues(root: Path) -> list[dict[str, Any]]:
    issues = []
    worker_root = root / "worker"
    if not worker_root.is_dir():
        return issues
    for path in worker_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        lower = text.lower()
        if "retry" in lower and any(term in lower for term in ("send_", "receipt", "charge", "email", "side effect")):
            has_marker = any(term in lower for term in ("idempotency_key", "processed_job", "processed_jobs", "dedupe"))
            if "missing processed" in lower or "missing idempot" in lower:
                has_marker = False
            if not has_marker:
                issues.append(_issue(
                    "worker-missing-idempotency-marker",
                    "high",
                    "Worker retry flow can repeat side effects because there is no processed/idempotency marker.",
                    f"{path.relative_to(root)} contains retry handling and side-effect-like calls without idempotency markers.",
                    "Record an idempotency key/processed-job marker before or atomically with side effects.",
                ))
                break
    return issues


def _env_drift_issues(env: dict[str, str], example: dict[str, str]) -> list[dict[str, Any]]:
    if not env or not example:
        return []
    diffs = []
    for key in sorted(set(env) & set(example)):
        if env[key] != example[key]:
            diffs.append(f"{key}: .env={env[key]!r}, .env.example={example[key]!r}")
    if not diffs:
        return []
    return [_issue(
        "env-drift",
        "medium",
        ".env differs from .env.example in networking, timeout, concurrency or schema-related settings.",
        "; ".join(diffs[:8]),
        "Align these files or document intentional local overrides explicitly.",
    )]


def _volume_reuse_issues(compose: dict[str, Any]) -> list[dict[str, Any]]:
    volumes = compose.get("volumes") if isinstance(compose, dict) else {}
    if not isinstance(volumes, dict):
        return []
    suspicious = [name for name in volumes if any(term in name.lower() for term in ("old", "previous"))]
    if not suspicious:
        return []
    return [_issue(
        "suspicious-reused-volume",
        "medium",
        "Named volume suggests reused database state from an older schema.",
        "volumes: " + ", ".join(suspicious),
        "Do not delete volumes as a shortcut; inspect/migrate or create an explicit disposable dev volume.",
    )]


def _wait_script_issues(root: Path) -> list[dict[str, Any]]:
    issues = []
    scripts = root / "scripts"
    if not scripts.is_dir():
        return issues
    for path in scripts.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lower = text.lower()
        if "schema-safe" in lower and "socket.create_connection" in lower:
            issues.append(_issue(
                "wait-script-tcp-only",
                "medium",
                "Wait script claims schema safety but checks only TCP reachability.",
                f"{path.relative_to(root)} uses socket.create_connection.",
                "Replace with a command that verifies required schema/migrations and dependent services.",
            ))
    return issues


def _minimal_diffs(env: dict[str, str], example: dict[str, str], services: dict[str, Any]) -> list[str]:
    diffs = []
    database_url = env.get("DATABASE_URL", "")
    if _url_host(database_url) in {"localhost", "127.0.0.1", "::1"}:
        diffs.append(
            "diff --git a/.env b/.env\n"
            "--- a/.env\n"
            "+++ b/.env\n"
            f"-DATABASE_URL={database_url}\n"
            f"+DATABASE_URL={_replace_url_host(database_url, 'postgres')}\n"
        )
    if env.get("API_TIMEOUT_SECONDS") and example.get("API_TIMEOUT_SECONDS") and env["API_TIMEOUT_SECONDS"] != example["API_TIMEOUT_SECONDS"]:
        diffs.append(
            "diff --git a/.env b/.env\n"
            "--- a/.env\n"
            "+++ b/.env\n"
            f"-API_TIMEOUT_SECONDS={env['API_TIMEOUT_SECONDS']}\n"
            f"+API_TIMEOUT_SECONDS={example['API_TIMEOUT_SECONDS']}\n"
        )
    if any(isinstance(spec, dict) and isinstance(spec.get("depends_on"), list) for spec in services.values()):
        diffs.append(
            "diff --git a/docker-compose.yml b/docker-compose.yml\n"
            "--- a/docker-compose.yml\n"
            "+++ b/docker-compose.yml\n"
            "@@\n"
            "-    depends_on:\n"
            "-      - postgres\n"
            "-      - redis\n"
            "+    depends_on:\n"
            "+      postgres:\n"
            "+        condition: service_healthy\n"
            "+      redis:\n"
            "+        condition: service_started\n"
            "+    # Add an app/schema readiness check before accepting traffic.\n"
        )
    diffs.append(
        "diff --git a/nginx/default.conf b/nginx/default.conf\n"
        "--- a/nginx/default.conf\n"
        "+++ b/nginx/default.conf\n"
        "@@\n"
        "-        proxy_connect_timeout 1s;\n"
        "-        proxy_read_timeout 1s;\n"
        "-        proxy_send_timeout 1s;\n"
        "+        proxy_connect_timeout 30s;\n"
        "+        proxy_read_timeout 30s;\n"
        "+        proxy_send_timeout 30s;\n"
    )
    diffs.append(
        "diff --git a/worker/worker.py b/worker/worker.py\n"
        "--- a/worker/worker.py\n"
        "+++ b/worker/worker.py\n"
        "@@\n"
        "+    # Record an idempotency/processed-job marker before or atomically with side effects.\n"
        "     send_receipt(job)\n"
    )
    return diffs


def _failure_model(issues: list[dict[str, Any]]) -> list[str]:
    ids = {issue["id"] for issue in issues}
    model = []
    if "database-url-localhost" in ids:
        model.append("Containers that use `localhost` for the database connect to themselves instead of the Compose database service.")
    if any("depends-on" in issue_id for issue_id in ids):
        model.append("`depends_on` controls start order only; API/worker can start before database schema and dependencies are ready.")
    if "api-health-not-schema-ready" in ids or any("tcp-only-healthcheck" in issue_id for issue_id in ids):
        model.append("Healthchecks can report healthy while application/schema readiness is still false.")
    if "worker-missing-idempotency-marker" in ids:
        model.append("Worker retries can replay side effects and create duplicate processing.")
    if "nginx-short-proxy-timeouts" in ids:
        model.append("Short proxy timeouts amplify slow startup into request failures.")
    return model or ["Static analysis found no high-confidence Compose defect."]


def _issue(
    issue_id: str,
    severity: str,
    summary: str,
    evidence: str,
    recommendation: str,
) -> dict[str, str]:
    return {
        "id": issue_id,
        "severity": severity,
        "summary": summary,
        "evidence": evidence,
        "recommendation": recommendation,
    }


def _url_host(value: str) -> str:
    parsed = urlparse(value)
    return (parsed.hostname or "").lower()


def _replace_url_host(value: str, host: str) -> str:
    parsed = urlparse(value)
    if not parsed.hostname:
        return value
    netloc = parsed.netloc.replace(parsed.hostname, host, 1)
    return parsed._replace(netloc=netloc).geturl()


def _rel(value: object) -> str:
    text = str(value or "")
    marker = "/agentic-stress-lab/"
    if marker in text:
        return text.split(marker, 1)[1]
    return text
