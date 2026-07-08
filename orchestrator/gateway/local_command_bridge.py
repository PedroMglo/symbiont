"""Conversational bridge for safe local read-only command answers."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from orchestrator.agentic.context import get_agentic_context
from orchestrator.agentic.contracts import AgentDecision
from orchestrator.agentic.tools.command.mounts import build_context
from orchestrator.agentic.tools.command.service import CommandToolService
from orchestrator.capabilities.command_registry import CommandRegistryEntry, match_command_registry_query
from orchestrator.capabilities.local_command_shortcuts import local_command_shortcuts
from orchestrator.routing.path_intents import (
    is_explicit_extrator_processing_request,
    is_storage_request,
    needs_storage_context,
)

_STORAGE_MUTABLE_REQUEST_ACTION = "storage.mutable_request"


@dataclass(frozen=True)
class LocalCommandPlan:
    kind: str
    cwd: str
    commands: tuple[str, ...]
    action: str | None = None
    include_rag_status: bool = False



def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _word_or_phrase_hits(text: str, terms: tuple[str, ...]) -> int:
    hits = 0
    for term in terms:
        if " " in term:
            hits += int(term in text)
            continue
        pattern = rf"(?<!\w){re.escape(term)}(?!\w)"
        hits += int(re.search(pattern, text) is not None)
    return hits


def _resource_status_score(text: str) -> int:
    """Score how strongly a query asks for live resource telemetry."""
    signals = local_command_shortcuts().resource_status
    metric_hits = _word_or_phrase_hits(text, signals.metric_terms)
    if metric_hits == 0:
        return 0
    state_hits = _word_or_phrase_hits(text, signals.state_terms)
    return metric_hits * 2 + state_hits


def _looks_like_compound_prompt(query: str) -> bool:
    text = query or ""
    if len(text) < 1000:
        return False
    return text.count("\n") >= 8 or len(re.findall(r"(?m)^\s*[-*]\s+", text)) >= 5


def _should_defer_to_agentic_pipeline(query: str) -> bool:
    """Avoid local status shortcuts for explicit local artifact tasks."""

    text = query or ""
    q = " ".join(text.lower().split())
    has_path = re.search(r"(?<!\w)(?:/|~/?|\./|\.\./)", text) is not None
    if not has_path:
        return False
    local_artifact_terms = (
        "analisa",
        "analisar",
        "analyse",
        "analyze",
        "cria",
        "criar",
        "create",
        "gera",
        "gerar",
        "generate",
        "documentação",
        "documentacao",
        "documentation",
        "materializa",
        "materializar",
        "materialize",
        "publicar",
        "publish",
        "pasta",
        "folder",
        "subpasta",
        "subfolder",
        "ficheiro",
        "ficheiros",
        "files",
    )
    return any(term in q for term in local_artifact_terms)


def describe_local_command_route(
    query: str,
    *,
    client_cwd: str | None = None,
    client_files: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return a concise capability label if the local bridge will answer."""
    if _looks_like_compound_prompt(query) and not _query_wants_extrator_processing(query):
        return None
    if _should_defer_to_agentic_pipeline(query):
        return None
    registered_command = match_command_registry_query(query)
    if registered_command is not None:
        return registered_command.capability_id or registered_command.name.lstrip("/")
    if client_files and _query_wants_file_inspection(query):
        return "client_file.inspect"
    if _query_wants_extrator_processing(query):
        return "extrator.path"
    plan = plan_local_command(query, client_cwd=client_cwd)
    if plan is None:
        return None
    if plan.kind == "storage_policy_operation":
        return plan.action or "storage.policy"
    return local_command_shortcuts().route_labels.get(plan.kind, plan.kind)


