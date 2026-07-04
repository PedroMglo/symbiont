"""Pre-filter for graph extraction — scores files before sending to LLM.

Uses cheap heuristics (no LLM calls) to determine which files have
potential for generating useful graph relations. Files below the
configured threshold are indexed as lexical nodes only, skipping
the expensive LLM extraction step.

All thresholds are configurable via [graphify] in rag config.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Regex patterns for cheap signal detection
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_EXTERNAL_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_IMPORT_RE = re.compile(r"^(?:from|import)\s+\S+", re.MULTILINE)
_HEADER_RE = re.compile(r"^#{1,6}\s+.+", re.MULTILINE)
_NAMED_ENTITY_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")
_TECH_NAMES_RE = re.compile(
    r"\b(?:Ollama|Qdrant|Neo4j|Docker|Kubernetes|FastAPI|LangChain|LangGraph|"
    r"PostgreSQL|Redis|MongoDB|Graphify|Obsidian|OpenAI|Anthropic|Gemini|"
    r"PyTorch|TensorFlow|ONNX|vLLM|Triton|CUDA|"
    r"RAG|CAG|GraphRAG|LightRAG|MCP|OTEL|ClickHouse|Grafana|"
    r"Dask|Celery|RabbitMQ|Kafka)\b",
    re.IGNORECASE,
)

# Extension weight for scoring
_EXTENSION_WEIGHTS: dict[str, float] = {
    ".md": 1.0,
    ".txt": 0.7,
    ".rst": 0.7,
    ".adoc": 0.7,
    ".py": 0.5,
    ".ts": 0.5,
    ".js": 0.5,
    ".yaml": 0.3,
    ".yml": 0.3,
    ".toml": 0.3,
    ".json": 0.2,
    ".cfg": 0.2,
    ".ini": 0.2,
}


def score_file_for_graph(
    path: Path,
    content: str,
    *,
    min_chars: int = 200,
) -> float:
    """Score a file's potential for useful graph relation extraction.

    Returns a score between 0.0 and 1.0.
    Scoring is based on cheap signals (no LLM involved):
      - Content length
      - Wikilinks / markdown links
      - Import statements
      - Headers (document structure)
      - Named entities (capitalized phrases)
      - Known tech names
      - File extension weight

    Args:
        path: File path (for extension-based scoring)
        content: Text content of the file
        min_chars: Minimum content length to consider

    Returns:
        Float score 0.0–1.0 indicating graph extraction potential
    """
    if len(content) < min_chars:
        return 0.0

    signals: list[float] = []

    # Extension weight (base signal)
    ext = path.suffix.lower()
    ext_weight = _EXTENSION_WEIGHTS.get(ext, 0.1)
    signals.append(ext_weight * 0.15)

    # Wikilinks — strong signal for connected notes
    wikilinks = len(_WIKILINK_RE.findall(content))
    if wikilinks > 0:
        signals.append(min(wikilinks / 10.0, 0.25))

    # External links
    ext_links = len(_EXTERNAL_LINK_RE.findall(content))
    if ext_links > 0:
        signals.append(min(ext_links / 8.0, 0.15))

    # Import statements (code connectivity)
    imports = len(_IMPORT_RE.findall(content))
    if imports > 0:
        signals.append(min(imports / 15.0, 0.20))

    # Headers (structured content = more meaningful chunks)
    headers = len(_HEADER_RE.findall(content))
    if headers > 0:
        signals.append(min(headers / 8.0, 0.15))

    # Named entities (potential graph nodes)
    entities = len(_NAMED_ENTITY_RE.findall(content))
    if entities > 0:
        signals.append(min(entities / 10.0, 0.20))

    # Known tech names (domain relevance)
    tech_matches = len(_TECH_NAMES_RE.findall(content))
    if tech_matches > 0:
        signals.append(min(tech_matches / 5.0, 0.20))

    # Content length bonus (longer = more potential relations)
    char_count = len(content)
    if char_count > 500:
        signals.append(min(char_count / 5000.0, 0.10))

    # Sum signals, cap at 1.0
    score = min(sum(signals), 1.0)
    return round(score, 3)


def filter_files_for_llm(
    files: list[Path],
    *,
    threshold: float = 0.4,
    min_chars: int = 200,
) -> tuple[list[Path], list[Path]]:
    """Partition files into those that should go through LLM extraction and those that should not.

    Args:
        files: List of file paths to evaluate
        threshold: Minimum score to pass to LLM (from config)
        min_chars: Minimum content length (from config)

    Returns:
        (pass_list, skip_list) — files above/below threshold
    """
    pass_list: list[Path] = []
    skip_list: list[Path] = []

    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skip_list.append(path)
            continue

        score = score_file_for_graph(path, content, min_chars=min_chars)
        if score >= threshold:
            pass_list.append(path)
        else:
            skip_list.append(path)

    if skip_list:
        log.info(
            "[Prefilter] %d/%d ficheiros abaixo do threshold (%.2f) — skipping LLM extraction.",
            len(skip_list), len(files), threshold,
        )

    return pass_list, skip_list
