"""Markdown and plain text parsing/conversion."""

from __future__ import annotations

import html
from pathlib import Path

from extrator import __version__
from extrator.config import get_config
from extrator.formats import file_metadata, source_type_for
from extrator.hashing import sha256_file, stable_id
from extrator.normalization import clean_text, title_from_markdown
from extrator.types import NormalizedDocument


def parse(path: Path) -> NormalizedDocument:
    cfg = get_config()
    raw = path.read_text(encoding="utf-8", errors="ignore")
    markdown = clean_text(raw)
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
        parser="markdown",
        parser_version=__version__,
    )


def markdown_to_html(markdown: str) -> str:
    lines: list[str] = []
    in_list = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            if in_list:
                lines.append("</ul>")
                in_list = False
            continue
        if line.startswith("#"):
            if in_list:
                lines.append("</ul>")
                in_list = False
            marker, _, title = line.partition(" ")
            level = min(len(marker), 6)
            lines.append(f"<h{level}>{html.escape(title.strip())}</h{level}>")
        elif line.startswith(("- ", "* ")):
            if not in_list:
                lines.append("<ul>")
                in_list = True
            lines.append(f"<li>{html.escape(line[2:].strip())}</li>")
        else:
            if in_list:
                lines.append("</ul>")
                in_list = False
            lines.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        lines.append("</ul>")
    return "\n".join(lines)
