"""JSONL/NDJSON extraction, including gzip-compressed streams."""

from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator

from extrator import __version__
from extrator.config import get_config
from extrator.formats import file_metadata, source_type_for
from extrator.hashing import sha256_file, sha256_text, stable_id
from extrator.types import NormalizedDocument, TableInfo


def _open_text(path: Path):
    if path.name.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    with _open_text(path) as fh:
        for line_no, raw in enumerate(fh, start=1):
            text = raw.strip()
            if not text:
                yield line_no, None, "empty"
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                yield line_no, None, f"invalid_json:{exc.msg}"
                continue
            if not isinstance(value, dict):
                yield line_no, None, f"non_object:{_type_name(value)}"
                continue
            yield line_no, value, None


def _write_valid_records(path: Path, *, records_path: Path) -> tuple[int, int, int, list[dict[str, Any]], Counter[str], dict[str, Counter[str]], list[dict[str, Any]]]:
    records_path.parent.mkdir(parents=True, exist_ok=True)
    total_lines = 0
    valid_records = 0
    invalid_records = 0
    field_counts: Counter[str] = Counter()
    type_counts: dict[str, Counter[str]] = defaultdict(Counter)
    samples: list[dict[str, Any]] = []
    invalid_samples: list[dict[str, Any]] = []

    with records_path.open("w", encoding="utf-8") as out:
        for line_no, record, error in _iter_jsonl(path):
            total_lines = max(total_lines, line_no)
            if record is None:
                invalid_records += 1
                if len(invalid_samples) < 5:
                    invalid_samples.append({"line": line_no, "error": error or "invalid"})
                continue
            valid_records += 1
            if len(samples) < 5:
                samples.append(record)
            for key, value in record.items():
                key_text = str(key)
                field_counts[key_text] += 1
                type_counts[key_text][_type_name(value)] += 1
            out.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            out.write("\n")

    return total_lines, valid_records, invalid_records, samples, field_counts, type_counts, invalid_samples


def _summary_markdown(
    *,
    path: Path,
    total_lines: int,
    valid_records: int,
    invalid_records: int,
    field_counts: Counter[str],
    type_counts: dict[str, Counter[str]],
    samples: list[dict[str, Any]],
    invalid_samples: list[dict[str, Any]],
) -> str:
    fields = sorted(field_counts)
    lines = [
        f"# JSONL stream {path.name}",
        "",
        f"Source: {path.name}",
        f"Lines: {total_lines}",
        f"Valid object records: {valid_records}",
        f"Invalid/empty/non-object records: {invalid_records}",
        "",
        "## Fields",
    ]
    if fields:
        for field in fields:
            type_summary = ", ".join(
                f"{name}={count}" for name, count in sorted(type_counts[field].items())
            )
            lines.append(f"- `{field}`: present={field_counts[field]}; types={type_summary}")
    else:
        lines.append("- none")

    if invalid_samples:
        lines.extend(["", "## Invalid samples"])
        for item in invalid_samples:
            lines.append(f"- line {item['line']}: {item['error']}")

    if samples:
        lines.extend(["", "## Sample records", "```json"])
        lines.append(json.dumps(samples, ensure_ascii=False, indent=2, sort_keys=True)[:4000])
        lines.append("```")

    return "\n".join(lines)


def parse(path: Path, *, table_dir: Path) -> NormalizedDocument:
    cfg = get_config()
    file_hash = sha256_file(path, block_size=cfg.hashing.block_size_bytes)
    source_type = source_type_for(path)
    doc_id = stable_id("doc", str(path.resolve()), file_hash, cfg.config_hash)
    records_path = table_dir / f"{doc_id.replace(':', '_')}.valid.jsonl"

    (
        total_lines,
        valid_records,
        invalid_records,
        samples,
        field_counts,
        type_counts,
        invalid_samples,
    ) = _write_valid_records(path, records_path=records_path)

    markdown = _summary_markdown(
        path=path,
        total_lines=total_lines,
        valid_records=valid_records,
        invalid_records=invalid_records,
        field_counts=field_counts,
        type_counts=type_counts,
        samples=samples,
        invalid_samples=invalid_samples,
    )
    table_id = stable_id("table", doc_id, "jsonl", sha256_text(markdown))
    table = TableInfo(
        table_id=table_id,
        doc_id=doc_id,
        name=path.name,
        rows=valid_records,
        columns=len(field_counts),
        output_path=str(records_path),
        summary=markdown,
    )
    metadata = file_metadata(path)
    metadata.update(
        {
            "line_count": total_lines,
            "valid_records": valid_records,
            "invalid_records": invalid_records,
            "field_count": len(field_counts),
            "fields": sorted(field_counts),
            "type_counts": {field: dict(counts) for field, counts in sorted(type_counts.items())},
            "invalid_samples": invalid_samples,
            "sample_records": samples,
        }
    )
    return NormalizedDocument(
        doc_id=doc_id,
        source_path=str(path),
        source_type=source_type,
        mime_type=str(metadata.get("mime_type") or ""),
        file_hash=file_hash,
        title=path.stem,
        markdown=markdown,
        metadata=dict(metadata),
        tables=[table],
        parser="jsonl",
        parser_version=__version__,
    )