def plan_local_command(query: str, *, client_cwd: str | None = None) -> LocalCommandPlan | None:
    q = " ".join((query or "").lower().split())
    if not q:
        return None
    if _should_defer_to_agentic_pipeline(query):
        return None

    shortcuts = local_command_shortcuts()
    terms = shortcuts.common
    negative_rag_instruction = _has_any(q, terms["negative_rag_terms"])
    intent_text = q
    for phrase in terms["negative_rag_terms"]:
        intent_text = intent_text.replace(phrase, "")

    cwd = _sandbox_cwd_for_client(client_cwd)
    wants_current_dir = _has_any(q, terms["current_dir_terms"])
    wants_folders = _has_any(q, terms["folder_terms"])
    wants_files = _has_any(q, terms["file_terms"])
    wants_count = _has_any(q, terms["count_terms"])
    wants_system = _has_any(q, shortcuts.resource_status.metric_terms)
    wants_git = _has_any(q, terms["git_terms"])
    wants_rag_status = _has_any(q, terms["rag_status_terms"])
    wants_agentic = _has_any(q, terms["agentic_terms"])
    wants_sandbox = _has_any(q, terms["sandbox_terms"])
    storage_operation = is_storage_request(query)
    wants_storage = needs_storage_context(query)
    resource_score = _resource_status_score(q)
    storage_status_only = wants_storage and not storage_operation
    storage_score = (5 if wants_storage else 0) + (4 if storage_operation else 0)
    multi_capability_probe = wants_storage and _has_any(intent_text, terms["multi_capability_terms"])
    wants_storage_boundary = (
        _has_any(q, terms["storage_boundary_question_terms"])
        and _has_any(q, terms["storage_boundary_owner_terms"])
        and _has_any(q, terms["storage_boundary_operation_terms"])
    )
    wants_alias_transport = _has_any(q, terms["alias_transport_subject_terms"]) and _has_any(
        q,
        terms["alias_transport_evidence_terms"],
    )
    wants_operational_status = _has_any(q, terms["operational_status_terms"])
    knowledge_only = (
        (re.search(r"\brag\b", intent_text) or _has_any(intent_text, terms["knowledge_terms"]))
        and not negative_rag_instruction
        and not any(
            (
                wants_current_dir,
                wants_folders,
                wants_files,
                wants_count,
                wants_system,
                wants_git,
                wants_storage,
                wants_rag_status,
                wants_agentic,
                wants_sandbox,
                wants_operational_status,
            )
        )
    )
    if knowledge_only:
        return None

    include_rag_status = wants_rag_status and not negative_rag_instruction
    if wants_operational_status and any((wants_storage, wants_system, wants_git, wants_rag_status, wants_agentic, wants_sandbox)):
        return LocalCommandPlan(
            kind="full_operational_status",
            cwd=cwd,
            commands=shortcuts.commands["cwd_operational_inventory"],
            include_rag_status=include_rag_status,
        )
    if wants_agentic and _has_any(q, terms["agentic_status_terms"]):
        return LocalCommandPlan(kind="agentic_overview", cwd=cwd, commands=())
    if wants_alias_transport:
        return LocalCommandPlan(kind="alias_transport", cwd=cwd, commands=())
    if wants_storage_boundary:
        return LocalCommandPlan(kind="storage_boundary", cwd=cwd, commands=())
    if resource_score > 0 and resource_score >= storage_score:
        return LocalCommandPlan(kind="system_status", cwd=cwd, commands=())
    if wants_storage and storage_operation:
        return LocalCommandPlan(
            kind="storage_policy_operation",
            cwd=cwd,
            commands=(),
            action=_STORAGE_MUTABLE_REQUEST_ACTION,
        )
    if multi_capability_probe and not storage_operation:
        return None
    if wants_storage and (storage_status_only or not storage_operation):
        return LocalCommandPlan(kind="storage_status", cwd=cwd, commands=())
    if wants_current_dir and wants_git:
        return LocalCommandPlan(
            kind="cwd_operational_inventory",
            cwd=cwd,
            commands=shortcuts.commands["cwd_operational_inventory"],
        )
    if wants_current_dir and wants_count and (wants_folders or wants_files):
        return LocalCommandPlan(
            kind="cwd_inventory",
            cwd=cwd,
            commands=shortcuts.commands["cwd_inventory"],
        )
    if wants_system and resource_score > 0:
        return LocalCommandPlan(kind="system_status", cwd=cwd, commands=())
    return None


async def maybe_answer_local_command(
    query: str,
    *,
    client_cwd: str | None = None,
    client_system: dict[str, Any] | None = None,
    client_files: list[dict[str, Any]] | None = None,
    feature_client: Any | None = None,
) -> str | None:
    if _looks_like_compound_prompt(query) and not _query_wants_extrator_processing(query):
        return None
    if _should_defer_to_agentic_pipeline(query):
        return None
    registered_command = match_command_registry_query(query)
    if registered_command is not None:
        return _format_registered_command(registered_command)
    extrator_response = await asyncio.to_thread(_maybe_extrator_processing_response, query, feature_client)
    if extrator_response is not None:
        return extrator_response
    if client_files and _query_wants_file_inspection(query):
        return _format_client_file_inspection(client_files)
    quick_response = _maybe_simple_conversation_response(query)
    if quick_response is not None:
        return quick_response
    rag_empty_response = await asyncio.to_thread(_maybe_empty_rag_only_response, query, feature_client)
    if rag_empty_response is not None:
        return rag_empty_response
    plan = plan_local_command(query, client_cwd=client_cwd)
    if plan is None:
        return None
    if plan.kind == "agentic_overview":
        return await asyncio.to_thread(_format_agentic_overview, client_system)
    if plan.kind == "alias_transport":
        return _format_alias_transport_boundary()
    if plan.kind == "storage_boundary":
        return _format_storage_boundary()
    if plan.kind == "system_status":
        return await asyncio.to_thread(_format_system_status, client_system)
    if plan.kind == "storage_status":
        return await asyncio.to_thread(_format_storage_status, feature_client, query)
    if plan.kind == "storage_policy_operation":
        return await asyncio.to_thread(_format_storage_policy_operation, plan.action or "storage.scan", query, feature_client)

    service = CommandToolService()
    ctx = get_agentic_context()
    try:
        session = await asyncio.to_thread(
            service.create_session,
            cwd=plan.cwd,
            task_id=ctx.task_id if ctx else None,
            trace_id=ctx.trace_id if ctx else None,
            metadata={"source": "terminal_alias.local_command_bridge", "plan": plan.kind},
        )
    except Exception:
        return None

    runs = []
    try:
        for command in plan.commands:
            run = await asyncio.to_thread(
                service.run_command,
                session["id"],
                command=command,
                cwd=plan.cwd,
                task_id=ctx.task_id if ctx else None,
                trace_id=ctx.trace_id if ctx else None,
            )
            runs.append(run)
            if plan.kind in {"cwd_operational_inventory", "full_operational_status"} and command.startswith("git ") and run.get("status") != "completed":
                continue
            if run.get("status") != "completed":
                return _format_failed_run(plan, run)
    finally:
        await asyncio.to_thread(service.close_session, session["id"], reason="local_command_bridge_completed")

    if plan.kind == "cwd_inventory":
        return _format_cwd_inventory(plan, runs)
    if plan.kind == "cwd_operational_inventory":
        return _format_cwd_operational_inventory(plan, runs)
    if plan.kind == "full_operational_status":
        agentic_status = await asyncio.to_thread(_format_agentic_status)
        system_status = await asyncio.to_thread(_format_system_status, client_system)
        storage_status = await asyncio.to_thread(
            _format_storage_status,
            feature_client,
            "verifica storage_guardian, mounts externos e prewarnings sem executar archive/restore",
        )
        rag_data = await asyncio.to_thread(_rag_status, feature_client) if plan.include_rag_status else None
        rag_status = str(rag_data.get("content") or "").strip() if rag_data else ""
        inventory = _format_cwd_operational_inventory(plan, runs)
        sections = [
            f"**Agentic**\n{agentic_status}",
            f"**Sistema**\n{system_status}",
            f"**Storage**\n{storage_status}",
        ]
        if rag_status:
            sections.append(f"**RAG**\n{rag_status}")
        sections.append(f"**Workspace**\n{inventory}")
        return "\n\n".join(sections)
    return None


