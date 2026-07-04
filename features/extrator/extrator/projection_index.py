"""Recover extrator manifest entries from managed storage projections."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from extrator.config import get_config
from extrator.projection_contract import PROJECTION_CONTRACT_VERSION
from extrator.types import ChunkPayload, DocumentInfo, JobStatus, TableInfo

if TYPE_CHECKING:
    from extrator.manifest import ExtratorManifest

MAX_PROJECTION_EVIDENCE_FILES = 10_000


def recover_document_by_fingerprint(
    file_hash: str,
    *,
    source_type: str | None = None,
    manifest: ExtratorManifest | None = None,
) -> DocumentInfo | None:
    """Rebuild a completed manifest row from existing managed projections.

    This is a repair path for cases where the persistent artifacts exist but
    the runtime manifest was lost or recreated. Identity is the source bytes
    hash plus source type; the original path is only provenance.
    """

    if not file_hash:
        return None
    if manifest is None:
        from extrator.manifest import get_manifest

        manifest = get_manifest()

    for root in _projection_roots():
        for evidence_path in _iter_evidence_paths(root):
            evidence = _load_json(evidence_path)
            if not isinstance(evidence, dict):
                continue
            if str(evidence.get("source_hash") or evidence.get("file_hash") or "") != file_hash:
                continue
            recovered_source_type = str(evidence.get("source_type") or "")
            if source_type and recovered_source_type and recovered_source_type != source_type:
                continue
            doc = _document_from_projection(root, evidence_path, evidence)
            if doc is None:
                continue
            manifest.upsert_document(doc)
            chunks = _load_chunks(doc.output_paths.get("chunks_jsonl", ""), doc_id=doc.doc_id)
            if chunks:
                manifest.replace_chunks(doc.doc_id, chunks)
            tables = _load_tables(doc.output_paths.get("table_catalog_json", ""), doc_id=doc.doc_id)
            if tables:
                manifest.replace_tables(doc.doc_id, tables)
            return doc
    return None


def _projection_roots() -> list[Path]:
    cfg = get_config()
    candidates: list[str] = []
    for key in ("EXTRATOR_PROJECTION_ROOT", "EXTRATOR_PROJECTION_INPUT_ROOT"):
        raw = os.environ.get(key, "").strip()
        if raw:
            candidates.append(raw)
    candidates.extend(
        [
            "/data/input",
            cfg.paths.data_dir,
            *cfg.security.allowed_roots,
        ]
    )

    roots: list[Path] = []
    seen: set[str] = set()
    for raw in candidates:
        root = Path(raw).expanduser()
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        if (root / "gold" / "output").is_dir():
            roots.append(root)
    return roots


def _iter_evidence_paths(root: Path) -> list[Path]:
    evidence_root = root / "gold" / "output"
    paths = [path for path in evidence_root.glob("*/*/evidence.json") if path.is_file()]
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[:MAX_PROJECTION_EVIDENCE_FILES]


def _document_from_projection(root: Path, evidence_path: Path, evidence: dict[str, Any]) -> DocumentInfo | None:
    try:
        segment = evidence_path.parent.relative_to(root / "gold" / "output").as_posix()
    except ValueError:
        return None

    doc_id = str(evidence.get("doc_id") or "").strip()
    source_hash = str(evidence.get("source_hash") or evidence.get("file_hash") or "").strip()
    source_path = str(evidence.get("source_path") or "").strip()
    source_type = str(evidence.get("source_type") or "").strip()
    if not (doc_id and source_hash and source_type):
        return None

    output_paths, managed_projections = _projection_output_paths(root, segment, source_path)
    if "document_evidence_json" not in output_paths or "chunks_jsonl" not in output_paths:
        return None

    metadata = _projection_metadata(
        root,
        segment,
        evidence,
        output_paths=output_paths,
        managed_projections=managed_projections,
    )
    return DocumentInfo(
        doc_id=doc_id,
        source_path=source_path,
        source_type=source_type,
        file_hash=source_hash,
        status=JobStatus.COMPLETED.value,
        output_paths=output_paths,
        metadata=metadata,
    )


def _projection_output_paths(root: Path, segment: str, source_path: str) -> tuple[dict[str, str], dict[str, str]]:
    relatives = {
        "document_evidence_json": f"gold/output/{segment}/evidence.json",
        "manifest_json": f"gold/output/{segment}/manifest.json",
        "rag_bundle_manifest_json": f"gold/output/{segment}/rag_bundle_manifest.json",
        "chunks_jsonl": f"gold/output/{segment}/rag_chunks/chunks.jsonl",
        "chunks_parquet": f"gold/output/{segment}/rag_chunks/chunks.parquet",
        "graph_candidates_jsonl": f"gold/output/{segment}/graph_candidates/candidates.jsonl",
        "graph_candidates_parquet": f"gold/output/{segment}/graph_candidates/candidates.parquet",
        "tables_parquet": f"gold/output/{segment}/table_catalog/tables.parquet",
        "document_md": f"silver/output/{segment}/document.md",
        "document_json": f"silver/output/{segment}/document.json",
        "metadata_json": f"silver/output/{segment}/metadata.json",
        "table_catalog_json": f"silver/output/{segment}/tables/tables.json",
    }
    upload_dir = root / "uploads" / "input" / segment
    source_name = Path(source_path).name
    if source_name and (upload_dir / source_name).is_file():
        relatives["bronze_original"] = f"uploads/input/{segment}/{source_name}"
    elif upload_dir.is_dir():
        first_file = next((path for path in upload_dir.iterdir() if path.is_file()), None)
        if first_file is not None:
            relatives["bronze_original"] = f"uploads/input/{segment}/{first_file.name}"

    output_paths: dict[str, str] = {}
    managed_projections: dict[str, str] = {}
    for key, relative in relatives.items():
        path = root / relative
        if path.is_file():
            output_paths[key] = str(path)
            managed_projections[key] = relative
    return output_paths, managed_projections


def _projection_metadata(
    root: Path,
    segment: str,
    evidence: dict[str, Any],
    *,
    output_paths: dict[str, str],
    managed_projections: dict[str, str],
) -> dict[str, Any]:
    chunks_count = _count_jsonl(output_paths.get("chunks_jsonl", ""))
    table_payload = _load_json(output_paths.get("table_catalog_json", ""))
    tables_count = len(table_payload) if isinstance(table_payload, list) else 0
    return {
        "storage_projection_contract_version": PROJECTION_CONTRACT_VERSION,
        "storage_projection_doc_segment": segment,
        "managed_projections": managed_projections,
        "projection_recovered_from": "managed_storage_projection",
        "projection_recovered_root": str(root),
        "document_evidence": {
            "contract_version": evidence.get("contract_version"),
            "doc_id": evidence.get("doc_id"),
            "source_hash": evidence.get("source_hash"),
            "source_type": evidence.get("source_type"),
            "parser_id": evidence.get("parser_id"),
            "parser_version": evidence.get("parser_version"),
            "parser_confidence": evidence.get("parser_confidence"),
            "evidence_path": output_paths.get("document_evidence_json"),
        },
        "rag_bundle": {
            "contract_version": "rag_bundle.v1",
            "manifest_ref": output_paths.get("rag_bundle_manifest_json"),
            "chunk_count": chunks_count,
            "table_count": tables_count,
        },
    }


def _load_json(path: str | Path) -> Any:
    if not path:
        return None
    target = Path(path)
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _load_chunks(path: str, *, doc_id: str) -> list[ChunkPayload]:
    target = Path(path)
    if not target.is_file():
        return []
    chunks: list[ChunkPayload] = []
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if payload.get("doc_id") != doc_id:
                    continue
                payload["text_ref"] = f"{target}#{payload.get('chunk_id', '')}"
                chunks.append(ChunkPayload.model_validate(payload))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return []
    return chunks


def _load_tables(path: str, *, doc_id: str) -> list[TableInfo]:
    payload = _load_json(path)
    if not isinstance(payload, list):
        return []
    tables: list[TableInfo] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("doc_id") != doc_id:
            continue
        try:
            tables.append(TableInfo.model_validate(item))
        except ValueError:
            continue
    return tables


def _count_jsonl(path: str) -> int:
    target = Path(path)
    if not target.is_file():
        return 0
    try:
        with target.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0
