"""Mount planning and sandbox path translation for command sessions."""

from __future__ import annotations

from pathlib import Path

from orchestrator.agentic.tools.command.discovery import discover_roots
from orchestrator.agentic.tools.command.schemas import CommandContext, CommandMount

DEFAULT_PROFILE = "project_context"
WORKSPACE_GENERATION_PROFILE = "workspace_generation"
SANDBOX_PROJECT = "/workspace/project"


def build_context(
    profile: str = DEFAULT_PROFILE,
    *,
    allow_user_context_ro: bool = False,
    allow_host_context_ro: bool = False,
) -> CommandContext:
    roots = discover_roots()
    mounts: list[CommandMount] = [
        CommandMount(
            "/workspace/project",
            roots["PROJECT_ROOT"],
            True,
            "PROJECT_ROOT",
            roots["HOST_PROJECT_ROOT"],
        ),
        CommandMount(
            "/workspace/orchestrator",
            roots["ORCHESTRATOR_ROOT"],
            True,
            "ORCHESTRATOR_ROOT",
            roots["HOST_ORCHESTRATOR_ROOT"],
        ),
        CommandMount(
            "/workspace/agents",
            roots["AGENTS_ROOT"],
            True,
            "AGENTS_ROOT",
            roots["HOST_AGENTS_ROOT"],
        ),
        CommandMount(
            "/workspace/features",
            roots["FEATURES_ROOT"],
            True,
            "FEATURES_ROOT",
            roots["HOST_FEATURES_ROOT"],
        ),
        CommandMount(
            "/workspace/obsidian-rag",
            roots["RAG_ROOT"],
            True,
            "RAG_ROOT",
            roots["HOST_RAG_ROOT"],
        ),
        CommandMount(
            "/storage/ai-local",
            roots["AI_LOCAL_STORAGE_ROOT"],
            True,
            "AI_LOCAL_STORAGE_ROOT",
            roots["HOST_AI_LOCAL_STORAGE_ROOT"],
        ),
    ]
    if profile == "user_context_ro":
        if not allow_user_context_ro:
            raise PermissionError("user_context_ro requires explicit configuration")
        mounts.append(CommandMount("/user/home", roots["USER_HOME"], True, "USER_HOME"))
    elif profile == "host_context_ro":
        if not allow_host_context_ro:
            raise PermissionError("host_context_ro requires explicit configuration")
        mounts.append(CommandMount("/host/root", Path("/"), True, "HOST_ROOT"))
    elif profile == WORKSPACE_GENERATION_PROFILE:
        pass
    elif profile != DEFAULT_PROFILE:
        raise ValueError(f"Unknown command context profile: {profile}")
    return CommandContext(profile=profile, default_cwd=SANDBOX_PROJECT, mounts=tuple(mounts))


def resolve_sandbox_path(path: str, context: CommandContext) -> Path:
    """Resolve a sandbox path to a real path and ensure it stays inside mounts."""

    raw = path or context.default_cwd
    if not raw.startswith("/"):
        raw = f"{context.default_cwd.rstrip('/')}/{raw}"
    for mount in sorted(context.mounts, key=lambda item: len(item.sandbox_path), reverse=True):
        prefix = mount.sandbox_path.rstrip("/")
        if raw == prefix or raw.startswith(prefix + "/"):
            relative = raw.removeprefix(prefix).lstrip("/")
            candidate = (mount.source_path / relative).resolve()
            source = mount.source_path.resolve()
            if candidate == source or source in candidate.parents:
                return candidate
            raise PermissionError(f"Path escapes mount: {path}")
    raise PermissionError(f"Path is outside command context mounts: {path}")


def rewrite_command_for_execution(command: str, context: CommandContext) -> str:
    """Translate stable sandbox paths to real paths before execution."""

    rewritten = command
    for mount in sorted(context.mounts, key=lambda item: len(item.sandbox_path), reverse=True):
        rewritten = rewritten.replace(mount.sandbox_path, str(mount.source_path))
    return rewritten


def redact_real_paths(text: str, context: CommandContext) -> str:
    redacted = text
    for mount in sorted(context.mounts, key=lambda item: len(str(item.source_path)), reverse=True):
        redacted = redacted.replace(str(mount.source_path), mount.sandbox_path)
    return redacted
