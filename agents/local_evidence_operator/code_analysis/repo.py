"""Repo provider — local git state and file structure."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from code_analysis.config import get_settings
from code_analysis.types import RepoStatusResponse

log = logging.getLogger(__name__)

_ABS_PATH_RE = re.compile(r"(?P<path>/[^\s\"'`,;:]+)")
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".sh", ".zsh", ".bash", ".toml",
    ".yaml", ".yml", ".json", ".md",
}


def _run_git_read(cmd: list[str], cwd: str | None = None) -> tuple[str, str | None]:
    """Run a read-only Git command and return stdout plus a structured error."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
    except FileNotFoundError:
        return "", "git_unavailable"
    except subprocess.TimeoutExpired:
        return "", "git_timeout"
    except OSError as exc:
        return "", f"git_os_error:{type(exc).__name__}"
    if result.returncode != 0:
        command = cmd[1] if len(cmd) > 1 else "unknown"
        return "", f"git_{command}_failed:{result.returncode}"
    return result.stdout.strip(), None


def _find_git_repos(scan_paths: list[str], max_depth: int = 2) -> list[Path]:
    """Find all git repos under scan_paths up to max_depth levels deep."""
    repos: list[Path] = []

    def _recurse(path: Path, depth: int) -> None:
        if (path / ".git").exists():
            repos.append(path)
            return  # don't recurse into nested git repos
        if depth == 0:
            return
        try:
            for child in sorted(path.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    _recurse(child, depth - 1)
        except PermissionError:
            pass

    for scan in scan_paths:
        p = Path(scan).expanduser()
        if p.exists():
            _recurse(p, max_depth)

    return repos


def get_repo_status(repo_path: str | None = None) -> RepoStatusResponse:
    """Get git status for a repository."""
    if repo_path:
        cwd = repo_path
    else:
        # Find first git repo under scan_paths
        cfg = get_settings()
        repos = _find_git_repos(cfg.repo.scan_paths)
        cwd = str(repos[0]) if repos else None
        if not cwd:
            return RepoStatusResponse()

    branch, branch_error = _run_git_read(["git", "branch", "--show-current"], cwd=cwd)
    status_output, status_error = _run_git_read(["git", "status", "--porcelain"], cwd=cwd)

    modified = []
    untracked = []
    for line in status_output.split("\n"):
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked.append(line[3:])
        else:
            modified.append(line[3:])

    return RepoStatusResponse(
        branch=branch,
        modified_files=modified[:50],
        untracked_files=untracked[:20],
        error=branch_error or status_error,
    )


def get_repo_context(query: str) -> str:
    """Build repo context string based on query — summarises all discovered git repos."""
    cfg = get_settings()
    if not cfg.repo.include_git_status:
        return ""

    repos = _find_git_repos(cfg.repo.scan_paths)
    if not repos:
        return "## Repository\n\nRepo context unavailable: git_repository_not_found"

    # Build combined status for up to 5 repos
    all_parts: list[str] = []
    for repo_path in repos[:5]:
        status = get_repo_status(str(repo_path))
        if status.error and not status.branch:
            all_parts.append(f"## Repo: {repo_path.name}\n\nStatus unavailable: {status.error}")
            continue
        parts = [f"## Repo: {repo_path.name}\n\nBranch: `{status.branch}`"]
        if status.modified_files:
            parts.append(f"Modified ({len(status.modified_files)}):")
            for f in status.modified_files[:10]:
                parts.append(f"  - {f}")
        if status.untracked_files:
            parts.append(f"Untracked ({len(status.untracked_files)}):")
            for f in status.untracked_files[:5]:
                parts.append(f"  - {f}")
        all_parts.append("\n".join(parts))

    return "\n\n".join(all_parts) if all_parts else ""


def get_file_context(query: str, budget_tokens: int = 2000) -> str:
    """Return focused context for absolute code/config paths mentioned in query."""
    cfg = get_settings()
    roots = [Path(p).expanduser().resolve() for p in cfg.repo.scan_paths]
    parts: list[str] = []
    for raw_path in _extract_paths(query)[:3]:
        path = _map_host_path(raw_path)
        if path is None:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not _is_allowed(resolved, roots) or not resolved.is_file():
            continue
        if resolved.suffix.lower() not in _CODE_EXTENSIONS:
            continue
        try:
            if resolved.stat().st_size > 512_000:
                parts.append(f"## File: {raw_path}\nSkipped: file is larger than 512KB.")
                continue
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            parts.append(f"## File: {raw_path}\nRead failed: {type(exc).__name__}")
            continue
        parts.append(_format_file_focus(raw_path, resolved, text, query, budget_tokens))
    return "\n\n".join(parts)


def _extract_paths(query: str) -> list[str]:
    paths: list[str] = []
    for match in _ABS_PATH_RE.finditer(query or ""):
        raw = match.group("path").rstrip(").]")
        if raw and raw not in paths:
            paths.append(raw)
    return paths


def _map_host_path(raw_path: str) -> Path | None:
    if raw_path.startswith("/projects/"):
        return Path(raw_path)
    marker = "/_projects/"
    if marker in raw_path:
        return Path("/projects") / raw_path.split(marker, 1)[1]
    return None


def _is_allowed(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _format_file_focus(raw_path: str, path: Path, text: str, query: str, budget_tokens: int) -> str:
    lines = text.splitlines()
    terms = _focus_terms(query)
    primary_terms = {term for term in terms if "_" in term}
    primary_matches = [
        idx
        for idx, line in enumerate(lines)
        if any(term in line.lower() for term in primary_terms)
    ]
    matches = [
        idx
        for idx, line in enumerate(lines)
        if any(term in line.lower() for term in terms)
    ]
    if primary_matches:
        matches = primary_matches
    if not matches:
        ranges = [(0, min(len(lines), 120))]
    elif primary_matches:
        ranges = _merge_ranges((max(0, i - 4), min(len(lines), i + 10)) for i in matches[:16])
    else:
        ranges = _merge_ranges((max(0, i - 10), min(len(lines), i + 22)) for i in matches[:12])

    max_chars = max(3600, budget_tokens * 5)
    out = [
        f"## File: {raw_path}",
        f"container_path: {path}",
        f"lines: {len(lines)}",
        "```",
    ]
    used = 0
    for start, end in ranges:
        if used >= max_chars:
            break
        if start > 0:
            out.append("...")
        for idx in range(start, end):
            rendered = f"{idx + 1:04d}: {lines[idx]}"
            used += len(rendered) + 1
            if used > max_chars:
                out.append("[...truncated...]")
                break
            out.append(rendered)
    out.append("```")
    return "\n".join(out)


def _focus_terms(query: str) -> set[str]:
    terms = {
        word.strip(".,!?()[]{}\"'`").lower()
        for word in (query or "").split()
        if len(word.strip(".,!?()[]{}\"'`")) >= 4
    }
    return terms | {"context_blocks", "stream_messages", "streaming", "route", "dispatch"}


def _merge_ranges(ranges) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged
