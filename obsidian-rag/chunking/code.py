"""Chunking de código Python para RAG — usa ast.parse() (stdlib, sem dependências).

Estratégia:
  - Um chunk por função/método (decorators + docstring + corpo)
  - Um chunk por classe (docstring + assinaturas dos métodos)
  - Um chunk por módulo (imports + constants + module docstring)

Ficheiros não-Python no repo (.md, .yaml, .toml, .sh) são enviados para o
chunker Markdown existente com source_type="repo_doc".

Metadata compatível com o Chunk dataclass existente — todos os campos standard
estão presentes para que o retrieval e a API funcionem sem alterações.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
from pathlib import Path
from typing import Iterator

from chunking.markdown import Chunk, chunk_note
from metadata import stable_source_id

# Extensões tratadas como "repo doc" (via chunker Markdown)
_REPO_DOC_EXTENSIONS = {".md", ".mdx", ".txt", ".rst", ".yaml", ".yml", ".toml", ".sh", ".zsh", ".env"}
# Extensões de código Python
_PYTHON_EXTENSION = ".py"

# Extensões que tree-sitter pode tratar (se instalado)
_TREESITTER_EXTENSIONS: dict[str, str] = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cxx": "cpp", ".cc": "cpp", ".hpp": "cpp", ".hxx": "cpp",
    ".cs": "c_sharp",
    ".rb": "ruby",
}


def _compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _get_decorator_start(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    """Linha de início real (incluindo decorators)."""
    if node.decorator_list:
        return node.decorator_list[0].lineno - 1
    return node.lineno - 1


def _build_chunk(
    text: str,
    rel_path: str,
    repo_name: str,
    note_title: str,
    section_header: str,
    symbol_type: str,
    chunk_index: int,
    contextual_prefix: bool,
    source_id: str | None = None,
) -> Chunk | None:
    display = text.strip()
    if not display:
        return None

    if contextual_prefix:
        prefix = f"Repo: {repo_name} | Ficheiro: {rel_path} | {symbol_type.capitalize()}: {section_header}"
        embedding_text = f"{prefix}\n{display}"
    else:
        embedding_text = display

    source_id = source_id or repo_name
    chunk_id = _compute_hash(f"v2:{source_id}:{rel_path}:{section_header}:{chunk_index}:{display}")
    metadata = {
        "source_id": source_id,
        "source_path": rel_path,
        "source_type": "code",
        "repo_name": repo_name,
        "note_title": note_title,          # compat com retrieval existente
        "section_header": section_header,
        "symbol_type": symbol_type,
        "chunk_index": chunk_index,
        "display_text": display,
    }
    return Chunk(id=chunk_id, text=embedding_text, metadata=metadata)


def _chunk_python_source(
    source: str,
    rel_path: str,
    repo_name: str,
    cfg,
    source_id: str | None = None,
) -> list[Chunk]:
    """Parse um ficheiro Python e produz chunks semânticos por função/classe/módulo."""
    note_title = Path(rel_path).name
    chunks: list[Chunk] = []
    chunk_index = 0

    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        # Fallback: tratar como texto plano se o parse falhar
        return _chunk_text_fallback(source, rel_path, repo_name, note_title, cfg, source_id=source_id)

    lines = source.splitlines()

    # Recolher top-level nodes que interessam (funções, classes, e resto para módulo-level)
    top_level_nodes = list(ast.iter_child_nodes(tree))

    # 1. Chunk de módulo — docstring + imports + constants (tudo excepto funções/classes)
    module_lines: list[str] = []
    for node in top_level_nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = getattr(node, "lineno", 1) - 1
        end = getattr(node, "end_lineno", getattr(node, "lineno", 1))
        module_lines.extend(lines[start:end])

    module_text = "\n".join(module_lines).strip()
    if module_text and len(module_text) >= cfg.min_chars:
        c = _build_chunk(
            text=module_text,
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
            chunk_index += 1

    # 2. Chunks por função e classe top-level
    for node in top_level_nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_start = _get_decorator_start(source, node)
            func_end = getattr(node, "end_lineno", node.lineno)
            text = "\n".join(lines[func_start:func_end])
            header = node.name

            # Split se muito grande
            sub_chunks = _split_if_long(text, cfg.max_chars, cfg.overlap_chars)
            for i, sub in enumerate(sub_chunks):
                if len(sub.strip()) < cfg.min_chars:
                    continue
                label = header if len(sub_chunks) == 1 else f"{header} (parte {i+1})"
                c = _build_chunk(
                    text=sub,
                    rel_path=rel_path,
                    repo_name=repo_name,
                    note_title=note_title,
                    section_header=label,
                    symbol_type="function",
                    chunk_index=chunk_index,
                    contextual_prefix=cfg.contextual_prefix,
                    source_id=source_id,
                )
                if c:
                    chunks.append(c)
                    chunk_index += 1

        elif isinstance(node, ast.ClassDef):
            class_start = _get_decorator_start(source, node)
            class_end = getattr(node, "end_lineno", node.lineno)
            class_text_lines = lines[class_start:class_end]

            # Chunk da classe: cabeçalho + docstring + assinaturas de métodos
            class_summary_parts = []
            # Linhas da class def até ao fim da docstring
            in_body = False
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                    # docstring da classe
                    start = child.lineno - 1 - class_start
                    end = getattr(child, "end_lineno", child.lineno) - class_start
                    class_summary_parts.extend(class_text_lines[:end + 1])
                    in_body = True
                    break
            if not in_body:
                class_summary_parts.extend(class_text_lines[:3])  # só cabeçalho

            # Adicionar assinaturas dos métodos
            for method in ast.iter_child_nodes(node):
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sig_start = _get_decorator_start(source, method)
                    # Só a linha de def (sem corpo)
                    sig_line = lines[sig_start : method.lineno]
                    class_summary_parts.extend(sig_line)
                    class_summary_parts.append("    ...")

            class_summary = "\n".join(class_summary_parts).strip()
            if len(class_summary) >= cfg.min_chars:
                c = _build_chunk(
                    text=class_summary,
                    rel_path=rel_path,
                    repo_name=repo_name,
                    note_title=note_title,
                    section_header=f"class {node.name}",
                    symbol_type="class",
                    chunk_index=chunk_index,
                    contextual_prefix=cfg.contextual_prefix,
                    source_id=source_id,
                )
                if c:
                    chunks.append(c)
                    chunk_index += 1

            # Chunks individuais por método
            for method in ast.iter_child_nodes(node):
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_start = _get_decorator_start(source, method)
                    m_end = getattr(method, "end_lineno", method.lineno)
                    method_text = "\n".join(lines[m_start:m_end])
                    sub_chunks = _split_if_long(method_text, cfg.max_chars, cfg.overlap_chars)
                    for i, sub in enumerate(sub_chunks):
                        if len(sub.strip()) < cfg.min_chars:
                            continue
                        label = f"{node.name}.{method.name}"
                        if len(sub_chunks) > 1:
                            label += f" (parte {i+1})"
                        c = _build_chunk(
                            text=sub,
                            rel_path=rel_path,
                            repo_name=repo_name,
                            note_title=note_title,
                            section_header=label,
                            symbol_type="method",
                            chunk_index=chunk_index,
                            contextual_prefix=cfg.contextual_prefix,
                            source_id=source_id,
                        )
                        if c:
                            chunks.append(c)
                            chunk_index += 1

    return chunks


def _split_if_long(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Divide texto longo preservando linhas inteiras."""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    lines = text.splitlines(keepends=True)
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current_len + len(line) > max_chars and current:
            chunks.append("".join(current).strip())
            # overlap: manter últimas linhas
            overlap_chars_left = overlap_chars
            overlap_lines: list[str] = []
            for ln in reversed(current):
                if overlap_chars_left <= 0:
                    break
                overlap_lines.insert(0, ln)
                overlap_chars_left -= len(ln)
            current = overlap_lines
            current_len = sum(len(ln) for ln in current)
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current).strip())
    return [c for c in chunks if c]


