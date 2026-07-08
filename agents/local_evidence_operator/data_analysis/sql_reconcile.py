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
    reports = [
        _inspect_database(
            path,
            root,
            query=query,
            visible_sql=visible_sql,
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
            ("next_safe_step", "review visible SELECT results, numeric totals, duplicate keys, and join-risk queries"),
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
        if path.suffix.lower() not in _SQLITE_SUFFIXES:
            continue
        paths.append(path)
    return paths


def _find_visible_sql_files(root: Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.sql")):
        if len(files) >= 12:
            break
        if not path.is_file():
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
        if not path.is_file():
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

def _inspect_database(
    path: Path,
    root: Path,
    *,
    query: str,
    visible_sql: list[dict[str, str]],
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
        return {
            "path": rel,
            "query_terms": _query_terms(query),
            "tables": table_reports,
            "numeric_totals": numeric_totals,
            "duplicate_keys": duplicate_keys,
            "join_risks": join_risks,
            "visible_sql_results": visible_sql_results,
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


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for word in (
        "reconcile",
        "sqlite",
        "database",
        "metric",
        "record",
        "status",
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
    }


def _quote_ident(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'
