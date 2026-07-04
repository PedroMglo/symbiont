"""Graph schema definition — allowed node and relation types.

When schema_locked=True (configurable in [graphify]), the graph post-processor
filters out any nodes/edges whose types are not in the allowed sets.

Types can be extended via JSON files referenced in config:
  [graphify]
  allowed_node_types_file = "path/to/node_types.json"
  allowed_relation_types_file = "path/to/relation_types.json"

JSON format: a simple array of strings, e.g. ["Project", "Service", ...]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Built-in node types (comprehensive for code + documentation graphs)
BUILTIN_NODE_TYPES: frozenset[str] = frozenset({
    # High-level architecture
    "Project",
    "Repository",
    "Service",
    "Agent",
    "Feature",
    "Model",
    "Backend",
    "Container",
    "ConfigFile",
    "Dataset",
    "Document",
    "Chunk",
    # Conceptual
    "Concept",
    "Decision",
    "Problem",
    "Solution",
    "Metric",
    "HardwareResource",
    # Code-level (from graphify AST extraction)
    "function",
    "class",
    "module",
    "method",
    "variable",
    "constant",
    "interface",
    "type",
    "enum",
    "decorator",
    "package",
    "file",
    "namespace",
})

# Built-in relation types
BUILTIN_RELATION_TYPES: frozenset[str] = frozenset({
    # Architectural
    "PART_OF",
    "DEPENDS_ON",
    "USES_MODEL",
    "USES_BACKEND",
    "RUNS_IN",
    "CONFIGURED_BY",
    "PRODUCES",
    "CONSUMES",
    "MENTIONS",
    "SOLVES",
    "CAUSES",
    "CONFLICTS_WITH",
    "REPLACES",
    "DUPLICATES",
    "MEASURED_BY",
    "RELATED_TO",
    # Code-level (from graphify)
    "calls",
    "imports_from",
    "uses",
    "contains",
    "defines",
    "implements",
    "implements_method",
    "inherits",
    "overrides",
    "decorates",
    "instantiates",
    "returns",
    "raises",
    "catches",
    "reads",
    "writes",
    "method",
    "rationale_for",
    # Semantic (from LLM extraction)
    "INFERRED",
    "semantic_similarity",
})


def load_types_from_file(path: str | Path) -> frozenset[str]:
    """Load type names from a JSON array file."""
    p = Path(path)
    if not p.exists():
        log.warning("[Schema] Ficheiro de tipos não encontrado: %s", p)
        return frozenset()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return frozenset(str(t) for t in data)
        log.warning("[Schema] Formato inválido em %s — esperado array JSON.", p)
        return frozenset()
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[Schema] Erro ao ler %s: %s", p, e)
        return frozenset()


def get_allowed_node_types(config_file: str = "") -> frozenset[str]:
    """Return the full set of allowed node types (builtin + config file)."""
    types = set(BUILTIN_NODE_TYPES)
    if config_file:
        extra = load_types_from_file(config_file)
        types.update(extra)
    return frozenset(types)


def get_allowed_relation_types(config_file: str = "") -> frozenset[str]:
    """Return the full set of allowed relation types (builtin + config file)."""
    types = set(BUILTIN_RELATION_TYPES)
    if config_file:
        extra = load_types_from_file(config_file)
        types.update(extra)
    return frozenset(types)


def filter_graph(
    graph_data: dict,
    *,
    allowed_node_types: frozenset[str] | None = None,
    allowed_relation_types: frozenset[str] | None = None,
) -> tuple[dict, dict[str, int]]:
    """Filter graph.json nodes and links by allowed types.

    Returns:
        (filtered_graph_data, stats) where stats contains counts of removed items.
    """
    stats = {"nodes_removed": 0, "links_removed": 0, "nodes_kept": 0, "links_kept": 0}

    if allowed_node_types is None and allowed_relation_types is None:
        stats["nodes_kept"] = len(graph_data.get("nodes", []))
        stats["links_kept"] = len(graph_data.get("links", []))
        return graph_data, stats

    # Filter nodes
    kept_node_ids: set[str] = set()
    filtered_nodes: list[dict] = []
    for node in graph_data.get("nodes", []):
        node_type = node.get("type", "")
        if allowed_node_types and node_type not in allowed_node_types:
            stats["nodes_removed"] += 1
        else:
            filtered_nodes.append(node)
            kept_node_ids.add(node["id"])
            stats["nodes_kept"] += 1

    # Filter links: remove if relation type not allowed OR if source/target node was removed
    filtered_links: list[dict] = []
    for link in graph_data.get("links", []):
        relation = link.get("relation", "")
        source = link.get("source", "")
        target = link.get("target", "")

        if source not in kept_node_ids or target not in kept_node_ids:
            stats["links_removed"] += 1
            continue
        if allowed_relation_types and relation not in allowed_relation_types:
            stats["links_removed"] += 1
            continue

        filtered_links.append(link)
        stats["links_kept"] += 1

    result = dict(graph_data)
    result["nodes"] = filtered_nodes
    result["links"] = filtered_links
    return result, stats
