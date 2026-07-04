"""HTML parsing and conversion helpers."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from extrator import __version__
from extrator.config import get_config
from extrator.formats import file_metadata
from extrator.hashing import sha256_file, stable_id
from extrator.normalization import clean_text, title_from_markdown
from extrator.types import NormalizedDocument


def html_to_markdown(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()

    lines: list[str] = []
    for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]):
        text = element.get_text(" ", strip=True)
        if not text:
            continue
        if element.name and element.name.startswith("h"):
            level = int(element.name[1])
            lines.append(f"{'#' * level} {text}")
        elif element.name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return clean_text("\n\n".join(lines))


def parse(path: Path) -> NormalizedDocument:
    cfg = get_config()
    raw = path.read_text(encoding="utf-8", errors="ignore")
    markdown = html_to_markdown(raw)
    file_hash = sha256_file(path, block_size=cfg.hashing.block_size_bytes)
    metadata = file_metadata(path)
    doc_id = stable_id("doc", str(path.resolve()), file_hash, cfg.config_hash)
    return NormalizedDocument(
        doc_id=doc_id,
        source_path=str(path),
        source_type="html",
        mime_type=str(metadata.get("mime_type") or ""),
        file_hash=file_hash,
        title=title_from_markdown(markdown, path.stem),
        markdown=markdown,
        metadata=dict(metadata),
        tables=[],
        parser="html",
        parser_version=__version__,
    )