def _format_registered_command(entry: CommandRegistryEntry) -> str:
    target = entry.target
    target_type = str(target.get("type") or "")
    if target_type == "api":
        target_ref = f"{target.get('method', 'GET')} {target.get('service') + ':' if target.get('service') else ''}{target.get('path')}"
    elif target_type == "make":
        target_ref = f"make {target.get('name')}"
    else:
        target_ref = str(entry.capability_id or target.get("capability_id") or entry.name)
    capability_line = f"\nCapability: `{entry.capability_id}`." if entry.capability_id else ""
    return (
        f"Comando `{entry.name}` registado.\n"
        f"Owner: `{entry.owner}`.\n"
        f"Target: `{target_ref}`.\n"
        f"Policy: `{entry.policy_action}`; read_only={entry.read_only}.{capability_line}\n"
        "Execução: nenhuma ação foi executada; este registry é seleção declarativa para o fluxo policy/owner."
    )


def _maybe_simple_conversation_response(query: str) -> str | None:
    q = " ".join((query or "").strip().lower().split())
    if not q:
        return None
    if re.search(r"\bresponde\s+s[oó]\s+com\s+ok\b", q) or re.search(r"\banswer\s+only\s+ok\b", q):
        return "OK"
    bare_greetings = {
        "ola",
        "olá",
        "oi",
        "hello",
        "hi",
        "bom dia",
        "boa tarde",
        "boa noite",
    }
    if q in bare_greetings:
        return "Olá. Estou pronto."
    return None


def _maybe_empty_rag_only_response(query: str, feature_client: Any | None = None) -> str | None:
    q = " ".join((query or "").strip().lower().split())
    if not q:
        return None
    mentions_rag = _word_or_phrase_hits(q, ("rag", "obsidian", "vault", "qdrant")) > 0
    asks_sources = any(term in q for term in ("exclusivamente", "apenas", "só", "so", "fontes", "índice", "indice"))
    if not (mentions_rag and asks_sources):
        return None
    status = _rag_status(feature_client)
    if status is None:
        return None
    total_chunks = _as_int(status.get("total_chunks")) or 0
    code_chunks = _as_int(status.get("code_chunks")) or 0
    if total_chunks > 0 or code_chunks > 0:
        return None
    return (
        "Não encontrei fontes para responder a partir do RAG/Obsidian nesta stack.\n\n"
        f"{status.get('content') or ''}\n\n"
        "Resposta: não vou inventar notas, documentos ou conclusões sem chunks indexados."
    )


def _rag_status(feature_client: Any | None) -> dict[str, Any] | None:
    if feature_client is None:
        return None
    try:
        response = feature_client.invoke_endpoint(
            "research",
            method="GET",
            path="/v1/research/status",
            timeout=5.0,
            policy_action="rag.status",
        )
    except Exception:
        return None
    if not getattr(response, "success", False):
        return None
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        return None
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return {
        "content": str(data.get("content") or ""),
        "total_chunks": metadata.get("total_chunks"),
        "code_chunks": metadata.get("code_chunks"),
    }


