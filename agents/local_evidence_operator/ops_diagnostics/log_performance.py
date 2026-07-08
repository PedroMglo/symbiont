"""Read-only streaming analytics for compressed access logs."""

from __future__ import annotations

import gzip
import json
import math
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_REQUEST_RE = re.compile(r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) ([^ ]+) HTTP/1\.[01]"')
_ACCESS_TS_RE = re.compile(r"\[(\d{2})/([A-Z][a-z]{2})/(\d{4}):")
_JSON_SUFFIX_RE = re.compile(r"\{.*\}\s*$")
_REQUEST_TIME_MS_RE = re.compile(r"request_time=([0-9.]+)ms")
_MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


def resolve_log_workspace(path: str | None, *, host_home_prefix: str | None = None) -> Path | None:
    """Resolve a client workspace path for read-only log inspection."""

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


def build_log_performance_report(
    workspace: Path,
    *,
    baseline_start: str | None = None,
    baseline_end: str | None = None,
    current_start: str | None = None,
    current_end: str | None = None,
    min_requests: int = 1000,
    top_n: int = 100,
) -> dict[str, Any]:
    """Compute p95 latency deltas from gzip logs without extracting to disk."""

    root = workspace.resolve()
    log_root = root / "logs" if (root / "logs").is_dir() else root
    data: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"baseline": [], "current": []})
    parsed_records: list[tuple[str, str, float]] = []
    stats = {
        "files_seen": 0,
        "lines_seen": 0,
        "lines_parsed": 0,
        "malformed_lines": 0,
        "comment_lines": 0,
        "unsupported_seconds_format_lines": 0,
    }

    for gz_path in sorted(log_root.glob("*.gz")):
        if not gz_path.is_file():
            continue
        stats["files_seen"] += 1
        with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stats["lines_seen"] += 1
                parsed = parse_log_line(line)
                if parsed is None:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        stats["comment_lines"] += 1
                    elif "request_time=" in stripped and stripped.endswith("s"):
                        stats["unsupported_seconds_format_lines"] += 1
                    else:
                        stats["malformed_lines"] += 1
                    continue
                parsed_records.append(parsed)
                stats["lines_parsed"] += 1

    periods = _resolve_periods(
        parsed_records,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        current_start=current_start,
        current_end=current_end,
    )
    for day, endpoint, latency_ms in parsed_records:
        week = _week_bucket(
            day,
            baseline_start=periods["baseline_start"],
            baseline_end=periods["baseline_end"],
            current_start=periods["current_start"],
            current_end=periods["current_end"],
        )
        if week is None:
            continue
        data[endpoint][week].append(latency_ms)

    rows: list[dict[str, Any]] = []
    excluded_low_volume = 0
    for endpoint, weeks in data.items():
        baseline_count = len(weeks["baseline"])
        current_count = len(weeks["current"])
        if baseline_count < min_requests or current_count < min_requests:
            excluded_low_volume += 1
            continue
        baseline_p95 = _p95(weeks["baseline"])
        current_p95 = _p95(weeks["current"])
        increase_pct = ((current_p95 - baseline_p95) / baseline_p95 * 100.0) if baseline_p95 else math.inf
        rows.append({
            "endpoint": endpoint,
            "baseline_count": baseline_count,
            "current_count": current_count,
            "baseline_p95_ms": baseline_p95,
            "current_p95_ms": current_p95,
            "increase_pct": increase_pct,
        })

    rows.sort(key=lambda row: row["increase_pct"], reverse=True)
    return {
        "workspace": str(root),
        "log_root": str(log_root),
        "policy": {
            "mode": "read_only_streaming_gzip",
            "decompress_to_disk": False,
            "baseline_week": [periods["baseline_start"], periods["baseline_end"]],
            "current_week": [periods["current_start"], periods["current_end"]],
            "window_source": periods["source"],
            "min_requests_per_week": min_requests,
            "p95_method": "nearest_rank_ceil_0.95_n",
            "normalization": [
                "remove query string",
                "numeric path segments become {id}",
                "uuid path segments become {uuid}",
            ],
            "malformed_handling": [
                "ignore blank/comment/header lines",
                "ignore malformed JSON or non-numeric latency",
                "ignore ambiguous combined lines with seconds-style request_time",
            ],
        },
        "stats": {**stats, "endpoints_seen": len(data), "excluded_low_volume_endpoints": excluded_low_volume},
        "rows": rows[:top_n],
    }


