"""Parser registry for Extrator adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from extrator.adapters import (
    code,
    docling,
    html,
    image_ocr,
    jsonl,
    markdown,
    markitdown,
    pdf_text,
    tabular,
    tika,
    unstructured,
)
from extrator.config import get_config
from extrator.errors import AdapterUnavailable
from extrator.formats import source_type_for
from extrator.security import extension_for
from extrator.types import NormalizedDocument, ParserAttemptEvidence, ParserSelectionEvidence


ParseFunc = Callable[[Path, Path], NormalizedDocument]


@dataclass(frozen=True)
class ParserEvidence:
    parser: str
    confidence: float
    tables: bool = False
    images: bool = False
    layout: bool = False
    warnings: tuple[str, ...] = ()
    cost: int = 1


@dataclass(frozen=True)
class ParserAdapter:
    name: str
    extensions: frozenset[str]
    source_types: frozenset[str]
    parse_func: ParseFunc
    evidence: ParserEvidence

    def supports(self, path: Path) -> bool:
        suffixes = _extensions_for(path)
        source_type = source_type_for(path)
        return bool(self.extensions.intersection(suffixes)) or source_type in self.source_types

    def parse(self, path: Path, *, table_dir: Path) -> NormalizedDocument:
        return self.parse_func(path, table_dir)


def _simple(parser: Callable[[Path], NormalizedDocument]) -> ParseFunc:
    def _parse(path: Path, _table_dir: Path) -> NormalizedDocument:
        return parser(path)

    return _parse


def _tabular(path: Path, table_dir: Path) -> NormalizedDocument:
    return tabular.parse(path, table_dir=table_dir)


def _jsonl(path: Path, table_dir: Path) -> NormalizedDocument:
    return jsonl.parse(path, table_dir=table_dir)


def _extensions_for(path: Path) -> frozenset[str]:
    name = path.name.lower()
    suffix = path.suffix.lower().lstrip(".")
    compound = ""
    if name.endswith((".jsonl.gz", ".ndjson.gz", ".csv.gz", ".tsv.gz")):
        compound = ".".join(name.rsplit(".", 2)[-2:])
    values = {suffix} if suffix else set()
    if compound:
        values.add(compound)
    return frozenset(values)


_ADAPTERS: tuple[ParserAdapter, ...] = (
    ParserAdapter(
        name="markdown",
        extensions=frozenset({"md", "markdown", "txt", "json"}),
        source_types=frozenset({"markdown", "text", "json"}),
        parse_func=_simple(markdown.parse),
        evidence=ParserEvidence("markdown", confidence=0.95, cost=1),
    ),
    ParserAdapter(
        name="jsonl",
        extensions=frozenset({"jsonl", "ndjson", "jsonl.gz", "ndjson.gz"}),
        source_types=frozenset({"jsonl"}),
        parse_func=_jsonl,
        evidence=ParserEvidence("jsonl", confidence=0.95, tables=True, cost=1),
    ),
    ParserAdapter(
        name="html",
        extensions=frozenset({"html", "htm"}),
        source_types=frozenset({"html"}),
        parse_func=_simple(html.parse),
        evidence=ParserEvidence("html", confidence=0.9, layout=True, cost=1),
    ),
    ParserAdapter(
        name="tabular",
        extensions=frozenset({"csv", "tsv", "xlsx", "xls", "csv.gz", "tsv.gz"}),
        source_types=frozenset({"csv", "tsv", "xlsx", "xls"}),
        parse_func=_tabular,
        evidence=ParserEvidence("tabular", confidence=0.95, tables=True, cost=2),
    ),
    ParserAdapter(
        name="code",
        extensions=frozenset({"py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp", "h", "hpp", "cs", "sh", "yaml", "yml", "toml", "ini", "xml"}),
        source_types=frozenset({"code"}),
        parse_func=_simple(code.parse),
        evidence=ParserEvidence("code", confidence=0.85, cost=1),
    ),
    ParserAdapter(
        name="docling",
        extensions=frozenset({"pdf", "docx", "pptx"}),
        source_types=frozenset({"pdf", "docx", "pptx"}),
        parse_func=_simple(docling.parse),
        evidence=ParserEvidence("docling", confidence=0.9, tables=True, images=True, layout=True, cost=4),
    ),
    ParserAdapter(
        name="markitdown",
        extensions=frozenset({"pdf", "docx", "pptx", "xlsx", "xls", "html", "htm", "csv", "json", "xml"}),
        source_types=frozenset({"pdf", "docx", "pptx", "xlsx", "xls", "html", "csv", "json", "code"}),
        parse_func=_simple(markitdown.parse),
        evidence=ParserEvidence("markitdown", confidence=0.82, tables=True, layout=True, cost=2),
    ),
    ParserAdapter(
        name="pypdf",
        extensions=frozenset({"pdf"}),
        source_types=frozenset({"pdf"}),
        parse_func=_simple(pdf_text.parse),
        evidence=ParserEvidence(
            "pypdf",
            confidence=0.78,
            layout=False,
            warnings=("text-only PDF fallback",),
            cost=2,
        ),
    ),
    ParserAdapter(
        name="tika",
        extensions=frozenset({"pdf", "docx", "pptx", "doc", "html", "htm", "xml"}),
        source_types=frozenset({"pdf", "docx", "pptx", "doc", "html"}),
        parse_func=_simple(tika.parse),
        evidence=ParserEvidence(
            "tika",
            confidence=0.75,
            layout=False,
            warnings=("metadata/text fallback",),
            cost=3,
        ),
    ),
    ParserAdapter(
        name="unstructured",
        extensions=frozenset({"pdf", "docx", "pptx"}),
        source_types=frozenset({"pdf", "docx", "pptx"}),
        parse_func=_simple(unstructured.parse),
        evidence=ParserEvidence("unstructured", confidence=0.72, layout=True, cost=4),
    ),
    ParserAdapter(
        name="image_ocr",
        extensions=frozenset({"png", "jpg", "jpeg", "webp", "tif", "tiff"}),
        source_types=frozenset({"image"}),
        parse_func=_simple(image_ocr.parse),
        evidence=ParserEvidence(
            "image_ocr",
            confidence=0.5,
            images=True,
            warnings=("requires OCR review",),
            cost=5,
        ),
    ),
)


def _priority_index(name: str, priorities: list[str]) -> tuple[int, str]:
    try:
        return (priorities.index(name), name)
    except ValueError:
        return (len(priorities), name)


def parser_candidates(path: Path) -> list[ParserAdapter]:
    """Return eligible adapters sorted by configured priority and evidence."""
    priorities = [item.strip().lower() for item in get_config().parsers.parser_priorities]
    candidates = [adapter for adapter in _ADAPTERS if adapter.supports(path)]
    candidates.sort(
        key=lambda adapter: (
            _priority_index(adapter.name, priorities),
            adapter.evidence.cost,
            -adapter.evidence.confidence,
        )
    )
    return candidates


def _attempt(adapter: ParserAdapter, *, status: str, reason: str | None = None) -> ParserAttemptEvidence:
    return ParserAttemptEvidence(
        parser=adapter.name,
        status=status,
        reason=reason,
        confidence=adapter.evidence.confidence,
        cost=adapter.evidence.cost,
        tables=adapter.evidence.tables,
        images=adapter.evidence.images,
        layout=adapter.evidence.layout,
        warnings=list(adapter.evidence.warnings),
    )


def _selection_metadata(
    path: Path,
    candidates: list[ParserAdapter],
    attempts: list[ParserAttemptEvidence],
    *,
    selected: ParserAdapter,
) -> ParserSelectionEvidence:
    return ParserSelectionEvidence(
        source_type=source_type_for(path),
        extension=extension_for(path),
        candidate_order=[adapter.name for adapter in candidates],
        selected_parser=selected.name,
        selected_index=[adapter.name for adapter in candidates].index(selected.name),
        fallback_used=any(attempt.status != "selected" for attempt in attempts),
        attempts=attempts,
    )


def parse_file(path: Path, *, table_dir: Path) -> NormalizedDocument:
    """Parse a file through the first available configured adapter."""
    candidates = parser_candidates(path)
    if not candidates:
        raise AdapterUnavailable(f"No parser available for .{path.suffix.lower().lstrip('.')}")

    unavailable: list[str] = []
    attempts: list[ParserAttemptEvidence] = []
    for adapter in candidates:
        try:
            document = adapter.parse(path, table_dir=table_dir)
            attempts.append(_attempt(adapter, status="selected"))
            metadata = dict(document.metadata)
            metadata.setdefault("parser_evidence", adapter.evidence.__dict__)
            metadata["parser_selection"] = _selection_metadata(
                path,
                candidates,
                attempts,
                selected=adapter,
            ).model_dump(mode="json")
            document.metadata = metadata
            return document
        except AdapterUnavailable as exc:
            attempts.append(_attempt(adapter, status="unavailable", reason=str(exc)))
            unavailable.append(f"{adapter.name}: {exc}")
            continue
        except Exception as exc:
            attempts.append(_attempt(adapter, status="failed", reason=str(exc)))
            unavailable.append(f"{adapter.name}: {exc}")
            continue

    detail = "; ".join(unavailable) if unavailable else "no eligible adapter succeeded"
    raise AdapterUnavailable(f"No configured parser available for .{path.suffix.lower().lstrip('.')}: {detail}")