def _format_system_status(client_system: dict[str, Any] | None = None) -> str:
    client_snapshot = client_system if isinstance(client_system, dict) else {}
    process_count = _as_int(client_snapshot.get("process_count"))
    process_source = "host via `@`"
    if process_count is None:
        try:
            import psutil

            process_count = len(psutil.pids())
            process_source = "runtime do symbiont"
        except Exception:
            process_count = None

    runtime_snapshot: dict[str, Any] = {}
    try:
        from orchestrator.observability.resources import ResourceCollector

        runtime_snapshot = ResourceCollector(ollama_base_url="https://host.docker.internal:11434").snapshot()
    except Exception:
        runtime_snapshot = {}

    governor_snapshot: dict[str, Any] = {}
    try:
        from orchestrator.resource_governor.service import get_resource_governor_service

        resource_snapshot = get_resource_governor_service().snapshot()
        raw_snapshot = (
            resource_snapshot.model_dump(mode="json")
            if hasattr(resource_snapshot, "model_dump")
            else dict(resource_snapshot)
        )
        governor_snapshot = {
            "cpu_percent": raw_snapshot.get("cpu_percent"),
            "ram_available_mb": raw_snapshot.get("ram_available_mb"),
            "ram_total_mb": raw_snapshot.get("ram_total_mb"),
            "ram_percent": raw_snapshot.get("ram_percent"),
            "swap_used_mb": raw_snapshot.get("swap_used_mb"),
            "swap_percent": raw_snapshot.get("swap_percent"),
            "gpu_name": raw_snapshot.get("gpu_name"),
            "gpu_vram_free_mb": raw_snapshot.get("vram_free_mb"),
            "gpu_vram_total_mb": raw_snapshot.get("vram_total_mb"),
            "gpu_vram_used_mb": raw_snapshot.get("vram_used_mb"),
            "gpu_utilization_pct": raw_snapshot.get("gpu_utilization_pct"),
            "gpu_temperature_c": raw_snapshot.get("gpu_temperature_c"),
            "gpu_power_w": raw_snapshot.get("gpu_power_w"),
            "gpu_processes": raw_snapshot.get("gpu_processes"),
            "pressure_level": raw_snapshot.get("pressure_level"),
            "pressure_reasons": raw_snapshot.get("pressure_reasons"),
            "telemetry_incomplete": raw_snapshot.get("telemetry_incomplete"),
        }
    except Exception:
        governor_snapshot = {}

    snapshot = {
        **runtime_snapshot,
        **{k: v for k, v in governor_snapshot.items() if v is not None},
        **{k: v for k, v in client_snapshot.items() if v is not None},
    }

    _record_system_tool_call(process_count=process_count, snapshot=snapshot)

    client_ram_total = _as_int(client_snapshot.get("ram_total_mb"))
    client_ram_available = _as_int(client_snapshot.get("ram_available_mb"))
    if client_ram_total is not None and client_ram_available is not None:
        ram_total = client_ram_total
        ram_available = client_ram_available
        ram_used = _as_int(client_snapshot.get("ram_used_mb"))
        ram_percent = client_snapshot.get("ram_percent_used", client_snapshot.get("ram_percent"))
    else:
        ram_available = _as_int(snapshot.get("ram_available_mb"))
        ram_total = _as_int(snapshot.get("ram_total_mb"))
        ram_used = _as_int(snapshot.get("ram_used_mb"))
        ram_percent = snapshot.get("ram_percent_used", snapshot.get("ram_percent"))
    if ram_used is None and ram_total is not None and ram_available is not None:
        ram_used = max(0, ram_total - ram_available)
    swap_total = _as_int(snapshot.get("swap_total_mb"))
    swap_used = _as_int(snapshot.get("swap_used_mb"))
    cpu_percent = snapshot.get("cpu_percent")
    cpu_count = _as_int(snapshot.get("cpu_count"))
    gpu_name = snapshot.get("gpu_name")
    gpu_source = str(snapshot.get("gpu_detected_by") or "").strip()
    gpu_free = _as_int(snapshot.get("gpu_vram_free_mb"))
    gpu_total = _as_int(snapshot.get("gpu_vram_total_mb"))
    gpu_used = _as_int(snapshot.get("gpu_vram_used_mb"))
    gpu_utilization = snapshot.get("gpu_utilization_pct")
    gpu_temperature = snapshot.get("gpu_temperature_c")
    gpu_power = snapshot.get("gpu_power_w")
    gpu_processes = snapshot.get("gpu_processes")
    pressure_level = str(snapshot.get("pressure_level") or "").strip()
    pressure_reasons = snapshot.get("pressure_reasons")
    telemetry_incomplete = snapshot.get("telemetry_incomplete")
    if gpu_used is None and gpu_total is not None and gpu_free is not None:
        gpu_used = max(0, gpu_total - gpu_free)
    disk_total_gb = _as_float(snapshot.get("disk_total_gb"))
    disk_free_gb = _as_float(snapshot.get("disk_free_gb"))
    disk_used_gb = _as_float(snapshot.get("disk_used_gb"))
    disk_percent = snapshot.get("disk_percent_used")
    disk_mount = str(snapshot.get("disk_mount") or snapshot.get("disk_path") or "").strip()
    storage_total_gb = _as_float(snapshot.get("storage_disk_total_gb"))
    storage_free_gb = _as_float(snapshot.get("storage_disk_free_gb"))
    storage_used_gb = _as_float(snapshot.get("storage_disk_used_gb"))
    storage_percent = snapshot.get("storage_disk_percent_used")
    storage_path = str(snapshot.get("storage_disk_path") or "").strip()
    ollama_models = snapshot.get("ollama_models_loaded")
    ollama_vram = snapshot.get("ollama_vram_used_mb")

    lines = ["Recursos atuais:"]
    lines.append("- Modo: deterministic_status; llm_used=false.")
    if pressure_level:
        reasons = ", ".join(str(reason) for reason in pressure_reasons or []) or "sem reasons explícitos"
        lines.append(f"- Pressão Resource Governor: {pressure_level}; reasons: {reasons}.")
    if telemetry_incomplete is not None:
        state = "incompleta" if telemetry_incomplete else "completa"
        lines.append(f"- Telemetria Resource Governor: {state}.")
    if process_count is not None:
        lines.append(f"- Processos: {process_count} visíveis no {process_source}.")
    else:
        lines.append("- Processos: métrica indisponível no snapshot local e no runtime.")
    if ram_available is not None and ram_total is not None:
        lines.append(
            f"- RAM: {_format_mb(ram_used or 0)} usadas de {_format_mb(ram_total)}; "
            f"{_format_mb(ram_available)} disponíveis"
            + (f" ({ram_percent}% em uso)." if ram_percent is not None else ".")
        )
    else:
        lines.append("- RAM: métrica indisponível no runtime atual.")
    if swap_total is not None and swap_total > 0:
        lines.append(f"- Swap: {_format_mb(swap_used or 0)} usados de {_format_mb(swap_total)}.")
    if cpu_percent is not None:
        cpu_detail = f"{cpu_percent}% em uso"
        if cpu_count is not None:
            cpu_detail += f" em {cpu_count} CPU lógica(s)"
        lines.append(f"- CPU: {cpu_detail}.")
    if gpu_name:
        source_detail = f" via {gpu_source}" if gpu_source else ""
        if gpu_total is not None:
            gpu_detail = (
                f"- GPU: {gpu_name}; VRAM {_format_mb(gpu_used or 0)} usadas de {_format_mb(gpu_total)}"
                + (f" ({_format_mb(gpu_free)} livres)" if gpu_free is not None else "")
            )
            extras = []
            if gpu_utilization is not None:
                extras.append(f"utilização {gpu_utilization}%")
            if gpu_temperature is not None:
                extras.append(f"{gpu_temperature} C")
            if gpu_power is not None:
                extras.append(f"{gpu_power} W")
            if extras:
                gpu_detail += "; " + ", ".join(extras)
            lines.append(gpu_detail + f"{source_detail}.")
        else:
            lines.append(
                f"- GPU: {gpu_name} detetada{source_detail}; "
                "VRAM/utilização indisponíveis porque a métrica NVIDIA não respondeu."
            )
    else:
        gpu_note = "- GPU: não reportada pelo snapshot local nem exposta ao container do symbiont"
        if ollama_models is not None:
            gpu_note += f"; Ollama reporta {ollama_models} modelo(s) carregado(s)"
            if ollama_vram is not None:
                gpu_note += f" e {ollama_vram} MB de VRAM em uso"
        lines.append(gpu_note + ".")
    if ollama_models is not None:
        ollama_detail = f"- Ollama: {ollama_models} modelo(s) carregado(s)"
        if ollama_vram is not None:
            ollama_detail += f"; VRAM reportada pela API: {_format_mb(_as_int(ollama_vram) or 0)}"
        lines.append(ollama_detail + ".")
    if isinstance(gpu_processes, list) and gpu_processes:
        process_bits = []
        for proc in gpu_processes[:5]:
            if not isinstance(proc, dict):
                continue
            name = str(proc.get("name") or proc.get("process_name") or "process")
            pid = proc.get("pid")
            used = proc.get("used_memory_mb")
            detail = name
            if pid is not None:
                detail += f"[{pid}]"
            if used is not None:
                detail += f": {_format_mb(_as_int(used) or 0)}"
            process_bits.append(detail)
        if process_bits:
            lines.append("- GPU processos: " + "; ".join(process_bits) + ".")
    if disk_total_gb is not None and disk_free_gb is not None:
        if disk_used_gb is None:
            disk_used_gb = max(0.0, disk_total_gb - disk_free_gb)
        suffix = f" ({disk_percent}% em uso)" if disk_percent is not None else ""
        mount = f" em `{disk_mount}`" if disk_mount else ""
        lines.append(f"- Disco/SSD{mount}: {disk_used_gb:.1f} GB usados de {disk_total_gb:.1f} GB; {disk_free_gb:.1f} GB livres{suffix}.")
    if storage_total_gb is not None and storage_free_gb is not None:
        if storage_used_gb is None:
            storage_used_gb = max(0.0, storage_total_gb - storage_free_gb)
        suffix = f" ({storage_percent}% em uso)" if storage_percent is not None else ""
        path = f" em `{storage_path}`" if storage_path else ""
        lines.append(f"- Storage externo/SSD{path}: {storage_used_gb:.1f} GB usados de {storage_total_gb:.1f} GB; {storage_free_gb:.1f} GB livres{suffix}.")
    elif disk_total_gb is None:
        lines.append("- Disco/SSD: métrica indisponível no snapshot atual.")
    lines.append("")
    lines.append("Método: agentic local probe `system.status` via Resource Governor + snapshot `@` + runtime/Ollama; sem RAG e sem LLM.")
    return "\n".join(lines)


