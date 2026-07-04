"""Read-only local data quality and drift diagnostics."""

from __future__ import annotations

import csv
import gzip
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_DATA_SUFFIXES = (".csv", ".tsv", ".jsonl", ".ndjson", ".csv.gz", ".tsv.gz", ".jsonl.gz", ".ndjson.gz")
_MAX_FILES = 24
_MAX_ROWS_PER_FILE = 100_000


def resolve_data_workspace(path: str | None, *, host_home_prefix: str | None = None) -> Path | None:
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


def build_data_quality_drift_report(workspace: Path, query: str = "") -> dict[str, Any]:
    """Inspect local tabular/JSONL datasets read-only and compute quality signals."""

    del query
    root = workspace.resolve()
    files = _find_data_files(root)
    dataset_reports = [_inspect_file(path, root) for path in files]
    schema_drift = _schema_drift(dataset_reports)
    event_metrics = _event_metrics(root)
    summary = {
        "files_seen": len(dataset_reports),
        "rows_seen": sum(int(item.get("row_count", 0)) for item in dataset_reports),
        "malformed_rows": sum(int(item.get("malformed_rows", 0)) for item in dataset_reports),
        "duplicate_rows": sum(int(item.get("duplicate_rows", 0)) for item in dataset_reports),
        "schema_drift_groups": len(schema_drift),
    }
    metrics_json = _build_metrics_json(dataset_reports, schema_drift, event_metrics, summary)
    return {
        "workspace": str(root),
        "analysis_mode": "read_only_data_quality_drift",
        "policy": {
            "writes_performed": False,
            "decompress_to_disk": False,
            "max_files": _MAX_FILES,
            "max_rows_per_file": _MAX_ROWS_PER_FILE,
        },
        "datasets": dataset_reports,
        "schema_drift": schema_drift,
        "event_metrics": event_metrics,
        "metrics_json": metrics_json,
        "summary": summary,
        "limitations": [
            "This provider streams gzip inputs and does not decompress datasets to disk.",
            "Types are inferred from observed values and are not a replacement for a declared contract.",
            "Large files are capped per file; reports include whether the cap was reached.",
        ],
    }


