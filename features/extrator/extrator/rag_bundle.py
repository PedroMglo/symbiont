"""RAG bundle manifest builder for extrator outputs."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from extrator.hashing import sha256_file
from extrator.types import (
    ChunkPayload,
    GraphCandidate,
    NormalizedDocument,
    RagBundleArtifact,
    RagBundleChainOfCustody,
    RagBundleManifest,
    RagBundleReprocessPlan,
)

_ARTIFACT_ROLES = {
    "chunks_jsonl": "rag_chunks",
    "chunks_parquet": "rag_chunks",
    "tables_parquet": "table_catalog",
    "table_catalog_json": "table_catalog",
    "graph_candidates_jsonl": "graph_candidates",
    "graph_candidates_parquet": "graph_candidates",
    "document_evidence_json": "document_evidence",
    "manifest_json": "document_manifest",
    "document_md": "normalized_markdown",
    "document_json": "normalized_document",
    "metadata_json": "metadata",
    "bronze_original": "source_reference",
}

_REPROCESS_ARTIFACTS = {"chunks_jsonl", "chunks_parquet", "document_evidence_json"}


def build_rag_bundle_manifest(
    doc: NormalizedDocument,
    chunks: list[ChunkPayload],
    graph_candidates: list[GraphCandidate],
    *,
    output_refs: dict[str, str],
    local_paths: dict[str, str],
) -> RagBundleManifest:
    """Build a manifest RAG can consume without importing extrator internals."""

    artifacts = [
        _artifact(key, ref, local_paths.get(key))
        for key, ref in sorted(output_refs.items())
        if key in _ARTIFACT_ROLES
    ]
    artifact_keys = {artifact.key for artifact in artifacts}
    required = [key for key in ("chunks_jsonl", "chunks_parquet") if key in artifact_keys]
    if "document_evidence_json" in artifact_keys:
        required.append("document_evidence_json")
    can_reprocess = bool(required and "document_evidence_json" in artifact_keys)
    reason = (
        "normalized chunks and document evidence carry source hash and provenance"
        if can_reprocess
        else "missing normalized chunks or document evidence"
    )
    return RagBundleManifest(
        doc_id=doc.doc_id,
        source_path=doc.source_path,
        source_type=doc.source_type,
        source_hash=doc.file_hash,
        parser_id=doc.parser,
        parser_version=doc.parser_version,
        chunk_count=len(chunks),
        table_count=len(doc.tables),
        graph_candidate_count=len(graph_candidates),
        artifacts=artifacts,
        chain_of_custody=RagBundleChainOfCustody(
            source_hash=doc.file_hash,
            evidence_ref=output_refs.get("document_evidence_json"),
            original_ref=output_refs.get("bronze_original"),
            verified_by=[
                "extrator.source_hash",
                "extrator.document_evidence",
                "storage_guardian.artifact_ref" if _has_storage_refs(output_refs) else "extrator.local_scratch_ref",
            ],
        ),
        reprocess=RagBundleReprocessPlan(
            can_reprocess_without_original=can_reprocess,
            reason=reason,
            required_artifacts=required,
            forbidden_actions=[
                "do_not_embed_inside_extrator",
                "do_not_read_original_when_can_reprocess_without_original_is_true",
                "do_not_write_durable_objects_outside_storage_guardian",
            ],
        ),
        metadata={
            "parser_selection": doc.metadata.get("parser_selection"),
            "parser_quality": doc.metadata.get("parser_quality"),
        },
    )


def rag_bundle_summary(manifest: RagBundleManifest, *, manifest_ref: str | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "contract_version": manifest.contract_version,
        "doc_id": manifest.doc_id,
        "consumer": manifest.consumer,
        "embedding_owner": manifest.embedding_owner,
        "embeddings_included": manifest.embeddings_included,
        "can_reprocess_without_original": manifest.reprocess.can_reprocess_without_original,
        "required_artifacts": list(manifest.reprocess.required_artifacts),
    }
    if manifest_ref:
        summary["manifest_ref"] = manifest_ref
    return summary


def _artifact(key: str, ref: str, local_path: str | None) -> RagBundleArtifact:
    media_name = Path(local_path or ref).name
    media_type = mimetypes.guess_type(media_name)[0] or "application/octet-stream"
    return RagBundleArtifact(
        key=key,
        role=_ARTIFACT_ROLES[key],
        ref=ref,
        media_type=media_type,
        sha256=_local_sha256(local_path),
        required_for_reprocess=key in _REPROCESS_ARTIFACTS,
    )


def _local_sha256(local_path: str | None) -> str | None:
    if not local_path:
        return None
    path = Path(local_path)
    if not path.is_file():
        return None
    return sha256_file(path, block_size=1024 * 1024)


def _has_storage_refs(output_refs: dict[str, str]) -> bool:
    return any(str(ref).startswith("storage_guardian://") for ref in output_refs.values())
