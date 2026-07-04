"""Cheap GraphRAG candidate generation without LLM calls."""

from __future__ import annotations

import re

from extrator.types import ChunkPayload, GraphCandidate

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)|\[\[([^\]]+)\]\]")
_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE)
_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]{2,}\b")


def build_graph_candidates(chunks: list[ChunkPayload]) -> list[GraphCandidate]:
    result: list[GraphCandidate] = []
    for chunk in chunks:
        entities = set()
        relations: list[dict] = []
        for match in _MD_LINK_RE.finditer(chunk.text):
            label = match.group(1) or match.group(3) or ""
            target = match.group(2) or match.group(3) or ""
            if label:
                entities.add(label)
            if target:
                entities.add(target)
                relations.append({"type": "link", "source": label or chunk.title, "target": target})
        for match in _IMPORT_RE.finditer(chunk.text):
            target = match.group(1) or match.group(2)
            if target:
                entities.add(target)
                relations.append({"type": "import", "source": chunk.source_path, "target": target})
        entities.update(_ENTITY_RE.findall(chunk.section))
        entities.update(chunk.heading_path)
        if not entities and not relations:
            continue
        result.append(
            GraphCandidate(
                doc_id=chunk.doc_id,
                chunk_id=chunk.chunk_id,
                candidate_entities=sorted(entities),
                candidate_relations=relations,
                source_path=chunk.source_path,
                evidence_text=chunk.text,
                confidence_hint=0.5,
                extraction_method="rules",
            )
        )
    return result
