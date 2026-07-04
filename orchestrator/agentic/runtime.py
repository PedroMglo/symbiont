"""Helpers that connect the existing gateway/LangGraph path to the ledger."""

from __future__ import annotations

import logging
import os
import posixpath
import re
import uuid
from dataclasses import dataclass
from typing import Any

from orchestrator.agentic.context import AgenticContext
from orchestrator.agentic.models import TaskStatus

log = logging.getLogger(__name__)

_MATERIAL_DELIVERY_VERBS = (
    "build",
    "create",
    "emit",
    "export",
    "generate",
    "implement",
    "materialise",
    "materialize",
    "produce",
    "publish",
    "save",
    "scaffold",
    "store",
    "write",
    "constrói",
    "constroi",
    "cria",
    "criar",
    "exporta",
    "exportar",
    "gera",
    "gerar",
    "guarda",
    "guardar",
    "implementa",
    "implementar",
    "materializa",
    "materializar",
    "produz",
    "produzir",
    "publica",
    "publicar",
    "salva",
    "salvar",
)
_MATERIAL_DELIVERABLES = (
    "api",
    "app",
    "cli",
    "code",
    "container",
    "docker",
    "dockerfile",
    "endpoint",
    "file",
    "files",
    "docs",
    "documentation",
    "markdown",
    "project",
    "readme",
    "report",
    "service",
    "system",
    "tests",
    "ficheiro",
    "ficheiros",
    "documentação",
    "documentacao",
    "markdown",
    "plataforma",
    "projeto",
    "projecto",
    "relatório",
    "relatorio",
    "serviço",
    "servico",
    "sistema",
)
_MATERIAL_SECTION_MARKERS = (
    "deliverables:",
    "delivery:",
    "entrega:",
    "requisitos:",
    "requirements:",
)


def task_requires_material_output(goal: str, metadata: dict[str, Any] | None = None) -> bool:
    """Return whether a task needs effect/artifact evidence before completion."""

    data = metadata if isinstance(metadata, dict) else {}
    explicit = data.get("material_output_required")
    if explicit is not None:
        return bool(explicit)
    if data.get("expected_artifact_root") or data.get("expected_artifacts"):
        return True
    text = " ".join(str(goal or "").casefold().split())
    if not text:
        return False
    has_delivery = any(marker in text for marker in _MATERIAL_SECTION_MARKERS)
    has_verb = any(verb in text for verb in _MATERIAL_DELIVERY_VERBS)
    has_deliverable = any(deliverable in text for deliverable in _MATERIAL_DELIVERABLES)
    return bool(has_deliverable and (has_delivery or has_verb))


def material_task_metadata(goal: str, *, client_cwd: str | None = None) -> dict[str, Any]:
    """Build generic metadata for delegated material-output tasks."""

    metadata: dict[str, Any] = {
        "material_output_required": True,
        "completion_evidence_required": "effectful_action_or_artifact",
        "workspace_generation_context_profile": "workspace_generation",
        "durable_publish": True,
    }
    project = _first_backtick_identifier(goal)
    docs_root = _requested_docs_artifact_root(goal)
    if project:
        metadata["expected_artifact_root"] = project
        metadata["requested_project"] = project
    elif docs_root:
        metadata["expected_artifact_root"] = docs_root
        metadata["requested_project"] = docs_root
    requested_publish_root = _requested_publish_destination_root(goal)
    if requested_publish_root:
        requested_publish_root = _publish_destination_root_for_artifact_root(
            requested_publish_root,
            artifact_root=str(metadata.get("expected_artifact_root") or ""),
        )
        metadata["material_publish_destination_root"] = requested_publish_root
        metadata["material_publish_direct_to_destination_root"] = True
    if client_cwd:
        metadata["client_cwd"] = client_cwd
        metadata.setdefault("material_publish_destination_root", _material_publish_destination_root(client_cwd))
    return metadata


def _first_backtick_identifier(text: str) -> str:
    for match in re.finditer(r"`([^`/\\]{1,128})`", text or ""):
        value = "-".join(match.group(1).strip().split())
        if value and re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            return value
    return ""


def _requested_docs_artifact_root(text: str) -> str:
    q = " ".join(str(text or "").casefold().split())
    if re.search(r"(?<!\w)docs(?!\w)", q):
        return "docs"
    if any(term in q for term in ("documentação", "documentacao", "documentation")):
        return "docs"
    return ""


