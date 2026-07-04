"""Parser quality metrics for extracted documents."""

from __future__ import annotations

from pathlib import Path

from extrator.hashing import sha256_text
from extrator.types import ChunkPayload, NormalizedDocument, ParserQualityMetrics


def evaluate_parser_quality(
    source_path: str | Path,
    doc: NormalizedDocument,
    chunks: list[ChunkPayload],
) -> ParserQualityMetrics:
    """Return deterministic quality signals for parser/golden-corpus checks."""

    extraction_loss, extraction_loss_reason = _extraction_loss(source_path, doc.markdown)
    table_fidelity, table_fidelity_reason = _table_fidelity(doc)
    chunk_stability, chunk_stability_hash = _chunk_stability(chunks)
    return ParserQualityMetrics(
        extraction_loss=extraction_loss,
        extraction_loss_reason=extraction_loss_reason,
        table_fidelity=table_fidelity,
        table_fidelity_reason=table_fidelity_reason,
        chunk_stability=chunk_stability,
        chunk_stability_hash=chunk_stability_hash,
        chunk_count=len(chunks),
    )


def _extraction_loss(source_path: str | Path, markdown: str) -> tuple[float | None, str | None]:
    try:
        source_size = Path(source_path).stat().st_size
    except OSError:
        return None, "source_size_unavailable"
    if source_size <= 0:
        return 1.0, "empty_source"
    extracted_size = len((markdown or "").encode("utf-8"))
    if extracted_size <= 0:
        return 1.0, "no_text_extracted"
    ratio = min(extracted_size / source_size, 1.0)
    return round(1.0 - ratio, 6), "byte_text_yield_heuristic"


def _table_fidelity(doc: NormalizedDocument) -> tuple[float | None, str | None]:
    parser_evidence = doc.metadata.get("parser_evidence")
    expects_tables = bool(parser_evidence.get("tables")) if isinstance(parser_evidence, dict) else False
    if not doc.tables:
        if expects_tables:
            return 0.0, "parser_claimed_tables_but_none_emitted"
        return None, "not_table_oriented"
    populated = sum(1 for table in doc.tables if table.rows > 0 and table.columns > 0)
    return round(populated / len(doc.tables), 6), "populated_table_ratio"


def _chunk_stability(chunks: list[ChunkPayload]) -> tuple[float, str | None]:
    if not chunks:
        return 0.0, None
    payload = "\n".join(chunk.content_hash for chunk in chunks)
    return 1.0, sha256_text(payload)
