"""Preflight document diagnostics owned by the extrator feature."""

from __future__ import annotations

import re
from pathlib import Path

from extrator.config import get_config
from extrator.formats import detect_mime, source_type_for, supported_conversion_pairs
from extrator.security import PathSecurityError, extension_for, validate_input_path
from extrator.types import (
    DocumentCostEstimate,
    DocumentDiagnostic,
    DocumentDiagnosticRequest,
    DocumentLanguageDiagnostic,
    DocumentOcrDiagnostic,
    DocumentSensitivityDiagnostic,
    DocumentStructureDiagnostic,
    DocumentWorkflowAction,
    DocumentWorkflowRecommendation,
    JobKind,
)

_TEXT_SAMPLE_BYTES = 65536
_TEXT_SOURCE_TYPES = frozenset({"markdown", "text", "html", "json", "jsonl", "code", "csv", "tsv", "xml"})
_TABULAR_SOURCE_TYPES = frozenset({"csv", "tsv", "xlsx", "xls", "jsonl"})
_CODE_SOURCE_TYPES = frozenset({"code"})
_IMAGE_SOURCE_TYPES = frozenset({"image"})
_OCR_MAYBE_SOURCE_TYPES = frozenset({"pdf"})
_SANDBOX_CONVERSION_SOURCES = frozenset({"pdf", "doc", "docx", "ppt", "pptx", "xlsx", "xls"})
_SANDBOX_CONVERSION_OUTPUTS = frozenset({"pdf", "docx", "parquet"})

_HIGH_SENSITIVITY_PATTERNS = (
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"password\s*[:=]",
        r"api[_-]?key\s*[:=]",
        r"secret\s*[:=]",
        r"private[_ -]?key",
        r"bearer\s+[A-Za-z0-9._-]{12,}",
        r"token\s*[:=]\s*[A-Za-z0-9._-]{12,}",
    )
)
_HIGH_SENSITIVITY_PATTERNS = tuple(_HIGH_SENSITIVITY_PATTERNS)
_MEDIUM_SENSITIVITY_PATTERNS = (
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        r"\b(confidential|confidencial|restricted|sensitive|personal|passport|iban|nif)\b",
    )
)
_MEDIUM_SENSITIVITY_PATTERNS = tuple(_MEDIUM_SENSITIVITY_PATTERNS)


def diagnose_document(request: DocumentDiagnosticRequest) -> DocumentDiagnostic:
    """Return a deterministic preflight diagnosis for an extraction/conversion target."""

    cfg = get_config()
    path = Path(request.input_path).expanduser()
    validation_error: str | None = None
    target = path
    for_extraction = request.conversion_format is None
    try:
        target = validate_input_path(path, for_extraction=for_extraction)
    except PathSecurityError as exc:
        validation_error = str(exc)
        target = path.resolve(strict=False)

    exists = target.exists()
    is_file = exists and target.is_file()
    is_dir = exists and target.is_dir()
    ext = "" if is_dir else extension_for(target)
    source_type = "directory" if is_dir else source_type_for(target)
    mime_type = detect_mime(target) if is_file and validation_error is None else ""
    size_bytes = target.stat().st_size if is_file else None
    sample = _sample_text(target, source_type=source_type) if is_file and validation_error is None else ""

    structure = DocumentStructureDiagnostic(
        path_kind=_path_kind(exists=exists, is_file=is_file, is_dir=is_dir),
        extension=ext,
        source_type=source_type,
        mime_type=mime_type,
        size_bytes=size_bytes,
        likely_tabular=source_type in _TABULAR_SOURCE_TYPES,
        likely_code=source_type in _CODE_SOURCE_TYPES,
        likely_multi_document=bool(request.recursive or is_dir),
        requires_parser=source_type not in {"markdown", "text", "json", "jsonl", "code"},
    )
    workflow = _workflow_recommendation(
        request,
        validation_error=validation_error,
        source_type=source_type,
        is_dir=is_dir,
    )
    warnings = [validation_error] if validation_error else []
    return DocumentDiagnostic(
        input_path=str(target),
        status="blocked" if validation_error else "diagnosed",
        sensitivity=_sensitivity(target, sample),
        language=_language(sample),
        structure=structure,
        ocr=_ocr(source_type),
        cost=_cost_estimate(target, is_file=is_file, is_dir=is_dir, size_bytes=size_bytes, recursive=request.recursive),
        workflow=workflow,
        warnings=warnings,
        metadata={
            "force": request.force,
            "recursive": request.recursive,
            "conversion_format": request.conversion_format,
            "ocr_min_confidence": cfg.parsers.min_ocr_confidence,
        },
    )


def diagnostic_summary(diagnostic: DocumentDiagnostic) -> dict:
    return diagnostic.model_dump(mode="json")


def _sample_text(path: Path, *, source_type: str) -> str:
    if source_type not in _TEXT_SOURCE_TYPES:
        return ""
    try:
        return path.read_bytes()[:_TEXT_SAMPLE_BYTES].decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _path_kind(*, exists: bool, is_file: bool, is_dir: bool) -> str:
    if not exists:
        return "missing"
    if is_file:
        return "file"
    if is_dir:
        return "directory"
    return "other"


def _sensitivity(path: Path, sample: str) -> DocumentSensitivityDiagnostic:
    haystack = f"{path.name}\n{sample}"
    signals: list[str] = []
    for pattern in _HIGH_SENSITIVITY_PATTERNS:
        if pattern.search(haystack):
            signals.append(pattern.pattern)
    if signals:
        return DocumentSensitivityDiagnostic(level="high", signals=signals, sample_scanned=bool(sample))
    for pattern in _MEDIUM_SENSITIVITY_PATTERNS:
        if pattern.search(haystack):
            signals.append(pattern.pattern)
    level = "medium" if signals else "low"
    return DocumentSensitivityDiagnostic(level=level, signals=signals, sample_scanned=bool(sample))