def _chunk_text_fallback(
    text: str,
    rel_path: str,
    repo_name: str,
    note_title: str,
    cfg,
    source_id: str | None = None,
) -> list[Chunk]:
    """Fallback: chunking por tamanho quando ast.parse() falha."""
    chunks = []
    parts = _split_if_long(text, cfg.max_chars, cfg.overlap_chars)
    for i, part in enumerate(parts):
        if len(part.strip()) < cfg.min_chars:
            continue
        c = _build_chunk(
            text=part,
            rel_path=rel_path,
            repo_name=repo_name,
            note_title=note_title,
            section_header=note_title,
            symbol_type="text",
            chunk_index=i,
            contextual_prefix=cfg.contextual_prefix,
            source_id=source_id,
        )
        if c:
            chunks.append(c)
    return chunks


def chunk_file(path: Path, repo_dir: Path, cfg) -> list[Chunk]:
    """Processa um único ficheiro do repo → lista de Chunks.

    - .py  → chunking AST (funções, classes, módulo)
    - JS/TS/Java/Go/Rust/C/C++/C#/Ruby → tree-sitter (se instalado)
    - .md/.yaml/.toml/.sh etc. → chunk_note() do chunker Markdown
    """
    repo_name = repo_dir.name
    source_id = stable_source_id(repo_name, repo_dir)
    suffix = path.suffix.lower()

    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    if not source.strip():
        return []

    if suffix == _PYTHON_EXTENSION:
        rel_path = str(path.relative_to(repo_dir))
        return _chunk_python_source(source, rel_path, repo_name, cfg, source_id=source_id)

    # Tree-sitter languages
    lang_key = _TREESITTER_EXTENSIONS.get(suffix)
    if lang_key is not None:
        rel_path = str(path.relative_to(repo_dir))
        try:
            from chunking.treesitter import chunk_treesitter, is_available
            if is_available():
                return chunk_treesitter(source, rel_path, repo_name, lang_key, cfg, source_id=source_id)
        except ImportError:
            pass
        # Fallback to text chunking if tree-sitter not installed
        note_title = Path(rel_path).name
        return _chunk_text_fallback(source, rel_path, repo_name, note_title, cfg, source_id=source_id)

    if suffix in _REPO_DOC_EXTENSIONS:
        # Reutiliza o chunker Markdown com metadata enriquecida
        md_chunks = chunk_note(path, source_dir=repo_dir)
        # Enriquecer metadata com info do repo
        enriched = []
        for c in md_chunks:
            c.metadata["source_type"] = "repo_doc"
            c.metadata["repo_name"] = repo_name
            c.metadata["source_id"] = source_id
            enriched.append(c)
        return enriched

    return []