def _requested_publish_destination_root(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    candidates = _absolute_path_candidates(raw)
    if not candidates:
        return ""
    scored = [
        (_publish_destination_score(raw, start, end), start, path)
        for path, start, end in candidates
    ]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return ""
    _score, _start, path = max(scored, key=lambda item: (item[0], item[1]))
    return _map_host_path_to_container_root(path)


def _absolute_path_candidates(text: str) -> list[tuple[str, int, int]]:
    candidates: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[`'\"](?P<path>/[^`'\"]+)[`'\"]", text):
        candidates.append((match.group("path"), match.start("path"), match.end("path")))
    for match in re.finditer(r"(?<![A-Za-z0-9_.-])(?P<path>/[^\s,.;:]+)", text):
        path = _clean_absolute_path_candidate(match.group("path"))
        if path:
            candidates.append((path, match.start("path"), match.start("path") + len(path)))

    unique: list[tuple[str, int, int]] = []
    seen: set[tuple[str, int]] = set()
    for path, start, end in candidates:
        key = (path, start)
        if key in seen:
            continue
        seen.add(key)
        unique.append((path, start, end))
    return unique


def _clean_absolute_path_candidate(path: str) -> str:
    return str(path or "").strip().strip(".,;:)】]}>'\"`")


def _publish_destination_score(text: str, start: int, end: int) -> int:
    before = text[max(0, start - 120) : start].casefold()
    after = text[end : min(len(text), end + 80)].casefold()
    destination_cues = (
        "artifact",
        "artefacto",
        "artefato",
        "destination",
        "destino",
        "directory",
        "diretório",
        "diretorio",
        "docs",
        "documentação",
        "documentacao",
        "documentation",
        "export",
        "exporta",
        "ficar",
        "folder",
        "guarda",
        "materializa",
        "materialize",
        "output",
        "pasta",
        "produce",
        "produz",
        "publish",
        "publica",
        "relatório",
        "relatorio",
        "report",
        "salva",
        "save",
        "store",
        "write",
    )
    source_cues = (
        "analisa",
        "analisar",
        "analyze",
        "analyse",
        "fonte",
        "from",
        "input",
        "inspect",
        "inspeciona",
        "lê",
        "le ",
        "ler",
        "read",
        "source",
    )
    near_after = after[:40]
    score = sum(2 for cue in destination_cues if cue in before)
    score += sum(1 for cue in destination_cues if cue in near_after)
    source_window = f"{before[-70:]} {near_after}"
    score -= sum(3 for cue in source_cues if cue in source_window)
    if re.search(r"(?:dentro de|inside|em|in)\s+[`'\"]?$", before, flags=re.IGNORECASE):
        score += 2
    if re.search(r"(?:para|to|at|em)\s+[`'\"]?$", before, flags=re.IGNORECASE):
        score += 1
    return score


def _publish_destination_root_for_artifact_root(path: str, *, artifact_root: str) -> str:
    """Return the extraction destination root for an artifact with a top-level root.

    Storage publication extracts archives into ``publish_destination_root``. When
    the user-provided destination already names the artifact root, extraction must
    target the parent directory; otherwise a ``docs`` artifact published to
    ``.../docs`` becomes ``.../docs/docs``.
    """

    normalized = _clean_posix_path(path)
    root = str(artifact_root or "").strip().strip("/").replace("\\", "/")
    if not normalized or not root or "/" in root:
        return normalized
    if posixpath.basename(normalized).casefold() != root.casefold():
        return normalized
    parent = posixpath.dirname(normalized.rstrip("/"))
    return parent if parent and parent != "." else "/"


def _map_host_path_to_container_root(path: str) -> str:
    normalized = _clean_posix_path(path)
    if not normalized or not normalized.startswith("/"):
        return ""
    host_home = _clean_posix_path(os.environ.get("HOST_HOME_PREFIX") or os.environ.get("AI_LOCAL_HOST_HOME") or "")
    if host_home and (normalized == host_home or normalized.startswith(host_home.rstrip("/") + "/")):
        rel = normalized[len(host_home.rstrip("/")) :].lstrip("/")
        return "/host_home" if not rel else posixpath.join("/host_home", rel)
    host_project = _clean_posix_path(os.environ.get("AI_LOCAL_HOST_PROJECT_ROOT") or "")
    if host_project and (normalized == host_project or normalized.startswith(host_project.rstrip("/") + "/")):
        rel = normalized[len(host_project.rstrip("/")) :].lstrip("/")
        return "/workspace/ai-local" if not rel else posixpath.join("/workspace/ai-local", rel)
    return normalized


def _material_publish_destination_root(client_cwd: str) -> str:
    normalized = _clean_posix_path(client_cwd)
    host_project = _clean_posix_path(os.environ.get("AI_LOCAL_HOST_PROJECT_ROOT") or "")
    if host_project and (normalized == host_project or normalized.startswith(host_project.rstrip("/") + "/")):
        rel = normalized[len(host_project.rstrip("/")) :].lstrip("/")
        mapped = "/workspace/ai-local" if not rel else posixpath.join("/workspace/ai-local", rel)
        return posixpath.join(mapped, ".ai-local", "material_outputs")

    host_home = _clean_posix_path(os.environ.get("HOST_HOME_PREFIX") or os.environ.get("AI_LOCAL_HOST_HOME") or "")
    if host_home and (normalized == host_home or normalized.startswith(host_home.rstrip("/") + "/")):
        rel = normalized[len(host_home.rstrip("/")) :].lstrip("/")
        mapped = "/host_home" if not rel else posixpath.join("/host_home", rel)
        return posixpath.join(mapped, ".ai-local", "material_outputs")

    return "/workspace/ai-local/.local/material_outputs"


def _clean_posix_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    return posixpath.normpath(text)


@dataclass(frozen=True)
class ShadowTaskHandle:
    task_id: str
    trace_id: str
    request_id: str
    session_id: str
    mode: str
    run_id: str | None = None

    def context(self) -> AgenticContext:
        return AgenticContext(
            task_id=self.task_id,
            trace_id=self.trace_id,
            request_id=self.request_id,
            session_id=self.session_id,
            mode=self.mode,
        )


def shadow_enabled() -> bool:
    try:
        from orchestrator.config import get_settings

        cfg = get_settings().agentic_runtime
        return bool(cfg.enabled and cfg.shadow_ledger_enabled)
    except Exception:
        return False


def begin_shadow_task(
    *,
    goal: str,
    session_id: str,
    entrypoint: str,
    metadata: dict[str, Any] | None = None,
) -> ShadowTaskHandle | None:
    if not shadow_enabled():
        return None
    try:
        from orchestrator.agentic.store import get_agentic_store
        from orchestrator.config import get_settings

        cfg = get_settings().agentic_runtime
        request_id = uuid.uuid4().hex[:16]
        trace_id = uuid.uuid4().hex[:16]
        store = get_agentic_store()
        task = store.create_task(
            goal=goal,
            mode=cfg.default_mode,
            source=entrypoint,
            session_id=session_id,
            trace_id=trace_id,
            metadata=metadata or {},
            status=TaskStatus.RUNNING.value,
        )
        run_id = store.start_run(
            task_id=task.id,
            trace_id=trace_id,
            graph_run_id=None,
            entrypoint=entrypoint,
            metadata={"request_id": request_id},
        )
        return ShadowTaskHandle(
            task_id=task.id,
            trace_id=trace_id,
            request_id=request_id,
            session_id=session_id,
            mode=cfg.default_mode,
            run_id=run_id,
        )
    except Exception as exc:
        log.debug("Agentic shadow task creation skipped: %s", exc)
        return None


def complete_shadow_task(
    handle: ShadowTaskHandle | None,
    *,
    final_state: dict[str, Any],
    latency_ms: float,
    graph_tracer: Any = None,
) -> None:
    if handle is None:
        return
    try:
        from orchestrator.agentic.store import get_agentic_store

        store = get_agentic_store()
        graph_run_id = getattr(graph_tracer, "graph_run_id", None)
        if handle.run_id:
            store.finish_run(
                handle.run_id,
                status="completed",
                metadata={"graph_run_id": graph_run_id, "latency_ms": latency_ms},
            )
        record_graph_steps(handle, graph_tracer)
        result = final_state_summary(final_state, latency_ms=latency_ms, graph_run_id=graph_run_id)
        deliberation_result = _task_deliberation_result(store, handle.task_id, final_state)
        if deliberation_result.get("available"):
            result["agentic_deliberation"] = deliberation_result
            store.record_event(
                task_id=handle.task_id,
                event_type="agent.deliberation.integrated",
                actor="symbiont",
                payload=deliberation_result,
                trace_id=handle.trace_id,
            )
        store.update_task(handle.task_id, status=TaskStatus.COMPLETED.value, result=result)
        store.record_event(
            task_id=handle.task_id,
            event_type="pipeline.completed",
            actor="symbiont",
            payload=result,
            trace_id=handle.trace_id,
        )
    except Exception as exc:
        log.debug("Agentic shadow task completion skipped: %s", exc)


def fail_shadow_task(handle: ShadowTaskHandle | None, *, error: BaseException | str) -> None:
    if handle is None:
        return
    try:
        from orchestrator.agentic.store import get_agentic_store

        err = {
            "type": type(error).__name__ if not isinstance(error, str) else "Error",
            "message": str(error)[:1000],
        }
        store = get_agentic_store()
        if handle.run_id:
            store.finish_run(handle.run_id, status="failed", metadata={"error": err})
        store.update_task(handle.task_id, status=TaskStatus.FAILED.value, error=err)
        store.record_event(
            task_id=handle.task_id,
            event_type="pipeline.failed",
            actor="symbiont",
            payload=err,
            trace_id=handle.trace_id,
        )
    except Exception as exc:
        log.debug("Agentic shadow task failure recording skipped: %s", exc)


def cancel_task(task_id: str) -> bool:
    from orchestrator.agentic.store import get_agentic_store

    store = get_agentic_store()
    if store.get_task(task_id) is None:
        return False
    store.update_task(task_id, status=TaskStatus.CANCELLED.value)
    return True


def retry_task(task_id: str) -> dict[str, Any] | None:
    """Create a queued retry task with the same goal.

    Retries preserve the execution contract of the original task while dropping
    volatile runner/resource fields from the failed attempt.
    """
    from orchestrator.agentic.store import get_agentic_store

    store = get_agentic_store()
    original = store.get_task(task_id)
    if original is None:
        return None
    metadata = dict(original.metadata or {})
    for key in (
        "claimed_at",
        "cpu_only",
        "defer_reason",
        "defer_until",
        "lease_decision",
        "previous_status",
        "request_id",
        "resource_limits",
        "runner_run_id",
        "runner_worker_id",
    ):
        metadata.pop(key, None)
    metadata["retry_of"] = task_id
    task = store.create_task(
        goal=original.goal,
        mode=original.mode,
        source="retry",
        session_id=original.session_id,
        priority=original.priority,
        budget=original.budget,
        metadata=metadata,
        status=TaskStatus.QUEUED.value,
    )
    store.record_event(
        task_id=task.id,
        event_type="task.retry_created",
        actor="symbiont",
        payload={"retry_of": task_id},
        trace_id=task.trace_id,
    )
    return task.to_dict()


def resume_task(task_id: str, *, reason: str = "") -> bool:
    from orchestrator.agentic.store import get_agentic_store

    return get_agentic_store().resume_task(task_id, reason=reason)


def final_state_summary(final_state: dict[str, Any], *, latency_ms: float, graph_run_id: str | None = None) -> dict[str, Any]:
    intent = final_state.get("intent")
    complexity = final_state.get("complexity")
    context_blocks = final_state.get("context_blocks", []) or []
    return {
        "model_used": final_state.get("model_used", ""),
        "intent": intent.value if hasattr(intent, "value") else str(intent or ""),
        "complexity": complexity.value if hasattr(complexity, "value") else str(complexity or ""),
        "sources_used": [getattr(b, "source", "") for b in context_blocks],
        "tokens_used": final_state.get("tokens_used", 0),
        "latency_ms": round(latency_ms, 1),
        "graph_run_id": graph_run_id,
    }


def _task_deliberation_result(store: Any, task_id: str, final_state: dict[str, Any]) -> dict[str, Any]:
    existing = final_state.get("agentic_deliberation")
    if isinstance(existing, dict) and existing.get("available"):
        return existing
    try:
        from orchestrator.agentic.deliberation import summarize_agentic_deliberation

        return summarize_agentic_deliberation(store, task_id)
    except Exception:
        return {"available": False}


def record_graph_steps(handle: ShadowTaskHandle, graph_tracer: Any) -> None:
    if graph_tracer is None:
        return
    try:
        from orchestrator.agentic.store import get_agentic_store

        store = get_agentic_store()
        for node in getattr(graph_tracer, "completed_nodes", []):
            status = "completed" if node.get("success") else "failed"
            store.record_step(
                task_id=handle.task_id,
                run_id=handle.run_id,
                step_name=node.get("node_name", "unknown"),
                step_type=node.get("node_type", "graph_node"),
                status=status,
                duration_ms=node.get("duration_ms"),
                error=(
                    {
                        "type": node.get("error_type", ""),
                        "message": node.get("error_message", ""),
                    }
                    if not node.get("success")
                    else None
                ),
                metadata={
                    "graph_run_id": getattr(graph_tracer, "graph_run_id", ""),
                    "tokens_used": node.get("tokens_used", 0),
                    "input_keys": node.get("input_keys", []),
                    "output_keys": node.get("output_keys", []),
                },
            )
    except Exception as exc:
        log.debug("Agentic graph step recording skipped: %s", exc)