def _format_agentic_overview(client_system: dict[str, Any] | None = None) -> str:
    return "\n".join(
        [
            "- **Modo agentic vivo**: camada operacional persistente sobre o symbiont; usa ledger, runner, policy/approval, leases de recursos, event loop seguro e sandbox efémera para observar e agir sem perder controlo.",
            f"- **Ativo agora**: {_format_agentic_status()}",
            (
                "- **Limites atuais**: ações destrutivas continuam bloqueadas por approval; "
                "RAG pode estar acessível mas vazio nesta stack; storage pesado depende do SSD/mount externo; "
                f"{_format_system_status(client_system)}"
            ),
        ]
    )


def _format_alias_transport_boundary() -> str:
    return "\n".join(
        [
            "Owner: `orchestrator/cli`.",
            "Contrato: o alias `@` constrói o payload JSON localmente, grava o payload num ficheiro temporário e passa-o ao curl com `--data-binary @<payloadfile>`.",
            "Contrato: o header com credenciais também é gravado num ficheiro temporário e passado com `-H @<headerfile>`.",
            "argv: a linha de comando fica limitada a caminhos temporários e flags; segredos e corpo completo do pedido não são serializados diretamente nos argumentos do processo.",
            "Método: `orchestrator.cli.alias_transport`; llm_used=false; sem RAG.",
        ]
    )


def _format_storage_boundary() -> str:
    return "\n".join(
        [
            "Owner: `storage_guardian`.",
            "Fronteira: `storage_guardian` é o dono de writes geridos, criação/remoção de pastas geridas, archive, restore, manifestos, hashes e cadeia de custódia.",
            "Orchestrator: pode escolher rota, validar policy, propor `AgentDecision` e chamar o owner por API/dispatch; não deve implementar archive/restore/delete nem interpretar a semântica de storage.",
            "Execução: nenhuma ação de storage foi executada nesta resposta.",
            "Método: `storage.boundary`; llm_used=false; sem RAG.",
        ]
    )


