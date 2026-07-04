"""Generate structured repo overview chunks for RAG indexing.

Produces a high-level summary of a repository containing:
- Project metadata (name, description, dependencies)
- File tree (filtered, max depth)
- Key exports and entry points
- Dependency graph summary

This runs at index time (not per-query) and produces chunks with
source_type="repo_overview" that help answer project-structure questions.

Inspired by Repomix (https://github.com/yamadashy/repomix) but implemented
in pure Python without Node.js dependency.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from obsidian_rag.chunking.markdown import Chunk
from obsidian_rag.metadata import stable_source_id

log = logging.getLogger("obsidian_rag")

# Max tree depth to avoid massive output
_MAX_TREE_DEPTH = 4
# Max files shown per directory level
_MAX_FILES_PER_DIR = 15
# Dirs to always skip in tree
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs",
    "egg-info", ".egg-info", "htmlcov", ".coverage",
}


def generate_repo_overview(repo_dir: Path | str) -> list[Chunk]:
    """Generate overview chunks for a single repository.

    Returns 1-3 chunks depending on repo size:
    - Always: project summary + file tree
    - If found: dependency info
    - If found: key entry points / exports
    """
    repo_dir = Path(repo_dir).expanduser().resolve()
    if not repo_dir.exists():
        return []

    repo_name = repo_dir.name
    chunks: list[Chunk] = []

    # 1. Project summary + file tree
    summary = _build_summary(repo_dir, repo_name)
    if summary:
        chunks.append(_make_chunk(
            text=summary,
            repo_name=repo_name,
            section="project_overview",
            title=f"{repo_name} — Project Overview",
        ))

    # 2. Dependencies
    deps = _extract_dependencies(repo_dir, repo_name)
    if deps:
        chunks.append(_make_chunk(
            text=deps,
            repo_name=repo_name,
            section="dependencies",
            title=f"{repo_name} — Dependencies",
        ))

    # 3. Entry points / key exports
    entry_points = _extract_entry_points(repo_dir, repo_name)
    if entry_points:
        chunks.append(_make_chunk(
            text=entry_points,
            repo_name=repo_name,
            section="entry_points",
            title=f"{repo_name} — Entry Points & Key Modules",
        ))

    return chunks


def _make_chunk(text: str, repo_name: str, section: str, title: str) -> Chunk:
    """Create a Chunk with repo_overview metadata."""
    source_id = stable_source_id(f"{repo_name}/{section}")
    return Chunk(
        text=text,
        metadata={
            "source_id": source_id,
            "source_path": f"{repo_name}/{section}",
            "source_type": "repo_overview",
            "repo_name": repo_name,
            "note_title": title,
            "section_header": section,
            "symbol_type": "overview",
            "chunk_index": 0,
            "display_text": text,
        },
    )


def _build_summary(repo_dir: Path, repo_name: str) -> str:
    """Build project summary with file tree."""
    parts: list[str] = [f"# Repository: {repo_name}\n"]

    # Try to get description from common files
    desc = _get_project_description(repo_dir)
    if desc:
        parts.append(f"## Description\n{desc}\n")

    # File tree
    tree = _generate_file_tree(repo_dir)
    if tree:
        parts.append(f"## File Structure\n```\n{tree}\n```\n")

    # Language stats
    stats = _language_stats(repo_dir)
    if stats:
        parts.append("## Languages\n" + "\n".join(
            f"- {lang}: {count} files" for lang, count in stats
        ) + "\n")

    return "\n".join(parts)


def _get_project_description(repo_dir: Path) -> str:
    """Extract project description from README or pyproject.toml."""
    # Try pyproject.toml
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                tomllib = None  # type: ignore[assignment]
        if tomllib:
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                desc = data.get("project", {}).get("description", "")
                if desc:
                    return desc
            except Exception:
                pass

    # Try package.json
    pkg_json = repo_dir / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            desc = data.get("description", "")
            if desc:
                return desc
        except Exception:
            pass

    # Try first paragraph of README
    for readme_name in ("README.md", "readme.md", "README.rst", "README"):
        readme = repo_dir / readme_name
        if readme.is_file():
            try:
                lines = readme.read_text(encoding="utf-8", errors="ignore").splitlines()
                # Skip title and blank lines, get first content paragraph
                content_lines: list[str] = []
                past_title = False
                for line in lines[:30]:
                    if line.startswith("#"):
                        past_title = True
                        continue
                    if past_title and line.strip():
                        content_lines.append(line.strip())
                    elif past_title and not line.strip() and content_lines:
                        break
                if content_lines:
                    return " ".join(content_lines)[:300]
            except Exception:
                pass

    return ""


def _generate_file_tree(repo_dir: Path, max_depth: int = _MAX_TREE_DEPTH) -> str:
    """Generate a filtered file tree string."""
    lines: list[str] = [repo_dir.name + "/"]
    _tree_recurse(repo_dir, "", 1, max_depth, lines)
    return "\n".join(lines[:100])  # Cap at 100 lines


def _tree_recurse(
    directory: Path,
    prefix: str,
    depth: int,
    max_depth: int,
    lines: list[str],
) -> None:
    """Recursively build tree lines."""
    if depth > max_depth:
        lines.append(f"{prefix}...")
        return

    entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    # Filter
    entries = [
        e for e in entries
        if e.name not in _SKIP_DIRS
        and not e.name.startswith(".")
        and not e.name.endswith(".egg-info")
    ]

    dirs = [e for e in entries if e.is_dir()]
    files = [e for e in entries if e.is_file()]

    # Show directories
    for d in dirs:
        lines.append(f"{prefix}{d.name}/")
        _tree_recurse(d, prefix + "  ", depth + 1, max_depth, lines)

    # Show files (capped)
    shown = files[:_MAX_FILES_PER_DIR]
    for f in shown:
        lines.append(f"{prefix}{f.name}")
    if len(files) > _MAX_FILES_PER_DIR:
        lines.append(f"{prefix}... ({len(files) - _MAX_FILES_PER_DIR} more files)")


def _language_stats(repo_dir: Path) -> list[tuple[str, int]]:
    """Count files by extension."""
    ext_map: dict[str, str] = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".jsx": "React JSX", ".tsx": "React TSX",
        ".java": "Java", ".go": "Go", ".rs": "Rust",
        ".c": "C", ".cpp": "C++", ".cs": "C#", ".rb": "Ruby",
        ".md": "Markdown", ".yaml": "YAML", ".yml": "YAML",
        ".toml": "TOML", ".json": "JSON",
    }
    counts: dict[str, int] = {}
    for path in repo_dir.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        lang = ext_map.get(path.suffix.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1

    # Sort by count descending, return top 8
    sorted_langs = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return sorted_langs[:8]


def _extract_dependencies(repo_dir: Path, repo_name: str) -> str:
    """Extract dependency information from project files."""
    parts: list[str] = [f"# {repo_name} — Dependencies\n"]
    found = False

    # Python: pyproject.toml
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                tomllib = None  # type: ignore[assignment]
        if tomllib:
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                deps = data.get("project", {}).get("dependencies", [])
                if deps:
                    parts.append("## Python Dependencies\n" + "\n".join(f"- {d}" for d in deps[:30]) + "\n")
                    found = True
                # Optional deps
                opt = data.get("project", {}).get("optional-dependencies", {})
                if opt:
                    for group, group_deps in opt.items():
                        parts.append(f"### Optional [{group}]\n" + "\n".join(f"- {d}" for d in group_deps[:10]) + "\n")
                    found = True
            except Exception:
                pass

    # Node.js: package.json
    pkg_json = repo_dir / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = data.get("dependencies", {})
            dev_deps = data.get("devDependencies", {})
            if deps:
                parts.append("## Node.js Dependencies\n" + "\n".join(f"- {k}: {v}" for k, v in list(deps.items())[:30]) + "\n")
                found = True
            if dev_deps:
                parts.append("### Dev Dependencies\n" + "\n".join(f"- {k}: {v}" for k, v in list(dev_deps.items())[:20]) + "\n")
                found = True
        except Exception:
            pass

    # requirements.txt fallback
    req_txt = repo_dir / "requirements.txt"
    if req_txt.is_file() and not found:
        try:
            lines = [line_.strip() for line_ in req_txt.read_text(encoding="utf-8").splitlines()
                     if line_.strip() and not line_.startswith("#")]
            if lines:
                parts.append("## Python Requirements\n" + "\n".join(f"- {line_}" for line_ in lines[:30]) + "\n")
                found = True
        except Exception:
            pass

    return "\n".join(parts) if found else ""


def _extract_entry_points(repo_dir: Path, repo_name: str) -> str:
    """Extract key entry points and exported symbols."""
    parts: list[str] = [f"# {repo_name} — Entry Points & Key Modules\n"]
    found = False

    # Python: find __init__.py __all__ exports and main entry points
    for init_file in repo_dir.rglob("__init__.py"):
        if any(part in _SKIP_DIRS for part in init_file.parts):
            continue
        try:
            content = init_file.read_text(encoding="utf-8", errors="ignore")
            # Look for __all__ exports
            if "__all__" in content:
                import ast
                try:
                    tree = ast.parse(content)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Assign):
                            for target in node.targets:
                                if isinstance(target, ast.Name) and target.id == "__all__":
                                    if isinstance(node.value, (ast.List, ast.Tuple)):
                                        exports = [
                                            elt.value for elt in node.value.elts
                                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                                        ]
                                        if exports:
                                            rel_path = init_file.relative_to(repo_dir)
                                            parts.append(f"## {rel_path}\nExports: {', '.join(exports)}\n")
                                            found = True
                except SyntaxError:
                    pass
        except Exception:
            pass

    # pyproject.toml scripts/entry_points
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                tomllib = None  # type: ignore[assignment]
        if tomllib:
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                scripts = data.get("project", {}).get("scripts", {})
                if scripts:
                    parts.append("## CLI Entry Points\n" + "\n".join(
                        f"- `{cmd}` → {target}" for cmd, target in scripts.items()
                    ) + "\n")
                    found = True
            except Exception:
                pass

    # package.json scripts
    pkg_json = repo_dir / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            main = data.get("main", "")
            if main:
                parts.append(f"## Main Entry: `{main}`\n")
                found = True
            if scripts:
                parts.append("## npm Scripts\n" + "\n".join(
                    f"- `{cmd}`: {script}" for cmd, script in list(scripts.items())[:15]
                ) + "\n")
                found = True
        except Exception:
            pass

    return "\n".join(parts) if found else ""


def generate_overviews_for_all_repos() -> Iterator[Chunk]:
    """Generate overview chunks for all configured repos.

    Reads repo paths from settings and yields overview chunks.
    Intended to be called during the indexing pipeline.
    """
    from obsidian_rag.config import settings

    repos_cfg = settings.repos
    if not hasattr(repos_cfg, "paths") or not repos_cfg.paths:
        log.info("No repos configured for overview generation")
        return

    for repo_path in repos_cfg.paths:
        path = Path(repo_path).expanduser().resolve()
        if not path.exists():
            log.debug("Repo path not found, skipping overview: %s", path)
            continue
        log.info("Generating repo overview for: %s", path.name)
        yield from generate_repo_overview(path)
