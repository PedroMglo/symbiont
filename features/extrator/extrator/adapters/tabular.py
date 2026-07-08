"""CSV/XLSX extraction and Parquet conversion."""

from __future__ import annotations

from dataclasses import dataclass
import gzip
from pathlib import Path

import pandas as pd

from extrator import __version__
from extrator.config import get_config
from extrator.formats import file_metadata, source_type_for
from extrator.hashing import sha256_file, stable_id
from extrator.types import NormalizedDocument, TableInfo


_CSV_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin1")
_CSV_DELIMITERS = (",", ";", "\t", "|")
_SAMPLE_BYTES = 8192


@dataclass(frozen=True)
class _ReadTable:
    name: str
    frame: pd.DataFrame
    encoding: str | None = None
    delimiter: str | None = None
    compression: str | None = None
    bad_lines_policy: str | None = None
    skipped_rows: int = 0


def _frame_summary(name: str, df: pd.DataFrame, source_path: Path) -> str:
    columns = [str(col) for col in df.columns]
    preview = df.head(5).to_markdown(index=False)
    return (
        f"# Table {name}\n\n"
        f"Source: {source_path.name}\n\n"
        f"Rows: {len(df)}\n\n"
        f"Columns: {', '.join(columns)}\n\n"
        f"Preview:\n\n{preview}"
    )


def _write_table(df: pd.DataFrame, *, table_dir: Path, doc_id: str, name: str) -> TableInfo:
    table_dir.mkdir(parents=True, exist_ok=True)
    table_id = stable_id("table", doc_id, name, list(map(str, df.columns)), len(df))
    out_path = table_dir / f"{table_id.replace(':', '_')}.parquet"
    df.to_parquet(out_path, index=False)
    return TableInfo(
        table_id=table_id,
        doc_id=doc_id,
        name=name,
        rows=int(len(df)),
        columns=int(len(df.columns)),
        output_path=str(out_path),
        summary=_frame_summary(name, df, Path(name)),
    )


def _sample_text(path: Path, *, encoding: str, compression: str | None) -> str:
    if compression == "gzip":
        with gzip.open(path, "rb") as handle:
            raw = handle.read(_SAMPLE_BYTES)
    else:
        with path.open("rb") as handle:
            raw = handle.read(_SAMPLE_BYTES)
    return raw.decode(encoding)


def _delimiter_profile(lines: list[tuple[int, str]], delimiter: str) -> tuple[int, int, int]:
    best: tuple[int, int, int] | None = None
    for position, (index, line) in enumerate(lines):
        count = line.count(delimiter)
        if count <= 0:
            continue
        next_counts = [candidate.count(delimiter) for _, candidate in lines[position + 1 : position + 6]]
        stable_neighbors = sum(1 for item in next_counts if item == count)
        compatible_neighbors = sum(1 for item in next_counts if item >= count)
        score = stable_neighbors * 2 + compatible_neighbors
        if score > 0:
            return index, count, score
        candidate = (index, count, score)
        if best is None or (count, -index) > (best[1], -best[0]):
            best = candidate
    return best or (0, 0, 0)


def _delimiter_candidates(preferred: str, sample: str) -> tuple[tuple[str, int], ...]:
    lines = [(index, line) for index, line in enumerate(sample.splitlines()) if line.strip()]
    profiles = {
        delimiter: _delimiter_profile(lines, delimiter)
        for delimiter in _CSV_DELIMITERS
    }
    ranked = sorted(
        _CSV_DELIMITERS,
        key=lambda item: (
            -profiles[item][2],
            -profiles[item][1],
            0 if item == preferred else 1,
            profiles[item][0],
            item,
        ),
    )
    return tuple((delimiter, profiles[delimiter][0] if profiles[delimiter][1] > 0 else 0) for delimiter in ranked)


def _candidate_score(
    frame: pd.DataFrame,
    *,
    delimiter: str,
    preferred: str,
    skipped_rows: int,
    bad_lines_policy: str | None,
) -> tuple[int, int, int, int, int, int, int, int]:
    column_count = int(len(frame.columns))
    row_count = int(len(frame))
    non_empty_columns = int(frame.dropna(axis=1, how="all").shape[1]) if column_count else 0
    non_empty_rows = int(frame.dropna(how="all").shape[0]) if row_count else 0
    populated_cells = int(frame.notna().sum().sum()) if column_count and row_count else 0
    return (
        1 if column_count > 1 else 0,
        min(non_empty_columns, 100),
        min(column_count, 100),
        min(non_empty_rows, 10_000),
        min(populated_cells, 100_000),
        1 if bad_lines_policy is None else 0,
        1 if delimiter == preferred else 0,
        -skipped_rows,
    )