def _format_agentic_status() -> str:
    try:
        from orchestrator.agentic.store import get_agentic_store
        from orchestrator.agentic.tools.command.service import get_command_tool_status
        from orchestrator.config import get_settings

        cfg = get_settings().agentic_runtime
        store = get_agentic_store()
        counts = store.task_status_counts()
        active = store.active_task_ids()
        command = get_command_tool_status()
        return (
            f"Runtime agentic: {'ativo' if cfg.enabled else 'desligado'}; "
            f"modo default `{cfg.default_mode}`; policy `{cfg.policy_mode}`; "
            f"runner {'ativo' if cfg.runner_enabled else 'desligado'}; "
            f"autonomous_safe {'ativo' if cfg.autonomous_safe_enabled else 'desligado'}. "
            f"Tasks ativas: {len(active)}; estados conhecidos: {counts}. "
            f"Command backend: `{command.get('backend')}`; sandbox image `{command.get('sandbox_image')}`; "
            f"safe_actions_only={command.get('safe_actions_only')}."
        )
    except Exception as exc:
        return f"Runtime agentic: estado indisponível ({str(exc)[:160]})."


def _format_storage_status(feature_client: Any | None, query: str) -> str | None:
    if feature_client is None:
        return None
    safe_query = query or "verifica storage_guardian, mounts externos e prewarnings sem executar archive/restore"
    try:
        response = feature_client.query_source(
            "storage",
            query=safe_query,
            budget_tokens=1200,
            timeout=5.0,
            metadata={"source": "terminal_alias.local_command_bridge", "mode": "storage_status"},
        )
    except Exception as exc:
        return f"storage_guardian status indisponível via dispatch: {str(exc)[:200]}"
    if getattr(response, "success", False) and str(getattr(response, "content", "") or "").strip():
        return str(response.content).strip()
    error = str(getattr(response, "error", "") or "sem conteúdo de status").strip()
    return f"storage_guardian status indisponível via dispatch: {error[:200]}"


def _format_storage_policy_operation(action: str, query: str, feature_client: Any | None) -> str:
    action = _STORAGE_MUTABLE_REQUEST_ACTION
    agent_decision = _storage_mutable_request_decision(query)
    payload = {
        "query": query,
        "agent_decision": agent_decision.model_dump(mode="json"),
        "dry_run_result": {
            "would_execute": False,
            "reason": "Typed storage control proposal only; no storage mutation was executed.",
        },
    }
    try:
        from orchestrator.agentic.policy import audit_policy_check

        policy_decision = audit_policy_check(action, payload=payload, component="local_command_bridge.storage")
    except Exception:
        policy_decision = None

    approval_id = _find_current_approval_id(action, payload)
    status_text = _format_storage_status(
        feature_client,
        "verifica storage_guardian, mounts externos e prewarnings sem executar archive/restore",
    ) or "storage_guardian status indisponível via dispatch."
    if policy_decision is None:
        policy_line = f"Policy: `{action}` não pôde ser avaliada; operação não executada."
    else:
        policy_line = (
            f"Policy: ação `{policy_decision.action}` classificada como `{policy_decision.risk_level}`; "
            f"decisão `{policy_decision.decision}`; motivo: {policy_decision.reason}."
        )
    approval_line = (
        f"Approval pendente: `{approval_id}`."
        if approval_id
        else "Approval pendente: não criado/indisponível neste contexto."
    )
    return (
        f"{status_text}\n\n"
        f"AgentDecision: proposta `{agent_decision.status}` com ação `{agent_decision.proposed_actions[0].type}` "
        f"`{agent_decision.proposed_actions[0].action_id}`; endpoint do dono `storage_guardian:/internal/storage/query`.\n"
        f"{policy_line}\n"
        f"{approval_line}\n"
        "Execução: nenhuma ação de storage foi executada; restore/archive/delete/promote continuam bloqueados sem approval explícito por payload."
    )


def _storage_mutable_request_decision(query: str) -> AgentDecision:
    ctx = get_agentic_context()
    task_id = ctx.task_id if ctx is not None else "local-command-bridge"
    trace_id = ctx.trace_id if ctx is not None else "local-command-bridge"
    input_state_hash = _current_agent_state_hash(task_id) or sha256(
        f"local-command-bridge:{query}".encode("utf-8")
    ).hexdigest()
    return AgentDecision(
        task_id=task_id,
        trace_id=trace_id,
        input_state_hash=input_state_hash,
        status="waiting_for_user",
        confidence=0.9,
        new_facts=[
            "O pedido parece requerer uma operação mutável de storage.",
            "O gateway não classificou a operação concreta; a semântica pertence ao storage_guardian.",
        ],
        proposed_actions=[
            {
                "type": "api_call",
                "action_id": "storage-control-request",
                "method": "POST",
                "endpoint": "storage_guardian:/internal/storage/query",
                "payload": {
                    "query": query,
                    "metadata": {
                        "source": "terminal_alias.local_command_bridge",
                        "requires_explicit_approval": True,
                    },
                },
                "expected_effect": "destructive",
                "reason": (
                    "Encaminhar o pedido para o storage_guardian apenas depois de "
                    "PolicyEngine e approval explícito autorizarem a operação."
                ),
                "metadata": {
                    "owner": "storage_guardian",
                    "policy_action": _STORAGE_MUTABLE_REQUEST_ACTION,
                    "dispatch_source": "storage",
                },
            }
        ],
        questions_for_user=[
            "Confirmas explicitamente a operação de storage depois de rever o dry-run do storage_guardian?"
        ],
        reasoning_summary=(
            "Pedido de storage potencialmente mutável convertido em proposta tipada; "
            "o gateway não executa nem interpreta a operação concreta."
        ),
        metadata={"origin": "local_command_bridge", "owner": "storage_guardian"},
    )


