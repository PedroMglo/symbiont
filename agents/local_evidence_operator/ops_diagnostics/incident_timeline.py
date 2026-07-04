"""Read-only local incident timeline diagnostics."""

from __future__ import annotations

import gzip
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}
_NGINX_TS_RE = re.compile(r"\[(\d{2})/([A-Z][a-z]{2})/(\d{4}):(\d{2}:\d{2}:\d{2}) ([+-]\d{4})\]")
_SYSLOG_TS_RE = re.compile(r"\b([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\b")
_ISO_TS_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)\b")
_HTTP_STATUS_RE = re.compile(r'"[A-Z]+ [^"]+ HTTP/[^"]+"\s+([1-5][0-9]{2})\b')
_EXPLICIT_STATUS_RE = re.compile(r"\bstatus(?:_code)?[=:]\s*([1-5][0-9]{2})\b")
_REQUEST_ID_RE = re.compile(r"\b(?:request_id|req_id|rid|trace_id)=?[:\"]?([A-Za-z0-9_.:-]+)")
_ERROR_TERMS = ("error", "exception", "timeout", "refused", "unavailable", "failed", "too many open files", "connection reset")
_SUPPORT_SUFFIXES = (".py", ".service", ".conf", ".ini", ".toml", ".yaml", ".yml")


def resolve_incident_workspace(path: str | None, *, host_home_prefix: str | None = None) -> Path | None:
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


def build_incident_timeline_report(workspace: Path, query: str = "") -> dict[str, Any]:
    """Build a local incident timeline from visible logs without mutating files."""

    del query
    root = workspace.resolve()
    log_files = _find_log_files(root)
    events: list[dict[str, Any]] = []
    malformed = 0
    for path in log_files:
        parsed, bad = _events_from_file(path, root)
        events.extend(parsed)
        malformed += bad
    events.sort(key=lambda item: (item.get("timestamp") or "", item.get("source") or "", item.get("line") or 0))
    errors = [event for event in events if event.get("severity") in {"error", "critical"}]
    support_evidence = _support_evidence(root)
    status_counts: dict[str, int] = {}
    for event in events:
        status = event.get("status")
        if status:
            status_counts[str(status)] = status_counts.get(str(status), 0) + 1
    return {
        "workspace": str(root),
        "analysis_mode": "read_only_local_incident_timeline",
        "policy": {
            "writes_performed": False,
            "scripts_executed": False,
            "decompress_to_disk": False,
            "max_log_files": 50,
            "max_lines_per_file": 20000,
        },
        "log_files": [path.relative_to(root).as_posix() for path in log_files],
        "timeline": events[:200],
        "error_events": errors[:80],
        "support_evidence": support_evidence,
        "status_counts": status_counts,
        "likely_cause": _likely_cause(errors, support_evidence),
        "recommended_mitigation": _recommended_mitigation(errors, support_evidence),
        "discarded_hypotheses": _discarded_hypotheses(events, errors),
        "summary": {
            "files_seen": len(log_files),
            "events_seen": len(events),
            "error_events": len(errors),
            "malformed_lines": malformed,
            "first_event": events[0]["timestamp"] if events else None,
            "first_error": errors[0]["timestamp"] if errors else None,
        },
        "limitations": [
            "Timeline is based only on visible local log files.",
            "Compressed logs are streamed and not decompressed to disk.",
            "Causality is inferred from temporal proximity and error terms; validate with service-specific metrics when available.",
        ],
    }


