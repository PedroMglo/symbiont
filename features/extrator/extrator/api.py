"""FastAPI application for the Extrator feature."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from extrator import __version__
from extrator.config import ConfigError, get_config
from extrator.diagnostics import diagnose_document, diagnostic_summary
from extrator.errors import ExtratorError
from extrator.formats import source_type_for
from extrator.hashing import sha256_file
from extrator.jobs import create_conversion_job, create_extraction_job
from extrator.manifest import get_manifest
from extrator.observability import configure_logging
from extrator.pipeline import process_job
from extrator.projection_index import recover_document_by_fingerprint
from extrator.query_intents import (
    ExtratorPathRequest,
    resolve_path_request_job_mode,
    select_path_request,
    select_path_request_from_metadata,
)
from extrator.queue import get_queue
from extrator.security import (
    PathSecurityError,
    validate_extension,
    validate_input_path,
    validate_upload_size,
    verify_api_key,
)
from extrator.sandbox_plan import build_extrator_sandbox_plan
from extrator.storage import ensure_directories, upload_path
from extrator.types import (
    CapabilitiesResponse,
    CleanupRequest,
    ConversionPathRequest,
    DocumentDiagnostic,
    DocumentDiagnosticRequest,
    DocumentInfo,
    ExtratorQueryRequest,
    ExtratorQueryResponse,
    ExtractionPathRequest,
    FormatsResponse,
    HealthResponse,
    JobCreateResponse,
    JobKind,
    JobStatus,
    QueryAction,
    ReprocessRequest,
    StatsResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    ensure_directories()
    get_manifest().health()
    queue = get_queue()
    queue.set_processor(process_job)
    await queue.start()
    yield
    await queue.stop()


app = FastAPI(
    title="Extrator Feature",
    version=__version__,
    description="Document ETL and file conversion service for ai-local",
    lifespan=lifespan,
)


@app.exception_handler(PathSecurityError)
async def path_security_handler(request, exc: PathSecurityError):
    return JSONResponse(status_code=403, content={"error": str(exc)})


@app.exception_handler(ConfigError)
async def config_error_handler(request, exc: ConfigError):
    return JSONResponse(status_code=500, content={"error": str(exc)})


@app.exception_handler(ExtratorError)
async def extrator_error_handler(request, exc: ExtratorError):
    return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        service="extrator",
        version=__version__,
        manifest_reachable=get_manifest().health(),
    )


@app.get("/v1/extrator/capabilities", response_model=CapabilitiesResponse)
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse(
        name="extrator",
        capabilities=[
            "document_etl",
            "document_diagnosis",
            "document_workflow_selection",
            "document_extraction",
            "file_conversion",
            "rag_bundle",
            "workspace_sandbox_preparation_plan",
        ],
        description="Extracts, normalizes, chunks, and converts local documents without embedding them.",
        workflow_actions=[
            {
                "action": "diagnose",
                "capability": "document_diagnosis",
                "endpoint": "/v1/extrator/diagnostics/path",
                "policy_action": "document.extract",
                "requires_workspace_execution": False,
                "description": "Preflight document sensitivity, language, structure, OCR, cost, and workflow signals.",
            },
            {
                "action": "extract",
                "capability": "document_extraction",
                "endpoint": "/v1/extrator/extractions/path",
                "policy_action": "document.extract",
                "requires_workspace_execution": False,
                "description": "Normalize a supported document into evidence, chunks, tables, and RAG bundle artifacts.",
            },
            {
                "action": "convert",
                "capability": "file_conversion",
                "endpoint": "/v1/extrator/conversions/path",
                "policy_action": "document.extract",
                "requires_workspace_execution": False,
                "description": "Convert a supported low-risk format pair through the extrator conversion API.",
            },
            {
                "action": "sandbox_required",
                "capability": "workspace_sandbox_preparation_plan",
                "endpoint": "/v1/extrator/query",
                "policy_action": "workspace.sandbox.create",
                "requires_workspace_execution": True,
                "description": "Return a workspace_execution preparation plan when conversion or probing should run in a disposable copy.",
            },
        ],
        contracts={
            "document_diagnostic": "document_diagnostic.v1",
            "document_evidence": "document_evidence.v1",
            "rag_bundle": "rag_bundle.v1",
            "sandbox_preparation_plan": "sandbox_preparation_plan.v1",
        },
    )


@app.get(
    "/v1/extrator/formats",
    response_model=FormatsResponse,
    dependencies=[Depends(verify_api_key)],
)
def formats() -> FormatsResponse:
    cfg = get_config()
    return FormatsResponse(
        extract_input_extensions=cfg.formats.extract_input_extensions,
        conversion_pairs=cfg.formats.conversion_pairs,
        output_formats=cfg.formats.output_formats,
    )


@app.post(
    "/v1/extrator/diagnostics/path",
    response_model=DocumentDiagnostic,
    dependencies=[Depends(verify_api_key)],
)
def diagnose_path(request: DocumentDiagnosticRequest) -> DocumentDiagnostic:
    return diagnose_document(request)


async def _enqueue(job_id: str) -> None:
    try:
        await get_queue().enqueue(job_id)
    except asyncio.QueueFull as exc:
        raise HTTPException(status_code=429, detail="Job queue is full") from exc


@app.post(
    "/v1/extrator/extractions/path",
    status_code=202,
    response_model=JobCreateResponse,
    dependencies=[Depends(verify_api_key)],
)
async def extract_path(request: ExtractionPathRequest) -> JobCreateResponse:
    job_id = create_extraction_job(request)
    await _enqueue(job_id)
    return JobCreateResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        status_url=f"/v1/extrator/jobs/{job_id}",
    )


@app.post(
    "/v1/extrator/extractions/upload",
    status_code=202,
    response_model=JobCreateResponse,
    dependencies=[Depends(verify_api_key)],
)
async def extract_upload(
    file: UploadFile = File(...),
    recursive: bool = Query(...),
    force: bool = Query(...),
    targets_json: str = Query(...),
    metadata_json: str = Query(...),
) -> JobCreateResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    validate_extension(file.filename, for_extraction=True)
    content = await file.read()
    validate_upload_size(len(content))
    target = upload_path(file.filename)
    target.write_bytes(content)
    request = ExtractionPathRequest(
        input_path=str(target),
        recursive=recursive,
        force=force,
        targets=json.loads(targets_json),
        metadata=json.loads(metadata_json),
    )
    return await extract_path(request)


@app.post(
    "/v1/extrator/conversions/path",
    status_code=202,
    response_model=JobCreateResponse,
    dependencies=[Depends(verify_api_key)],
)
async def convert_path(request: ConversionPathRequest) -> JobCreateResponse:
    job_id = create_conversion_job(request)
    await _enqueue(job_id)
    return JobCreateResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        status_url=f"/v1/extrator/jobs/{job_id}",
    )


@app.post(
    "/v1/extrator/query",
    response_model=ExtratorQueryResponse,
    dependencies=[Depends(verify_api_key)],
)
async def query(request: ExtratorQueryRequest) -> ExtratorQueryResponse:
    start = time.time()
    selected = select_path_request_from_metadata(request.metadata or {}) or select_path_request(request.query)
    if selected is None:
        return ExtratorQueryResponse(
            action=QueryAction.NO_ACTION,
            success=False,
            latency_ms=(time.time() - start) * 1000,
            metadata={
                "query_action": QueryAction.NO_ACTION.value,
                "query": request.query,
            },
            error="No processable absolute path found in query",
        )

    selected, job_kind_value, job_kind_source = resolve_path_request_job_mode(selected, request.metadata or {})
    job_kind = JobKind(job_kind_value)
    sandbox_plan = build_extrator_sandbox_plan(selected, job_kind=job_kind).model_dump(mode="json")
    diagnostic = diagnose_document(
        DocumentDiagnosticRequest(
            input_path=selected.input_path,
            recursive=selected.recursive,
            force=selected.force,
            conversion_format=selected.conversion_format,
            metadata=request.metadata or {},
        )
    )
    diagnostic_metadata = diagnostic_summary(diagnostic)
    try:
        target_path = _validate_query_selection(selected, job_kind=job_kind)
    except PathSecurityError as exc:
        return ExtratorQueryResponse(
            action=QueryAction.BLOCKED,
            success=False,
            latency_ms=(time.time() - start) * 1000,
            metadata={
                **(request.metadata or {}),
                "query_action": QueryAction.BLOCKED.value,
                "query": request.query,
                "original_path": selected.original_path,
                "container_path": selected.input_path,
                "job_kind": job_kind.value,
                "job_kind_source": job_kind_source,
                "sandbox_preparation_plan": sandbox_plan,
                "document_diagnostic": diagnostic_metadata,
            },
            error=str(exc),
        )

    reused = _reusable_document(selected, target_path, job_kind=job_kind)
    if reused is not None:
        content = _format_reused_query_content(reused, selected.original_path, selected.input_path)
        return ExtratorQueryResponse(
            content=content,
            action=QueryAction.REUSED_RESULT,
            token_estimate=max(1, len(content) // 4),
            success=True,
            latency_ms=(time.time() - start) * 1000,
            metadata={
                **(request.metadata or {}),
                "query_action": QueryAction.REUSED_RESULT.value,
                "query": request.query,
                "doc_id": reused.doc_id,
                "original_path": selected.original_path,
                "container_path": selected.input_path,
                "job_kind": job_kind.value,
                "job_kind_source": job_kind_source,
                "status": reused.status,
                "summary": {"documents_reused": 1},
                "outputs": reused.output_paths,
                "document_evidence": reused.metadata.get("document_evidence"),
                "document_diagnostic": diagnostic_metadata,
                "rag_bundle": reused.metadata.get("rag_bundle"),
                "semantic_digest": _document_semantic_digest(
                    reused,
                    source_paths=[selected.original_path],
                    status=reused.status,
                ),
                "sandbox_preparation_plan": sandbox_plan,
            },
        )

    if (request.metadata or {}).get("reuse_only") or (request.metadata or {}).get("reuse_policy") == "reuse_only":
        return ExtratorQueryResponse(
            action=QueryAction.NO_ACTION,
            success=False,
            latency_ms=(time.time() - start) * 1000,
            metadata={
                **(request.metadata or {}),
                "query_action": QueryAction.NO_ACTION.value,
                "query": request.query,
                "original_path": selected.original_path,
                "container_path": selected.input_path,
                "job_kind": job_kind.value,
                "job_kind_source": job_kind_source,
                "status": "reuse_miss",
                "summary": {"documents_reused": 0},
                "document_diagnostic": diagnostic_metadata,
                "sandbox_preparation_plan": sandbox_plan,
                "semantic_digest": {
                    "contract_version": "semantic_evidence_digest.v1",
                    "provider": "extrator",
                    "capability": "document_extraction",
                    "digest_kind": "document",
                    "source_paths": [selected.original_path],
                    "status": "reuse_miss",
                    "semantic_content_available": False,
                    "excerpts": [],
                    "summary": {"documents_reused": 0},
                    "output_refs": {},
                    "missing_semantic_evidence": ["No reusable extraction result found for reuse_only request."],
                },
            },
            error="No reusable extraction result found for reuse_only request.",
        )

    metadata = {
        **(request.metadata or {}),
        "source": (request.metadata or {}).get("source", "symbiont"),
        "query": request.query,
        "original_path": selected.original_path,
        "container_path": selected.input_path,
        "query_action": QueryAction.CREATED_JOB.value,
        "document_diagnostic": diagnostic_metadata,
    }
    if selected.conversion_format:
        job_id = create_conversion_job(
            ConversionPathRequest(
                input_path=selected.input_path,
                output_format=selected.conversion_format,
                force=selected.force,
                preserve_layout=True,
                recursive=selected.recursive,
                output_path=selected.output_path,
                output_paths=selected.output_paths,
                metadata=metadata,
            )
        )
    else:
        job_id = create_extraction_job(
            ExtractionPathRequest(
                input_path=selected.input_path,
                recursive=selected.recursive,
                force=selected.force,
                targets=["rag"],
                metadata=metadata,
            )
        )
    await _enqueue(job_id)

    job = get_manifest().get_job(job_id)
    wait_seconds = request.wait_seconds
    if wait_seconds is None and request.timeout_seconds is not None:
        wait_seconds = max(0.5, request.timeout_seconds - 0.5)
    deadline = time.time() + max(0.5, min(wait_seconds or 20.0, 20.0))
    while time.time() < deadline:
        job = get_manifest().get_job(job_id)
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            break
        await asyncio.sleep(0.5)

    content = _format_query_job_content(
        job_kind.value,
        selected.original_path,
        selected.input_path,
        _job_payload(job),
    )
    return ExtratorQueryResponse(
        content=content,
        action=QueryAction.CREATED_JOB,
        token_estimate=max(1, len(content) // 4),
        success=job.status != JobStatus.FAILED,
        latency_ms=(time.time() - start) * 1000,
        metadata={
            **(request.metadata or {}),
            "query_action": QueryAction.CREATED_JOB.value,
            "query": request.query,
            "job_id": job_id,
            "job_kind": job_kind.value,
            "job_kind_source": job_kind_source,
            "original_path": selected.original_path,
            "container_path": selected.input_path,
            "status": job.status.value,
            "summary": job.summary,
            "outputs": job.outputs,
            "sandbox_preparation_plan": sandbox_plan,
            "document_diagnostic": diagnostic_metadata,
            "semantic_digest": _job_semantic_digest(
                job.summary,
                outputs=job.outputs,
                source_paths=[selected.original_path],
                status=job.status.value,
            ),
        },
        error=job.error,
    )


def _validate_query_selection(selected: ExtratorPathRequest, *, job_kind: JobKind) -> Path:
    target = validate_input_path(
        selected.input_path,
        for_extraction=job_kind != JobKind.CONVERSION,
    )
    if job_kind == JobKind.CONVERSION and not (target.is_file() or target.is_dir()):
        raise PathSecurityError(f"Conversion input must be a file or directory: {target}")
    return target


def _reusable_document(
    selected: ExtratorPathRequest,
    target_path: Path,
    *,
    job_kind: JobKind,
) -> DocumentInfo | None:
    if job_kind != JobKind.EXTRACTION or selected.force or selected.recursive or not target_path.is_file():
        return None
    cfg = get_config()
    file_hash = sha256_file(target_path, block_size=cfg.hashing.block_size_bytes)
    source_type = source_type_for(target_path)
    existing = get_manifest().find_document_by_source(str(target_path))
    if existing is None or existing.status != JobStatus.COMPLETED.value or existing.file_hash != file_hash:
        existing = get_manifest().find_document_by_fingerprint(
            file_hash,
            cfg.config_hash,
            source_type=source_type,
        )
    if existing is None:
        existing = recover_document_by_fingerprint(
            file_hash,
            source_type=source_type,
            manifest=get_manifest(),
        )
    if existing is None:
        return None
    return existing


@app.post(
    "/v1/extrator/conversions/upload",
    status_code=202,
    response_model=JobCreateResponse,
    dependencies=[Depends(verify_api_key)],
)
async def convert_upload(
    file: UploadFile = File(...),
    output_format: str = Query(...),
    force: bool = Query(...),
    preserve_layout: bool = Query(...),
    metadata_json: str = Query(...),
) -> JobCreateResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    validate_extension(file.filename, for_extraction=False)
    content = await file.read()
    validate_upload_size(len(content))
    target = upload_path(file.filename)
    target.write_bytes(content)
    request = ConversionPathRequest(
        input_path=str(target),
        output_format=output_format,
        force=force,
        preserve_layout=preserve_layout,
        metadata=json.loads(metadata_json),
    )
    return await convert_path(request)


@app.get("/v1/extrator/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
def get_job(job_id: str):
    try:
        return get_manifest().get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.get("/v1/extrator/jobs/{job_id}/result", dependencies=[Depends(verify_api_key)])
def get_job_result(job_id: str):
    try:
        job = get_manifest().get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail=f"Job is not completed: {job.status.value}")
    return job


@app.get("/v1/extrator/documents/{doc_id}", dependencies=[Depends(verify_api_key)])
def get_document(doc_id: str):
    try:
        return get_manifest().get_document(doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc


@app.get("/v1/extrator/documents/{doc_id}/chunks", dependencies=[Depends(verify_api_key)])
def get_document_chunks(doc_id: str):
    return get_manifest().get_chunks(doc_id)


@app.get("/v1/extrator/documents/{doc_id}/tables", dependencies=[Depends(verify_api_key)])
def get_document_tables(doc_id: str):
    return get_manifest().get_tables(doc_id)


@app.get("/v1/extrator/stats", response_model=StatsResponse, dependencies=[Depends(verify_api_key)])
def stats() -> StatsResponse:
    return StatsResponse(**get_manifest().stats())


@app.post(
    "/v1/extrator/documents/{doc_id}/reprocess",
    status_code=202,
    response_model=JobCreateResponse,
    dependencies=[Depends(verify_api_key)],
)
async def reprocess_document(doc_id: str, request: ReprocessRequest) -> JobCreateResponse:
    try:
        doc = get_manifest().get_document(doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    extraction = ExtractionPathRequest(
        input_path=doc.source_path,
        recursive=False,
        force=request.force,
        targets=request.targets,
        metadata=request.metadata,
    )
    return await extract_path(extraction)


@app.post("/v1/extrator/maintenance/cleanup", dependencies=[Depends(verify_api_key)])
def cleanup(request: CleanupRequest):
    return {
        "status": "accepted" if request.dry_run else "not_implemented",
        "dry_run": request.dry_run,
        "older_than_hours": request.older_than_hours,
        "include_failed": request.include_failed,
    }


def _job_payload(job) -> dict:
    if hasattr(job, "model_dump"):
        return job.model_dump(mode="json")
    return job.dict()


def _format_query_job_content(job_kind: str, original_path: str, container_path: str, data: dict) -> str:
    status = str(data.get("status", "unknown"))
    job_id = str(data.get("job_id", ""))
    summary = data.get("summary") or {}
    outputs = data.get("outputs") or {}
    error = data.get("error")

    lines = [
        f"Extrator {job_kind} job {status}",
        f"job_id: {job_id}",
        f"input_path: {original_path}",
        f"container_path: {container_path}",
    ]
    if summary:
        lines.append(f"summary: {summary}")
    if outputs:
        lines.append(f"outputs: {outputs}")
    if error:
        lines.append(f"error: {error}")
    return "\n".join(lines)


def _format_reused_query_content(doc: DocumentInfo, original_path: str, container_path: str) -> str:
    lines = [
        "Extrator extraction result reused",
        f"doc_id: {doc.doc_id}",
        f"input_path: {original_path}",
        f"container_path: {container_path}",
        f"source_hash: {doc.file_hash}",
    ]
    evidence = doc.metadata.get("document_evidence")
    if evidence:
        lines.append(f"document_evidence: {evidence}")
    if doc.output_paths:
        lines.append(f"outputs: {doc.output_paths}")
    return "\n".join(lines)


def _job_semantic_digest(
    summary: dict,
    *,
    outputs: dict[str, str],
    source_paths: list[str],
    status: str,
) -> dict:
    docs: list[DocumentInfo] = []
    doc_ids: list[str] = []
    results = summary.get("results") if isinstance(summary, dict) else None
    if isinstance(results, list):
        for item in results[:8]:
            if not isinstance(item, dict):
                continue
            doc_id = str(item.get("doc_id") or "").strip()
            if doc_id:
                doc_ids.append(doc_id)
    if isinstance(outputs, dict):
        for key in outputs:
            doc_id = str(key or "").strip()
            if doc_id.startswith("doc:"):
                doc_ids.append(doc_id)
    manifest = get_manifest()
    seen_doc_ids: set[str] = set()
    for doc_id in doc_ids:
        if doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)
        try:
            docs.append(manifest.get_document(doc_id))
        except KeyError:
            continue
    excerpts: list[str] = []
    for doc in docs:
        excerpts.extend(_document_chunk_excerpts(doc.doc_id))
    missing: list[str] = []
    if summary and not excerpts:
        missing.append("extrator document chunk excerpts unavailable for job metadata/refs")
    return {
        "contract_version": "semantic_evidence_digest.v1",
        "provider": "extrator",
        "capability": "document_extraction",
        "digest_kind": "document",
        "source_paths": source_paths,
        "status": status,
        "semantic_content_available": bool(excerpts),
        "excerpts": excerpts[:6],
        "summary": {
            "documents_processed": summary.get("documents_processed", 0) if isinstance(summary, dict) else 0,
            "documents_unchanged": summary.get("documents_unchanged", 0) if isinstance(summary, dict) else 0,
            "documents_reused": summary.get("documents_reused", 0) if isinstance(summary, dict) else 0,
        },
        "output_refs": outputs,
        "missing_semantic_evidence": missing,
    }


def _document_semantic_digest(doc: DocumentInfo, *, source_paths: list[str], status: str) -> dict:
    excerpts = _document_chunk_excerpts(doc.doc_id)
    evidence = doc.metadata.get("document_evidence") if isinstance(doc.metadata, dict) else None
    rag_bundle = doc.metadata.get("rag_bundle") if isinstance(doc.metadata, dict) else None
    missing: list[str] = []
    if not excerpts:
        missing.append("extrator has refs/metadata but no compact chunk excerpts available")
    summary: dict[str, object] = {"documents_reused": 1}
    if isinstance(rag_bundle, dict):
        for key in ("chunk_count", "table_count"):
            if key in rag_bundle:
                summary[key] = rag_bundle[key]
    if isinstance(evidence, dict):
        for key in ("parser_id", "parser_confidence", "source_type"):
            if key in evidence:
                summary[key] = evidence[key]
    return {
        "contract_version": "semantic_evidence_digest.v1",
        "provider": "extrator",
        "capability": "document_extraction",
        "digest_kind": "document",
        "source_paths": source_paths,
        "status": status,
        "semantic_content_available": bool(excerpts),
        "excerpts": excerpts[:6],
        "summary": summary,
        "output_refs": doc.output_paths,
        "missing_semantic_evidence": missing,
    }


def _document_chunk_excerpts(doc_id: str) -> list[str]:
    try:
        chunks = get_manifest().get_chunks(doc_id)
    except Exception:
        return []
    excerpts: list[str] = []
    for chunk in chunks[:6]:
        text = " ".join(str(chunk.text or "").split())
        if len(text) < 40:
            continue
        excerpts.append(text[:700])
    return excerpts