def _current_agent_state_hash(task_id: str) -> str | None:
    try:
        from orchestrator.agentic.store import get_agentic_store

        snapshot = get_agentic_store().current_agent_state(task_id)
    except Exception:
        return None
    if not isinstance(snapshot, dict):
        return None
    state_hash = str(snapshot.get("state_hash") or "")
    return state_hash if len(state_hash) == 64 else None


def _query_wants_file_inspection(query: str) -> bool:
    raw = query or ""
    q = " ".join((query or "").lower().split())
    if not q:
        return False
    if any(
        term in q
        for term in (
            "resolve",
            "resolver",
            "resolva",
            "analisa",
            "analisar",
            "investiga",
            "investigar",
            "diagnostica",
            "diagnosticar",
            "cenário",
            "cenario",
            "scenario",
            "lab",
            "task.md",
        )
    ):
        return False
    if any(term in q for term in ("transcreve", "transcrever", "transcribe")):
        return False
    if any(
        term in q
        for term in (
            "extrai",
            "extrair",
            "extract",
            "processa",
            "processar",
            "process",
            "converte",
            "converter",
            "convert",
            "conversion",
            "job",
            "para rag",
            "rag bundle",
        )
    ):
        return False
    if not _query_has_explicit_file_path(raw):
        return False
    return any(
        term in q
        for term in (
            "extrator",
            "inspeciona",
            "inspecionar",
            "inspect",
            "preview",
            "pré-visualiza",
            "pre-visualiza",
            "metadados",
            "metadata",
            "colunas",
            "csv",
            "amostra",
            "tipo de ficheiro",
            "tipo do ficheiro",
            "file type",
            "quantas linhas",
            "line count",
        )
    )


def _user_query_without_alias_context(query: str) -> str:
    text = query or ""
    markers = (
        "[Contexto local read-only recolhido pelo alias @",
        "[Local read-only context collected by alias @",
    )
    cut = len(text)
    for marker in markers:
        idx = text.find(marker)
        if idx >= 0:
            cut = min(cut, idx)
    return text[:cut]


def _query_wants_extrator_processing(query: str) -> bool:
    user_query = _user_query_without_alias_context(query)
    return is_explicit_extrator_processing_request(user_query)


def _query_has_explicit_file_path(query: str) -> bool:
    return bool(re.search(r'(^|[\s"\'])(?:/|~/|\./|\../|[A-Za-z]:[\\/])', query or ""))


def _maybe_extrator_processing_response(query: str, feature_client: Any | None = None) -> str | None:
    if not _query_wants_extrator_processing(query):
        return None
    if feature_client is None:
        return None

    user_query = _user_query_without_alias_context(query)
    metadata = {
        "source": "terminal_alias.local_command_bridge",
    }

    try:
        response = feature_client.query_source(
            "extrator",
            query=user_query,
            budget_tokens=0,
            timeout=30.0,
            metadata=metadata,
        )
    except Exception as exc:
        return (
            "Extrator não conseguiu criar/concluir o job.\n"
            f"error: {str(exc)[:300]}"
        )

    if not getattr(response, "success", False):
        return (
            "Extrator não conseguiu criar/concluir o job.\n"
            f"error: {str(getattr(response, 'error', '') or 'unknown')[:300]}"
        )
    content = str(getattr(response, "content", "") or "").strip()
    if content:
        return content
    return "Extrator concluiu o pedido, mas não devolveu conteúdo."


def _format_client_file_inspection(client_files: list[dict[str, Any]]) -> str | None:
    files = [item for item in client_files if isinstance(item, dict)]
    if not files:
        return None
    file_info = files[0]
    _record_client_file_tool_call(file_info)
    path = str(file_info.get("path") or "(sem path)")
    exists = bool(file_info.get("exists"))
    if not exists:
        return (
            f"O ficheiro `{path}` não existe ou não está acessível no host onde o `@` correu.\n\n"
            "Método usado: `client_file.inspect` read-only via alias `@`; sem RAG."
        )
    suffix = str(file_info.get("suffix") or "").lower() or "(sem extensão)"
    size = _format_bytes(_as_int(file_info.get("size_bytes")) or 0)
    lines = [
        f"Ficheiro `{path}`: acessível no host via `@`.",
        f"Tipo/extensão: `{suffix}`; tamanho: {size}.",
    ]
    line_count = _as_int(file_info.get("line_count"))
    if line_count is not None:
        lines.append(f"Linhas detetadas: {line_count}.")
    columns = file_info.get("csv_columns")
    if isinstance(columns, list) and columns:
        rendered_columns = ", ".join(f"`{str(col)}`" for col in columns[:30])
        if len(columns) > 30:
            rendered_columns += f", ... (+{len(columns) - 30})"
        lines.append(f"Colunas CSV: {rendered_columns}.")
    sample_row = file_info.get("csv_sample_row")
    if isinstance(sample_row, list) and sample_row:
        rendered_sample = ", ".join(str(value) for value in sample_row[:8])
        if len(sample_row) > 8:
            rendered_sample += ", ..."
        lines.append(f"Primeira linha de dados: {rendered_sample}.")
    if not file_info.get("preview") and suffix not in {".csv", ".txt", ".md", ".json", ".yaml", ".yml", ".sql", ".py", ".r", ".ipynb", ".toml", ".ini", ".log"}:
        lines.append("Conteúdo não pré-visualizado porque o tipo não é textual simples.")
    lines.append(
        "Método usado: `client_file.inspect` read-only via alias `@`; sem RAG. "
        "O serviço extrator não foi usado para este path do host a menos que o diretório esteja montado no container."
    )
    return "\n\n".join(lines)


