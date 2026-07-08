"""Tree-sitter based chunking for non-Python languages.

Uses tree-sitter to parse source files and extract semantic chunks
(functions, classes, methods, structs, interfaces, etc.) from languages
that the stdlib ``ast`` module cannot handle.

Requires: ``pip install obsidian-rag[treesitter]``

Supported languages:
  JavaScript (.js, .jsx, .mjs)
  TypeScript (.ts, .tsx)
  Java (.java)
  Go (.go)
  Rust (.rs)
  C (.c, .h)
  C++ (.cpp, .cxx, .cc, .hpp, .hxx)
  C# (.cs)
  Ruby (.rb)
"""

from __future__ import annotations

import logging
from pathlib import Path

from chunking.markdown import Chunk

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language registry — extension → (tree-sitter module name, language factory)
# ---------------------------------------------------------------------------

# Maps file extension → (pip package import path, ts Language callable name)
_LANG_REGISTRY: dict[str, tuple[str, str]] = {
    ".js":   ("tree_sitter_javascript", "javascript"),
    ".jsx":  ("tree_sitter_javascript", "javascript"),
    ".mjs":  ("tree_sitter_javascript", "javascript"),
    ".ts":   ("tree_sitter_typescript", "typescript"),
    ".tsx":  ("tree_sitter_typescript", "tsx"),
    ".java": ("tree_sitter_java", "java"),
    ".go":   ("tree_sitter_go", "go"),
    ".rs":   ("tree_sitter_rust", "rust"),
    ".c":    ("tree_sitter_c", "c"),
    ".h":    ("tree_sitter_c", "c"),
    ".cpp":  ("tree_sitter_cpp", "cpp"),
    ".cxx":  ("tree_sitter_cpp", "cpp"),
    ".cc":   ("tree_sitter_cpp", "cpp"),
    ".hpp":  ("tree_sitter_cpp", "cpp"),
    ".hxx":  ("tree_sitter_cpp", "cpp"),
    ".cs":   ("tree_sitter_c_sharp", "c_sharp"),
    ".rb":   ("tree_sitter_ruby", "ruby"),
}

# Node types that represent "top-level definitions" per language family.
# These are the node types we extract as individual chunks.
_DEFINITION_NODE_TYPES: dict[str, set[str]] = {
    "javascript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "export_statement",
        "lexical_declaration",       # const/let at top level
        "variable_declaration",
    },
    "typescript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "export_statement",
        "lexical_declaration",
        "variable_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    },
    "tsx": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "export_statement",
        "lexical_declaration",
        "variable_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    },
    "java": {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "method_declaration",
        "constructor_declaration",
        "record_declaration",
    },
    "go": {
        "function_declaration",
        "method_declaration",
        "type_declaration",
    },
    "rust": {
        "function_item",
        "impl_item",
        "struct_item",
        "enum_item",
        "trait_item",
        "mod_item",
        "type_item",
    },
    "c": {
        "function_definition",
        "struct_specifier",
        "enum_specifier",
        "declaration",
    },
    "cpp": {
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "enum_specifier",
        "namespace_definition",
        "template_declaration",
        "declaration",
    },
    "c_sharp": {
        "class_declaration",
        "struct_declaration",
        "interface_declaration",
        "enum_declaration",
        "method_declaration",
        "constructor_declaration",
        "namespace_declaration",
    },
    "ruby": {
        "method",
        "singleton_method",
        "class",
        "module",
    },
}

# Map node type → symbol_type label for metadata
_SYMBOL_TYPE_MAP: dict[str, str] = {
    "function_declaration": "function",
    "function_definition": "function",
    "function_item": "function",
    "method_definition": "method",
    "method_declaration": "method",
    "method": "method",
    "singleton_method": "method",
    "constructor_declaration": "constructor",
    "class_declaration": "class",
    "class_specifier": "class",
    "class": "class",
    "struct_specifier": "struct",
    "struct_item": "struct",
    "struct_declaration": "struct",
    "interface_declaration": "interface",
    "trait_item": "trait",
    "enum_declaration": "enum",
    "enum_specifier": "enum",
    "enum_item": "enum",
    "impl_item": "impl",
    "mod_item": "module",
    "module": "module",
    "namespace_definition": "namespace",
    "namespace_declaration": "namespace",
    "type_declaration": "type",
    "type_alias_declaration": "type",
    "type_item": "type",
    "record_declaration": "record",
    "template_declaration": "template",
    "export_statement": "export",
    "lexical_declaration": "variable",
    "variable_declaration": "variable",
    "declaration": "declaration",
}


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """Return True if tree-sitter core is installed."""
    try:
        import tree_sitter  # noqa: F401
        return True
    except ImportError:
        return False


