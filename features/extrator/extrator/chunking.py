"""Deterministic chunking for normalized documents."""

from __future__ import annotations

import re
from typing import Iterable

from extrator.config import get_config
from extrator.hashing import sha256_text, stable_id
from extrator.normalization import clean_text
from extrator.types import ChunkPayload, EmbeddingPolicy, NormalizedDocument

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def estimate_tokens(text: str) -> int:
    return len(text.split())


def _policy_map() -> dict[str, EmbeddingPolicy]:
    result: dict[str, EmbeddingPolicy] = {}
    for item in get_config().policies.embedding_policy_by_source_type:
        source_type, sep, policy = item.partition(":")
        if sep:
            result[source_type.strip().lower()] = EmbeddingPolicy(policy.strip())
    return result


def embedding_policy_for(source_type: str) -> EmbeddingPolicy:
    policy = _policy_map().get(source_type.lower())
    if policy is None:
        return EmbeddingPolicy.SKIP
    return policy


def split_markdown_sections(markdown: str) -> list[tuple[list[str], str, str]]:
    lines = clean_text(markdown).splitlines()
    sections: list[tuple[list[str], str, str]] = []
    heading_stack: list[str] = []
    current_title = ""
    current: list[str] = []

    def flush() -> None:
        body = "\n".join(current).strip()
        if body:
            sections.append((list(heading_stack), current_title, body))

    for line in lines:
        match = _HEADING_RE.match(line.strip())
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            heading_stack[:] = heading_stack[: level - 1]
            heading_stack.append(title)
            current_title = title
            current = [line]
            continue
        current.append(line)
    flush()
    if not sections and markdown.strip():
        sections.append(([], "", markdown.strip()))
    return sections


def _word_windows(text: str) -> Iterable[str]:
    cfg = get_config().chunking
    words = text.split()
    if len(words) <= cfg.max_tokens:
        yield text.strip()
        return

    step = cfg.target_tokens - cfg.overlap_tokens
    start = 0
    while start < len(words):
        end = min(start + cfg.target_tokens, len(words))
        yield " ".join(words[start:end]).strip()
        if end == len(words):
            break
        start += step


def chunk_document(doc: NormalizedDocument) -> list[ChunkPayload]:
    chunks: list[ChunkPayload] = []
    policy = embedding_policy_for(doc.source_type)
    cfg = get_config()
    sections = split_markdown_sections(doc.markdown)

    for section_index, (heading_path, section, body) in enumerate(sections):
        for part_index, chunk_text in enumerate(_word_windows(body)):
            text = clean_text(chunk_text)
            if not text:
                continue
            if len(text) < cfg.chunking.min_chars and len(sections) > 1:
                continue
            content_hash = sha256_text(text)
            chunk_id = stable_id(
                "chunk",
                doc.doc_id,
                section_index,
                part_index,
                content_hash,
                cfg.config_hash,
            )
            chunks.append(
                ChunkPayload(
                    chunk_id=chunk_id,
                    doc_id=doc.doc_id,
                    source_path=doc.source_path,
                    source_type=doc.source_type,
                    title=doc.title,
                    section=section,
                    heading_path=heading_path,
                    page_start=None,
                    page_end=None,
                    text=text,
                    token_count=estimate_tokens(text),
                    language=str(doc.metadata.get("language") or ""),
                    content_hash=content_hash,
                    parser=doc.parser,
                    parser_version=doc.parser_version,
                    embedding_policy=policy,
                    text_ref="",
                )
            )
    return chunks