# Ficheiros/pastas a ignorar no repo
_IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", "dist", "build", ".eggs", "*.egg-info",
    "logs", "models", "output", "rag", "input", "graphify-out", "data",
    "site-packages", "source",
}

_IGNORE_FILES = {
    ".gitignore", ".env", ".env.example", "Makefile", "compose.yaml",
    "docker-compose.yaml", "docker-compose.yml",
}


def _is_venv_dir(part: str) -> bool:
    """True se o nome do directório parece ser um virtual environment."""
    low = part.lower()
    return low.startswith(".venv") or low.startswith("venv") or low == "env"


def _should_skip(path: Path, repo_dir: Path) -> bool:
    """True se o ficheiro deve ser ignorado."""
    rel = path.relative_to(repo_dir)
    parts = rel.parts
    # Ignorar dirs especiais
    for part in parts[:-1]:  # só dirs (não o filename)
        if part in _IGNORE_DIRS or part.endswith(".egg-info") or _is_venv_dir(part):
            return True
    # Ignorar ficheiros específicos
    if path.name in _IGNORE_FILES:
        return True
    # Ignorar binários, imagens, etc.
    if path.suffix.lower() in {
        ".pyc", ".pyd", ".so", ".dylib", ".dll",
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
        ".zip", ".tar", ".gz", ".pkl", ".npy", ".npz",
        ".faiss", ".index", ".sqlite", ".db",
    }:
        return True
    return False


def chunk_repo(repo_dir: Path | str, cfg=None) -> list[Chunk]:
    """Processa todos os ficheiros relevantes de um repo git.

    Retorna lista de Chunks compatível com o pipeline de ingestão.
    """
    from rag_config import settings as _settings
    if cfg is None:
        cfg = _settings.repos.chunking

    repo_dir = Path(repo_dir).expanduser().resolve()
    if not repo_dir.exists():
        raise FileNotFoundError(f"Repo não encontrado: {repo_dir}")

    all_chunks: list[Chunk] = []
    for path in iter_repo_files(repo_dir):
        all_chunks.extend(chunk_file(path, repo_dir, cfg))

    return all_chunks


def iter_repo_files(repo_dir: Path | str) -> Iterator[Path]:
    """Yield valid file paths from a repo, filtering ignored dirs/files/extensions.

    Respects .gitignore and .git/info/exclude patterns.
    This is the streaming equivalent of the scan loop in chunk_repo().
    Used by the bounded ingest pipeline to process files one at a time.
    """
    repo_dir = Path(repo_dir).expanduser().resolve()
    valid_extensions = {_PYTHON_EXTENSION} | _REPO_DOC_EXTENSIONS | set(_TREESITTER_EXTENSIONS.keys())
    gitignore_patterns = _load_gitignore_patterns(repo_dir)

    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file():
            continue
        if _should_skip(path, repo_dir):
            continue
        if path.suffix.lower() not in valid_extensions:
            continue
        if _is_gitignored(path, repo_dir, gitignore_patterns):
            continue
        yield path


def _load_gitignore_patterns(repo_dir: Path) -> list[str]:
    """Load gitignore patterns from .gitignore and .git/info/exclude."""
    patterns: list[str] = []
    for ignore_file in (
        repo_dir / ".gitignore",
        repo_dir / ".git" / "info" / "exclude",
    ):
        if ignore_file.is_file():
            try:
                for line in ignore_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
            except OSError:
                pass
    return patterns


def _is_gitignored(path: Path, repo_dir: Path, patterns: list[str]) -> bool:
    """Return True if *path* matches any of the gitignore *patterns*."""
    if not patterns:
        return False
    try:
        rel = str(path.relative_to(repo_dir))
    except ValueError:
        return False
    rel_posix = rel.replace("\\", "/")
    name = path.name
    for pattern in patterns:
        # Directory pattern (trailing slash) — match any path component
        if pattern.endswith("/"):
            dir_pattern = pattern.rstrip("/")
            if any(fnmatch.fnmatch(part, dir_pattern) for part in path.relative_to(repo_dir).parts[:-1]):
                return True
            continue
        # Match against full relative path or just filename
        if fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        # Pattern without slash matches basename in any directory
        if "/" not in pattern and fnmatch.fnmatch(name, pattern):
            return True
    return False