def _read_delimited_table(path: Path, *, name: str, sep: str, compression: str | None = None) -> _ReadTable:
    errors: list[str] = []
    candidates: list[tuple[tuple[int, int, int, int, int, int, int, int], _ReadTable]] = []
    for encoding in _CSV_ENCODINGS:
        try:
            sample = _sample_text(path, encoding=encoding, compression=compression)
        except UnicodeError as exc:
            errors.append(f"{encoding}: {exc}")
            continue
        for delimiter, skiprows in _delimiter_candidates(sep, sample):
            try:
                frame = pd.read_csv(
                    path,
                    sep=delimiter,
                    compression=compression,
                    encoding=encoding,
                    skiprows=skiprows,
                )
            except UnicodeError as exc:
                errors.append(f"{encoding}/{delimiter!r}: {exc}")
                break
            except pd.errors.ParserError as exc:
                errors.append(f"{encoding}/{delimiter!r}: {exc}")
                try:
                    frame = pd.read_csv(
                        path,
                        sep=delimiter,
                        compression=compression,
                        encoding=encoding,
                        skiprows=skiprows,
                        on_bad_lines="skip",
                    )
                except (UnicodeError, pd.errors.ParserError) as recovered_exc:
                    errors.append(f"{encoding}/{delimiter!r}/skip-bad-lines: {recovered_exc}")
                    continue
                bad_lines_policy = "skip"
            else:
                bad_lines_policy = None
            table = _ReadTable(
                name=name,
                frame=frame,
                encoding=encoding,
                delimiter=delimiter,
                compression=compression,
                bad_lines_policy=bad_lines_policy,
                skipped_rows=skiprows,
            )
            candidates.append(
                (
                    _candidate_score(
                        frame,
                        delimiter=delimiter,
                        preferred=sep,
                        skipped_rows=skiprows,
                        bad_lines_policy=bad_lines_policy,
                    ),
                    table,
                )
            )
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
    joined = "; ".join(errors) or "no encoding/delimiter candidates attempted"
    raise UnicodeError(f"Unable to decode delimited table {path.name}: {joined}")


def _read_tables(path: Path) -> list[_ReadTable]:
    name = path.name.lower()
    suffix = path.suffix.lower().lstrip(".")
    if name.endswith((".csv.gz", ".tsv.gz")):
        sep = "\t" if name.endswith(".tsv.gz") else ","
        stem = path.name.rsplit(".", 2)[0]
        return [_read_delimited_table(path, name=stem, sep=sep, compression="gzip")]
    if suffix in {"csv", "tsv"}:
        sep = "\t" if suffix == "tsv" else ","
        return [_read_delimited_table(path, name=path.stem, sep=sep)]
    sheets = pd.read_excel(path, sheet_name=None)
    return [_ReadTable(name=str(name), frame=df) for name, df in sheets.items()]


def parse(path: Path, *, table_dir: Path) -> NormalizedDocument:
    cfg = get_config()
    file_hash = sha256_file(path, block_size=cfg.hashing.block_size_bytes)
    source_type = source_type_for(path)
    doc_id = stable_id("doc", str(path.resolve()), file_hash, cfg.config_hash)
    tables: list[TableInfo] = []
    summaries: list[str] = []

    read_tables = _read_tables(path)
    for table in read_tables:
        info = _write_table(table.frame, table_dir=table_dir, doc_id=doc_id, name=table.name)
        info.summary = _frame_summary(table.name, table.frame, path)
        tables.append(info)
        summaries.append(info.summary)

    metadata = file_metadata(path)
    metadata["table_count"] = len(tables)
    metadata["table_names"] = [table.name for table in read_tables]
    encodings = sorted({table.encoding for table in read_tables if table.encoding})
    delimiters = sorted({table.delimiter for table in read_tables if table.delimiter})
    compressions = sorted({table.compression for table in read_tables if table.compression})
    bad_lines_policies = sorted({table.bad_lines_policy for table in read_tables if table.bad_lines_policy})
    if encodings:
        metadata["tabular_encodings"] = encodings
    if delimiters:
        metadata["tabular_delimiters"] = delimiters
    if compressions:
        metadata["tabular_compressions"] = compressions
    if bad_lines_policies:
        metadata["tabular_bad_line_policies"] = bad_lines_policies
    skipped_rows = sorted({table.skipped_rows for table in read_tables if table.skipped_rows})
    if skipped_rows:
        metadata["tabular_skipped_rows"] = skipped_rows
    return NormalizedDocument(
        doc_id=doc_id,
        source_path=str(path),
        source_type=source_type,
        mime_type=str(metadata.get("mime_type") or ""),
        file_hash=file_hash,
        title=path.stem,
        markdown="\n\n".join(summaries),
        metadata=dict(metadata),
        tables=tables,
        parser="tabular",
        parser_version=__version__,
    )


def convert_to_parquet(input_path: Path, output_path: Path) -> Path:
    tables = _read_tables(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if len(tables) == 1:
        tables[0].frame.to_parquet(output_path, index=False)
        return output_path
    directory = output_path.with_suffix("")
    directory.mkdir(parents=True, exist_ok=True)
    for table in tables:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in table.name)
        table.frame.to_parquet(directory / f"{safe}.parquet", index=False)
    return directory