def format_data_quality_drift_report(report: dict[str, Any], *, published_uri: str | None = None) -> str:
    drift = report.get("schema_drift", [])
    summary = dict(report.get("summary", {}))
    summary["schema_drift_groups"] = len(drift)
    lines = ["# Data quality drift report", ""]
    if published_uri:
        lines.append(f"- storage_guardian object: `{published_uri}`")
    lines.extend([
        f"- analysis mode: {report.get('analysis_mode')}",
        "- safety: read-only; gzip streams are not decompressed to disk.",
        "",
        "## Summary",
    ])
    for key in ("files_seen", "rows_seen", "malformed_rows", "duplicate_rows", "schema_drift_groups"):
        lines.append(f"- {key}: {summary.get(key, 0)}")

    if report.get("metrics_json"):
        lines.extend([
            "",
            "## Metrics JSON",
            "```json",
            json.dumps(report["metrics_json"], sort_keys=True, indent=2),
            "```",
        ])

    lines.extend(["", "## Dataset metrics"])
    event_metrics = report.get("event_metrics") or {}
    if event_metrics:
        lines.extend(["## Event metrics"])
        for key in (
            "weekly_active_users",
            "weekly_active_accounts",
            "valid_unique_events",
            "invalid_events",
            "duplicates_removed",
            "duplicate_conflicts",
        ):
            lines.append(f"- {key}: {event_metrics.get(key)}")
        if event_metrics.get("retention"):
            lines.append("- retention:")
            for name, value in sorted(event_metrics["retention"].items()):
                lines.append(f"  - {name}: cohort={value.get('cohort')} retained={value.get('retained')}")
        if event_metrics.get("new_fields"):
            lines.append(f"- new_fields: {event_metrics['new_fields']}")
        if event_metrics.get("type_drift"):
            lines.append(f"- type_drift: {event_metrics['type_drift']}")
        if event_metrics.get("missing_accounts"):
            lines.append(f"- missing_accounts: {event_metrics['missing_accounts']}")
        if event_metrics.get("duplicate_user_ids"):
            lines.append(f"- duplicate_user_ids_after_normalization: {event_metrics['duplicate_user_ids']}")
        lines.append("")

    for item in report.get("datasets", []):
        lines.append(f"### `{item.get('path')}`")
        if item.get("error"):
            lines.append(f"- error: {item['error']}")
            continue
        lines.extend([
            f"- format: {item.get('format')}",
            f"- rows: {item.get('row_count')}",
            f"- malformed rows: {item.get('malformed_rows')}",
            f"- duplicate rows: {item.get('duplicate_rows')}",
            f"- row cap reached: {item.get('row_cap_reached')}",
            "- columns:",
        ])
        for col in item.get("columns", []):
            lines.append(
                f"  - `{col['name']}` type={col['type']} nulls={col['null_count']} "
                f"distinct={col['distinct_count']}"
            )
        for time_col in item.get("time_ranges", []):
            lines.append(
                f"- time range `{time_col['column']}`: {time_col['min']} to {time_col['max']} "
                f"(parsed={time_col['parsed_count']})"
            )

    if drift:
        lines.extend(["", "## Schema drift"])
        for item in drift:
            lines.append(
                f"- family `{item['family']}` has {len(item['schemas'])} schemas; "
                f"added/removed columns vary across files: {item['columns_by_file']}"
            )

    lines.extend(["", "## Limitations"])
    for item in report.get("limitations", []):
        lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def _build_metrics_json(
    datasets: list[dict[str, Any]],
    schema_drift: list[dict[str, Any]],
    event_metrics: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "data_quality_metrics.v1",
        "summary": dict(summary),
        "datasets": [
            {
                "path": item.get("path"),
                "format": item.get("format"),
                "row_count": item.get("row_count", 0),
                "malformed_rows": item.get("malformed_rows", 0),
                "duplicate_rows": item.get("duplicate_rows", 0),
                "row_cap_reached": bool(item.get("row_cap_reached")),
                "columns": [
                    {
                        "name": col.get("name"),
                        "type": col.get("type"),
                        "null_count": col.get("null_count", 0),
                        "distinct_count": col.get("distinct_count", 0),
                    }
                    for col in item.get("columns", [])
                ],
                "time_ranges": item.get("time_ranges", []),
                "error": item.get("error"),
            }
            for item in datasets
        ],
        "schema_drift": [
            {
                "family": item.get("family"),
                "schemas": item.get("schemas", []),
                "columns_by_file": item.get("columns_by_file", {}),
            }
            for item in schema_drift
        ],
        "event_metrics": event_metrics or {},
        "policy": {
            "writes_performed": False,
            "decompress_to_disk": False,
            "duplicate_policy": (event_metrics or {}).get("policy", {}).get("duplicate_policy"),
            "id_normalization": (event_metrics or {}).get("policy", {}).get("id_normalization"),
            "timestamp_normalization": (event_metrics or {}).get("policy", {}).get("timestamp_normalization"),
        },
    }


def _find_data_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(paths) >= _MAX_FILES:
            break
        if not path.is_file():
            continue
        lowered = path.name.lower()
        if any(lowered.endswith(suffix) for suffix in _DATA_SUFFIXES):
            paths.append(path)
    return paths


def _inspect_file(path: Path, root: Path) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    fmt = _format(path)
    try:
        rows = _iter_rows(path, fmt)
        return _profile_rows(rel, fmt, rows)
    except OSError as exc:
        return {"path": rel, "format": fmt, "error": str(exc), "row_count": 0}


def _iter_rows(path: Path, fmt: str) -> Iterable[dict[str, Any] | None]:
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as handle:
        if fmt in {"csv", "tsv"}:
            delimiter = "\t" if fmt == "tsv" else ","
            reader = csv.DictReader(handle, delimiter=delimiter)
            for row in reader:
                yield dict(row)
            return
        for line in handle:
            stripped = line.strip()
            if not stripped:
                yield None
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                yield None
                continue
            yield obj if isinstance(obj, dict) else None


