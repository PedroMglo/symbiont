"""Read-only security cache leak diagnostics for local workspaces."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_RE = re.compile(r"\b(?:token|secret|authorization|bearer)=\S+", re.I)


def resolve_security_workspace(path: str | None, *, host_home_prefix: str | None = None) -> Path | None:
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


def build_security_cache_report(workspace: Path, query: str = "") -> dict[str, Any]:
    del query
    root = workspace.resolve()
    code_findings = _cache_key_code_findings(root)
    api_events = _parse_kv_logs(root / "logs")
    nginx_events = _parse_http_access_logs(root / "logs")
    redis_events = _parse_cache_command_logs(root / "logs")
    trace_rows, malformed_traces = _parse_trace_jsonl_dir(root / "traces")
    exposures = _cross_tenant_trace_exposures(trace_rows)
    false_leads = _false_leads(root, api_events, nginx_events, trace_rows, malformed_traces, redis_events)
    affected_keys = _affected_cache_keys(code_findings, exposures, redis_events)

    return {
        "workspace": str(root),
        "analysis_mode": "read_only_redacted_security_cache_correlation",
        "code_findings": code_findings,
        "timeline": _timeline(api_events, trace_rows, nginx_events, exposures),
        "exposures": exposures,
        "false_leads": false_leads,
        "malformed_traces": malformed_traces,
        "root_cause": _root_cause(code_findings, exposures),
        "affected_cache_keys": affected_keys,
        "immediate_mitigation": [
            "Temporarily disable or bypass affected tenant-scoped cache paths for the impacted resource.",
            f"Purge affected keys with a controlled namespace scan, not a broad cache flush: {', '.join(affected_keys) or '<affected-cache-prefixes>'}.",
            "Increase logging around cache namespace, request_id, tenant_id and redacted user/account identifiers.",
        ],
        "minimal_fix": [
            "Include tenant/account namespace in every cache key for tenant-owned resources, e.g. `<resource>:{tenant_id}:{user_id}`.",
            "Replace global latest/current/list keys with tenant-scoped keys such as `<resource>:{tenant_id}:latest`.",
            "Keep authz checks, but do not treat authz success as proof of cache isolation.",
        ],
        "regression_tests": [
            "Two tenants requesting the same cached resource shape must never reuse payloads across tenant/account IDs.",
            "Two tenants hitting a `latest`/`current`/collection path must produce separate tenant/account-scoped cache keys.",
            "A cache hit should assert rendered tenant/account equals request tenant/account.",
            "Malformed telemetry and aborted/403 requests should be preserved but excluded from exposure counts.",
        ],
        "communication_summary": _communication_summary(exposures),
    }


def format_security_cache_report(report: dict[str, Any], *, published_uri: str | None = None) -> str:
    lines = ["# Security cache leak report", ""]
    if published_uri:
        lines.append(f"- storage_guardian object: `{published_uri}`")
    exposures = report.get("exposures", [])
    affected_keys = report.get("affected_cache_keys", [])
    lines.extend([
        f"- analysis mode: {report.get('analysis_mode')}",
        "- redaction: emails, names, tokens and full payloads are omitted or redacted.",
        "- scope badge: local-correlated, tenant/cache-scoped evidence only; no public-breach or person-count claim.",
        f"- observed exposure count: {len(exposures)} redacted correlation(s).",
        f"- affected cache key hints: {', '.join(affected_keys) if affected_keys else '<none proven>'}.",
        "",
        "## Redacted evidence timeline",
    ])
    for item in report.get("timeline", []):
        lines.append(f"- `{item['request_id']}` {item['summary']} Evidence: {item['evidence']}")

    lines.extend(["", "## Likely cause"])
    lines.append(report.get("root_cause", "No high-confidence root cause found."))

    lines.extend(["", "## Code evidence"])
    for finding in report.get("code_findings", []):
        lines.append(f"- `{finding['path']}:{finding['line']}` {finding['summary']} `{finding['redacted_line']}`")

    lines.extend(["", "## Discarded or secondary hypotheses"])
    for lead in report.get("false_leads", []):
        lines.append(f"- {lead}")

    lines.extend(["", "## Immediate mitigation"])
    for item in report.get("immediate_mitigation", []):
        lines.append(f"- {item}")

    lines.extend(["", "## Minimal fix"])
    for item in report.get("minimal_fix", []):
        lines.append(f"- {item}")

    lines.extend(["", "## Regression tests"])
    for item in report.get("regression_tests", []):
        lines.append(f"- {item}")

    lines.extend([
        "",
        "## Internal communication summary",
        report.get("communication_summary", ""),
        "",
        "## Scope limits",
        "- This report counts only local evidence with matching request IDs and redacted tenant/cache metadata.",
        "- It does not claim a public unauthenticated breach or enumerate affected people.",
    ])
    return "\n".join(lines).strip() + "\n"


def _cache_key_code_findings(root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in (root / "api").rglob("*.py") if (root / "api").is_dir() else []:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if not re.search(r"\bcache_key\s*=", stripped):
                continue
            lower = stripped.lower()
            if not _has_tenant_namespace(lower) and _looks_like_tenant_owned_cache_key(lower):
                findings.append({
                    "path": path.relative_to(root).as_posix(),
                    "line": number,
                    "summary": "tenant-owned cache key is not tenant/account namespaced",
                    "redacted_line": _redact(stripped),
                    "cache_key_hint": _cache_key_hint(stripped),
                })
    return findings


def _parse_kv_logs(log_root: Path) -> list[dict[str, str]]:
    events = []
    if not log_root.is_dir():
        return events
    for path in sorted(log_root.glob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            fields = _kv_fields(line)
            if "request_id" in fields:
                fields["source"] = path.name
                fields["raw_redacted"] = _redact(line)
                events.append(fields)
    return events


def _parse_http_access_logs(log_root: Path) -> list[dict[str, str]]:
    events = []
    if not log_root.is_dir():
        return events
    for path in sorted(log_root.glob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            request_id = _extract_request_id(line)
            if not request_id:
                continue
            match = re.search(r'"([A-Z]+) ([^ ]+) HTTP/[0-9.]+" ([0-9]{3})', line)
            if not match:
                continue
            events.append({
                "request_id": request_id,
                "route": match.group(2),
                "status": match.group(3),
                "source": path.name,
                "raw_redacted": _redact(line),
            })
    return events


def _parse_cache_command_logs(log_root: Path) -> list[dict[str, str]]:
    events = []
    if not log_root.is_dir():
        return events
    for path in sorted(log_root.glob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = re.search(r'"(GET|SETEX|DEL|SET|MGET|HGET|HSET)"\s+"([^"]+)"', line)
            if not match:
                continue
            events.append({
                "operation": match.group(1),
                "key": match.group(2),
                "source": path.name,
                "raw_redacted": _redact(line),
            })
    return events


def _parse_trace_jsonl_dir(trace_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows = []
    malformed = []
    if not trace_root.is_dir():
        return rows, malformed
    for path in sorted(trace_root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, 1):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                malformed.append(f"{path.name}:{index}")
                continue
            if isinstance(obj, dict):
                obj["_source"] = path.name
                rows.append(obj)
    return rows, malformed


def _cross_tenant_trace_exposures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exposures = []
    for row in rows:
        tenant = row.get("tenant_id")
        rendered_field, rendered = _rendered_scope(row)
        status = str(row.get("status", ""))
        if not tenant or not rendered or tenant == rendered:
            continue
        if status in {"403", "401", "499"}:
            continue
        exposures.append({
            "request_id": str(row.get("request_id", "unknown")),
            "tenant_id": str(tenant),
            "rendered_scope_field": rendered_field,
            "rendered_scope": str(rendered),
            "cache_key": str(row.get("cache_key", "")),
            "cache_result": str(row.get("cache_result", "")),
        })
    return exposures


def _timeline(
    api_events: list[dict[str, str]],
    trace_rows: list[dict[str, Any]],
    nginx_events: list[dict[str, str]],
    exposures: list[dict[str, Any]],
) -> list[dict[str, str]]:
    api_by_req = {event.get("request_id"): event for event in api_events}
    nginx_by_req = {event.get("request_id"): event for event in nginx_events}
    trace_by_req = {str(row.get("request_id")): row for row in trace_rows if row.get("request_id")}
    request_ids = []
    for req in [*api_by_req.keys(), *trace_by_req.keys(), *nginx_by_req.keys()]:
        if req and req not in request_ids:
            request_ids.append(req)
    exposure_ids = {item["request_id"] for item in exposures}
    items = []
    for req in request_ids:
        api = api_by_req.get(req, {})
        trace = trace_by_req.get(req, {})
        nginx = nginx_by_req.get(req, {})
        cache_key = trace.get("cache_key") or api.get("cache_key") or ""
        cache_result = trace.get("cache_result") or api.get("cache") or ""
        tenant = trace.get("tenant_id") or api.get("tenant_id") or ""
        rendered_field, rendered = _rendered_scope(trace)
        if req in exposure_ids:
            summary = (
                f"cache hit for tenant/account `{tenant}` rendered `{rendered}` from `{rendered_field}` "
                f"with cache key `{cache_key}`."
            )
        elif str(trace.get("status") or api.get("status") or nginx.get("status")) in {"403", "401", "499"}:
            summary = "non-success path preserved as non-exposure evidence."
        elif cache_key:
            summary = f"cache `{cache_result}` on key `{cache_key}` for tenant `{tenant}`."
        else:
            summary = "request observed without cache exposure signal."
        evidence = []
        if api:
            evidence.append("api.log")
        if trace:
            evidence.append("trace")
        if nginx:
            evidence.append(f"nginx status {nginx.get('status')}")
        items.append({"request_id": req, "summary": summary, "evidence": ", ".join(evidence)})
    return items[:12]


def _false_leads(
    root: Path,
    api_events: list[dict[str, str]],
    nginx_events: list[dict[str, str]],
    trace_rows: list[dict[str, Any]],
    malformed: list[str],
    redis_events: list[dict[str, str]],
) -> list[str]:
    leads = []
    frontend_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (root / "frontend").rglob("*")
        if path.is_file() and path.suffix in {".js", ".jsx", ".ts", ".tsx"}
    ) if (root / "frontend").is_dir() else ""
    if re.search(r"\bstale\b|\breuse\b|\bstate\b", frontend_text, re.I):
        leads.append("Frontend state reuse is plausible UI risk, but backend traces show cache hits rendering another tenant before the frontend can explain it.")
    denied_ids = {
        event.get("request_id") for event in api_events + nginx_events
        if str(event.get("status")) in {"401", "403"}
    }
    duplicate_ids = {
        req for req in denied_ids
        if sum(1 for event in api_events + nginx_events if event.get("request_id") == req) > 1
    }
    if duplicate_ids:
        leads.append("Duplicate-looking authorization-denied request IDs support authz presence rather than public unauthenticated exposure.")
    elif denied_ids:
        leads.append("Authorization-denied request IDs are preserved as authz evidence, not counted as cache exposure.")
    if any(str(row.get("status")) == "499" for row in trace_rows) or any(event.get("status") == "499" for event in nginx_events):
        leads.append("Client-aborted 499/timezone-offset telemetry is preserved but excluded from exposure evidence.")
    if any(event.get("operation") == "DEL" for event in redis_events):
        leads.append("Cache `DEL` and feature-flag keys look like maintenance noise, not the cross-tenant resource leak.")
    if malformed:
        leads.append(f"Malformed trace rows preserved as bad telemetry, not proof: {', '.join(malformed[:3])}.")
    return leads


def _root_cause(code_findings: list[dict[str, Any]], exposures: list[dict[str, Any]]) -> str:
    if code_findings and exposures:
        details = "; ".join(
            f"`{item['request_id']}` tenant/account `{item['tenant_id']}` rendered `{item['rendered_scope']}` via `{item['cache_key']}`"
            for item in exposures[:3]
        )
        return (
            "Likely cache isolation bug: tenant-owned resource cache keys are not tenant/account namespaced, "
            f"and trace/log evidence shows cross-tenant cached payload reuse ({details})."
        )
    if code_findings:
        return "Likely cache isolation risk: tenant-owned resource cache keys are not tenant/account namespaced, but no correlated exposure event was found."
    return "No high-confidence cache key root cause found."


def _communication_summary(exposures: list[dict[str, Any]]) -> str:
    if not exposures:
        return (
            "We found a tenant-isolation cache risk but no confirmed local cross-tenant render event. "
            "Continue investigation with redacted request IDs and avoid naming affected people."
        )
    reqs = ", ".join(f"`{item['request_id']}`" for item in exposures)
    return (
        f"Confirmed in local evidence: request(s) {reqs} show a tenant-scoped resource request receiving cached data "
        "from another tenant/account namespace. Scope is limited to observed cache keys in this local dataset; "
        "do not claim public auth bypass or enumerate affected users until production telemetry is reviewed."
    )


def _has_tenant_namespace(text: str) -> bool:
    namespace_terms = (
        "tenant",
        "account",
        "organization",
        "organisation",
        "org_id",
        "workspace_id",
        "customer_id",
    )
    return any(term in text for term in namespace_terms)


def _looks_like_tenant_owned_cache_key(text: str) -> bool:
    if "cache_key" not in text:
        return False
    risk_terms = (
        "entity_id",
        "resource_id",
        "user_id",
        "user:",
        "latest",
        "current",
        "list",
        "detail",
        "collection",
        "profile",
        "report",
        "summary",
        "project",
        "team",
    )
    return any(term in text for term in risk_terms) or bool(re.search(r"cache_key\s*=\s*[\"']\w+:", text))


def _cache_key_hint(line: str) -> str:
    for pattern in (
        r"cache_key\s*=\s*f?[\"']([^\"']+)[\"']",
        r"cache_key\s*=\s*([^#\n]+)",
    ):
        match = re.search(pattern, line)
        if match:
            return _redact(match.group(1).strip())
    return ""


def _affected_cache_keys(
    code_findings: list[dict[str, Any]],
    exposures: list[dict[str, Any]],
    cache_events: list[dict[str, str]],
) -> list[str]:
    keys: list[str] = []
    for item in exposures:
        key = str(item.get("cache_key") or "")
        if key and key not in keys:
            keys.append(key)
    for item in code_findings:
        hint = str(item.get("cache_key_hint") or "")
        if hint and hint not in keys:
            keys.append(hint)
    for event in cache_events:
        key = str(event.get("key") or "")
        if key and any(key.startswith(prefix.split(":", 1)[0] + ":") for prefix in keys if ":" in prefix):
            if key not in keys:
                keys.append(key)
    return keys[:12]


def _rendered_scope(row: dict[str, Any]) -> tuple[str, str]:
    for field in (
        "rendered_tenant",
        "payload_tenant",
        "cached_tenant",
        "response_tenant",
        "rendered_account",
        "payload_account",
        "cached_account",
        "response_account",
        "tenant",
        "account_id",
    ):
        if field in row and row.get(field):
            return field, str(row[field])
    return "", ""


def _kv_fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key, value in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)", line):
        fields[key] = value.strip('"')
    return fields


def _extract_request_id(line: str) -> str:
    match = re.search(r"request_id=([A-Za-z0-9_-]+)", line)
    return match.group(1) if match else ""


def _redact(text: str) -> str:
    text = _EMAIL_RE.sub("<email:redacted>", text)
    text = _TOKEN_RE.sub("<token:redacted>", text)
    text = re.sub(r'"customer_name"\s*:\s*"[^"]+"', '"customer_name":"<name:redacted>"', text)
    return text[:500]