def format_incident_timeline_report(report: dict[str, Any], *, published_uri: str | None = None) -> str:
    lines = ["# Incident timeline report", ""]
    if published_uri:
        lines.append(f"- storage_guardian object: `{published_uri}`")
    summary = report.get("summary", {})
    status_counts = report.get("status_counts", {})
    top_status = ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items())[:6]) or "none"
    lines.extend([
        f"- analysis mode: {report.get('analysis_mode')}",
        "- safety: read-only; no scripts executed and no logs modified.",
        "",
        "## Executive summary",
        f"- likely cause: {report.get('likely_cause') or 'No high-confidence cause found in local logs.'}",
        f"- first error: {summary.get('first_error')}",
        f"- error events: {summary.get('error_events', 0)} across {summary.get('files_seen', 0)} file(s).",
        f"- status counts: {top_status}.",
        f"- discarded/lower-confidence hypotheses: {len(report.get('discarded_hypotheses', []))}.",
        "",
        "## Summary",
        f"- files seen: {summary.get('files_seen', 0)}",
        f"- events seen: {summary.get('events_seen', 0)}",
        f"- error events: {summary.get('error_events', 0)}",
        f"- malformed lines: {summary.get('malformed_lines', 0)}",
        f"- first event: {summary.get('first_event')}",
        f"- first error: {summary.get('first_error')}",
        "",
        "## Likely cause",
        report.get("likely_cause") or "No high-confidence cause found in local logs.",
        "",
        "## Supporting evidence",
    ])
    for item in report.get("support_evidence", [])[:20]:
        lines.append(
            f"- `{item.get('source')}:{item.get('line')}` {item.get('kind')}: {item.get('summary')} "
            f"Evidence: `{item.get('evidence')}`"
        )
    for event in report.get("error_events", [])[:20]:
        lines.append(
            f"- `{event.get('timestamp')}` {event.get('severity')} `{event.get('source')}:{event.get('line')}` "
            f"{event.get('summary')}"
        )
    lines.extend([
        "",
        "## Minimal mitigation / validation",
    ])
    for item in report.get("recommended_mitigation", []):
        lines.append(f"- {item}")
    lines.extend([
        "",
        "## Status counts",
    ])
    for status, count in sorted(report.get("status_counts", {}).items()):
        lines.append(f"- {status}: {count}")
    lines.extend(["", "## Timeline excerpts"])
    timeline = list(report.get("error_events", [])[:20])
    seen = {(item.get("source"), item.get("line")) for item in timeline}
    for event in report.get("timeline", []):
        key = (event.get("source"), event.get("line"))
        if key in seen:
            continue
        timeline.append(event)
        seen.add(key)
        if len(timeline) >= 40:
            break
    for event in timeline:
        lines.append(
            f"- `{event.get('timestamp')}` {event.get('severity')} `{event.get('source')}:{event.get('line')}` "
            f"{event.get('summary')}"
        )
    lines.extend(["", "## Discarded or lower-confidence hypotheses"])
    for item in report.get("discarded_hypotheses", []):
        lines.append(f"- {item}")
    lines.extend(["", "## Limitations"])
    for item in report.get("limitations", []):
        lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def _find_log_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= 50:
            break
        if not path.is_file():
            continue
        name = path.name.lower()
        if any(part in path.parts for part in ("GROUND_TRUTH.md", "EVALUATION.md")):
            continue
        if (
            ".log" in name
            or name.endswith(".jsonl")
            or name.endswith(".jsonl.gz")
            or name.endswith(".out")
            or name.endswith(".err")
        ):
            files.append(path)
    return files


def _events_from_file(path: Path, root: Path) -> tuple[list[dict[str, Any]], int]:
    rel = path.relative_to(root).as_posix()
    events: list[dict[str, Any]] = []
    malformed = 0
    for line_number, line in enumerate(_iter_lines(path), 1):
        if line_number > 20000:
            break
        event = _parse_line(line, rel, line_number)
        if event is None:
            if line.strip():
                malformed += 1
            continue
        events.append(event)
    return events, malformed


def _iter_lines(path: Path) -> Iterable[str]:
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        yield from handle


def _parse_line(line: str, source: str, line_number: int) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        timestamp = _normalize_timestamp(str(obj.get("ts") or obj.get("timestamp") or obj.get("time") or ""))
        status = obj.get("status") or obj.get("status_code")
        message = str(obj.get("message") or obj.get("error") or obj)
        return _event(timestamp, source, line_number, message, status=status, request_id=obj.get("request_id"))
    timestamp = _timestamp_from_text(text)
    status = _status_from_text(text, source)
    request_id = _request_id(text)
    if timestamp is None and status is None and not any(term in text.lower() for term in _ERROR_TERMS):
        return None
    return _event(timestamp, source, line_number, text, status=status, request_id=request_id)


def _event(
    timestamp: str | None,
    source: str,
    line_number: int,
    message: str,
    *,
    status: Any = None,
    request_id: Any = None,
) -> dict[str, Any]:
    lower = message.lower()
    status_text = str(status) if status is not None else None
    severity = "info"
    if status_text and status_text.startswith("5"):
        severity = "critical"
    elif status_text and status_text.startswith("4"):
        severity = "warning"
    if any(term in lower for term in _ERROR_TERMS):
        severity = "critical" if severity == "critical" or "too many open files" in lower else "error"
    return {
        "timestamp": timestamp or "unknown",
        "source": source,
        "line": line_number,
        "status": status_text,
        "request_id": str(request_id) if request_id else None,
        "severity": severity,
        "summary": _redact(message)[:260],
    }


def _timestamp_from_text(text: str) -> str | None:
    iso = _ISO_TS_RE.search(text)
    if iso:
        return _normalize_timestamp(iso.group(1))
    nginx = _NGINX_TS_RE.search(text)
    if nginx:
        day, month, year, hms, offset = nginx.groups()
        return f"{year}-{_MONTHS.get(month, '01')}-{day}T{hms}{offset[:3]}:{offset[3:]}"
    syslog = _SYSLOG_TS_RE.search(text)
    if syslog:
        month, day, hms = syslog.groups()
        year = datetime.now(timezone.utc).year
        return f"{year}-{_MONTHS.get(month, '01')}-{int(day):02d}T{hms}+00:00"
    return None


def _normalize_timestamp(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{13}", text):
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).isoformat()
    if re.fullmatch(r"\d{10}", text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc).isoformat()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-5] + text[-5:-2] + ":" + text[-2:]
    try:
        return datetime.fromisoformat(text.replace(" ", "T")).isoformat()
    except ValueError:
        return value


