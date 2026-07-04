"""Lightweight source-code parser."""

from __future__ import annotations

import ast
from pathlib import Path

from extrator import __version__
from extrator.config import get_config
from extrator.formats import file_metadata
from extrator.hashing import sha256_file, stable_id
from extrator.normalization import clean_text
from extrator.types import NormalizedDocument


def _python_symbols(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    symbols: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(f"{node.__class__.__name__}: {node.name}")
    return symbols


def parse(path: Path) -> NormalizedDocument:
    cfg = get_config()
    text = path.read_text(encoding="utf-8", errors="ignore")
    metadata = file_metadata(path)
    symbols = _python_symbols(text) if path.suffix == ".py" else []
    metadata["symbol_count"] = len(symbols)
    file_hash = sha256_file(path, block_size=cfg.hashing.block_size_bytes)
    doc_id = stable_id("doc", str(path.resolve()), file_hash, cfg.config_hash)
    header = f"# {path.name}"
    symbol_text = "\n".join(f"- {symbol}" for symbol in symbols)
    markdown = clean_text(f"{header}\n\n## Symbols\n\n{symbol_text}\n\n## Source\n\n```text\n{text}\n```")
    return NormalizedDocument(
        doc_id=doc_id,
        source_path=str(path),
        source_type="code",
        mime_type=str(metadata.get("mime_type") or ""),
        file_hash=file_hash,
        title=path.name,
        markdown=markdown,
        metadata=dict(metadata),
        tables=[],
        parser="code",
        parser_version=__version__,
    )
