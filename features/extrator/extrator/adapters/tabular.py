"""CSV/XLSX extraction and Parquet conversion."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from extrator import __version__
from extrator.config import get_config
from extrator.formats import file_metadata, source_type_for
from extrator.hashing import sha256_file, stable_id
from extrator.types import NormalizedDocument, TableInfo


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


def _read_tables(path: Path) -> list[tuple[str, pd.DataFrame]]:
    name = path.name.lower()
    suffix = path.suffix.lower().lstrip(".")
    if name.endswith((".csv.gz", ".tsv.gz")):
        sep = "\t" if name.endswith(".tsv.gz") else ","
        stem = path.name.rsplit(".", 2)[0]
        return [(stem, pd.read_csv(path, sep=sep, compression="gzip"))]
    if suffix in {"csv", "tsv"}:
        sep = "\t" if suffix == "tsv" else ","
        return [(path.stem, pd.read_csv(path, sep=sep))]
    sheets = pd.read_excel(path, sheet_name=None)
    return [(str(name), df) for name, df in sheets.items()]


def parse(path: Path, *, table_dir: Path) -> NormalizedDocument:
    cfg = get_config()
    file_hash = sha256_file(path, block_size=cfg.hashing.block_size_bytes)
    source_type = source_type_for(path)
    doc_id = stable_id("doc", str(path.resolve()), file_hash, cfg.config_hash)
    tables: list[TableInfo] = []
    summaries: list[str] = []

    for name, df in _read_tables(path):
        info = _write_table(df, table_dir=table_dir, doc_id=doc_id, name=name)
        info.summary = _frame_summary(name, df, path)
        tables.append(info)
        summaries.append(info.summary)

    metadata = file_metadata(path)
    metadata["table_count"] = len(tables)
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
        tables[0][1].to_parquet(output_path, index=False)
        return output_path
    directory = output_path.with_suffix("")
    directory.mkdir(parents=True, exist_ok=True)
    for name, df in tables:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
        df.to_parquet(directory / f"{safe}.parquet", index=False)
    return directory