def _event_metrics(root: Path) -> dict[str, Any]:
    event_files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name.lower().startswith("events-") and path.name.lower().endswith((".jsonl", ".jsonl.gz", ".ndjson", ".ndjson.gz"))
    )
    if not event_files:
        return {}
    expected_schema = _load_expected_schema(root)
    users = _load_csv_by_name(root, "users.csv")
    accounts = _load_csv_by_name(root, "accounts.csv")
    account_ids = {
        str(row.get("account_id", "")).strip()
        for row in accounts
        if str(row.get("account_id", "")).strip()
    }
    duplicate_user_ids = sorted(
        key for key, count in Counter(_normalize_id(row.get("user_id")) for row in users).items()
        if key and count > 1
    )

    seen_events: dict[str, str] = {}
    valid_events: list[dict[str, Any]] = []
    invalid_events = 0
    duplicates_removed = 0
    duplicate_conflicts = 0
    new_fields: set[str] = set()
    type_drift: dict[str, set[str]] = defaultdict(set)

    for path in event_files[:_MAX_FILES]:
        for row in _iter_rows(path, _format(path)):
            if row is None:
                invalid_events += 1
                continue
            event_id_raw = row.get("event_id")
            event_id = str(event_id_raw) if event_id_raw is not None else ""
            timestamp = _parse_time_value(row.get("timestamp"))
            if not event_id or timestamp is None:
                invalid_events += 1
                _collect_drift(row, expected_schema, new_fields, type_drift)
                continue
            _collect_drift(row, expected_schema, new_fields, type_drift)
            canonical = json.dumps(row, sort_keys=True, default=str)
            if event_id in seen_events:
                if seen_events[event_id] == canonical:
                    duplicates_removed += 1
                else:
                    duplicate_conflicts += 1
                continue
            seen_events[event_id] = canonical
            enriched = dict(row)
            enriched["_timestamp"] = timestamp
            enriched["_user_id"] = _normalize_id(row.get("user_id"))
            enriched["_account_id"] = str(row.get("account_id", "")).strip()
            enriched["_source_file"] = path.name
            valid_events.append(enriched)

    if not valid_events and not invalid_events:
        return {}

    retention = _retention_metrics(users, valid_events)
    missing_accounts = sorted({event["_account_id"] for event in valid_events if event.get("_account_id")} - account_ids)
    return {
        "weekly_active_users": len({event["_user_id"] for event in valid_events if event.get("_user_id")}),
        "weekly_active_accounts": len({event["_account_id"] for event in valid_events if event.get("_account_id")}),
        "valid_unique_events": len(valid_events),
        "invalid_events": invalid_events,
        "duplicates_removed": duplicates_removed,
        "duplicate_conflicts": duplicate_conflicts,
        "retention": retention,
        "new_fields": sorted(new_fields),
        "type_drift": {key: sorted(values) for key, values in sorted(type_drift.items())},
        "missing_accounts": missing_accounts,
        "duplicate_user_ids": duplicate_user_ids,
        "policy": {
            "id_normalization": "IDs are converted to strings; numeric-looking IDs have leading zeroes removed.",
            "timestamp_normalization": "Epoch seconds, epoch milliseconds, ISO Z and offset timestamps are normalized to UTC.",
            "duplicate_policy": "Identical duplicate event_id rows are removed; conflicting duplicate event_id rows keep the first valid event and count a conflict.",
        },
    }


