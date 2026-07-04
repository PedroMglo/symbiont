"""Optional Docling parser adapter."""

from __future__ import annotations

from pathlib import Path

from extrator import __version__
from extrator.config import get_config
from extrator.errors import AdapterUnavailable
from extrator.formats import file_metadata, source_type_for
from extrator.hashing import sha256_file, stable_id
from extrator.normalization import clean_text, title_from_markdown
from extrator.types import NormalizedDocument


def parse(path: Path) -> NormalizedDocument:
    try:
        from docling.document_converter import DocumentConverter
    except Exception as exc:
        raise AdapterUnavailable("Docling is not installed in this runtime") from exc

    cfg = get_config()
    result = DocumentConverter().convert(path)
    document = result.document
    if hasattr(document, "export_to_markdown"):
        markdown = document.export_to_markdown()
    elif hasattr(document, "export_to_text"):
        markdown = document.export_to_text()
    else:
        markdown = str(document)

    markdown = clean_text(markdown)
    file_hash = sha256_file(path, block_size=cfg.hashing.block_size_bytes)
    metadata = file_metadata(path)
    source_type = source_type_for(path)
    doc_id = stable_id("doc", str(path.resolve()), file_hash, cfg.config_hash)
    return NormalizedDocument(
        doc_id=doc_id,
        source_path=str(path),
        source_type=source_type,
        mime_type=str(metadata.get("mime_type") or ""),
        file_hash=file_hash,
        title=title_from_markdown(markdown, path.stem),
        markdown=markdown,
        metadata=dict(metadata),
        tables=[],
        parser="docling",
        parser_version=__version__,
    )
