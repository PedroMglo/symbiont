"""Lightweight PDF text extraction adapter."""

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
        from pypdf import PdfReader
    except Exception as exc:
        raise AdapterUnavailable("pypdf is not installed in this runtime") from exc

    try:
        reader = PdfReader(str(path))
        pages = list(getattr(reader, "pages", []) or [])
        page_texts = []
        for index, page in enumerate(pages, start=1):
            text = ""
            if hasattr(page, "extract_text"):
                text = str(page.extract_text() or "")
            text = clean_text(text)
            if text:
                page_texts.append(f"## Page {index}\n\n{text}")
    except Exception as exc:
        raise AdapterUnavailable(f"pypdf could not read PDF text: {exc}") from exc

    markdown = clean_text("\n\n".join(page_texts))
    if not markdown:
        raise AdapterUnavailable(f"pypdf returned no text for: {path}")

    cfg = get_config()
    file_hash = sha256_file(path, block_size=cfg.hashing.block_size_bytes)
    metadata = file_metadata(path)
    metadata["pdf_pages"] = len(pages)
    metadata["pypdf_encrypted"] = bool(getattr(reader, "is_encrypted", False))
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
        parser="pypdf",
        parser_version=__version__,
    )