def _record_client_file_tool_call(file_info: dict[str, Any]) -> None:
    ctx = get_agentic_context()
    if ctx is None:
        return
    try:
        from orchestrator.agentic.store import get_agentic_store

        get_agentic_store().record_tool_call(
            task_id=ctx.task_id,
            tool_name="client_file.inspect",
            risk_level="low",
            status="completed" if file_info.get("exists") else "not_found",
            input_payload={"path": file_info.get("path")},
            output_payload={k: v for k, v in file_info.items() if k != "preview"},
            requires_approval=False,
            metadata={"component": "local_command_bridge"},
        )
    except Exception:
        pass


def _format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024


def _find_current_approval_id(action: str, payload: dict[str, Any]) -> str | None:
    ctx = get_agentic_context()
    if ctx is None:
        return None
    try:
        from orchestrator.agentic.models import ApprovalStatus
        from orchestrator.agentic.store import get_agentic_store

        approval = get_agentic_store().find_approval_for_payload(
            task_id=ctx.task_id,
            action=action,
            payload=payload,
            statuses=(ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value),
        )
        return str(approval.get("id")) if approval else None
    except Exception:
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_mb(value: int) -> str:
    if value >= 1024:
        return f"{value / 1024:.1f} GB"
    return f"{value} MB"


def _record_system_tool_call(*, process_count: int | None, snapshot: dict[str, Any]) -> None:
    ctx = get_agentic_context()
    if ctx is None:
        return
    try:
        from orchestrator.agentic.store import get_agentic_store

        get_agentic_store().record_tool_call(
            task_id=ctx.task_id,
            tool_name="system.status",
            risk_level="low",
            status="completed",
            input_payload={"probe": "local_system_status"},
            output_payload={"process_count": process_count, **snapshot},
            requires_approval=False,
            metadata={"component": "local_command_bridge"},
        )
    except Exception:
        pass


def _sandbox_cwd_for_client(client_cwd: str | None) -> str:
    if not client_cwd:
        return "/workspace/project"
    if "\x00" in client_cwd:
        return "/workspace/project"
    try:
        client_path = Path(client_cwd).expanduser().resolve()
    except OSError:
        return "/workspace/project"

    context = build_context()
    for mount in context.mounts:
        candidates = [mount.docker_source_path, mount.source_path]
        for root in candidates:
            if root is None:
                continue
            try:
                root_path = Path(root).expanduser().resolve()
                relative = client_path.relative_to(root_path)
            except (OSError, ValueError):
                continue
            suffix = str(relative)
            return mount.sandbox_path if suffix == "." else f"{mount.sandbox_path.rstrip('/')}/{suffix}"
    return "/workspace/project"


def _stdout_lines(run: dict) -> list[str]:
    text = str(run.get("stdout_preview") or run.get("stdout") or "")
    return [line.strip().removeprefix("./") for line in text.splitlines() if line.strip()]


def _command_backend_label() -> str:
    try:
        from orchestrator.config import get_settings

        return str(get_settings().agentic_runtime.command_tool_backend or "workspace_execution")
    except Exception:
        return "workspace_execution"


def _format_cwd_inventory(plan: LocalCommandPlan, runs: list[dict]) -> str:
    pwd = (_stdout_lines(runs[0]) or [plan.cwd])[0]
    folders = _stdout_lines(runs[1])
    files = _stdout_lines(runs[2])
    folder_names = ", ".join(folders) if folders else "(nenhuma)"
    backend = _command_backend_label()
    return (
        f"No diretório `{pwd}`, encontrei {len(folders)} pastas diretas e {len(files)} ficheiros diretos.\n\n"
        f"Pastas diretas: {folder_names}\n\n"
        f"Método usado: agentic command tool via `{backend}`, com comandos read-only "
        "`find . -mindepth 1 -maxdepth 1 -type d` e `find . -mindepth 1 -maxdepth 1 -type f`."
    )


def _format_cwd_operational_inventory(plan: LocalCommandPlan, runs: list[dict]) -> str:
    pwd = (_stdout_lines(runs[0]) or [plan.cwd])[0]
    folders = _stdout_lines(runs[1])
    files = _stdout_lines(runs[2])
    git_inside = (_stdout_lines(runs[3]) or ["false"])[0] == "true"
    branch = (_stdout_lines(runs[4]) or [""])[0] if len(runs) > 4 else ""
    status_lines = _stdout_lines(runs[5]) if len(runs) > 5 else []
    if git_inside:
        git_summary = (
            f"Repo Git: sim; branch `{branch or 'detached'}`; "
            f"{len(status_lines)} ficheiro(s) com alterações no working tree."
        )
    else:
        git_summary = "Repo Git: não detetado neste diretório."
    folder_names = ", ".join(folders[:30]) if folders else "(nenhuma)"
    if len(folders) > 30:
        folder_names += f", ... (+{len(folders) - 30})"
    backend = _command_backend_label()
    return (
        f"Inventário de `{pwd}`: {len(folders)} pastas diretas e {len(files)} ficheiros diretos.\n\n"
        f"Pastas diretas: {folder_names}\n\n"
        f"{git_summary}\n\n"
        f"Método usado: agentic command tool via `{backend}`, com comandos read-only "
        "`find`, `git rev-parse`, `git branch --show-current` e `git status --short`."
    )


def _format_failed_run(plan: LocalCommandPlan, run: dict) -> str:
    return (
        "A sandbox de comandos foi acionada, mas o comando read-only falhou.\n\n"
        f"Plano: `{plan.kind}`\n"
        f"Comando: `{run.get('command')}`\n"
        f"Estado: `{run.get('status')}`\n"
        f"Erro: `{run.get('error')}`"
    )