def format_log_performance_report(report: dict[str, Any], *, published_uri: str | None = None) -> str:
    """Render a concise Markdown report for log performance analysis."""

    stats = report.get("stats", {})
    policy = report.get("policy", {})
    rows = list(report.get("rows", []))
    top_rows = rows[:10]
    lines = [
        "# Log performance report",
        "",
        "## Executive summary",
        f"- result: {len(rows)} endpoints met the volume gate and were ranked by p95 increase.",
        f"- top offender: `{top_rows[0]['endpoint']}` at {top_rows[0]['increase_pct']:.2f}% increase." if top_rows else "- top offender: none above the volume gate.",
        f"- parsed evidence: {stats.get('lines_parsed', 0)} lines from {stats.get('files_seen', 0)} gzip file(s).",
        "- next safe step: validate the top endpoint first, then inspect service-specific traces for that route.",
        "",
        "## Policy",
        "- Read gzip logs as streams; no decompression to disk.",
        f"- Baseline week: {policy.get('baseline_week', ['', ''])[0]} through {policy.get('baseline_week', ['', ''])[1]}.",
        f"- Current week: {policy.get('current_week', ['', ''])[0]} through {policy.get('current_week', ['', ''])[1]}.",
        f"- Exclude endpoints with fewer than {policy.get('min_requests_per_week')} requests in either week.",
        "- Normalize endpoints by removing query strings and replacing numeric IDs/UUIDs.",
        f"- p95 method: {policy.get('p95_method')}.",
        "- Malformed/comment/ambiguous lines are ignored and counted.",
    ]
    if published_uri:
        lines.append(f"- storage_guardian object: `{published_uri}`")
    lines.extend([
        "",
        "## Parsing stats",
        f"- files seen: {stats.get('files_seen', 0)}",
        f"- lines seen: {stats.get('lines_seen', 0)}",
        f"- parsed lines: {stats.get('lines_parsed', 0)}",
        f"- malformed lines: {stats.get('malformed_lines', 0)}",
        f"- comment/header lines: {stats.get('comment_lines', 0)}",
        f"- ambiguous seconds-format lines ignored: {stats.get('unsupported_seconds_format_lines', 0)}",
        f"- endpoints excluded by low volume: {stats.get('excluded_low_volume_endpoints', 0)}",
        "",
        "## Top endpoint p95 increases",
        "| rank | endpoint | baseline n | current n | baseline p95 ms | current p95 ms | increase pct |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ])
    for index, row in enumerate(top_rows, 1):
        lines.append(
            f"| {index} | `{row['endpoint']}` | {row['baseline_count']} | {row['current_count']} | "
            f"{row['baseline_p95_ms']:.2f} | {row['current_p95_ms']:.2f} | {row['increase_pct']:.2f}% |"
        )
    if len(rows) > len(top_rows):
        lines.extend([
            "",
            "## Appendix: complete ranked endpoint table",
            "| rank | endpoint | baseline n | current n | baseline p95 ms | current p95 ms | increase pct |",
            "|---:|---|---:|---:|---:|---:|---:|",
        ])
        for index, row in enumerate(rows, 1):
            lines.append(
                f"| {index} | `{row['endpoint']}` | {row['baseline_count']} | {row['current_count']} | "
                f"{row['baseline_p95_ms']:.2f} | {row['current_p95_ms']:.2f} | {row['increase_pct']:.2f}% |"
            )
    lines.extend([
        "",
        "## Limitations",
        "- This report does not infer semantics from malformed or ambiguous lines.",
        "- It reports endpoint templates from observed request paths only.",
    ])
    return "\n".join(lines).strip() + "\n"


def parse_log_line(line: str) -> tuple[str, str, float] | None:
    """Parse one access log line into day, normalized endpoint and latency ms."""

    text = line.strip()
    if not text or text.startswith("#") or text.startswith("MALFORMED"):
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            raw_latency = obj.get("latency_ms", obj.get("latency"))
            if isinstance(raw_latency, str):
                return None
            latency = float(raw_latency)
            if str(obj.get("unit", "")).lower() == "s":
                latency *= 1000.0
            return str(obj["ts"])[:10], normalize_endpoint(str(obj["path"])), latency
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    request = _REQUEST_RE.search(text)
    timestamp = _ACCESS_TS_RE.search(text)
    if not request or not timestamp:
        return None

    suffix = _JSON_SUFFIX_RE.search(text)
    try:
        if suffix:
            suffix_obj = json.loads(suffix.group(0))
            latency = float(suffix_obj["latency_ms"])
        else:
            latency_match = _REQUEST_TIME_MS_RE.search(text)
            if not latency_match:
                return None
            latency = float(latency_match.group(1))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    month = _MONTHS.get(timestamp.group(2))
    if month is None:
        return None
    return f"{timestamp.group(3)}-{month}-{timestamp.group(1)}", normalize_endpoint(request.group(1)), latency


def _resolve_periods(
    records: list[tuple[str, str, float]],
    *,
    baseline_start: str | None,
    baseline_end: str | None,
    current_start: str | None,
    current_end: str | None,
) -> dict[str, str]:
    if baseline_start and baseline_end and current_start and current_end:
        return {
            "baseline_start": baseline_start,
            "baseline_end": baseline_end,
            "current_start": current_start,
            "current_end": current_end,
            "source": "request_parameters",
        }
    dates = sorted(
        item for item in {_parse_iso_date(day) for day, _endpoint, _latency in records} if item is not None
    )
    if not dates:
        return {
            "baseline_start": "",
            "baseline_end": "",
            "current_start": "",
            "current_end": "",
            "source": "no_dates_observed",
        }
    current_end_date = dates[-1]
    current_start_date = current_end_date - timedelta(days=6)
    baseline_end_date = current_start_date - timedelta(days=1)
    baseline_start_date = baseline_end_date - timedelta(days=6)
    return {
        "baseline_start": baseline_start or baseline_start_date.isoformat(),
        "baseline_end": baseline_end or baseline_end_date.isoformat(),
        "current_start": current_start or current_start_date.isoformat(),
        "current_end": current_end or current_end_date.isoformat(),
        "source": "inferred_from_observed_log_dates",
    }


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def normalize_endpoint(path: str) -> str:
    """Normalize dynamic URL paths into stable endpoint templates."""

    path = (path or "").split("?", 1)[0]
    parts: list[str] = []
    for part in path.strip("/").split("/"):
        if re.fullmatch(r"\d+", part):
            parts.append("{id}")
        elif _UUID_RE.fullmatch(part):
            parts.append("{uuid}")
        elif part:
            parts.append(part)
    return "/" + "/".join(parts)


def _week_bucket(
    day: str,
    *,
    baseline_start: str,
    baseline_end: str,
    current_start: str,
    current_end: str,
) -> str | None:
    if baseline_start <= day <= baseline_end:
        return "baseline"
    if current_start <= day <= current_end:
        return "current"
    return None


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]
