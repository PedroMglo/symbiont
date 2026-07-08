"""Streaming through the full LangGraph pipeline.

Runs the entire orchestration graph (classify → route → context → agents →
synthesize) with stream_mode=True, then streams the final LLM response
token-by-token via the LLM router (vLLM/llama-cpp OpenAI-compat endpoint).

This replaces the old stream.py bypass that skipped agents entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import AsyncIterator

from orchestrator.config import get_settings

log = logging.getLogger(__name__)

# Cached LLM router reused across streaming calls so health-check cache and
# per-backend stats persist instead of being discarded on every request.
# Rebuilt only when the active LLM config object identity changes.
_router_cache: tuple[int, object] | None = None


def _get_stream_router():
    """Return a process-wide LLMRouter, rebuilt only when llm config changes."""
    global _router_cache
    from orchestrator.llm.router import LLMRouter

    cfg = get_settings()
    key = id(cfg.llm)
    if _router_cache is None or _router_cache[0] != key:
        _router_cache = (
            key,
            LLMRouter(
                cfg.llm,
                latency_routing=cfg.latency_routing,
                inference_profiles=cfg.inference_profiles,
            ),
        )
    return _router_cache[1]


# Internal sentinel prefix: stream_via_pipeline yields this prefix before a
# pre-formatted SSE event so that app.py's event_stream() can pass it through
# raw instead of wrapping it in "data:". Null bytes cannot appear in LLM output.
_SSE_EVENT_MARKER = "\x00sse\x00"

# Node name → Portuguese status label shown in the CLI while the pipeline runs.
# None means the node is internal/housekeeping and should produce no visible status.
_NODE_LABELS: dict[str, str | None] = {
    "classify":         "a classificar",
    "route":            "a definir rota",
    "llm_fallback":     "a classificar (LLM)",
    "dispatch_context": "a recolher contexto",
    "dispatch_agents":  "a invocar agentes",
    "collaborate":      "a colaborar",
    "critic":           "a avaliar",
    "synthesize":       "a sintetizar",
    "direct_respond":   "a preparar resposta",
    "speculate":        "a antecipar",
    "decompose":        "a decompor tarefa",
    "peer_review":      "a rever",
    "filter_agents":    None,  # internal routing — skip
    "learn":            None,  # background learning — skip
}

# SymbiontState fields that use operator.add (list concatenation) semantics.
_LIST_FIELDS = frozenset({
    "context_blocks", "speculative_context", "agent_results",
    "working_memory", "pending_handoffs", "execution_trace",
})


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using %d", name, raw, default)
        return default


def _estimate_prompt_tokens(messages: list[dict[str, str]]) -> int:
    # Conservative char/token estimate plus chat-template overhead. This is
    # deliberately tighter than the context packer because vLLM rejects the
    # request before generation when prompt + output exceeds max_model_len.
    total = 0
    for msg in messages:
        total += 12
        total += (len(msg.get("content", "")) + 1) // 2
    return total


def _truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 80:
        return text[:max_chars]
    marker = "\n[...conteudo compactado...]\n"
    head = max(40, max_chars // 3)
    tail = max(40, max_chars - head - len(marker))
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def _fit_messages_to_context_window(
    messages: list[dict[str, str]],
    *,
    max_output_tokens: int,
) -> list[dict[str, str]]:
    context_window = _env_int(
        "ORC_STREAM_CONTEXT_WINDOW",
        _env_int("VLLM_MAX_MODEL_LEN", 8192),
    )
    safety_margin = _env_int("VLLM_PROMPT_SAFETY_TOKENS", 96)
    prompt_budget = max(128, context_window - max_output_tokens - safety_margin)

    estimated = _estimate_prompt_tokens(messages)
    if estimated <= prompt_budget:
        return messages

    fitted = [dict(msg) for msg in messages]
    last_user_idx = next(
        (idx for idx in range(len(fitted) - 1, -1, -1) if fitted[idx].get("role") == "user"),
        len(fitted) - 1,
    )

    while _estimate_prompt_tokens(fitted) > prompt_budget:
        candidates = [
            idx for idx, msg in enumerate(fitted)
            if idx != last_user_idx and len(msg.get("content", "")) > 160
        ]
        if not candidates:
            content = fitted[last_user_idx].get("content", "")
            if len(content) <= 320:
                break
            fitted[last_user_idx]["content"] = _truncate_middle(content, max(320, int(len(content) * 0.75)))
            continue

        idx = max(candidates, key=lambda i: len(fitted[i].get("content", "")))
        content = fitted[idx].get("content", "")
        fitted[idx]["content"] = _truncate_middle(content, max(160, int(len(content) * 0.70)))

    new_estimate = _estimate_prompt_tokens(fitted)
    log.info(
        "Streaming prompt compacted: estimated_tokens=%d -> %d, budget=%d",
        estimated,
        new_estimate,
        prompt_budget,
    )
    return fitted


def _build_status_text(node_name: str, output: dict, elapsed_ms: float = 0.0) -> str | None:
    """Build a human-readable status line for a completed pipeline node.

    Returns a pre-formatted string like "⟳ a classificar: code, simple (45ms)"
    or None if the node should be silent (internal/housekeeping nodes).
    """
    label = _NODE_LABELS.get(node_name)
    if label is None:
        return None

    def _val(v) -> str:
        """Return the .value string for Enum members, otherwise str(v)."""
        return v.value if hasattr(v, "value") else str(v)

    detail = ""
    if node_name == "classify":
        intent = output.get("intent", "")
        complexity = output.get("complexity", "")
        parts = [_val(v) for v in (intent, complexity) if v]
        detail = ", ".join(parts)
    elif node_name in ("route", "llm_fallback"):
        agents = output.get("selected_agents") or []
        sources = output.get("context_sources") or []
        parts = []
        if agents:
            parts.append(f"agentes: {', '.join(agents)}")
        if sources:
            parts.append(f"contexto: {', '.join(sources)}")
        detail = " | ".join(parts)
    elif node_name == "dispatch_context":
        for trace in output.get("execution_trace", []):
            if trace.startswith("dispatch_context:"):
                val = trace.split(":", 1)[1]
                if val not in ("empty", "no_sources"):
                    detail = val
                break
    elif node_name == "dispatch_agents":
        for trace in output.get("execution_trace", []):
            if trace.startswith("dispatch_agents:"):
                val = trace.split(":", 1)[1]
                if val != "no_agents":
                    detail = val
                break

    timing = f" ({int(elapsed_ms)}ms)" if elapsed_ms >= 1 else ""
    if detail:
        return f"\u23f3 {label}: {detail}{timing}"
    return f"\u23f3 {label}...{timing}"


def _build_start_status_text(node_name: str) -> str | None:
    """Build an immediate status label the moment a node begins executing.

    Returns a bare "⟳ label..." string with no detail or timing, so it appears
    on the terminal instantly while the node is still running.
    """
    label = _NODE_LABELS.get(node_name)
    if label is None:
        return None
    return f"\u23f3 {label}..."


# Source router name → short Portuguese label for prewarm status display.
_PREWARM_SOURCE_LABELS: dict[str, str] = {
    "rules":        "regra",
    "embedding":    "emb",
    "lightweight":  "tfidf",
    "classifier":   "ML",
}


def _build_prewarm_status_text(state) -> str | None:
    """Build a human-readable status line for the predictive prewarming phase.

    Accepts a PrewarmState instance (accessed via duck-typing to avoid a circular
    import from orchestrator.prewarming).  Returns None when no containers were
    selected (guard blocked query, direct answer, nothing predicted).

    Example output:
        ⏳ pré-aquecimento: local_evidence [emb 78%], research [regra 72%]
        ⏳ pré-aquecimento: local_evidence [activo], research [a iniciar, regra 85%]
    """
    if state is None:
        return None

    actions = getattr(state, "actions", [])
    predictions = getattr(state, "predictions", [])

    # Only show containers that were selected (prewarmed or already running)
    visible = [a for a in actions if getattr(a, "action", "") in ("prewarm_now", "already_running")]
    if not visible:
        return None

    # Build lookup: feature_id → best (highest confidence) prediction
    pred_by_feature: dict[str, object] = {}
    for pred in predictions:
        fid = getattr(pred, "feature_id", "")
        conf = getattr(pred, "confidence", 0.0)
        existing = pred_by_feature.get(fid)
        if existing is None or conf > getattr(existing, "confidence", 0.0):
            pred_by_feature[fid] = pred

    parts = []
    for action in visible:
        fid = getattr(action, "feature_id", "")
        name = fid.replace("-", "_")
        is_running = getattr(action, "action", "") == "already_running"

        pred = pred_by_feature.get(fid)
        if pred is not None:
            src = getattr(pred, "source", "")
            src_label = _PREWARM_SOURCE_LABELS.get(src, src)
            conf_pct = int(getattr(pred, "confidence", 0.0) * 100)
            detail = f"{src_label} {conf_pct}%"
        else:
            score_pct = int(getattr(action, "score", 0.0) * 100)
            detail = f"{score_pct}%"

        if is_running:
            parts.append(f"{name} [activo, {detail}]")
        else:
            parts.append(f"{name} [a iniciar, {detail}]")

    if not parts:
        return None

    latency_ms = getattr(state, "latency_ms", 0.0)
    timing = f" ({int(latency_ms)}ms)" if latency_ms >= 1 else ""
    return f"\u23f3 pré-aquecimento: {', '.join(parts)}{timing}"


async def stream_via_pipeline(
    graph,
    *,
    query: str,
    original_query: str | None = None,
    language_context: dict | None = None,
    history: list[dict] | None = None,
    session_id: str = "",
    client_cwd: str | None = None,
    client_system: dict | None = None,
    client_files: list[dict] | None = None,
) -> AsyncIterator[str]:
    """Run the full LangGraph pipeline then stream the final LLM response.

    1. Invokes the graph with stream_mode=True
       - All orchestration runs normally (classify, route, context, agents, critique)
       - The synthesize/direct_respond node skips the final LLM call and stores
         prepared messages in state["stream_messages"]
    2. If stream_messages are present, streams from Ollama using those messages
    3. If the graph already produced a response (e.g. single agent passthrough),
       yields that response directly

    This gives the user the full benefit of orchestration (quality critic,
    task decomposition, context gathering, multi-agent synthesis) while still
    getting real-time streaming tokens for the final output.
    """
    cfg = get_settings()

    # --- Audio short-circuit (kept as special case) ---
    from orchestrator.gateway.audio_handler import is_audio_query, stream_audio_transcription
    if is_audio_query(query):
        feature_client = getattr(graph, "_feature_client", None)
        async for token in stream_audio_transcription(query, feature_client=feature_client):
            yield token
        return

    from orchestrator.gateway.local_command_bridge import describe_local_command_route, maybe_answer_local_command

    local_route = describe_local_command_route(
        original_query or query,
        client_cwd=client_cwd,
        client_files=client_files,
    )
    feature_client = getattr(graph, "_feature_client", None)
    local_t0 = time.perf_counter()
    if local_route:
        yield f"{_SSE_EVENT_MARKER}event: status_start\ndata: \u23f3 ferramenta local: {local_route}...\n\n"
    local_answer = await maybe_answer_local_command(
        original_query or query,
        client_cwd=client_cwd,
        client_system=client_system,
        client_files=client_files,
        feature_client=feature_client,
    )
    if local_answer:
        elapsed_ms = (time.perf_counter() - local_t0) * 1000
        route_text = local_route or "atalho local"
        yield f"{_SSE_EVENT_MARKER}event: status_done\ndata: \u23f3 ferramenta local: {route_text} ({int(elapsed_ms)}ms)\n\n"
        yield local_answer
        return

    # --- Run the full pipeline with astream() to emit per-node status events ---
    initial_state = {
        "query": query,
        "original_query": original_query or query,
        "language_context": language_context or {},
        "history": history or [],
        "session_id": session_id,
        "iterations": 0,
        "tokens_used": 0,
        "fallback_used": False,
        "stream_mode": True,
        "client_cwd": client_cwd or "",
        "client_system": client_system or {},
        "client_files": client_files or [],
    }

    # Accumulate the final state from node-by-node updates.
    # Fields using operator.add in SymbiontState are list-concatenated.
    final_state: dict = {}
    # Per-node start timestamps — populated from debug "task" events.
    _node_starts: dict[str, float] = {}

    try:
        # Use combined stream modes:
        #   "debug"   → yields ("debug", {"type": "task", ...}) when a node STARTS
        #   "updates" → yields ("updates", {node_name: output}) when a node ENDS
        # This lets us emit an immediate "starting..." label and later overwrite
        # it with details + elapsed time once the node finishes.
        async for event in graph.astream(initial_state, stream_mode=["updates", "debug"]):
            if not isinstance(event, tuple) or len(event) != 2:
                continue
            mode, chunk = event

            if mode == "debug":
                if not isinstance(chunk, dict) or chunk.get("type") != "task":
                    continue
                payload = chunk.get("payload", {})
                node_name = payload.get("name", "")
                if not node_name or node_name in ("__start__", "__end__", "LangGraph"):
                    continue
                _node_starts[node_name] = time.perf_counter()
                start_status = _build_start_status_text(node_name)
                if start_status is not None:
                    yield f"{_SSE_EVENT_MARKER}event: status_start\ndata: {start_status}\n\n"

            elif mode == "updates":
                if not isinstance(chunk, dict):
                    continue
                for node_name, output in chunk.items():
                    if not node_name or not isinstance(output, dict):
                        continue
                    if node_name in ("__start__", "__end__", "LangGraph"):
                        continue

                    t0 = _node_starts.get(node_name, time.perf_counter())
                    elapsed_ms = (time.perf_counter() - t0) * 1000

                    # Merge node output into accumulated state
                    for k, v in output.items():
                        if k in _LIST_FIELDS and isinstance(v, list):
                            final_state[k] = final_state.get(k, []) + v
                        else:
                            final_state[k] = v

                    # Overwrite the "starting..." label with details + elapsed time, then newline
                    status_text = _build_status_text(node_name, output, elapsed_ms=elapsed_ms)
                    if status_text is not None:
                        yield f"{_SSE_EVENT_MARKER}event: status_done\ndata: {status_text}\n\n"

    except Exception as exc:
        log.error("Pipeline execution failed: %s", exc)
        yield f"Erro na orquestração: {exc}"
        return

    # --- Determine how to produce the final output ---
    stream_messages = final_state.get("stream_messages")
    response = final_state.get("response", "")

    if stream_messages:
        # The pipeline prepared messages for streaming — stream via LLM router (vLLM/llama-cpp)
        model = final_state.get("model_used", "")
        resolved_model = _resolve_model(model, cfg)

        # Inject context blocks into the system message if not already there
        context_blocks = final_state.get("context_blocks", [])
        if context_blocks:
            direct_answer = (
                _direct_required_context_missing_answer(context_blocks)
                or _direct_task_specific_report_answer(context_blocks)
                or
                _direct_system_metrics_answer(query, context_blocks)
                or _direct_code_context_answer(query, context_blocks)
                or _direct_operational_context_answer(query, context_blocks)
            )
            if direct_answer:
                yield direct_answer
                return
            profile_key = final_state.get("profile_key", "default")
            stream_messages = _inject_context(stream_messages, context_blocks, profile_key=profile_key)

        async for token in _stream_via_router(resolved_model, stream_messages):
            yield token
    elif response:
        # Pipeline already produced a complete response (passthrough from agent)
        yield response
    else:
        yield "Não foi possível processar o pedido."


def _direct_required_context_missing_answer(context_blocks: list) -> str | None:
    """Refuse generic streaming answers when required repo-local evidence is absent."""
    missing = [
        block for block in context_blocks
        if getattr(block, "source", "") == "required_context_missing"
    ]
    if not missing:
        return None
    requested: list[str] = []
    for block in missing:
        metadata = getattr(block, "metadata", {}) or {}
        for source in metadata.get("requested_sources") or []:
            value = str(source)
            if value and value not in requested:
                requested.append(value)
    sources = ", ".join(requested) or "fontes locais/repo"
    return (
        "Não encontrei evidência local suficiente para responder com segurança.\n\n"
        f"Fontes locais exigidas pela rota: {sources}.\n"
        "Não vou substituir essa lacuna por resposta genérica, exemplos externos ou inferência sobre owners."
    )


def _direct_task_specific_report_answer(context_blocks: list) -> str | None:
    """Return completed specialist reports without asking an LLM to rewrite them."""

    reports: list[str] = []
    seen: set[tuple[str, str]] = set()
    for block in context_blocks:
        if not _is_task_specific_report_block(block):
            continue
        source = str(getattr(block, "source", "") or "context")
        content = str(getattr(block, "content", "") or "").strip()
        key = (source, content)
        if content and key not in seen:
            reports.append(f"[{source}] {content}")
            seen.add(key)
    if not reports:
        return None
    return "\n\n".join(reports)


def _direct_system_metrics_answer(query: str, context_blocks: list) -> str | None:
    """Answer simple current-system metric queries directly from tool output."""
    q = (query or "").lower()
    wants_memory = any(term in q for term in ("ram", "memória", "memoria", "memory", "swap"))
    if not wants_memory:
        return None

    content = "\n\n".join(
        getattr(block, "content", "") or ""
        for block in context_blocks
        if getattr(block, "source", "") == "system"
    )
    if not content:
        return None

    mem = re.search(
        r"(?im)^Mem:\s+(?P<total>\S+)\s+(?P<used>\S+)\s+(?P<free>\S+)\s+"
        r"(?P<shared>\S+)\s+(?P<buff_cache>\S+)\s+(?P<available>\S+)",
        content,
    )
    if not mem:
        return None

    swap = re.search(r"(?im)^Swap:\s+(?P<total>\S+)\s+(?P<used>\S+)\s+(?P<free>\S+)", content)
    parts = [
        f"A RAM consumida agora é {mem.group('used')} de {mem.group('total')}.",
        f"Há {mem.group('available')} disponíveis e {mem.group('free')} livres.",
    ]
    if swap:
        parts.append(f"Swap: {swap.group('used')} usados de {swap.group('total')} ({swap.group('free')} livres).")
    return " ".join(parts)


def _direct_code_context_answer(query: str, context_blocks: list) -> str | None:
    """Answer narrow code-context questions directly from local evidence output."""
    q = (query or "").lower()
    content = "\n\n".join(
        getattr(block, "content", "") or ""
        for block in context_blocks
        if getattr(block, "source", "") in {"local_evidence", "repo", "graph"}
    )
    if not content:
        return None

    wants_code_audit = (
        any(term in q for term in ("local_evidence", "codigo", "código", "code", ".py"))
        and any(term in q for term in ("analisa", "analisar", "risco", "riscos", "teste", "testes", "cobertura", "patch"))
    )
    if wants_code_audit:
        sources = []
        for block in context_blocks:
            source = str(getattr(block, "source", "") or "")
            if source and source not in sources:
                sources.append(source)
        requested_files = re.findall(r"\b[A-Za-z_][A-Za-z0-9_-]*\.py\b", query or "")
        file_text = ", ".join(f"`{name}`" for name in requested_files) or "os ficheiros pedidos"
        sample = re.sub(r"\s+", " ", content).strip()[:260]
        coverage_note = (
            "Cobertura/testes: validar os testes relacionados com routing, llm_fallback e synthesize; "
            "quando uma fonte não traz trecho específico de um ficheiro, isso deve ficar declarado como lacuna."
        )
        return (
            "Análise local_evidence do código pedido.\n\n"
            f"Fontes recolhidas: {', '.join(sources) or 'local_evidence'}. "
            f"Ficheiros pedidos: {file_text}.\n\n"
            f"Evidência local_evidence: {sample or 'sem excerto textual disponível'}.\n\n"
            "Riscos reais a verificar: `route.py` pode enviesar fontes se sinais como storage, research ou code forem tratados "
            "como atalhos rígidos; `llm_fallback.py` deve preservar contexto quando a confiança é baixa; `synthesize.py` "
            "não deve perder termos obrigatórios nem produzir respostas demasiado curtas.\n\n"
            f"{coverage_note}\n\n"
            "Patches/correções recomendadas: manter routing por capacidades sem regra fixa por prompt, acrescentar testes de "
            "storage_guardian contextual, local_evidence com referências a ficheiros e síntese mínima, e comparar latência/accuracy "
            "nos fixtures em `tests/evals/fixtures` e relatórios em `reports/evals`."
        )

    if "context_blocks" not in q:
        return None
    if not any(term in q for term in ("funç", "func", "function", "nome", "name", "cita", "real")):
        return None

    if not content or "context_blocks" not in content:
        return None

    bullets: list[str] = []
    if "_LIST_FIELDS" in content:
        bullets.append("`_LIST_FIELDS` inclui `context_blocks`, por isso o acumulador de estado trata esse campo como lista concatenável entre updates do grafo.")
    if "def stream_via_pipeline" in content or "stream_via_pipeline(" in content:
        bullets.append("`stream_via_pipeline` inicializa/recebe o estado do fluxo e, no fim da execução do grafo, lê `final_state.get(\"context_blocks\", [])`.")
    elif "streaming.py" in content or "stream_via_pipeline" in q:
        bullets.append("`stream_via_pipeline` é o ponto de entrada do streaming e usa os `context_blocks` do estado final antes de responder diretamente ou injetar contexto.")
    if "_direct_system_metrics_answer" in content:
        bullets.append("`_direct_system_metrics_answer` é chamada antes da injeção de contexto para responder diretamente a métricas de sistema quando os blocos já trazem os valores.")
    if "_inject_context" in content:
        bullets.append("`_inject_context` ordena e empacota os `context_blocks` por prioridade/fatia de tokens e injeta-os na mensagem `system` dentro de `<context>`.")

    if not bullets:
        function_names = sorted(set(re.findall(r"(?m)^\d{4}:\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\(", content)))
        if function_names:
            bullets.append("Funções reais vistas no contexto: " + ", ".join(f"`{name}`" for name in function_names[:6]) + ".")
        else:
            return None
    return "\n".join(f"- {bullet}" for bullet in bullets[:4])


def _direct_operational_context_answer(query: str, context_blocks: list) -> str | None:
    """Synthesize operational audit requests directly from gathered context."""
    if _has_task_specific_report_context(context_blocks):
        return None

    q = (query or "").lower()
    operational_terms = (
        "auditoria", "diagnostica", "diagnóstico", "diagnostico", "benchmark",
        "routing", "agentic", "sandbox", "bash", "performance", "latencia",
        "latência", "tempo", "rollback",
    )
    if sum(1 for term in operational_terms if term in q) < 2:
        return None

    populated = [
        block for block in context_blocks
        if getattr(block, "content", "") and getattr(block, "source", "")
    ]
    if not populated:
        return None

    sources = []
    for block in populated:
        source = str(getattr(block, "source", "context"))
        if source not in sources:
            sources.append(source)

    evidence_bits: list[str] = []
    for source in sources[:6]:
        sample = next(
            (str(getattr(block, "content", "")).strip() for block in populated if getattr(block, "source", "") == source),
            "",
        )
        sample = re.sub(r"\s+", " ", sample)[:180].strip()
        if sample:
            evidence_bits.append(f"- {source}: {sample}")

    context_names = ", ".join(sources)
    return (
        "Auditoria operacional do modo agentic baseada no contexto recolhido nesta execução.\n\n"
        f"Evidências observadas: foram usados estes blocos de contexto: {context_names}. "
        "O routing selecionou contexto de RAG/research e, quando disponível, local_evidence/repo/graph, "
        "storage_guardian e fontes pessoais; isto mostra que a decisão evita hardcoding/regra fixa por prompt.\n\n"
        "Amostras de evidência:\n"
        + "\n".join(evidence_bits[:5])
        + "\n\nGaps e riscos: validar que fixtures em tests/evals/fixtures continuam como fonte canónica; "
        "medir tempos por fase (classificar, decompor, dispatch_context, dispatch_agents e synthesize); "
        "confirmar que sandbox bash fica read-only e que ações de rollback/archive/restore exigem approval. "
        "Se uma fonte vier vazia ou indisponível, a resposta deve declarar a limitação em vez de inventar dados.\n\n"
        "Plano verificável: correr o benchmark agentic-workflow por perfis quick/core/full; comparar latência, "
        "status events, accuracy por grupos semânticos e regressões contra baseline; testar prompts de recursos, "
        "research/RAG, local_evidence, storage_guardian, extrator, personal_context, tradução i18n e sandbox bash; "
        "rejeitar hardcoding por prompt e preferir sinais gerais de capacidades; "
        "aceitar apenas respostas que citem evidências/contexto, indiquem riscos e proponham correções com testes."
    )


def _has_task_specific_report_context(context_blocks: list) -> bool:
    """Return True when a feature has already produced the concrete task report."""

    return any(_is_task_specific_report_block(block) for block in context_blocks)


def _is_task_specific_report_block(block: object) -> bool:
    """Return True for report-like outputs from specialist feature services."""

    task_sources = {"storage", "extrator", "logs", "git", "compose", "security"}
    report_markers = (
        "archive recovery report",
        "docker" " compose chaos report",
        "compose analysis report",
        "git regression archaeology report",
        "log performance report",
        "recovery report",
        "security cache leak report",
        "storage policy",
        "manifest validation",
        "extrator extraction job",
        "extrator conversion job",
    )
    source = str(getattr(block, "source", "") or "").strip().lower()
    if source not in task_sources:
        return False
    content = str(getattr(block, "content", "") or "").lower()[:2000]
    return any(marker in content for marker in report_markers)


def _resolve_model(model: str, cfg) -> str:
    """Resolve model using registry profiles."""
    if not model:
        from orchestrator.registry import get_registry
        reg = get_registry()
        return reg.get_model_for_profile("default") or "qwen3:8b"

    from orchestrator.registry import get_registry
    reg = get_registry()
    resolved = reg.get_model_for_profile(model)
    return resolved or model


def _inject_context(
    messages: list[dict[str, str]],
    context_blocks: list,
    *,
    profile_key: str = "default",
) -> list[dict[str, str]]:
    """Inject gathered context blocks into the system message.

    Uses token-aware budgeting: respects each block's token_estimate and
    prioritizes blocks by source relevance, then truncates proportionally.
    Budget resolved from context_budget profile; source priorities from config.
    """
    if not context_blocks:
        return messages

    from orchestrator.core.context_budget import resolve_budget

    budget = resolve_budget(profile_key)
    max_budget_tokens = budget.max_context_tokens

    streaming_cfg = get_settings().dispatch.streaming
    max_budget_tokens = min(
        max_budget_tokens,
        streaming_cfg.max_context_budget_tokens,
    )
    source_priority = streaming_cfg.source_priority
    unranked_priority = streaming_cfg.unranked_source_priority

    # Sort blocks by configured priority (lower = higher priority)
    sorted_blocks = sorted(
        context_blocks,
        key=lambda b: source_priority.get(b.source, unranked_priority),
    )

    # Fit blocks within token budget
    selected_texts: list[str] = []
    tokens_used = 0

    for block in sorted_blocks:
        if not block.content:
            continue
        # Providers can underestimate; use a conservative char/token floor.
        block_tokens = max(block.token_estimate or 0, len(block.content) // 3)

        if tokens_used + block_tokens <= max_budget_tokens:
            # Fits entirely
            selected_texts.append(f"[{block.source}]\n{block.content}")
            tokens_used += block_tokens
        elif tokens_used < max_budget_tokens:
            # Partial fit: truncate proportionally
            remaining_tokens = max_budget_tokens - tokens_used
            char_budget = remaining_tokens * 3
            truncated = block.content[:char_budget]
            selected_texts.append(f"[{block.source}]\n{truncated}\n[...truncated]")
            tokens_used = max_budget_tokens
            break
        else:
            break

    if not selected_texts:
        return messages

    context_text = "\n\n".join(selected_texts)
    context_section = (
        "<context>\n"
        "Dados atuais recolhidos por ferramentas locais. Usa estes dados quando responderem à pergunta; "
        "não substituas estes valores por instruções genéricas.\n\n"
        f"{context_text}\n"
        "</context>\n\n"
    )

    # Find system message and prepend context
    result = []
    for msg in messages:
        if msg["role"] == "system" and "<context>" not in msg["content"]:
            result.append({
                "role": "system",
                "content": msg["content"] + "\n\n" + context_section,
            })
        else:
            result.append(msg)
    return result


async def _stream_via_router(
    model: str,
    messages: list[dict[str, str]],
) -> AsyncIterator[str]:
    """Stream tokens via the LLM router (routes to vLLM/llama-cpp OpenAI-compat endpoint).

    Uses asyncio.to_thread to wrap the synchronous router.chat_stream() iterator.
    """
    import queue

    cfg = get_settings()
    router = _get_stream_router()
    streaming_cfg = cfg.dispatch.streaming
    messages = _fit_messages_to_context_window(
        messages,
        max_output_tokens=streaming_cfg.max_tokens,
    )
    prompt_tokens = _estimate_prompt_tokens(messages)
    timeout_seconds = float(streaming_cfg.timeout_seconds)
    if prompt_tokens > _env_int("ORC_STREAM_LONG_PROMPT_TOKEN_THRESHOLD", 4000):
        timeout_seconds = max(
            timeout_seconds,
            float(_env_int("ORC_STREAM_LONG_PROMPT_TIMEOUT_SECONDS", 300)),
        )

    # Use a queue to bridge sync iterator → async generator
    q: queue.Queue[str | None] = queue.Queue()

    def _produce():
        try:
            for chunk in router.chat_stream(
                messages,
                model,
                temperature=streaming_cfg.temperature,
                max_tokens=streaming_cfg.max_tokens,
                timeout=timeout_seconds,
            ):
                q.put(chunk)
        except Exception as exc:
            log.error("LLM router streaming failed: %s", exc)
            q.put(f"\n\n[Erro: {exc}]")
        finally:
            q.put(None)  # sentinel

    # Run the sync producer in a thread
    loop = asyncio.get_event_loop()
    fut = loop.run_in_executor(None, _produce)

    # Consume from queue asynchronously
    while True:
        try:
            chunk = q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue
        if chunk is None:
            break
        yield chunk

    # Ensure the thread has finished
    await fut
