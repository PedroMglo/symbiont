"""Output writers for silver/gold artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from extrator.evidence import evidence_summary
from extrator.config import get_config
from extrator.storage import gold_doc_dir, silver_doc_dir
from extrator.types import (
    ChunkPayload,
    DocumentEvidence,
    DOCUMENT_EVIDENCE_CONTRACT_VERSION,
    GraphCandidate,
    NormalizedDocument,
    RagBundleManifest,
    TableInfo,
)


def write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return str(path)


def write_jsonl(path: Path, records: list[Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            if hasattr(record, "model_dump"):
                payload = record.model_dump(mode="json")
            else:
                payload = record
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return str(path)


def write_parquet(path: Path, records: list[Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payloads = [
        record.model_dump(mode="json") if hasattr(record, "model_dump") else record
        for record in records
    ]
    table = pa.Table.from_pylist(payloads)
    pq.write_table(table, path)
    return str(path)


def write_silver_document(doc: NormalizedDocument) -> dict[str, str]:
    root = silver_doc_dir(doc.doc_id)
    tables_dir = root / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "document_md": str(root / "document.md"),
        "document_json": str(root / "document.json"),
        "metadata_json": str(root / "metadata.json"),
    }
    Path(outputs["document_md"]).write_text(doc.markdown, encoding="utf-8")
    write_json(Path(outputs["document_json"]), doc)
    write_json(Path(outputs["metadata_json"]), doc.metadata)
    return outputs


def write_gold_bundle(
    doc: NormalizedDocument,
    chunks: list[ChunkPayload],
    graph_candidates: list[GraphCandidate],
    evidence: DocumentEvidence | None = None,
) -> dict[str, str]:
    cfg = get_config()
    root = gold_doc_dir(doc.doc_id)
    rag_dir = root / "rag_chunks"
    table_dir = root / "table_catalog"
    graph_dir = root / "graph_candidates"

    paths: dict[str, str] = {}
    chunks_jsonl = rag_dir / "chunks.jsonl"
    chunks_parquet = rag_dir / "chunks.parquet"

    for chunk in chunks:
        chunk.text_ref = f"{chunks_jsonl}#{chunk.chunk_id}"

    if cfg.output.write_jsonl:
        paths["chunks_jsonl"] = write_jsonl(chunks_jsonl, chunks)
        if graph_candidates:
            paths["graph_candidates_jsonl"] = write_jsonl(graph_dir / "candidates.jsonl", graph_candidates)
    if cfg.output.write_parquet:
        paths["chunks_parquet"] = write_parquet(chunks_parquet, chunks)
        if doc.tables:
            paths["tables_parquet"] = write_parquet(table_dir / "tables.parquet", doc.tables)
        if graph_candidates:
            paths["graph_candidates_parquet"] = write_parquet(graph_dir / "candidates.parquet", graph_candidates)

    evidence_path = root / "evidence.json"
    manifest = {
        "contract_version": DOCUMENT_EVIDENCE_CONTRACT_VERSION,
        "doc_id": doc.doc_id,
        "source_path": doc.source_path,
        "source_type": doc.source_type,
        "file_hash": doc.file_hash,
        "source_hash": doc.file_hash,
        "parser_id": doc.parser,
        "parser_version": doc.parser_version,
        "chunks": len(chunks),
        "tables": len(doc.tables),
        "graph_candidates": len(graph_candidates),
    }
    if evidence is not None:
        manifest["document_evidence"] = evidence_summary(evidence, evidence_path=str(evidence_path))
    paths["manifest_json"] = write_json(root / "manifest.json", manifest)
    if evidence is not None:
        evidence.output_paths = {
            **evidence.output_paths,
            **paths,
            "document_evidence_json": str(evidence_path),
        }
        paths["document_evidence_json"] = write_json(evidence_path, evidence)
    return paths


def write_table_catalog_json(tables: list[TableInfo], doc_id: str) -> str:
    return write_json(silver_doc_dir(doc_id) / "tables" / "tables.json", [t.model_dump(mode="json") for t in tables])


def write_rag_bundle_manifest(doc_id: str, manifest: RagBundleManifest) -> str:
    return write_json(gold_doc_dir(doc_id) / "rag_bundle_manifest.json", manifest)