def _language(sample: str) -> DocumentLanguageDiagnostic:
    text = f" {sample.lower()} "
    if not text.strip():
        return DocumentLanguageDiagnostic(language="unknown", confidence=None, reason="no_text_sample")
    english = sum(text.count(f" {word} ") for word in ("the", "and", "for", "with", "this", "that"))
    portuguese = sum(text.count(f" {word} ") for word in ("que", "para", "com", "uma", "este", "esta"))
    if english == portuguese:
        return DocumentLanguageDiagnostic(language="unknown", confidence=0.5, reason="ambiguous_stopwords")
    if english > portuguese:
        return DocumentLanguageDiagnostic(language="en", confidence=0.65, reason="english_stopwords")
    return DocumentLanguageDiagnostic(language="pt", confidence=0.65, reason="portuguese_stopwords")


def _ocr(source_type: str) -> DocumentOcrDiagnostic:
    cfg = get_config()
    if source_type in _IMAGE_SOURCE_TYPES:
        return DocumentOcrDiagnostic(needed=True, enabled=cfg.parsers.ocr_enabled, reason="image_source")
    if source_type in _OCR_MAYBE_SOURCE_TYPES:
        return DocumentOcrDiagnostic(needed=None, enabled=cfg.parsers.ocr_enabled, reason="pdf_may_require_ocr")
    return DocumentOcrDiagnostic(needed=False, enabled=cfg.parsers.ocr_enabled, reason="text_or_structured_source")


def _cost_estimate(
    target: Path,
    *,
    is_file: bool,
    is_dir: bool,
    size_bytes: int | None,
    recursive: bool,
) -> DocumentCostEstimate:
    if is_file:
        estimated_tokens = max(1, (size_bytes or 0) // 4) if size_bytes is not None else None
        tier = _cost_tier(size_bytes or 0, 1)
        return DocumentCostEstimate(
            cost_tier=tier,
            estimated_bytes=size_bytes,
            estimated_tokens=estimated_tokens,
            estimated_items=1,
            reason="single_file_size",
        )
    if is_dir:
        estimated_bytes, estimated_items = _directory_estimate(target, recursive=recursive)
        return DocumentCostEstimate(
            cost_tier=_cost_tier(estimated_bytes, estimated_items),
            estimated_bytes=estimated_bytes,
            estimated_tokens=None,
            estimated_items=estimated_items,
            reason="directory_sample",
        )
    return DocumentCostEstimate(cost_tier="unknown", reason="path_not_readable")


def _directory_estimate(path: Path, *, recursive: bool) -> tuple[int, int]:
    total = 0
    count = 0
    iterator = path.rglob("*") if recursive else path.iterdir()
    try:
        for child in iterator:
            if not child.is_file():
                continue
            count += 1
            try:
                total += child.stat().st_size
            except OSError:
                pass
            if count >= 2000:
                break
    except OSError:
        return 0, 0
    return total, count


def _cost_tier(size_bytes: int, item_count: int) -> str:
    if size_bytes >= 50 * 1024 * 1024 or item_count >= 1000:
        return "high"
    if size_bytes >= 5 * 1024 * 1024 or item_count >= 100:
        return "medium"
    return "low"


def _workflow_recommendation(
    request: DocumentDiagnosticRequest,
    *,
    validation_error: str | None,
    source_type: str,
    is_dir: bool,
) -> DocumentWorkflowRecommendation:
    if validation_error:
        return DocumentWorkflowRecommendation(
            action=DocumentWorkflowAction.BLOCKED,
            reason=validation_error,
        )
    if request.conversion_format:
        output_format = request.conversion_format.lower()
        if is_dir:
            requires_sandbox = output_format in _SANDBOX_CONVERSION_OUTPUTS
            return DocumentWorkflowRecommendation(
                action=DocumentWorkflowAction.SANDBOX_REQUIRED if requires_sandbox else DocumentWorkflowAction.CONVERT,
                job_kind=JobKind.CONVERSION,
                requires_workspace_execution=requires_sandbox,
                reason="directory_conversion",
                output_format=output_format,
            )
        pair = (source_type.lower(), output_format)
        if pair not in supported_conversion_pairs():
            return DocumentWorkflowRecommendation(
                action=DocumentWorkflowAction.BLOCKED,
                job_kind=JobKind.CONVERSION,
                reason=f"unsupported_conversion_pair:{source_type}:{output_format}",
                output_format=output_format,
            )
        requires_sandbox = source_type in _SANDBOX_CONVERSION_SOURCES or output_format in _SANDBOX_CONVERSION_OUTPUTS
        return DocumentWorkflowRecommendation(
            action=DocumentWorkflowAction.SANDBOX_REQUIRED if requires_sandbox else DocumentWorkflowAction.CONVERT,
            job_kind=JobKind.CONVERSION,
            requires_workspace_execution=requires_sandbox,
            reason="external_conversion_tool_required" if requires_sandbox else "supported_local_conversion",
            output_format=output_format,
        )
    return DocumentWorkflowRecommendation(
        action=DocumentWorkflowAction.EXTRACT,
        job_kind=JobKind.EXTRACTION,
        reason="directory_extraction" if is_dir or request.recursive else "supported_document_extraction",
        targets=["rag"],
    )
