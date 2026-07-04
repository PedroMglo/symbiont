"""Portable path discovery for command contexts."""

from __future__ import annotations

import os
from pathlib import Path


def _first_existing(*paths: Path | None) -> Path | None:
    for path in paths:
        if path is not None and path.exists():
            return path.resolve()
    return None


def _path_env(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else None


def _scratch_output_root(project_root: Path) -> Path:
    scratch_root = _path_env("AI_LOCAL_PROJECT_SCRATCH_ROOT")
    return (_path_env("AI_LOCAL_OUTPUT_ROOT") or _scratch_child(scratch_root, project_root)).resolve()


def _scratch_child(scratch_root: Path | None, project_root: Path) -> Path:
    root = scratch_root or project_root / ".local" / "data" / "storage_guardian" / "scratch" / "project"
    return root / "agentic-command-output"


def _host_scratch_output_root(host_project_root: Path) -> Path:
    return (
        _path_env("AI_LOCAL_HOST_OUTPUT_ROOT")
        or host_project_root / ".local" / "data" / "storage_guardian" / "scratch" / "project" / "agentic-command-output"
    ).resolve()


def _is_project_root(path: Path) -> bool:
    return (
        (path / "config" / "main.yaml").exists()
        and (path / "orchestrator").is_dir()
        and (path / "agents").is_dir()
        and (path / "features").is_dir()
    )


def _find_project_root() -> Path:
    env_root = os.environ.get("AI_LOCAL_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    project_mount = Path("/project")
    if _is_project_root(project_mount):
        return project_mount.resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if _is_project_root(parent):
            return parent
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        if _is_project_root(parent):
            return parent
    return cwd


def _find_host_project_root(project_root: Path) -> Path:
    env_root = os.environ.get("AI_LOCAL_HOST_PROJECT_ROOT") or os.environ.get("ORC_LIFECYCLE_PROJECT_DIR")
    if env_root:
        return Path(env_root).expanduser().resolve()

    host_home = os.environ.get("HOST_HOME_PREFIX")
    candidates: list[Path] = []
    if host_home:
        candidates.append(Path(host_home).expanduser() / "_projects" / "ai-local")
    candidates.extend((
        project_root,
    ))
    for candidate in candidates:
        if _is_project_root(candidate):
            return candidate.resolve()
    return project_root.resolve()


def discover_roots() -> dict[str, Path]:
    """Return real host/container roots without user-specific hardcoding."""

    project_root = _find_project_root()
    host_project_root = _find_host_project_root(project_root)
    orchestrator_root = _first_existing(project_root / "orchestrator")
    agents_root = _first_existing(project_root / "agents")
    features_root = _first_existing(project_root / "features")
    rag_root = _first_existing(
        Path(os.environ["AI_LOCAL_RAG_ROOT"]).expanduser().resolve()
        if os.environ.get("AI_LOCAL_RAG_ROOT")
        else None,
        project_root / "obsidian-rag",
    )
    storage_root = _first_existing(
        Path(os.environ["AI_LOCAL_STORAGE_ROOT"]).expanduser().resolve()
        if os.environ.get("AI_LOCAL_STORAGE_ROOT")
        else None,
        Path(os.environ["AI_STORAGE_EXTERNAL_ROOT"]).expanduser().resolve()
        if os.environ.get("AI_STORAGE_EXTERNAL_ROOT")
        else None,
        project_root / ".local" / "storage",
    )
    host_storage_root = _first_existing(
        Path(os.environ["AI_LOCAL_HOST_STORAGE_ROOT"]).expanduser().resolve()
        if os.environ.get("AI_LOCAL_HOST_STORAGE_ROOT")
        else None,
        Path(os.environ["AI_STORAGE_HOST_BIND_ROOT"]).expanduser().resolve()
        if os.environ.get("AI_STORAGE_HOST_BIND_ROOT")
        else None,
    )
    if host_storage_root is None:
        effective_storage_root = storage_root or project_root / ".local" / "storage"
        try:
            relative_storage = effective_storage_root.resolve().relative_to(project_root.resolve())
            host_storage_root = (host_project_root / relative_storage).resolve()
        except ValueError:
            host_storage_root = effective_storage_root.resolve()
    output_root = _scratch_output_root(project_root)
    host_output_root = _host_scratch_output_root(host_project_root)
    user_home = Path(os.environ.get("HOME", str(Path.home()))).expanduser().resolve()
    return {
        "PROJECT_ROOT": project_root,
        "ORCHESTRATOR_ROOT": orchestrator_root or project_root / "orchestrator",
        "AGENTS_ROOT": agents_root or project_root / "agents",
        "FEATURES_ROOT": features_root or project_root / "features",
        "RAG_ROOT": rag_root or project_root / "obsidian-rag",
        "AI_LOCAL_STORAGE_ROOT": storage_root or project_root / ".local",
        "HOST_AI_LOCAL_STORAGE_ROOT": host_storage_root,
        "USER_HOME": user_home,
        "OUTPUT_ROOT": output_root,
        "HOST_PROJECT_ROOT": host_project_root,
        "HOST_ORCHESTRATOR_ROOT": host_project_root / "orchestrator",
        "HOST_AGENTS_ROOT": host_project_root / "agents",
        "HOST_FEATURES_ROOT": host_project_root / "features",
        "HOST_RAG_ROOT": (
            Path(os.environ["AI_LOCAL_HOST_RAG_ROOT"]).expanduser().resolve()
            if os.environ.get("AI_LOCAL_HOST_RAG_ROOT")
            else host_project_root / "obsidian-rag"
        ),
        "HOST_OUTPUT_ROOT": host_output_root,
    }
