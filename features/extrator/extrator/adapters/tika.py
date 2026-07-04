"""Optional Apache Tika parser adapter."""

from __future__ import annotations

from pathlib import Path

from extrator import __version__
from extrator.config import get_config
from extrator.errors import AdapterUnavailable
from extrator.formats import file_metadata, source_type_for
from extrator.hashing import sha256_file, stable_id
from extrator.normalization import clean_text, title_from_markdown
from extrator.types import NormalizedDocument


def _metadata_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def parse(path: Path) -> NormalizedDocument:
    try:
        from tika import parser
    except Exception as exc:
        raise AdapterUnavailable("Tika is not installed in this runtime") from exc

    parsed = parser.from_file(str(path))
    markdown = clean_text(str(parsed.get("content") or ""))
    if not markdown:
        raise AdapterUnavailable(f"Tika returned no text for: {path}")

    cfg = get_config()
    file_hash = sha256_file(path, block_size=cfg.hashing.block_size_bytes)
    metadata = file_metadata(path)
    tika_metadata = parsed.get("metadata") or {}
    if isinstance(tika_metadata, dict):
        metadata.update({f"tika:{key}": _metadata_value(value) for key, value in tika_metadata.items()})
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
        parser="tika",
        parser_version=__version__,
    )
