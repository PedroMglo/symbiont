"""Canonical RAG admin reprocess workflow and direct executor."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol

VALID_REPROCESS_TARGETS = frozenset({"local", "graph", "cag", "all"})


class _SyncModule(Protocol):
    def sync_local(self, *, vault_filter: str | None = None, force: bool = False) -> None: ...

    def sync_graphify(self, *, force: bool = False) -> None: ...

    def generate_cag_packs(self) -> None: ...

    def sync_all(self, *, vault_filter: str | None = None, force: bool = False) -> None: ...


def _load_sync_module() -> _SyncModule:
    from obsidian_rag.pipeline import sync

    return sync


def execute_reprocess_target(
    target: str,
    *,
    force: bool = False,
    vault: str | None = None,
    sync_module: _SyncModule | None = None,
) -> dict[str, Any]:
    """Run one RAG reprocess target through its owning pipeline function."""
    normalized_target = str(target).strip().lower()
    if normalized_target not in VALID_REPROCESS_TARGETS:
        raise ValueError(f"Unsupported reprocess target: {target}")

    sync = sync_module or _load_sync_module()
    if normalized_target == "local":
        sync.sync_local(vault_filter=vault, force=force)
    elif normalized_target == "graph":
        sync.sync_graphify(force=force)
    elif normalized_target == "cag":
        sync.generate_cag_packs()
    elif normalized_target == "all":
        sync.sync_all(vault_filter=vault, force=force)

    return {"target": normalized_target, "force": force, "vault": vault}


try:
    from temporalio import activity, workflow
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    activity = None
    workflow = None
    run_reprocess_activity = None
    RagReprocessWorkflow = None
else:

    @activity.defn(name="rag.reprocess")
    def run_reprocess_activity(payload: dict[str, Any]) -> dict[str, Any]:
        return execute_reprocess_target(
            str(payload.get("target", "all")),
            force=bool(payload.get("force", False)),
            vault=payload.get("vault"),
        )

    @workflow.defn(name="RagReprocessWorkflow")
    class RagReprocessWorkflow:
        @workflow.run
        async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
            timeout_seconds = int(payload.get("timeout_seconds") or 7200)
            return await workflow.execute_activity(
                run_reprocess_activity,
                payload,
                start_to_close_timeout=timedelta(seconds=timeout_seconds),
            )
