"""Format detection and conversion support registry."""

from __future__ import annotations

from pathlib import Path

from extrator.config import get_config
from extrator.security import detect_mime, extension_for


_ALIASES = {
    "md": "markdown",
    "markdown": "markdown",
    "htm": "html",
    "html": "html",
    "txt": "text",
    "json": "json",
    "jsonl": "jsonl",
    "ndjson": "jsonl",
    "jsonl.gz": "jsonl",
    "ndjson.gz": "jsonl",
    "csv": "csv",
    "csv.gz": "csv",
    "tsv": "tsv",
    "tsv.gz": "tsv",
    "xlsx": "xlsx",
    "xls": "xls",
    "py": "code",
    "js": "code",
    "ts": "code",
    "tsx": "code",
    "jsx": "code",
    "java": "code",
    "go": "code",
    "rs": "code",
    "c": "code",
    "cpp": "code",
    "h": "code",
    "hpp": "code",
    "cs": "code",
    "sh": "code",
    "yaml": "code",
    "yml": "code",
    "toml": "code",
    "ini": "code",
    "xml": "code",
    "png": "image",
    "jpg": "image",
    "jpeg": "image",
    "webp": "image",
    "tif": "image",
    "tiff": "image",
}


def source_type_for(path: str | Path) -> str:
    ext = extension_for(path)
    return _ALIASES.get(ext, ext)


def file_metadata(path: str | Path) -> dict[str, str | int]:
    p = Path(path)
    return {
        "extension": extension_for(p),
        "source_type": source_type_for(p),
        "mime_type": detect_mime(p),
        "size_bytes": p.stat().st_size,
    }


def supported_conversion_pairs() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for raw in get_config().formats.conversion_pairs:
        source, sep, target = raw.partition(":")
        if sep:
            pairs.add((source.strip().lower(), target.strip().lower()))
    return pairs


def ensure_conversion_supported(source_format: str, output_format: str) -> None:
    pair = (source_format.lower(), output_format.lower())
    if pair not in supported_conversion_pairs():
        raise ValueError(f"Unsupported conversion pair: {source_format}:{output_format}")