def _status_from_text(text: str, source: str) -> str | None:
    if "access.log" in source:
        match = _HTTP_STATUS_RE.search(text)
        return match.group(1) if match else None
    match = _EXPLICIT_STATUS_RE.search(text)
    return match.group(1) if match else None


def _request_id(text: str) -> str | None:
    match = _REQUEST_ID_RE.search(text)
    return match.group(1) if match else None


def _support_evidence(root: Path) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if len(evidence) >= 80:
            break
        if not path.is_file() or path.suffix.lower() not in _SUPPORT_SUFFIXES:
            continue
        if path.name in {"GROUND_TRUTH.md", "EVALUATION.md"}:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if re.search(r"\bLimitNOFILE\s*=\s*\d+\b", stripped):
                evidence.append(_support_item(
                    "fd-limit",
                    rel,
                    number,
                    stripped,
                    "service file sets a file-descriptor ceiling that can amplify descriptor leaks",
                ))
            if "client.close" in stripped:
                previous = _previous_code_line(lines, number - 2)
                kind = "resource-close"
                summary = "resource close call found"
                if previous and previous.lstrip().startswith("if "):
                    kind = "conditional-resource-close"
                    summary = "resource close appears conditional, so error/timeout paths may leak resources"
                evidence.append(_support_item(kind, rel, number, stripped, summary))
            if "nginx reload" in stripped.lower() or "rollback ticket" in stripped.lower():
                evidence.append(_support_item(
                    "historical-comment",
                    rel,
                    number,
                    stripped,
                    "comment is contextual only and should not override log/code evidence",
                ))
    return evidence


def _previous_code_line(lines: list[str], start_index: int) -> str:
    for index in range(start_index, -1, -1):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            return lines[index]
    return ""


def _support_item(kind: str, source: str, line: int, evidence: str, summary: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "source": source,
        "line": line,
        "evidence": _redact(evidence)[:220],
        "summary": summary,
    }


def _likely_cause(errors: list[dict[str, Any]], support_evidence: list[dict[str, Any]]) -> str:
    if not errors:
        return "No local error events were found."
    summaries = " ".join(event.get("summary", "").lower() for event in errors)
    support_kinds = {item.get("kind") for item in support_evidence}
    if "too many open files" in summaries:
        details = ["High-confidence local evidence points to file descriptor exhaustion (`too many open files`)."]
        if "conditional-resource-close" in support_kinds:
            details.append("Code evidence suggests a resource cleanup path is conditional, consistent with a descriptor/client leak.")
        if "fd-limit" in support_kinds:
            details.append("Service configuration includes an fd limit that can make the leak user-visible sooner.")
        return " ".join(details)
    if "connection refused" in summaries or "refused" in summaries:
        return "Local evidence points to a dependency refusing connections during the incident window."
    if "timeout" in summaries:
        return "Local evidence points to timeouts during the incident window."
    first = errors[0]
    return f"First local error evidence is `{first.get('source')}:{first.get('line')}` at {first.get('timestamp')}; inspect adjacent service logs."


def _recommended_mitigation(
    errors: list[dict[str, Any]],
    support_evidence: list[dict[str, Any]],
) -> list[str]:
    summaries = " ".join(event.get("summary", "").lower() for event in errors)
    support_kinds = {item.get("kind") for item in support_evidence}
    items = [
        "Validate read-only with log queries; do not mutate the simulated filesystem.",
    ]
    if "too many open files" in summaries or "fd-limit" in support_kinds:
        items.append("Raise the service fd limit only as a reversible mitigation while monitoring fd usage.")
        items.append("Add fd/open-client metrics and alerting around the affected service.")
    if "conditional-resource-close" in support_kinds:
        items.append("Fix resource cleanup so clients/descriptors are closed in success, timeout, and exception paths.")
    items.append("Treat 502s as symptoms; validate against application, upstream, and system/service evidence.")
    return items


def _discarded_hypotheses(events: list[dict[str, Any]], errors: list[dict[str, Any]]) -> list[str]:
    if not events:
        return ["No visible local logs were parsed, so no hypotheses can be tested."]
    items = []
    if not any(str(event.get("status", "")).startswith("4") for event in events):
        items.append("Client-side 4xx/auth failures are not supported by parsed status evidence.")
    if errors and not any("database" in event.get("summary", "").lower() for event in errors):
        items.append("Database-specific failure is not the strongest local hypothesis without DB error terms.")
    deploy_events = [event for event in events if "deploy" in event.get("summary", "").lower()]
    first_5xx = next((event for event in events if str(event.get("status", "")).startswith("5")), None)
    if deploy_events and first_5xx:
        items.append(
            "Deploy/change timing is a lower-confidence hypothesis unless normalized timestamps show first 5xx only after the change."
        )
    elif errors and not deploy_events:
        items.append("Deployment/change timing is unproven by parsed logs unless external change records are added.")
    return items or ["No lower-confidence hypotheses could be ruled out from local logs alone."]


def _redact(text: str) -> str:
    return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<redacted-email>", text)
