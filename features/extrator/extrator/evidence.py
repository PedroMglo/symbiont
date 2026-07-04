"""Versioned evidence contract builders for extrator outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from extrator.types import (
    ChunkEvidence,
    ChunkPayload,
    DocumentEvidence,
    EvidenceSecurityDecision,
    EvidenceTruncation,
    NormalizedDocument,
    ParserQualityMetrics,
    ParserSelectionEvidence,
    TableEvidence,
)


def accepted_source_decision(path: str | Path) -> EvidenceSecurityDecision:
    """Record that the source passed the extrator input boundary."""

    return EvidenceSecurityDecision(
        scope="source_path",
        decision="allowed",
        reason="validated by extrator input policy",
        reference=str(path),
    )


def build_document_evidence(
    doc: NormalizedDocument,
    chunks: list[ChunkPayload],
    *,
    output_paths: dict[str, str],
    security_decisions: list[EvidenceSecurityDecision],
) -> DocumentEvidence:
    """Build the public DocumentEvidence v1 payload for a normalized document."""

    warnings = _warnings(doc.metadata)
    truncation = _truncation(doc.metadata)
    parser_confidence = _parser_confidence(doc.metadata)
    parser_selection = _parser_selection(doc.metadata)
    quality_metrics = _quality_metrics(doc.metadata)
    return DocumentEvidence(
        doc_id=doc.doc_id,
        source_path=doc.source_path,
        source_type=doc.source_type,
        mime_type=doc.mime_type,
        source_hash=doc.file_hash,
        parser_id=doc.parser,
        parser_version=doc.parser_version,
        parser_confidence=parser_confidence,
        parser_selection=parser_selection,
        quality_metrics=quality_metrics,
        warnings=warnings,
        truncation=truncation,
        security_decisions=security_decisions,
        output_paths=dict(output_paths),
        metadata=dict(doc.metadata),
        chunks=[
            ChunkEvidence(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                source_hash=doc.file_hash,
                content_hash=chunk.content_hash,
                parser_id=chunk.parser,
                parser_version=chunk.parser_version,
                warnings=warnings,
                truncation=truncation,
                security_decisions=security_decisions,
                token_count=chunk.token_count,
                language=chunk.language,
                heading_path=list(chunk.heading_path),
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                embedding_policy=chunk.embedding_policy,
            )
            for chunk in chunks
        ],
        tables=[
            TableEvidence(
                table_id=table.table_id,
                doc_id=table.doc_id,
                source_hash=doc.file_hash,
                parser_id=doc.parser,
                parser_version=doc.parser_version,
                warnings=warnings,
                truncation=truncation,
                security_decisions=security_decisions,
                rows=table.rows,
                columns=table.columns,
                output_path=table.output_path,
                summary=table.summary,
            )
            for table in doc.tables
        ],
    )


def evidence_summary(evidence: DocumentEvidence, *, evidence_path: str | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "contract_version": evidence.contract_version,
        "doc_id": evidence.doc_id,
        "source_hash": evidence.source_hash,
        "parser_id": evidence.parser_id,
        "parser_version": evidence.parser_version,
        "fallback_used": evidence.parser_selection.fallback_used if evidence.parser_selection else False,
        "quality_metrics": evidence.quality_metrics.model_dump(mode="json") if evidence.quality_metrics else None,
        "warnings": list(evidence.warnings),
        "truncated": evidence.truncation.truncated,
        "chunks": len(evidence.chunks),
        "tables": len(evidence.tables),
    }
    if evidence_path:
        summary["path"] = evidence_path
    return summary


def _parser_evidence(metadata: dict[str, Any]) -> dict[str, Any]:
    value = metadata.get("parser_evidence")
    return value if isinstance(value, dict) else {}


def _warnings(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    parser_warnings = _parser_evidence(metadata).get("warnings") or ()
    for warning in parser_warnings:
        if warning:
            values.append(str(warning))
    for warning in metadata.get("warnings") or ():
        if warning:
            values.append(str(warning))
    return list(dict.fromkeys(values))


def _parser_confidence(metadata: dict[str, Any]) -> float | None:
    value = _parser_evidence(metadata).get("confidence")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parser_selection(metadata: dict[str, Any]) -> ParserSelectionEvidence | None:
    value = metadata.get("parser_selection")
    if isinstance(value, dict):
        return ParserSelectionEvidence.model_validate(value)
    return None


def _quality_metrics(metadata: dict[str, Any]) -> ParserQualityMetrics | None:
    value = metadata.get("parser_quality")
    if isinstance(value, dict):
        return ParserQualityMetrics.model_validate(value)
    return None


def _truncation(metadata: dict[str, Any]) -> EvidenceTruncation:
    value = metadata.get("truncation")
    if isinstance(value, dict):
        return EvidenceTruncation.model_validate(value)
    return EvidenceTruncation(
        truncated=bool(metadata.get("truncated", False)),
        reason=str(metadata["truncation_reason"]) if metadata.get("truncation_reason") else None,
    )
