"""Read-only SQLite reconciliation diagnostics for local workspaces."""

from __future__ import annotations

import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from sharedai.evidence.reporting import append_key_value_section, append_storage_reference

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


_SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
_KEY_HINTS = ("id", "_id", "key", "number", "code", "ref", "uuid")
_AMOUNT_HINTS = ("amount", "total", "subtotal", "balance", "price", "cost", "paid", "due", "value")
_TAX_HINTS = ("tax", "vat", "gst", "hst")
_DATE_HINTS = ("date", "_at", "_on", "timestamp", "time")
_POSITIVE_STATUS_VALUES = ("settled", "paid", "succeeded", "success", "completed", "captured", "posted")
_NEGATIVE_STATUS_VALUES = ("canceled", "cancelled", "void", "voided", "deleted", "failed", "reversed")
_MUTATING_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|VACUUM|ATTACH|DETACH|PRAGMA\s+(?!query_only\\b))\b",
    re.IGNORECASE,
)


def resolve_sql_workspace(path: str | None, *, host_home_prefix: str | None = None) -> Path | None:
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


def build_sql_reconcile_report(workspace: Path, query: str = "") -> dict[str, Any]:
    """Inspect SQLite databases read-only and compute generic reconciliation signals."""

    root = workspace.resolve()
    db_paths = _find_sqlite_files(root)
    visible_sql = _find_visible_sql_files(root)
    instruction_files = _read_instruction_files(root)
    instruction_text = "\n\n".join(item.get("content", "") for item in instruction_files)
    reports = [
        _inspect_database(
            path,
            root,
            query=query,
            visible_sql=visible_sql,
            instruction_text=instruction_text,
        )
        for path in db_paths
    ]
    return {
        "workspace": str(root),
        "analysis_mode": "read_only_sqlite_reconciliation",
        "policy": {
            "open_mode": "sqlite_uri_mode_ro",
            "query_only": True,
            "mutating_sql": False,
            "max_databases": 6,
            "max_pairwise_join_checks": 40,
        },
        "visible_sql_files": [item["path"] for item in visible_sql],
        "instruction_files": [
            {
                "path": item["path"],
                "excerpt": item["content"][:1200],
            }
            for item in instruction_files
        ],
        "databases": reports,
        "summary": _summary(reports),
        "limitations": [
            "This provider does not mutate databases and does not execute user-supplied non-SELECT SQL.",
            "Pairwise checks use shared column names and type hints; they are evidence for investigation, not schema proof.",
            "Business meaning is taken only from visible local instructions and should be validated by domain owners.",
        ],
    }


def format_sql_reconcile_report(report: dict[str, Any], *, published_uri: str | None = None) -> str:
    lines = ["# SQL reconciliation report", ""]
    append_storage_reference(lines, published_uri)
    summary = report.get("summary", {})
    append_key_value_section(
        lines,
        "Executive summary",
        [
            ("result", f"{summary.get('databases_seen', 0)} database(s), {summary.get('tables_seen', 0)} table(s) inspected read-only"),
            ("duplicate_key_groups", summary.get("duplicate_key_groups", 0)),
            ("join_risk_count", summary.get("join_risk_count", 0)),
            ("validated_metric_candidates", summary.get("validated_metric_candidates", 0)),
            ("next_safe_step", "review validated SELECT results and reconcile only factors whose values are proven by queries"),
        ],
    )
    lines.extend([
        f"- analysis mode: {report.get('analysis_mode')}",
        "- safety: SQLite opened read-only with query-only policy; no writes executed.",
        "",
        "## Summary",
    ])
    for key in ("databases_seen", "tables_seen", "duplicate_key_groups", "join_risk_count", "numeric_total_count"):
        lines.append(f"- {key}: {summary.get(key, 0)}")
    if summary.get("validated_metric_candidates"):
        lines.append(f"- validated_metric_candidates: {summary.get('validated_metric_candidates', 0)}")
    if report.get("visible_sql_files"):
        lines.append(f"- visible_sql_files_executed: {len(report.get('visible_sql_files', []))}")
    if report.get("instruction_files"):
        lines.append(f"- visible_instruction_files_read: {len(report.get('instruction_files', []))}")

    if report.get("instruction_files"):
        lines.extend(["", "## Visible instructions considered"])
        for item in report["instruction_files"][:6]:
            excerpt = str(item.get("excerpt", "")).strip()
            lines.append(f"### `{item.get('path')}`")
            if excerpt:
                lines.extend(["```text", excerpt, "```"])

    for db in report.get("databases", []):
        rel = db.get("path", "")
        lines.extend(["", f"## Database `{rel}`"])
        if db.get("error"):
            lines.append(f"- error: {db['error']}")
            continue
        lines.extend([
            "",
            "### Tables",
            "| table | rows | columns |",
            "|---|---:|---|",
        ])
        for table in db.get("tables", []):
            lines.append(f"| `{table['name']}` | {table['row_count']} | {', '.join(table.get('columns', []))} |")

        if db.get("numeric_totals"):
            lines.extend(["", "### Numeric totals"])
            for item in db["numeric_totals"][:20]:
                lines.append(
                    f"- `{item['table']}.{item['column']}` sum={item['sum']} non_null={item['non_null_count']}"
                )

        if db.get("duplicate_keys"):
            lines.extend(["", "### Duplicate key evidence"])
            for item in db["duplicate_keys"][:20]:
                lines.append(
                    f"- `{item['table']}.{item['column']}` duplicate_groups={item['duplicate_groups']} "
                    f"duplicate_rows={item['duplicate_rows']}"
                )

        if db.get("join_risks"):
            lines.extend(["", "### Join cardinality risks"])
            for item in db["join_risks"][:20]:
                lines.append(
                    f"- `{item['left_table']}.{item['column']}` <-> `{item['right_table']}.{item['column']}`: "
                    f"left_dupe_values={item['left_duplicate_values']}, "
                    f"right_dupe_values={item['right_duplicate_values']}, "
                    f"estimated_join_rows={item['estimated_join_rows']}"
                )

        if db.get("metric_candidates"):
            lines.extend(["", "### Candidate metric reconciliation"])
            for item in db["metric_candidates"][:4]:
                lines.append(f"#### `{item.get('name', 'candidate')}`")
                if item.get("skipped"):
                    lines.append(f"- skipped: {item['skipped']}")
                    continue
                if item.get("error"):
                    lines.append(f"- error: {item['error']}")
                    continue
                lines.append(f"- result: {item.get('result')}")
                if item.get("matched_targets"):
                    lines.append("- matched visible targets:")
                    for target in item["matched_targets"][:8]:
                        lines.append(f"  - {target.get('label')}: {target.get('value')}")
                if item.get("deltas"):
                    lines.append("- deltas against visible SQL/results:")
                    for delta in item["deltas"][:8]:
                        lines.append(
                            f"  - {delta.get('label')}: candidate_minus_reference={delta.get('delta')}"
                        )
                if item.get("exclusion_summary"):
                    lines.append("- exclusion summary:")
                    for key, value in item["exclusion_summary"].items():
                        lines.append(f"  - {key}: {value}")
                if item.get("explanation"):
                    lines.append("- explanation from validated SQL:")
                    status = item["explanation"].get("decomposition_status")
                    if status:
                        lines.append(f"  - decomposition_status: {status}")
                    for part in item["explanation"].get("factors", [])[:10]:
                        value = part.get("amount") if part.get("amount") is not None else part.get("count")
                        value_text = f" value={value}" if value is not None else ""
                        lines.append(f"  - {part.get('category')}: {part.get('evidence')}{value_text}")
                if item.get("breakdown_rows"):
                    lines.append("- included row sample:")
                    for row in item["breakdown_rows"][:12]:
                        lines.append(f"  - {row}")
                if item.get("sql"):
                    lines.extend(["", "Validated candidate SQL:", "```sql", item["sql"].rstrip(), "```"])
                if item.get("breakdown_sql"):
                    lines.extend(["Breakdown SQL:", "```sql", item["breakdown_sql"].rstrip(), "```"])

        if db.get("visible_sql_results"):
            lines.extend(["", "### Visible SQL results"])
            for item in db["visible_sql_results"][:8]:
                lines.append(f"#### `{item['path']}`")
                if item.get("skipped"):
                    lines.append(f"- skipped: {item['skipped']}")
                    continue
                if item.get("error"):
                    lines.append(f"- error: {item['error']}")
                    continue
                lines.append(f"- columns: {', '.join(item.get('columns', []))}")
                lines.append(f"- rows_returned: {item.get('row_count', 0)}")
                for row in item.get("rows", [])[:10]:
                    lines.append(f"- {row}")

        if db.get("suggested_queries"):
            lines.extend(["", "### Reproducible read-only queries"])
            for query in db["suggested_queries"][:20]:
                lines.extend(["```sql", query.rstrip(), "```"])

    lines.extend(["", "## Limitations"])
    for item in report.get("limitations", []):
        lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def _find_sqlite_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(paths) >= 6:
            break
        if not path.is_file():
            continue
        if path.name in {"GROUND_TRUTH.md", "EVALUATION.md"}:
            continue
        if path.suffix.lower() not in _SQLITE_SUFFIXES:
            continue
        paths.append(path)
    return paths