def supported_extensions() -> set[str]:
    """Return the set of file extensions this module can handle."""
    return set(_LANG_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_language(lang_key: str):
    """Lazy-load and return a tree_sitter.Language for the given key."""
    import importlib

    import tree_sitter as ts

    module_name, _lang_name = _LANG_REGISTRY.get(f".{lang_key}", (None, None))
    if module_name is None:
        # Try direct lookup by lang_key in registry values
        for _ext, (mod, name) in _LANG_REGISTRY.items():
            if name == lang_key:
                module_name = mod
                _lang_name = name
                break
        if module_name is None:
            raise ValueError(f"Unsupported language: {lang_key}")

    lang_module = importlib.import_module(module_name)

    # Try language-specific callable first (e.g. language_typescript, language_tsx)
    lang_fn = getattr(lang_module, f"language_{_lang_name}", None)
    # Fallback to generic language() callable
    if lang_fn is None:
        lang_fn = getattr(lang_module, "language", None)
    if lang_fn is None:
        raise ImportError(f"{module_name} does not expose a language() or language_{_lang_name}() function")

    return ts.Language(lang_fn())


def _get_parser(lang_key: str):
    """Create a tree-sitter Parser for the given language key."""
    import tree_sitter as ts
    language = _get_language(lang_key)
    parser = ts.Parser(language)
    return parser


def _extract_name(node, source_bytes: bytes) -> str:
    """Extract the 'name' of a definition node (function name, class name, etc.)."""
    # Most definition nodes have a child named 'name'
    name_node = node.child_by_field_name("name")
    if name_node:
        return source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")

    # Rust impl_item: name is in the 'type' field
    if node.type == "impl_item":
        type_node = node.child_by_field_name("type")
        if type_node:
            return source_bytes[type_node.start_byte:type_node.end_byte].decode("utf-8", errors="replace")

    # For export_statement, try to get the declaration inside
    if node.type == "export_statement":
        for child in node.children:
            if child.type in (
                "function_declaration", "class_declaration",
                "lexical_declaration", "variable_declaration",
            ):
                return _extract_name(child, source_bytes)
        # Fallback for export default etc.
        return "export"

    # For lexical/variable declarations, try to get the first declarator name
    if node.type in ("lexical_declaration", "variable_declaration"):
        for child in node.children:
            if child.type == "variable_declarator":
                n = child.child_by_field_name("name")
                if n:
                    return source_bytes[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

    # For Go type_declaration, find type_spec inside
    if node.type == "type_declaration":
        for child in node.children:
            if child.type == "type_spec":
                n = child.child_by_field_name("name")
                if n:
                    return source_bytes[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

    # Fallback: first line trimmed
    text = source_bytes[node.start_byte:min(node.start_byte + 80, node.end_byte)]
    return text.decode("utf-8", errors="replace").split("\n")[0].strip()[:60]


def _node_text(node, source_bytes: bytes) -> str:
    """Return the full text of a tree-sitter node."""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chunk_treesitter(
    source: str,
    rel_path: str,
    repo_name: str,
    lang_key: str,
    cfg,
    source_id: str | None = None,
) -> list[Chunk]:
    """Parse a source file with tree-sitter and produce semantic chunks.

    Args:
        source: file contents as string
        rel_path: relative path within the repo
        repo_name: name of the repo
        lang_key: language key (e.g. "javascript", "typescript", "go")
        cfg: chunking config with max_chars, min_chars, overlap_chars, contextual_prefix
    """
    from chunking.code import _build_chunk, _split_if_long

    try:
        parser = _get_parser(lang_key)
    except (ImportError, ValueError) as e:
        log.warning("tree-sitter not available for %s: %s — falling back to text", lang_key, e)
        from chunking.code import _chunk_text_fallback
        return _chunk_text_fallback(source, rel_path, repo_name, Path(rel_path).name, cfg, source_id=source_id)

    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    note_title = Path(rel_path).name
    definition_types = _DEFINITION_NODE_TYPES.get(lang_key, set())

    chunks: list[Chunk] = []
    chunk_index = 0
    seen_ranges: list[tuple[int, int]] = []  # (start_byte, end_byte) of extracted nodes

    # Walk top-level children for definition nodes
    for child in root.children:
        if child.type not in definition_types:
            continue

        text = _node_text(child, source_bytes)
        name = _extract_name(child, source_bytes)
        symbol_type = _SYMBOL_TYPE_MAP.get(child.type, "code")

        seen_ranges.append((child.start_byte, child.end_byte))

        # Split if the definition is too long
        sub_chunks = _split_if_long(text, cfg.max_chars, cfg.overlap_chars)
        for i, sub in enumerate(sub_chunks):
            if len(sub.strip()) < cfg.min_chars:
                continue
            label = name if len(sub_chunks) == 1 else f"{name} (part {i+1})"
            c = _build_chunk(
                text=sub,
                rel_path=rel_path,
                repo_name=repo_name,
                note_title=note_title,
                section_header=label,
                symbol_type=symbol_type,
                chunk_index=chunk_index,
                contextual_prefix=cfg.contextual_prefix,
                source_id=source_id,
            )
            if c:
                chunks.append(c)
                chunk_index += 1

        # For classes/structs/impls, also extract individual methods
        if child.type in (
            "class_declaration", "class_specifier", "class",
            "impl_item", "struct_specifier",
        ):
            _extract_methods(
                child, source_bytes, rel_path, repo_name, note_title,
                name, cfg, chunks, chunk_index, source_id=source_id,
            )
            chunk_index = len(chunks)

    # Collect leftover top-level code (imports, constants, etc.)
    leftover_parts: list[str] = []
    for child in root.children:
        if child.type in definition_types:
            continue
        # Skip comments at top level (they'll be associated with definitions)
        if child.type in ("comment", "line_comment", "block_comment"):
            continue
        part = _node_text(child, source_bytes).strip()
        if part:
            leftover_parts.append(part)

    if leftover_parts:
        leftover_text = "\n".join(leftover_parts).strip()
        if len(leftover_text) >= cfg.min_chars:
            c = _build_chunk(
                text=leftover_text,
                rel_path=rel_path,
                repo_name=repo_name,
                note_title=note_title,
                section_header=f"{note_title} (module-level)",
                symbol_type="module",
                chunk_index=chunk_index,
                contextual_prefix=cfg.contextual_prefix,
                source_id=source_id,
            )
            if c:
                chunks.append(c)

    return chunks


def _extract_methods(
    class_node,
    source_bytes: bytes,
    rel_path: str,
    repo_name: str,
    note_title: str,
    class_name: str,
    cfg,
    chunks: list[Chunk],
    chunk_index: int,
    source_id: str | None = None,
) -> None:
    """Extract individual methods from a class/impl/struct node."""

    method_types = {
        "method_definition", "method_declaration", "method",
        "singleton_method", "function_item", "function_definition",
        "constructor_declaration",
    }

    for child in class_node.children:
        # Some languages nest methods inside a 'body' or 'declaration_list' node
        target = child
        if child.type in ("class_body", "declaration_list", "block", "body"):
            for sub in child.children:
                if sub.type in method_types:
                    _extract_single_method(
                        sub, source_bytes, rel_path, repo_name, note_title,
                        class_name, cfg, chunks, source_id=source_id,
                    )
            continue

        if target.type in method_types:
            _extract_single_method(
                target, source_bytes, rel_path, repo_name, note_title,
                class_name, cfg, chunks, source_id=source_id,
            )


def _extract_single_method(
    method_node,
    source_bytes: bytes,
    rel_path: str,
    repo_name: str,
    note_title: str,
    class_name: str,
    cfg,
    chunks: list[Chunk],
    source_id: str | None = None,
) -> None:
    """Extract a single method as chunk(s)."""
    from chunking.code import _build_chunk, _split_if_long

    text = _node_text(method_node, source_bytes)
    name = _extract_name(method_node, source_bytes)
    symbol_type = _SYMBOL_TYPE_MAP.get(method_node.type, "method")

    sub_chunks = _split_if_long(text, cfg.max_chars, cfg.overlap_chars)
    chunk_index = len(chunks)
    for i, sub in enumerate(sub_chunks):
        if len(sub.strip()) < cfg.min_chars:
            continue
        label = f"{class_name}.{name}"
        if len(sub_chunks) > 1:
            label += f" (part {i+1})"
        c = _build_chunk(
            text=sub,
            rel_path=rel_path,
            repo_name=repo_name,
            note_title=note_title,
            section_header=label,
            symbol_type=symbol_type,
            chunk_index=chunk_index + i,
            contextual_prefix=cfg.contextual_prefix,
            source_id=source_id,
        )
        if c:
            chunks.append(c)
