"""Document ETL and conversion pipeline for extrator jobs."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any

from extrator.adapters import parse_file
from extrator.adapters import html as html_adapter
from extrator.adapters import libreoffice, markdown as markdown_adapter, pandoc, tabular
from extrator.chunking import chunk_document
from extrator.config import get_config
from extrator.errors import AdapterUnavailable, ConversionError
from extrator.evidence import accepted_source_decision, build_document_evidence, evidence_summary
from extrator.formats import ensure_conversion_supported, source_type_for
from extrator.graph_candidates import build_graph_candidates
from extrator.hashing import sha256_file, stable_id
from extrator.manifest import get_manifest
from extrator.parser_quality import evaluate_parser_quality
from extrator.projection_contract import PROJECTION_CONTRACT_VERSION
from extrator.projection_index import recover_document_by_fingerprint
from extrator.rag_bundle import build_rag_bundle_manifest, rag_bundle_summary
from extrator.security import (
    PathSecurityError,
    is_skipped,
    sanitize_filename,
    validate_input_path,
)
from extrator.storage_guardian_api import materialize_file, publish_file
from extrator.storage import (
    conversion_dir,
    copy_or_reference_original,
    ensure_directories,
    gold_doc_dir,
    silver_doc_dir,
)
from extrator.types import (
    ConversionEvidence,
    ConversionPathRequest,
    DocumentInfo,
    ExtractionPathRequest,
    JobStatus,
)
from extrator.writers import (
    write_gold_bundle,
    write_rag_bundle_manifest,
    write_silver_document,
    write_table_catalog_json,
)

SILVER_OUTPUT_KEYS = frozenset(
    {
        "document_md",
        "document_json",
        "metadata_json",
        "table_catalog_json",
    }
)
GOLD_OUTPUT_KEYS = frozenset(
    {
        "chunks_jsonl",
        "chunks_parquet",
        "document_evidence_json",
        "graph_candidates_jsonl",
        "graph_candidates_parquet",
        "manifest_json",
        "rag_bundle_manifest_json",
        "tables_parquet",
    }
)


def _storage_store_for_output(output_key: str, path: str) -> str:
    if output_key == "bronze_original":
        return "extrator_uploads"
    if output_key in SILVER_OUTPUT_KEYS:
        return "extrator_silver"
    if output_key in GOLD_OUTPUT_KEYS:
        return "extrator_gold"
    return "extrator_gold"


def _publish_output_paths(
    doc_id: str,
    output_paths: dict[str, str],
    *,
    projection_doc_segment: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    published: dict[str, str] = {}
    projections: dict[str, str] = {}
    for key, value in output_paths.items():
        path = Path(value)
        if not path.is_file():
            published[key] = value
            continue
        projection_path = _projection_path_for_output(
            doc_id,
            key,
            path,
            projection_doc_segment=projection_doc_segment,
        )
        published[key] = publish_file(
            path,
            agent="extrator",
            store=_storage_store_for_output(key, value),
            logical_name=f"{doc_id}_{key}_{path.name}",
            projection_path=projection_path,
            metadata={
                "doc_id": doc_id,
                "output_key": key,
                "projection_contract_version": PROJECTION_CONTRACT_VERSION,
            },
        )
        projections[key] = projection_path
    return published, projections


def _projection_path_for_output(
    doc_id: str,
    output_key: str,
    path: Path,
    *,
    projection_doc_segment: str | None = None,
) -> str:
    doc_segment = _projection_doc_path(projection_doc_segment or doc_id, fallback="document")
    safe_name = sanitize_filename(path.name)
    if output_key == "bronze_original":
        return f"input/{doc_segment}/{safe_name}"

    for root in (silver_doc_dir(doc_id), gold_doc_dir(doc_id)):
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
            return f"output/{doc_segment}/{relative}"
        except ValueError:
            continue
    key_segment = _projection_segment(output_key, fallback="artifact")
    return f"output/{doc_segment}/{key_segment}/{safe_name}"


def _projection_segment(value: str, *, fallback: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value).strip()).strip("._-")
    return cleaned or fallback


def _projection_doc_path(value: str, *, fallback: str) -> str:
    parts = [
        _projection_segment(part, fallback="")
        for part in str(value).replace("\\", "/").split("/")
    ]
    cleaned = [part for part in parts if part]
    return "/".join(cleaned) or fallback


def _projection_doc_segment_for_source(
    source_path: Path,
    processed_at: datetime,
    *,
    file_hash: str | None = None,
    existing_document: DocumentInfo | None = None,
) -> str:
    if existing_document is not None and existing_document.file_hash == file_hash:
        metadata = existing_document.metadata if isinstance(existing_document.metadata, dict) else {}
        existing_segment = metadata.get("storage_projection_doc_segment")
        if isinstance(existing_segment, str) and existing_segment.strip() and _document_has_managed_projections(
            existing_document
        ):
            return _projection_doc_path(existing_segment, fallback="document")
    safe_name = sanitize_filename(source_path.name)
    timestamp = processed_at.astimezone(UTC)
    date_segment = timestamp.strftime("%Y-%m-%d")
    time_segment = timestamp.strftime("%H%M%S%fZ")
    doc_segment = _projection_segment(f"{safe_name}__{time_segment}", fallback=f"document__{time_segment}")
    return f"{date_segment}/{doc_segment}"


def _document_has_managed_projections(doc: DocumentInfo) -> bool:
    metadata = doc.metadata if isinstance(doc.metadata, dict) else {}
    if metadata.get("storage_projection_contract_version") != PROJECTION_CONTRACT_VERSION:
        return False
    projections = metadata.get("managed_projections")
    if not isinstance(projections, dict):
        return False
    published_keys = [
        key
        for key, ref in doc.output_paths.items()
        if isinstance(ref, str) and ref.startswith("storage_guardian://")
    ]
    local_projection_keys = [
        key
        for key, ref in doc.output_paths.items()
        if isinstance(ref, str)
        and not ref.startswith("storage_guardian://")
        and str(projections.get(key) or "").strip()
        and Path(ref).is_file()
    ]
    reusable_keys = published_keys or local_projection_keys
    return bool(reusable_keys) and all(str(projections.get(key) or "").strip() for key in reusable_keys)


def _iter_files(root: Path, *, recursive: bool) -> list[Path]:
    if root.is_file():
        return [root]
    iterator = root.rglob("*") if recursive else root.glob("*")
    return [path for path in iterator if path.is_file()]


def _process_one_file(path: Path, *, force: bool) -> tuple[DocumentInfo | None, dict[str, Any]]:
    cfg = get_config()
    manifest = get_manifest()
    skipped, reason = is_skipped(path)
    if skipped:
        return None, {"status": "skipped", "reason": reason, "path": str(path)}

    validate_input_path(path, for_extraction=True)
    file_hash = sha256_file(path, block_size=cfg.hashing.block_size_bytes)
    source_type = source_type_for(path)
    existing = manifest.find_document_by_source(str(path))
    if existing is None or existing.file_hash != file_hash or existing.status != JobStatus.COMPLETED.value:
        existing = manifest.find_document_by_fingerprint(file_hash, cfg.config_hash, source_type=source_type)
    if existing is None:
        existing = recover_document_by_fingerprint(file_hash, source_type=source_type, manifest=manifest)
    if not force and existing is not None and _document_has_managed_projections(existing):
        return existing, {
            "status": "unchanged",
            "path": str(path),
            "reused_by": "source_hash",
            "reused_doc_id": existing.doc_id,
            "reused_source_path": existing.source_path,
        }

    doc_id = stable_id("doc", str(path.resolve()), file_hash, cfg.config_hash)
    processed_at = datetime.now(UTC)
    projection_doc_segment = _projection_doc_segment_for_source(
        path,
        processed_at,
        file_hash=file_hash,
        existing_document=existing,
    )
    table_dir = silver_doc_dir(doc_id) / "tables"
    doc = parse_file(path, table_dir=table_dir)
    bronze_path = copy_or_reference_original(path, doc.doc_id)
    chunks = chunk_document(doc)
    parser_quality = evaluate_parser_quality(path, doc, chunks)
    doc.metadata = {
        **doc.metadata,
        "parser_quality": parser_quality.model_dump(mode="json"),
    }
    silver_outputs = write_silver_document(doc)
    table_catalog = write_table_catalog_json(doc.tables, doc.doc_id)
    graph_candidates = build_graph_candidates(chunks) if cfg.output.graph_candidates_enabled else []
    base_output_paths = {
        "bronze_original": bronze_path,
        "table_catalog_json": table_catalog,
        **silver_outputs,
    }
    evidence = build_document_evidence(
        doc,
        chunks,
        output_paths=base_output_paths,
        security_decisions=[accepted_source_decision(path)],
    )
    gold_outputs = write_gold_bundle(doc, chunks, graph_candidates, evidence=evidence)

    local_output_paths = {
        **base_output_paths,
        **gold_outputs,
    }
    output_paths, managed_projections = _publish_output_paths(
        doc.doc_id,
        local_output_paths,
        projection_doc_segment=projection_doc_segment,
    )
    rag_bundle = build_rag_bundle_manifest(
        doc,
        chunks,
        graph_candidates,
        output_refs=output_paths,
        local_paths=local_output_paths,
    )
    rag_bundle_manifest = write_rag_bundle_manifest(doc.doc_id, rag_bundle)
    rag_projection_path = _projection_path_for_output(
        doc.doc_id,
        "rag_bundle_manifest_json",
        Path(rag_bundle_manifest),
        projection_doc_segment=projection_doc_segment,
    )
    rag_bundle_ref = publish_file(
        Path(rag_bundle_manifest),
        agent="extrator",
        store="extrator_gold",
        logical_name=f"{doc.doc_id}_rag_bundle_manifest.json",
        projection_path=rag_projection_path,
        metadata={
            "doc_id": doc.doc_id,
            "output_key": "rag_bundle_manifest_json",
            "projection_contract_version": PROJECTION_CONTRACT_VERSION,
        },
    )
    output_paths["rag_bundle_manifest_json"] = rag_bundle_ref
    managed_projections["rag_bundle_manifest_json"] = rag_projection_path
    metadata = {
        **doc.metadata,
        "storage_projection_contract_version": PROJECTION_CONTRACT_VERSION,
        "storage_projection_doc_segment": projection_doc_segment,
        "managed_projections": managed_projections,
        "document_evidence": evidence_summary(
            evidence,
            evidence_path=output_paths.get("document_evidence_json"),
        ),
        "rag_bundle": rag_bundle_summary(
            rag_bundle,
            manifest_ref=output_paths.get("rag_bundle_manifest_json"),
        ),
    }
    info = DocumentInfo(
        doc_id=doc.doc_id,
        source_path=doc.source_path,
        source_type=doc.source_type,
        file_hash=doc.file_hash,
        status="completed",
        output_paths=output_paths,
        metadata=metadata,
    )
    manifest.upsert_document(info)
    manifest.replace_chunks(doc.doc_id, chunks)
    manifest.replace_tables(doc.doc_id, doc.tables)
    return info, {
        "status": "processed",
        "path": str(path),
        "doc_id": doc.doc_id,
        "source_type": doc.source_type,
        "chunks": len(chunks),
        "tables": len(doc.tables),
        "metadata": doc.metadata,
        "rag_bundle": metadata["rag_bundle"],
        "table_summaries": [table.summary[:2000] for table in doc.tables[:5]],
    }


def process_extraction_job(job_id: str, request: ExtractionPathRequest) -> None:
    ensure_directories()
    manifest = get_manifest()
    manifest.update_job(job_id, status=JobStatus.RUNNING)
    summary: dict[str, Any] = {
        "files_seen": 0,
        "documents_processed": 0,
        "documents_unchanged": 0,
        "files_skipped": 0,
        "errors": [],
        "results": [],
    }
    outputs: dict[str, str] = {}

    try:
        root = validate_input_path(request.input_path, for_extraction=True)
        for path in _iter_files(root, recursive=request.recursive):
            summary["files_seen"] += 1
            try:
                doc, result = _process_one_file(path, force=request.force)
                if result["status"] == "processed":
                    summary["documents_processed"] += 1
                elif result["status"] == "unchanged":
                    summary["documents_unchanged"] += 1
                elif result["status"] == "skipped":
                    summary["files_skipped"] += 1
                if doc is not None:
                    outputs[doc.doc_id] = doc.output_paths.get("manifest_json", "")
                if len(summary["results"]) < 50:
                    summary["results"].append(result)
            except (PathSecurityError, AdapterUnavailable, ValueError, OSError) as exc:
                summary["errors"].append({"path": str(path), "error": str(exc)})
        status = JobStatus.COMPLETED if not summary["errors"] else JobStatus.FAILED
        manifest.update_job(job_id, status=status, outputs=outputs, summary=summary)
    except Exception as exc:
        manifest.update_job(job_id, status=JobStatus.FAILED, error=str(exc), outputs=outputs, summary=summary)


def _conversion_suffix(output_format: str) -> str:
    return ".md" if output_format == "markdown" else f".{output_format}"


def _conversion_output_path(job_id: str, input_path: Path, output_format: str, *, root: Path | None = None) -> Path:
    suffix = _conversion_suffix(output_format)
    if root is not None and root.is_dir():
        try:
            relative = input_path.relative_to(root)
        except ValueError:
            relative = Path(input_path.name)
        return conversion_dir(job_id) / relative.with_suffix(suffix)
    return conversion_dir(job_id) / f"{input_path.stem}{suffix}"


def _materialized_output_path(input_path: Path, output_format: str, request: ConversionPathRequest, *, root: Path | None = None) -> Path:
    suffix = _conversion_suffix(output_format)
    override = _materialized_output_override(input_path, output_format, request)
    if override is not None:
        return override
    if request.output_path:
        requested = Path(request.output_path)
        if root is not None and root.is_dir():
            try:
                relative = input_path.relative_to(root)
            except ValueError:
                relative = Path(input_path.name)
            return requested / relative.with_suffix(suffix)
        if not requested.suffix or (requested.exists() and requested.is_dir()):
            return requested / input_path.with_suffix(suffix).name
        return requested
    return input_path.with_suffix(suffix)


def _materialized_output_override(input_path: Path, output_format: str, request: ConversionPathRequest) -> Path | None:
    suffix = _conversion_suffix(output_format)
    for key in (str(input_path), input_path.name, input_path.stem):
        raw = request.output_paths.get(key)
        if not raw:
            continue
        requested = Path(raw)
        if not requested.suffix or (requested.exists() and requested.is_dir()):
            return requested / input_path.with_suffix(suffix).name
        return requested
    return None


def _storage_guardian_destination_path(path: Path) -> str:
    """Map extrator-visible project paths to the storage_guardian writable mount."""

    raw = str(path)
    project_prefix = os.environ.get("EXTRATOR_PROJECT_MOUNT", "/projects/ai-local").rstrip("/")
    storage_project_root = os.environ.get("STORAGE_GUARDIAN_PROJECT_ROOT", "/workspace/ai-local").rstrip("/")
    if raw == project_prefix:
        return storage_project_root
    if raw.startswith(f"{project_prefix}/"):
        return storage_project_root + raw[len(project_prefix) :]
    return raw


def _already_target_format(path: Path, output_format: str) -> bool:
    return path.suffix.lower() == _conversion_suffix(output_format).lower()


def _ensure_writable_output(path: Path, *, force: bool) -> None:
    cfg = get_config()
    if path.exists() and not force and not cfg.conversion.overwrite_allowed:
        raise ConversionError(f"Output already exists and overwrite is disabled: {path}")


def _convert(input_path: Path, output_format: str, *, job_id: str, force: bool, root: Path | None = None) -> Path:
    source_format = source_type_for(input_path)
    ensure_conversion_supported(source_format, output_format)
    output_path = _conversion_output_path(job_id, input_path, output_format, root=root)
    _ensure_writable_output(output_path, force=force)

    if source_format == "markdown" and output_format == "html":
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw = input_path.read_text(encoding="utf-8", errors="ignore")
        output_path.write_text(markdown_adapter.markdown_to_html(raw), encoding="utf-8")
        return output_path
    if source_format == "html" and output_format == "markdown":
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw = input_path.read_text(encoding="utf-8", errors="ignore")
        output_path.write_text(html_adapter.html_to_markdown(raw), encoding="utf-8")
        return output_path
    if source_format in {"csv", "tsv", "xlsx", "xls"} and output_format == "parquet":
        return tabular.convert_to_parquet(input_path, output_path)
    if source_format in {"docx", "pptx"} and output_format in {"pdf", "html"}:
        return libreoffice.convert(input_path, output_path.parent, output_format)
    if source_format in {"markdown", "docx"} and output_format in {"docx", "pdf", "html", "markdown"}:
        return pandoc.convert(input_path, output_path)
    if source_format == "pdf" and output_format in {"markdown", "json"}:
        raise AdapterUnavailable("PDF conversion requires Docling or Unstructured integration")
    raise ConversionError(f"No adapter for conversion {source_format}:{output_format}")


def process_conversion_job(job_id: str, request: ConversionPathRequest) -> None:
    ensure_directories()
    manifest = get_manifest()
    manifest.update_job(job_id, status=JobStatus.RUNNING)
    outputs: dict[str, str] = {}
    summary: dict[str, Any] = {
        "files_seen": 0,
        "files_converted": 0,
        "files_skipped": 0,
        "errors": [],
        "results": [],
    }
    try:
        root = validate_input_path(request.input_path, for_extraction=False)
        files = _iter_files(root, recursive=request.recursive)
        if not files:
            raise ConversionError(f"Conversion input did not include files: {root}")
        conversion_evidence: list[dict[str, Any]] = []
        for input_path in files:
            summary["files_seen"] += 1
            try:
                if root.is_dir() and _already_target_format(input_path, request.output_format):
                    summary["files_skipped"] += 1
                    if len(summary["results"]) < 50:
                        summary["results"].append(
                            {
                                "status": "skipped",
                                "reason": "already_target_format",
                                "input_path": str(input_path),
                            }
                        )
                    continue
                destination = _materialized_output_path(
                    input_path,
                    request.output_format,
                    request,
                    root=root if root.is_dir() else None,
                )
                if destination.exists() and not request.force:
                    conversion_id = manifest.record_conversion(
                        job_id,
                        str(input_path),
                        request.output_format,
                        str(destination),
                        "completed",
                    )
                    evidence = ConversionEvidence(
                        conversion_id=conversion_id,
                        job_id=job_id,
                        input_path=str(input_path),
                        source_hash=sha256_file(input_path, block_size=get_config().hashing.block_size_bytes),
                        output_format=request.output_format,
                        output_path=str(destination),
                        status="completed",
                        warnings=["output_already_exists"],
                        security_decisions=[accepted_source_decision(input_path)],
                    )
                    key = str(input_path.relative_to(root)) if root.is_dir() else "output"
                    outputs[key] = str(destination)
                    summary["files_skipped"] += 1
                    item = {
                        "status": "reused_existing_output",
                        "input_path": str(input_path),
                        "output_path": str(destination),
                        "conversion_id": conversion_id,
                    }
                    conversion_evidence.append(evidence.model_dump(mode="json"))
                    if len(summary["results"]) < 50:
                        summary["results"].append(item)
                    continue
                output = _convert(
                    input_path,
                    request.output_format,
                    job_id=job_id,
                    force=request.force,
                    root=root if root.is_dir() else None,
                )
                materialized_output = materialize_file(
                    output,
                    destination_path=_storage_guardian_destination_path(destination),
                    agent="extrator",
                    store="agent_outputs",
                    logical_name=f"{job_id}_conversion_{output.name}",
                    metadata={
                        "job_id": job_id,
                        "input_path": str(input_path),
                        "output_format": request.output_format,
                    },
                    overwrite=request.force,
                )
                conversion_id = manifest.record_conversion(
                    job_id,
                    str(input_path),
                    request.output_format,
                    materialized_output,
                    "completed",
                )
                evidence = ConversionEvidence(
                    conversion_id=conversion_id,
                    job_id=job_id,
                    input_path=str(input_path),
                    source_hash=sha256_file(input_path, block_size=get_config().hashing.block_size_bytes),
                    output_format=request.output_format,
                    output_path=materialized_output,
                    status="completed",
                    security_decisions=[accepted_source_decision(input_path)],
                )
                key = str(input_path.relative_to(root)) if root.is_dir() else "output"
                outputs[key] = materialized_output
                summary["files_converted"] += 1
                item = {
                    "status": "converted",
                    "input_path": str(input_path),
                    "output_path": materialized_output,
                    "conversion_id": conversion_id,
                }
                conversion_evidence.append(evidence.model_dump(mode="json"))
                if len(summary["results"]) < 50:
                    summary["results"].append(item)
            except Exception as exc:
                manifest.record_conversion(job_id, str(input_path), request.output_format, "", "failed")
                summary["errors"].append({"path": str(input_path), "error": str(exc)})
        status = JobStatus.COMPLETED if not summary["errors"] else JobStatus.FAILED
        summary["conversion_evidence"] = conversion_evidence
        manifest.update_job(
            job_id,
            status=status,
            outputs=outputs,
            summary=summary,
        )
    except Exception as exc:
        manifest.record_conversion(job_id, request.input_path, request.output_format, "", "failed")
        summary["errors"].append({"path": request.input_path, "error": str(exc)})
        manifest.update_job(job_id, status=JobStatus.FAILED, error=str(exc), outputs=outputs, summary=summary)


def process_job(job_id: str) -> None:
    manifest = get_manifest()
    job = manifest.get_job(job_id)
    payload = manifest.get_job_payload(job_id)
    if job.kind.value == "extraction":
        process_extraction_job(job_id, ExtractionPathRequest.model_validate(payload))
        return
    if job.kind.value == "conversion":
        process_conversion_job(job_id, ConversionPathRequest.model_validate(payload))
        return
    manifest.update_job(job_id, status=JobStatus.FAILED, error=f"Unsupported job kind: {job.kind.value}")


def serialize_request(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str))
