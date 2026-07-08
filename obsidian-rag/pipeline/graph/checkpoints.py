"""Checkpoint helpers for cooperative Graphify builds."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

CHECKPOINT_SCHEMA_VERSION = 1


def checkpoint_path(graphify_output_dir: Path) -> Path:
    return graphify_output_dir / "checkpoint.json"


def build_fingerprint(
    root: Path,
    *,
    graphable_extensions: frozenset[str],
    skip_patterns: tuple[str, ...] = (),
    size_limit: int | None = None,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    files = _iter_graphable_files(
        root,
        graphable_extensions=graphable_extensions,
        skip_patterns=skip_patterns,
        size_limit=size_limit,
    )
    total_bytes = 0
    doc_count = 0
    code_count = 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        total_bytes += stat.st_size
        if path.suffix.lower() in {".md", ".markdown", ".txt", ".rst", ".adoc"}:
            doc_count += 1
        else:
            code_count += 1
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(_file_sha256(path).encode("ascii"))
        digest.update(b"\0")
    return {
        "algorithm": "sha256:graphify-filtered-content:v1",
        "digest": digest.hexdigest(),
        "file_count": doc_count + code_count,
        "doc_count": doc_count,
        "code_count": code_count,
        "total_bytes": total_bytes,
    }


def estimate_work(fingerprint: dict[str, Any], *, force: bool) -> dict[str, Any]:
    doc_count = int(fingerprint.get("doc_count") or 0)
    code_count = int(fingerprint.get("code_count") or 0)
    total_bytes = int(fingerprint.get("total_bytes") or 0)
    llm_batches = (doc_count + 24) // 25 if doc_count else 0
    ast_batches = (code_count + 199) // 200 if code_count else 0
    estimated_duration_s = (llm_batches * 45) + (ast_batches * 5)
    risk = "high" if force and llm_batches >= 10 else "medium" if llm_batches else "low"
    return {
        "force": force,
        "file_count": int(fingerprint.get("file_count") or 0),
        "doc_count": doc_count,
        "code_count": code_count,
        "estimated_io_mb": round(total_bytes / 1024 / 1024, 2),
        "estimated_llm_doc_batches": llm_batches,
        "estimated_ast_batches": ast_batches,
        "estimated_gpu_batches": llm_batches,
        "estimated_duration_s": estimated_duration_s,
        "estimated_vram_mb": 2048 if llm_batches else 0,
        "risk": risk,
        "recommendation": "run_in_background_windows" if llm_batches else "ast_only_or_skip",
        "stages": [
            {"name": "scan", "resource": "cpu"},
            {"name": "parse", "resource": "cpu"},
            {"name": "diff", "resource": "cpu"},
            {"name": "plan", "resource": "cpu"},
            {"name": "embed", "resource": "gpu", "lease_lane": "graphify_background"},
            {"name": "enrich", "resource": "gpu", "lease_lane": "graphify_background"},
            {"name": "write", "resource": "io"},
            {"name": "checkpoint", "resource": "io"},
        ],
    }


def load_checkpoint(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def checkpoint_completed_for_fingerprint(checkpoint: dict[str, Any] | None, fingerprint: dict[str, Any]) -> bool:
    if not checkpoint:
        return False
    return bool(
        checkpoint.get("status") == "completed"
        and checkpoint.get("fingerprint", {}).get("digest") == fingerprint.get("digest")
    )


def write_checkpoint(
    path: Path,
    *,
    repo_path: Path,
    input_path: Path,
    status: str,
    stage: str,
    force: bool,
    fingerprint: dict[str, Any],
    dry_run_estimate: dict[str, Any],
    error: str | None = None,
) -> None:
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "repo_name": repo_path.name,
        "repo_path": str(repo_path),
        "input_path": str(input_path),
        "status": status,
        "stage": stage,
        "force": force,
        "fingerprint": fingerprint,
        "dry_run_estimate": dry_run_estimate,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if error:
        payload["error"] = error
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.rename(path)


def _iter_graphable_files(
    root: Path,
    *,
    graphable_extensions: frozenset[str],
    skip_patterns: tuple[str, ...],
    size_limit: int | None,
) -> list[Path]:
    files: list[Path] = []
    for child in root.rglob("*"):
        if not child.is_file() or child.is_symlink():
            continue
        rel = child.relative_to(root).as_posix()
        parts = child.relative_to(root).parts
        if any(part.startswith(".") or part in {"node_modules", "__pycache__", ".git", "venv", ".venv", "graphify-out"} for part in parts):
            continue
        if child.suffix.lower() not in graphable_extensions:
            continue
        if skip_patterns and any(_match_skip(rel, pattern) for pattern in skip_patterns):
            continue
        try:
            if size_limit is not None and child.stat().st_size > size_limit:
                continue
        except OSError:
            continue
        files.append(child)
    return sorted(files)


def _match_skip(rel_path: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(rel_path, pattern) or (
        "/" not in pattern and fnmatch.fnmatch(Path(rel_path).name, pattern)
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