def _load_expected_schema(root: Path) -> dict[str, str]:
    for path in sorted(root.rglob("expected_schema.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items()}
    return {}


def _load_csv_by_name(root: Path, name: str) -> list[dict[str, str]]:
    for path in sorted(root.rglob(name)):
        try:
            with open(path, "rt", encoding="utf-8", errors="replace", newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except OSError:
            return []
    return []


def _normalize_id(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if text.isdigit():
        return str(int(text))
    return text


def _parse_time_value(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        seconds = int(text)
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _collect_drift(
    row: dict[str, Any],
    expected_schema: dict[str, str],
    new_fields: set[str],
    type_drift: dict[str, set[str]],
) -> None:
    if not expected_schema:
        return
    for key, value in row.items():
        key_text = str(key)
        if key_text not in expected_schema:
            new_fields.add(key_text)
            continue
        if key_text in {"user_id", "account_id", "timestamp"}:
            continue
        if not _value_matches_expected(value, expected_schema[key_text]):
            type_drift[key_text].add(type(value).__name__)


def _value_matches_expected(value: Any, expected: str) -> bool:
    normalized = expected.lower()
    if normalized in {"string", "iso8601-string"}:
        return isinstance(value, str)
    if normalized in {"integer", "int"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized in {"number", "float"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized in {"boolean", "bool"}:
        return isinstance(value, bool)
    return True


def _retention_metrics(users: list[dict[str, str]], events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    if not users:
        return {}
    event_dates_by_user: dict[str, set] = defaultdict(set)
    event_dates = []
    for event in events:
        ts = event.get("_timestamp")
        if isinstance(ts, datetime):
            event_dates.append(ts.date())
            event_dates_by_user[event.get("_user_id", "")].add(ts.date())
    if not event_dates:
        return {}
    min_event_date = min(event_dates)
    max_event_date = max(event_dates)
    result: dict[str, dict[str, int]] = {}
    canonical_users: dict[str, dict[str, str]] = {}
    for user in users:
        user_id = _normalize_id(user.get("user_id"))
        if user_id and user_id not in canonical_users:
            canonical_users[user_id] = user
    for days in (1, 7, 30):
        cohort = 0
        retained = 0
        for user_id, user in canonical_users.items():
            created = _parse_time_value(user.get("created_at"))
            if created is None:
                continue
            target = created.date() + _dt_days(days)
            if min_event_date <= target <= max_event_date:
                cohort += 1
                if target in event_dates_by_user.get(user_id, set()):
                    retained += 1
        result[f"D{days}"] = {"cohort": cohort, "retained": retained}
    return result


def _dt_days(days: int):
    from datetime import timedelta

    return timedelta(days=days)


def _profile_rows(path: str, fmt: str, rows: Iterable[dict[str, Any] | None]) -> dict[str, Any]:
    row_count = 0
    malformed = 0
    duplicates = 0
    seen_hashes: set[str] = set()
    columns: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "null_count": 0,
        "values": Counter(),
        "types": Counter(),
        "time_values": [],
    })
    for row in rows:
        if row_count >= _MAX_ROWS_PER_FILE:
            break
        if row is None:
            malformed += 1
            continue
        row_count += 1
        row_key = json.dumps(row, sort_keys=True, default=str)
        if row_key in seen_hashes:
            duplicates += 1
        else:
            seen_hashes.add(row_key)
        for key, value in row.items():
            col = columns[str(key)]
            if value in (None, ""):
                col["null_count"] += 1
                col["types"]["null"] += 1
                continue
            typ = _infer_type(value)
            col["types"][typ] += 1
            text = str(value)
            if len(col["values"]) < 1000:
                col["values"][text] += 1
            if _looks_time_column(str(key)):
                parsed = _parse_time(text)
                if parsed is not None:
                    col["time_values"].append(parsed)

    col_reports = []
    time_ranges = []
    for name, data in sorted(columns.items()):
        col_type = data["types"].most_common(1)[0][0] if data["types"] else "unknown"
        col_reports.append({
            "name": name,
            "type": col_type,
            "null_count": int(data["null_count"]),
            "distinct_count": len(data["values"]),
        })
        if data["time_values"]:
            values = sorted(data["time_values"])
            time_ranges.append({
                "column": name,
                "min": values[0].isoformat(),
                "max": values[-1].isoformat(),
                "parsed_count": len(values),
            })

    return {
        "path": path,
        "format": fmt,
        "row_count": row_count,
        "malformed_rows": malformed,
        "duplicate_rows": duplicates,
        "row_cap_reached": row_count >= _MAX_ROWS_PER_FILE,
        "columns": col_reports,
        "time_ranges": time_ranges,
    }


def _schema_drift(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in datasets:
        if item.get("error"):
            continue
        groups[_family(item.get("path", ""))].append(item)
    drift = []
    for family, items in sorted(groups.items()):
        schemas = {tuple(col["name"] for col in item.get("columns", [])) for item in items}
        if len(schemas) <= 1:
            continue
        drift.append({
            "family": family,
            "schemas": [list(schema) for schema in sorted(schemas)],
            "columns_by_file": {
                item["path"]: [col["name"] for col in item.get("columns", [])]
                for item in items
            },
        })
    return drift


def _format(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".csv") or name.endswith(".csv.gz"):
        return "csv"
    if name.endswith(".tsv") or name.endswith(".tsv.gz"):
        return "tsv"
    return "jsonl"


def _family(path: str) -> str:
    name = Path(path).name.lower()
    for suffix in _DATA_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return "".join("0" if ch.isdigit() else ch for ch in name).strip("-_.") or name


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    text = str(value)
    try:
        int(text)
        return "int"
    except ValueError:
        pass
    try:
        float(text)
        return "float"
    except ValueError:
        pass
    if _parse_time(text) is not None:
        return "timestamp"
    return "string"


def _looks_time_column(name: str) -> bool:
    lower = name.lower()
    return any(term in lower for term in ("time", "date", "ts", "timestamp", "created", "updated"))


def _parse_time(value: str) -> datetime | None:
    return _parse_time_value(value)