def _find_visible_sql_files(root: Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.sql")):
        if len(files) >= 12:
            break
        if not path.is_file() or _is_hidden_evaluation_path(path):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append({"path": path.relative_to(root).as_posix(), "content": content})
    return files


def _read_instruction_files(root: Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    preferred = []
    for path in sorted(root.rglob("*")):
        if len(preferred) >= 8:
            break
        if not path.is_file() or _is_hidden_evaluation_path(path):
            continue
        if path.suffix.lower() not in {".md", ".txt"}:
            continue
        lower = path.name.lower()
        if lower in {"task.md", "readme.md"} or any(term in lower for term in ("rule", "spec", "instruction", "expected")):
            preferred.append(path)
    for path in preferred:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append({"path": path.relative_to(root).as_posix(), "content": content[:4000]})
    return files


def _is_hidden_evaluation_path(path: Path) -> bool:
    name = path.name.lower()
    return name in {"ground_truth.md", "evaluation.md"} or "evaluation" in name


def _inspect_database(
    path: Path,
    root: Path,
    *,
    query: str,
    visible_sql: list[dict[str, str]],
    instruction_text: str,
) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    try:
        conn = _connect_read_only(path)
    except sqlite3.Error as exc:
        return {"path": rel, "error": f"open_failed:{exc}", "tables": []}
    try:
        tables = _tables(conn)
        table_reports = [_table_report(conn, table) for table in tables]
        numeric_totals = _numeric_totals(conn, table_reports)
        duplicate_keys = _duplicate_keys(conn, table_reports)
        join_risks = _join_risks(conn, table_reports)
        visible_sql_results = _execute_visible_sql(conn, visible_sql)
        metric_candidates = _metric_candidates(
            conn,
            table_reports,
            visible_sql_results,
            visible_sql,
            instruction_text=instruction_text,
            query=query,
        )
        return {
            "path": rel,
            "query_terms": _query_terms(query),
            "tables": table_reports,
            "numeric_totals": numeric_totals,
            "duplicate_keys": duplicate_keys,
            "join_risks": join_risks,
            "visible_sql_results": visible_sql_results,
            "metric_candidates": metric_candidates,
            "suggested_queries": _suggested_queries(duplicate_keys, join_risks, numeric_totals),
        }
    finally:
        conn.close()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(_sql("execute_347.sql"))
    return conn


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        _sql("execute_353.sql")
    ).fetchall()
    return [str(row["name"]) for row in rows]


def _table_report(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    quoted = _quote_ident(table)
    columns_raw = conn.execute(_sql("fstring_372.sql").format(quoted)).fetchall()
    columns = [
        {
            "name": str(row["name"]),
            "type": str(row["type"] or ""),
            "notnull": bool(row["notnull"]),
            "pk": bool(row["pk"]),
        }
        for row in columns_raw
    ]
    row_count = int(conn.execute(_sql("fstring_382_5.sql").format(quoted)).fetchone()["n"])
    return {
        "name": table,
        "row_count": row_count,
        "columns": [col["name"] for col in columns],
        "column_details": columns,
        "key_candidates": _key_candidates(columns),
        "numeric_candidates": _numeric_candidates(columns),
    }


def _numeric_totals(conn: sqlite3.Connection, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: list[dict[str, Any]] = []
    for table in tables:
        table_name = table["name"]
        for column in table.get("numeric_candidates", []):
            row = conn.execute(
                _sql("fstring_399_6.sql").format(_quote_ident(column), _quote_ident(column), _quote_ident(table_name))
            ).fetchone()
            if row is None:
                continue
            total = row["total"]
            totals.append({
                "table": table_name,
                "column": column,
                "non_null_count": int(row["non_null_count"] or 0),
                "sum": round(float(total), 6) if total is not None else None,
            })
    return totals


def _duplicate_keys(conn: sqlite3.Connection, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    duplicates: list[dict[str, Any]] = []
    for table in tables:
        table_name = table["name"]
        for column in table.get("key_candidates", []):
            row = conn.execute(
                _sql("fstring_420_7.sql").format(_quote_ident(column), _quote_ident(table_name), _quote_ident(column), _quote_ident(column))
            ).fetchone()
            if row is None or int(row["groups_count"] or 0) == 0:
                continue
            duplicates.append({
                "table": table_name,
                "column": column,
                "duplicate_groups": int(row["groups_count"] or 0),
                "duplicate_rows": int(row["duplicate_rows"] or 0),
            })
    return duplicates


def _join_risks(conn: sqlite3.Connection, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    by_column: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for table in tables:
        for column in table.get("key_candidates", []):
            by_column[column.lower()].append({"table": table["name"], "column": column})

    checks = 0
    for _, refs in sorted(by_column.items()):
        if len(refs) < 2:
            continue
        for i, left in enumerate(refs):
            for right in refs[i + 1 :]:
                if checks >= 40:
                    return risks
                checks += 1
                stats = _join_stats(conn, left["table"], right["table"], left["column"], right["column"])
                if stats["estimated_join_rows"] > max(stats["left_rows"], stats["right_rows"]):
                    risks.append({
                        **stats,
                        "left_table": left["table"],
                        "right_table": right["table"],
                        "column": left["column"],
                    })
    risks.sort(key=lambda item: item["estimated_join_rows"], reverse=True)
    return risks


def _join_stats(
    conn: sqlite3.Connection,
    left_table: str,
    right_table: str,
    left_column: str,
    right_column: str,
) -> dict[str, int]:
    left_qt = _quote_ident(left_table)
    right_qt = _quote_ident(right_table)
    left_qc = _quote_ident(left_column)
    right_qc = _quote_ident(right_column)
    row = conn.execute(
        _sql("fstring_477_2.sql").format(left_qc, left_qt, left_qc, left_qc, right_qc, right_qt, right_qc, right_qc)
    ).fetchone()
    left_rows = conn.execute(_sql("fstring_490_3.sql").format(left_qt)).fetchone()["n"]
    right_rows = conn.execute(_sql("fstring_491_4.sql").format(right_qt)).fetchone()["n"]
    return {
        "left_rows": int(left_rows or 0),
        "right_rows": int(right_rows or 0),
        "estimated_join_rows": int(row["estimated_join_rows"] or 0),
        "left_duplicate_values": int(row["left_duplicate_values"] or 0),
        "right_duplicate_values": int(row["right_duplicate_values"] or 0),
    }


def _execute_visible_sql(conn: sqlite3.Connection, visible_sql: list[dict[str, str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in visible_sql:
        sql = item.get("content", "")
        path = item.get("path", "")
        cleaned = _strip_sql_comments(sql).strip()
        if not cleaned:
            results.append({"path": path, "skipped": "empty_sql"})
            continue
        if not _is_read_only_query(cleaned):
            results.append({"path": path, "skipped": "not_select_or_contains_mutating_sql"})
            continue
        try:
            cursor = conn.execute(cleaned)
            rows = cursor.fetchmany(25)
        except sqlite3.Error as exc:
            results.append({"path": path, "error": str(exc)})
            continue
        columns = [desc[0] for desc in (cursor.description or [])]
        rendered_rows = []
        for row in rows:
            rendered_rows.append({col: row[col] for col in columns})
        results.append({
            "path": path,
            "columns": columns,
            "row_count": len(rendered_rows),
            "rows": rendered_rows,
        })
    return results


def _strip_sql_comments(sql: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return "\n".join(line for line in without_block.splitlines() if not line.strip().startswith("--"))


def _is_read_only_query(sql: str) -> bool:
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return False
    if _MUTATING_SQL.search(stripped):
        return False
    return stripped[:6].lower() == "select" or stripped[:4].lower() == "with"


def _metric_candidates(
    conn: sqlite3.Connection,
    tables: list[dict[str, Any]],
    visible_sql_results: list[dict[str, Any]],
    visible_sql: list[dict[str, str]],
    *,
    instruction_text: str,
    query: str,
) -> list[dict[str, Any]]:
    """Build and execute generic read-only metric reconciliation candidates."""

    context_text = "\n\n".join([query or "", instruction_text or "", *(item.get("content", "") for item in visible_sql)])
    targets = _numeric_targets(context_text)
    roles = _infer_metric_roles(tables, context_text)
    if not roles.get("base") or not roles.get("line"):
        return [{
            "name": "generic_metric_workbench",
            "skipped": "no_parent_line_metric_shape_detected",
            "role_summary": _role_summary(roles),
        }]

    candidate = _build_metric_candidate_sql(roles, context_text)
    if not candidate:
        return [{
            "name": "generic_metric_workbench",
            "skipped": "insufficient_columns_for_candidate_query",
            "role_summary": _role_summary(roles),
        }]

    try:
        row = conn.execute(candidate["sql"]).fetchone()
        result = _first_numeric_value(row)
        breakdown_rows = _fetch_rows(conn, candidate["breakdown_sql"], limit=50)
        exclusion_summary = _fetch_one_dict(conn, candidate["exclusion_sql"])
    except sqlite3.Error as exc:
        return [{
            "name": "generic_metric_workbench",
            "error": str(exc),
            "role_summary": _role_summary(roles),
            "sql": candidate.get("sql"),
        }]

    matched_targets = _matched_targets(result, targets)
    visible_numbers = _visible_result_numbers(visible_sql_results)
    deltas = _candidate_deltas(result, visible_numbers)
    explanation = _explain_metric_candidate(
        result=result,
        deltas=deltas,
        exclusion_summary=exclusion_summary,
        breakdown_rows=breakdown_rows,
        roles=roles,
    )
    return [{
        "name": "generic_metric_workbench",
        "result": result,
        "matched_targets": matched_targets,
        "deltas": deltas,
        "exclusion_summary": exclusion_summary,
        "explanation": explanation,
        "breakdown_rows": breakdown_rows,
        "role_summary": _role_summary(roles),
        "targets_seen": targets[:12],
        "sql": candidate["sql"],
        "breakdown_sql": candidate["breakdown_sql"],
    }]


def _infer_metric_roles(tables: list[dict[str, Any]], text: str) -> dict[str, Any]:
    role: dict[str, Any] = {}
    by_name = {table["name"]: table for table in tables}
    base = _infer_base_table(tables)
    if base:
        role["base"] = base
        base_key = _preferred_key(base)
        if base_key:
            role["base_key"] = base_key
            role["base_date"] = _preferred_date_column(base)
            role["base_status"] = _first_column_matching(base, ("status", "state"))
            role["base_currency"] = _first_column_matching(base, ("currency", "currency_code"))

            line = _infer_line_table(tables, base["name"], base_key)
            if line:
                role["line"] = line
                role["line_fk"] = _matching_column(line, base_key)
                role["line_amounts"] = _line_amount_columns(line, text)
                role["line_excluded_tax"] = _tax_columns(line) if _mentions_tax_exclusion(text) else []

            settlement = _infer_settlement_table(tables, base["name"], base_key)
            if settlement:
                role["settlement"] = settlement
                role["settlement_fk"] = _matching_column(settlement, base_key)
                role["settlement_date"] = _preferred_date_column(settlement)
                role["settlement_status"] = _first_column_matching(settlement, ("status", "state"))
                role["settlement_currency"] = _first_column_matching(settlement, ("currency", "currency_code"))

            rate = _infer_rate_table(tables)
            if rate:
                role["rate"] = rate
                role["rate_currency"] = _first_column_matching(rate, ("currency", "currency_code"))
                role["rate_date"] = _preferred_date_column(rate)
                role["rate_value"] = _rate_value_column(rate)

            adjustment = _infer_adjustment_table(tables, base["name"], base_key)
            if adjustment:
                role["adjustment"] = adjustment
                role["adjustment_fk"] = _matching_column(adjustment, base_key)
                role["adjustment_date"] = _preferred_date_column(adjustment)
                role["adjustment_amount"] = _preferred_amount_column(adjustment)

            for column in base.get("columns", []):
                lower = column.lower()
                if lower.endswith("_id") and column != base_key:
                    target = by_name.get(lower[:-3] + "s") or by_name.get(lower[:-3])
                    if target and _has_exclusion_flag(target):
                        role["exclusion_dimension"] = target
                        role["exclusion_base_fk"] = column
                        role["exclusion_dim_key"] = _matching_column(target, column) or _preferred_key(target)
                    if target and _has_period_column(target):
                        role["period_dimension"] = target
                        role["period_base_fk"] = column
                        role["period_dim_key"] = _matching_column(target, column) or _preferred_key(target)

            if "exclusion_dimension" not in role:
                dim = _infer_exclusion_dimension(tables, base)
                if dim:
                    role.update(dim)
            if "period_dimension" not in role:
                period = _infer_period_dimension(tables, base)
                if period:
                    role.update(period)

    role["date_range"] = _extract_date_range(text)
    role["close_date"] = _extract_close_date(text)
    role["use_settlement_date_for_rate"] = bool(re.search(r"\b(payment|settlement|settled|paid)\b.*\b(rate|fx|conversion)\b", text, re.I))
    role["monthly_recognition"] = bool(re.search(r"\b(monthly|month)\b.*\b(recogn|amorti[sz]|defer|spread)\b|\bannual\b", text, re.I))
    return role


def _build_metric_candidate_sql(roles: dict[str, Any], text: str) -> dict[str, str] | None:
    base = roles.get("base")
    line = roles.get("line")
    base_key = roles.get("base_key")
    base_date = roles.get("base_date")
    line_fk = roles.get("line_fk")
    line_amounts = roles.get("line_amounts") or []
    if not base or not line or not base_key or not line_fk or not line_amounts:
        return None

    base_t = _quote_ident(base["name"])
    line_t = _quote_ident(line["name"])
    base_key_q = _quote_ident(base_key)
    line_fk_q = _quote_ident(line_fk)
    amount_expr = " + ".join(f"COALESCE(l.{_quote_ident(col)}, 0)" for col in line_amounts)
    tax_columns = roles.get("line_excluded_tax") or []
    tax_expr = " + ".join(f"COALESCE(l.{_quote_ident(col)}, 0)" for col in tax_columns) or "0"
    ctes = [
        "line_amounts AS (",
        _sql("builder_687_4.sql").format(line_fk_q),
        f"         SUM({amount_expr}) AS measure_cents,",
        f"         SUM({tax_expr}) AS excluded_tax_cents",
        f"  FROM {line_t} l",
        f"  GROUP BY l.{line_fk_q}",
        ")",
    ]
    joins = [f"JOIN line_amounts la ON la.entity_id = b.{base_key_q}"]
    select_fields = [
        f"b.{base_key_q} AS entity_id",
        "la.measure_cents",
        "la.excluded_tax_cents",
    ]
    where = []
    if base_date:
        select_fields.append(f"b.{_quote_ident(base_date)} AS entity_date")
        start, end = roles.get("date_range") or (None, None)
        if start:
            where.append(f"b.{_quote_ident(base_date)} >= '{start}'")
        if end:
            where.append(f"b.{_quote_ident(base_date)} < '{end}'")

    base_status = roles.get("base_status")
    if base_status:
        status_q = _quote_ident(base_status)
        select_fields.append(f"b.{status_q} AS entity_status")
        negatives = ", ".join(f"'{value}'" for value in _NEGATIVE_STATUS_VALUES)
        where.append(f"(b.{status_q} IS NULL OR lower(CAST(b.{status_q} AS TEXT)) NOT IN ({negatives}))")

    settlement = roles.get("settlement")
    if settlement and roles.get("settlement_fk"):
        settlement_t = _quote_ident(settlement["name"])
        settlement_fk = _quote_ident(roles["settlement_fk"])
        settlement_date = roles.get("settlement_date")
        settlement_status = roles.get("settlement_status")
        settlement_currency = roles.get("settlement_currency")
        filter_clause = ""
        if settlement_status:
            positives = ", ".join(f"'{value}'" for value in _POSITIVE_STATUS_VALUES)
            filter_clause = f"  WHERE lower(CAST(s.{_quote_ident(settlement_status)} AS TEXT)) IN ({positives})\n"
        ctes.extend([
            ", settled_entities AS (",
            _sql("builder_729_10.sql").format(settlement_fk),
            f"         MIN({f's.{_quote_ident(settlement_date)}' if settlement_date else 'NULL'}) AS settlement_date,",
            f"         MAX({f's.{_quote_ident(settlement_currency)}' if settlement_currency else 'NULL'}) AS settlement_currency,",
            "         COUNT(*) AS settlement_rows",
            f"  FROM {settlement_t} s",
            filter_clause.rstrip(),
            f"  GROUP BY s.{settlement_fk}",
            ")",
        ])
        joins.append("JOIN settled_entities se ON se.entity_id = b." + base_key_q)
        select_fields.extend(["se.settlement_date", "se.settlement_currency", "se.settlement_rows"])

    exclusion = roles.get("exclusion_dimension")
    if exclusion and roles.get("exclusion_base_fk") and roles.get("exclusion_dim_key"):
        dim_t = _quote_ident(exclusion["name"])
        base_fk = _quote_ident(roles["exclusion_base_fk"])
        dim_key = _quote_ident(roles["exclusion_dim_key"])
        joins.append(f"LEFT JOIN {dim_t} xd ON xd.{dim_key} = b.{base_fk}")
        test_exprs = _exclusion_predicates("xd", exclusion)
        if test_exprs:
            where.append("(" + " AND ".join(test_exprs) + ")")
        select_fields.append(f"b.{base_fk} AS exclusion_dimension_id")

    period = roles.get("period_dimension")
    period_expr = "la.measure_cents"
    if period and roles.get("period_base_fk") and roles.get("period_dim_key") and roles.get("monthly_recognition"):
        period_t = _quote_ident(period["name"])
        base_fk = _quote_ident(roles["period_base_fk"])
        dim_key = _quote_ident(roles["period_dim_key"])
        period_col = _period_column(period)
        if period_col:
            joins.append(f"LEFT JOIN {period_t} pd ON pd.{dim_key} = b.{base_fk}")
            period_q = _quote_ident(period_col)
            period_expr = (
                f"CASE WHEN lower(COALESCE(CAST(pd.{period_q} AS TEXT), '')) LIKE '%annual%' "
                f"OR lower(COALESCE(CAST(pd.{period_q} AS TEXT), '')) LIKE '%year%' "
                f"THEN la.measure_cents / 12.0 ELSE la.measure_cents END"
            )
            select_fields.append(f"pd.{period_q} AS recognition_period")

    rate_expr = "1.0"
    rate = roles.get("rate")
    if rate and roles.get("rate_currency") and roles.get("rate_date") and roles.get("rate_value"):
        rate_t = _quote_ident(rate["name"])
        rate_currency = _quote_ident(roles["rate_currency"])
        rate_date = _quote_ident(roles["rate_date"])
        rate_value = _quote_ident(roles["rate_value"])
        currency_expr = "NULL"
        if roles.get("use_settlement_date_for_rate") and settlement and roles.get("settlement_currency"):
            currency_expr = "se.settlement_currency"
        elif roles.get("base_currency"):
            currency_expr = f"b.{_quote_ident(roles['base_currency'])}"
        date_expr = "NULL"
        if roles.get("use_settlement_date_for_rate") and settlement and roles.get("settlement_date"):
            date_expr = "se.settlement_date"
        elif base_date:
            date_expr = f"b.{_quote_ident(base_date)}"
        fallback_currency_expr = (
            f"b.{_quote_ident(roles['base_currency'])}"
            if roles.get("base_currency")
            else currency_expr
        )
        joins.append(
            f"LEFT JOIN {rate_t} rt ON rt.{rate_currency} = COALESCE({currency_expr}, "
            f"{fallback_currency_expr}) "
            f"AND rt.{rate_date} = {date_expr}"
        )
        rate_expr = f"COALESCE(rt.{rate_value}, 1.0)"
        select_fields.append(f"{rate_expr} AS rate_to_base")
    else:
        select_fields.append("1.0 AS rate_to_base")

    recognized_expr = f"({period_expr})"
    select_fields.append(f"{recognized_expr} AS recognized_cents")

    where_sql = "\n  WHERE " + "\n    AND ".join(where) if where else ""
    ctes.extend([
        ", eligible_rows AS (",
        "  SELECT " + ",\n         ".join(select_fields),
        f"  FROM {base_t} b",
        "  " + "\n  ".join(joins),
        where_sql,
        ")",
    ])

    adjustment_sql = "0"
    adjustment = roles.get("adjustment")
    if adjustment and roles.get("adjustment_fk") and roles.get("adjustment_amount"):
        adjustment_t = _quote_ident(adjustment["name"])
        adjustment_fk = _quote_ident(roles["adjustment_fk"])
        adjustment_amount = _quote_ident(roles["adjustment_amount"])
        adjustment_date = roles.get("adjustment_date")
        close_date = roles.get("close_date")
        adjustment_where = "a." + adjustment_fk + " = e.entity_id"
        if adjustment_date and close_date:
            adjustment_where += f" AND a.{_quote_ident(adjustment_date)} <= '{close_date}'"
        adjustment_sql = (
            _sql("builder_826_5.sql").format(adjustment_amount, adjustment_t, adjustment_where)
        )

    with_sql = "WITH " + "\n".join(part for part in ctes if part != "")
    sql = (
        _sql("builder_832_1.sql").format(with_sql, adjustment_sql)
    )
    breakdown_sql = (
        _sql("builder_838_2.sql").format(with_sql)
    )
    exclusion_sql = _build_exclusion_summary_sql(roles, with_sql)
    return {"sql": sql, "breakdown_sql": breakdown_sql, "exclusion_sql": exclusion_sql}


def _build_exclusion_summary_sql(roles: dict[str, Any], with_sql: str) -> str:
    base = roles["base"]
    base_t = _quote_ident(base["name"])
    base_key = _quote_ident(roles["base_key"])
    base_date = roles.get("base_date")
    where = []
    start, end = roles.get("date_range") or (None, None)
    if base_date and start:
        where.append(f"b.{_quote_ident(base_date)} >= '{start}'")
    if base_date and end:
        where.append(f"b.{_quote_ident(base_date)} < '{end}'")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    not_eligible_where = " AND ".join([*where, _sql("builder_866_6.sql").format(base_key)])
    not_eligible_sql = "WHERE " + not_eligible_where
    return (
        _sql("builder_869_3.sql").format(with_sql, base_t, where_sql, base_t, not_eligible_sql)
    )


def _infer_base_table(tables: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        columns = [col.lower() for col in table.get("columns", [])]
        score = 0
        if _preferred_key(table):
            score += 3
        if _preferred_date_column(table):
            score += 3
        if any(col in {"status", "state"} for col in columns):
            score += 2
        if any("currency" in col for col in columns):
            score += 2
        if any(col.endswith("_id") and col != _preferred_key(table) for col in table.get("columns", [])):
            score += 1
        if score >= 6:
            scored.append((score, table))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    return scored[0][1] if scored else None


def _infer_line_table(tables: list[dict[str, Any]], base_name: str, base_key: str) -> dict[str, Any] | None:
    scored: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        if table["name"] == base_name:
            continue
        if not _matching_column(table, base_key):
            continue
        amounts = _line_amount_columns(table, "")
        if not amounts:
            continue
        score = 3 + len(amounts)
        if any("line" in col.lower() or "item" in col.lower() for col in table.get("columns", [])):
            score += 1
        scored.append((score, table))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    return scored[0][1] if scored else None


def _infer_settlement_table(tables: list[dict[str, Any]], base_name: str, base_key: str) -> dict[str, Any] | None:
    scored: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        if table["name"] == base_name:
            continue
        if not _matching_column(table, base_key):
            continue
        columns = [col.lower() for col in table.get("columns", [])]
        score = 0
        if _first_column_matching(table, ("status", "state")):
            score += 3
        if _preferred_date_column(table):
            score += 2
        if any(term in table["name"].lower() for term in ("payment", "settlement", "receipt", "capture")):
            score += 3
        if any(term in " ".join(columns) for term in ("payment", "settle", "paid", "capture")):
            score += 1
        if score >= 5:
            scored.append((score, table))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    return scored[0][1] if scored else None


def _infer_rate_table(tables: list[dict[str, Any]]) -> dict[str, Any] | None:
    for table in tables:
        if (
            _first_column_matching(table, ("currency", "currency_code"))
            and _preferred_date_column(table)
            and _rate_value_column(table)
        ):
            return table
    return None


def _infer_adjustment_table(tables: list[dict[str, Any]], base_name: str, base_key: str) -> dict[str, Any] | None:
    scored: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        if table["name"] == base_name:
            continue
        if not _matching_column(table, base_key):
            continue
        if not _preferred_amount_column(table) or not _preferred_date_column(table):
            continue
        text = " ".join([table["name"], *table.get("columns", [])]).lower()
        score = 1
        if any(term in text for term in ("refund", "credit", "adjust", "reversal")):
            score += 4
        if score >= 4:
            scored.append((score, table))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    return scored[0][1] if scored else None


def _infer_exclusion_dimension(tables: list[dict[str, Any]], base: dict[str, Any]) -> dict[str, Any] | None:
    for base_col in base.get("columns", []):
        if not base_col.lower().endswith("_id"):
            continue
        for table in tables:
            if table["name"] == base["name"]:
                continue
            dim_key = _matching_column(table, base_col)
            if dim_key and _has_exclusion_flag(table):
                return {
                    "exclusion_dimension": table,
                    "exclusion_base_fk": base_col,
                    "exclusion_dim_key": dim_key,
                }
    return None


def _infer_period_dimension(tables: list[dict[str, Any]], base: dict[str, Any]) -> dict[str, Any] | None:
    for base_col in base.get("columns", []):
        if not base_col.lower().endswith("_id"):
            continue
        for table in tables:
            if table["name"] == base["name"]:
                continue
            dim_key = _matching_column(table, base_col)
            if dim_key and _has_period_column(table):
                return {
                    "period_dimension": table,
                    "period_base_fk": base_col,
                    "period_dim_key": dim_key,
                }
    return None


def _suggested_queries(
    duplicates: list[dict[str, Any]],
    joins: list[dict[str, Any]],
    totals: list[dict[str, Any]],
) -> list[str]:
    queries: list[str] = []
    for item in duplicates[:8]:
        table = _quote_ident(item["table"])
        column = _quote_ident(item["column"])
        queries.append(
            _sql("builder_1013_7.sql").format(column, table, column, column)
        )
    for item in joins[:8]:
        left_table = _quote_ident(item["left_table"])
        right_table = _quote_ident(item["right_table"])
        column = _quote_ident(item["column"])
        queries.append(
            _sql("builder_1025_8.sql").format(column, left_table, column, column, right_table, column)
        )
    for item in totals[:6]:
        queries.append(
            _sql("builder_1037_9.sql").format(_quote_ident(item['column']), _quote_ident(item['column']), _quote_ident(item['table']))
        )
    return queries


def _key_candidates(columns: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    for column in columns:
        name = column["name"]
        lower = name.lower()
        if column.get("pk") or lower == "id" or any(hint in lower for hint in _KEY_HINTS):
            candidates.append(name)
    return candidates[:12]


def _numeric_candidates(columns: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    for column in columns:
        name = column["name"]
        typ = column.get("type", "").lower()
        lower = name.lower()
        if any(term in typ for term in ("int", "real", "numeric", "decimal", "double", "float")):
            if "id" not in lower or any(hint in lower for hint in _AMOUNT_HINTS):
                candidates.append(name)
        elif any(hint in lower for hint in _AMOUNT_HINTS):
                candidates.append(name)
    return candidates[:12]


def _preferred_key(table: dict[str, Any]) -> str | None:
    columns = table.get("columns", [])
    details = table.get("column_details", [])
    pk = next((col["name"] for col in details if col.get("pk")), None)
    if pk:
        return str(pk)
    singular = _singularize(table.get("name", "").lower())
    preferred = f"{singular}_id" if singular else ""
    for column in columns:
        if column.lower() == preferred:
            return column
    for column in columns:
        lower = column.lower()
        if lower == "id" or lower.endswith("_id"):
            return column
    return None


def _preferred_date_column(table: dict[str, Any]) -> str | None:
    columns = table.get("columns", [])
    ranked: list[tuple[int, str]] = []
    for column in columns:
        lower = column.lower()
        if not any(hint in lower for hint in _DATE_HINTS):
            continue
        score = 0
        if lower.endswith("_date") or lower == "date":
            score += 4
        if any(term in lower for term in ("payment", "settle", "paid", "complete", "capture", "post")):
            score += 3
        if any(term in lower for term in ("record", "document", "entry", "item", "order", "issue", "created", "posted")):
            score += 2
        ranked.append((score, column))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1] if ranked else None


def _first_column_matching(table: dict[str, Any], hints: tuple[str, ...]) -> str | None:
    for column in table.get("columns", []):
        lower = column.lower()
        if any(hint in lower for hint in hints):
            return column
    return None


def _matching_column(table: dict[str, Any], reference: str) -> str | None:
    ref = reference.lower()
    for column in table.get("columns", []):
        if column.lower() == ref:
            return column
    singular_ref = _singularize(ref.removesuffix("_id"))
    for column in table.get("columns", []):
        lower = column.lower()
        if lower.endswith("_id") and _singularize(lower.removesuffix("_id")) == singular_ref:
            return column
    return None


def _preferred_amount_column(table: dict[str, Any]) -> str | None:
    candidates = _numeric_candidates(table.get("column_details", []))
    if not candidates:
        return None
    for column in candidates:
        lower = column.lower()
        if any(term in lower for term in ("eur", "base", "adjust", "refund", "credit")):
            return column
    return candidates[0]


def _rate_value_column(table: dict[str, Any]) -> str | None:
    ranked: list[tuple[int, str]] = []
    for column in table.get("column_details", []):
        name = str(column.get("name") or "")
        lower = name.lower()
        typ = str(column.get("type") or "").lower()
        if not any(term in lower for term in ("rate", "conversion", "exchange")):
            continue
        if any(term in lower for term in ("date", "time", "_at", "_on")):
            continue
        score = 0
        if any(term in typ for term in ("int", "real", "numeric", "decimal", "double", "float")):
            score += 4
        if "to" in lower or "rate_to" in lower:
            score += 2
        ranked.append((score, name))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1] if ranked else None


def _line_amount_columns(table: dict[str, Any], text: str) -> list[str]:
    details = table.get("column_details", [])
    columns = _numeric_candidates(details)
    if _mentions_tax_exclusion(text):
        columns = [column for column in columns if column not in _tax_columns(table)]
    keyish = set(table.get("key_candidates", []))
    return [column for column in columns if column not in keyish][:6]


def _tax_columns(table: dict[str, Any]) -> list[str]:
    return [
        column
        for column in table.get("columns", [])
        if any(hint in column.lower() for hint in _TAX_HINTS)
    ]


def _mentions_tax_exclusion(text: str) -> bool:
    return bool(re.search(r"\b(exclud|without|net)\b.{0,40}\b(vat|tax|gst|hst)\b|\bnet\b", text or "", re.I))


def _has_exclusion_flag(table: dict[str, Any]) -> bool:
    return bool(_exclusion_columns(table))


def _exclusion_columns(table: dict[str, Any]) -> list[str]:
    columns = []
    for column in table.get("columns", []):
        lower = column.lower()
        if lower in {"is_test", "test", "is_demo", "demo"} or "test" in lower or "sandbox" in lower:
            columns.append(column)
        elif lower in {"type", "category", "segment", "kind"}:
            columns.append(column)
    return columns


def _exclusion_predicates(alias: str, table: dict[str, Any]) -> list[str]:
    predicates: list[str] = []
    for column in _exclusion_columns(table):
        quoted = _quote_ident(column)
        lower = column.lower()
        if lower in {"type", "category", "segment", "kind"}:
            predicates.append(
                f"({alias}.{quoted} IS NULL OR lower(CAST({alias}.{quoted} AS TEXT)) "
                "NOT IN ('test', 'demo', 'sandbox', 'synthetic'))"
            )
        else:
            predicates.append(
                f"({alias}.{quoted} IS NULL OR lower(CAST({alias}.{quoted} AS TEXT)) "
                "NOT IN ('1', 'true', 'yes', 'test', 'demo', 'sandbox', 'synthetic'))"
            )
    return predicates


def _has_period_column(table: dict[str, Any]) -> bool:
    return _period_column(table) is not None


def _period_column(table: dict[str, Any]) -> str | None:
    return _first_column_matching(table, ("period", "cadence", "frequency", "interval", "cycle"))


def _extract_date_range(text: str) -> tuple[str | None, str | None]:
    start = None
    end = None
    start_match = re.search(r">=\s*['\"](\d{4}-\d{2}-\d{2})['\"]", text or "")
    end_match = re.search(r"<\s*['\"](\d{4}-\d{2}-\d{2})['\"]", text or "")
    if start_match:
        start = start_match.group(1)
    if end_match:
        end = end_match.group(1)
    return start, end


def _extract_close_date(text: str) -> str | None:
    matches = re.findall(r"(?i)(?:close|cutoff|cut-off|through|until|end).{0,40}?(\d{4}-\d{2}-\d{2})", text or "")
    if matches:
        return matches[-1]
    return None


def _numeric_targets(text: str) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        lower = line.lower()
        if not any(term in lower for term in ("result", "expected", "actual", "target", "dashboard", "metric", "total", "residual", "reconciled")):
            continue
        for match in re.finditer(r"(?<![\d-])(\d{1,3}(?:,\d{3})+|\d{4,}|0)(?:\s*(eur|usd|gbp|cad|aud|base))?", line, re.I):
            raw = match.group(1)
            value = int(raw.replace(",", ""))
            start = max(0, match.start() - 48)
            end = min(len(line), match.end() + 48)
            label = line[start:end].strip()
            targets.append({
                "label": label[:120],
                "value": value,
                "currency": (match.group(2) or "").upper() or None,
            })
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for target in targets:
        key = (target["label"], int(target["value"]))
        if key not in seen:
            deduped.append(target)
            seen.add(key)
    return deduped[:20]


def _first_numeric_value(row: sqlite3.Row | None) -> int | float | None:
    if row is None:
        return None
    for key in row.keys():
        value = row[key]
        if isinstance(value, int | float):
            return int(value) if float(value).is_integer() else round(float(value), 6)
    return None


def _fetch_rows(conn: sqlite3.Connection, sql: str, *, limit: int) -> list[dict[str, Any]]:
    cursor = conn.execute(sql)
    rows = cursor.fetchmany(limit)
    return [{key: row[key] for key in row.keys()} for row in rows]


def _fetch_one_dict(conn: sqlite3.Connection, sql: str) -> dict[str, Any]:
    row = conn.execute(sql).fetchone()
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _matched_targets(value: int | float | None, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if value is None:
        return []
    return [target for target in targets if abs(float(target["value"]) - float(value)) < 0.000001]


def _visible_result_numbers(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    numbers: list[dict[str, Any]] = []
    for item in results:
        if item.get("skipped") or item.get("error"):
            continue
        path = item.get("path", "visible_sql")
        for row in item.get("rows", []):
            for key, value in row.items():
                if isinstance(value, int | float):
                    numbers.append({"label": f"{path}:{key}", "value": value})
    return numbers


def _candidate_deltas(value: int | float | None, references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if value is None:
        return []
    deltas = []
    for ref in references[:12]:
        ref_value = ref.get("value")
        if isinstance(ref_value, int | float):
            deltas.append({
                "label": ref.get("label"),
                "reference": ref_value,
                "delta": round(float(value) - float(ref_value), 6),
            })
    return deltas


def _explain_metric_candidate(
    *,
    result: int | float | None,
    deltas: list[dict[str, Any]],
    exclusion_summary: dict[str, Any],
    breakdown_rows: list[dict[str, Any]],
    roles: dict[str, Any],
) -> dict[str, Any]:
    factors: list[dict[str, Any]] = []
    parent_rows = _as_number(exclusion_summary.get("parent_rows_in_scope"))
    eligible_rows = _as_number(exclusion_summary.get("eligible_rows"))
    not_eligible = _as_number(exclusion_summary.get("parent_rows_not_eligible"))
    if not_eligible:
        factors.append({
            "category": "eligibility_scope",
            "count": int(not_eligible),
            "evidence": (
                f"{int(eligible_rows or 0)} eligible rows out of {int(parent_rows or 0)} "
                "visible parent rows after read-only filters"
            ),
        })

    excluded_tax = sum(float(row.get("excluded_tax") or 0) for row in breakdown_rows)
    if abs(excluded_tax) > 0.000001:
        factors.append({
            "category": "excluded_amount_component",
            "amount": round(excluded_tax, 6),
            "evidence": "included-row breakdown has excluded_tax derived from tax-like amount columns",
        })

    rate_rows = [
        row for row in breakdown_rows
        if _as_number(row.get("rate_to_base")) is not None and abs(float(row.get("rate_to_base")) - 1.0) > 0.000001
    ]
    if rate_rows:
        factors.append({
            "category": "rate_conversion",
            "count": len(rate_rows),
            "evidence": "one or more included rows used a non-1.0 conversion/rate value",
        })

    period_rows = [
        row for row in breakdown_rows
        if str(row.get("recognition_period") or "").strip()
        and abs(float(row.get("raw_measure") or 0) - float(row.get("recognized_value") or 0)) > 0.000001
    ]
    if period_rows:
        factors.append({
            "category": "period_recognition",
            "count": len(period_rows),
            "evidence": "recognized value differs from raw measure for rows with a period/cadence dimension",
        })

    if roles.get("adjustment"):
        factors.append({
            "category": "adjustment_policy",
            "evidence": "an adjustment table was inferred and filtered by visible close/cutoff rules when present",
        })

    if roles.get("settlement"):
        factors.append({
            "category": "settlement_filter",
            "evidence": "candidate query joins to a settlement-like table and keeps positive settlement states",
        })

    if deltas:
        first_delta = deltas[0]
        factors.insert(0, {
            "category": "reference_delta",
            "amount": first_delta.get("delta"),
            "evidence": f"candidate result {result} compared with visible reference {first_delta.get('label')}",
        })

    reference_delta = None
    if deltas and isinstance(deltas[0].get("delta"), int | float):
        reference_delta = float(deltas[0]["delta"])
    decomposed_amounts = [
        float(item["amount"])
        for item in factors
        if isinstance(item.get("amount"), int | float)
        and item.get("category") != "reference_delta"
    ]
    status = "not_fully_decomposed"
    if reference_delta is None and factors:
        status = "no_reference_delta"
    elif reference_delta is not None and decomposed_amounts:
        if abs(abs(sum(decomposed_amounts)) - abs(reference_delta)) < 0.000001:
            status = "fully_decomposed"
    return {
        "decomposition_status": status,
        "factors": factors,
    }


def _as_number(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _role_summary(roles: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in roles.items():
        if isinstance(value, dict) and "name" in value:
            summary[key] = value["name"]
        elif key not in {"base", "line", "settlement", "rate", "adjustment", "exclusion_dimension", "period_dimension"}:
            summary[key] = value
    return summary


def _singularize(value: str) -> str:
    if value.endswith("ies"):
        return value[:-3] + "y"
    if value.endswith("ses"):
        return value[:-2]
    if value.endswith("s") and len(value) > 1:
        return value[:-1]
    return value


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for word in (
        "reconcile",
        "sqlite",
        "database",
        "metric",
        "record",
        "settlement",
        "status",
        "rate",
        "adjustment",
        "duplicate",
        "join",
        "total",
    ):
        if word in (query or "").lower():
            terms.append(word)
    return terms


def _summary(reports: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "databases_seen": len(reports),
        "tables_seen": sum(len(db.get("tables", [])) for db in reports),
        "duplicate_key_groups": sum(
            int(item.get("duplicate_groups", 0))
            for db in reports
            for item in db.get("duplicate_keys", [])
        ),
        "join_risk_count": sum(len(db.get("join_risks", [])) for db in reports),
        "numeric_total_count": sum(len(db.get("numeric_totals", [])) for db in reports),
        "validated_metric_candidates": sum(
            1
            for db in reports
            for item in db.get("metric_candidates", [])
            if item.get("result") is not None and not item.get("error") and not item.get("skipped")
        ),
    }


def _quote_ident(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'
