"""Synthesize node — combines multiple agent outputs into a coherent response."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sharedai.llm.utils import strip_think

from orchestrator.agentic.deliberation import deliberation_to_synthesis_text, summarize_agentic_deliberation
from orchestrator.pipeline.state import SymbiontState
from orchestrator.types import AgentResult, Complexity

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


if TYPE_CHECKING:
    from orchestrator.config import IntelligentPipelineConfig

log = logging.getLogger(__name__)

_SYNTHESIS_PROMPT = _prompt("synthesis.md")

_POLISH_PROMPT = _prompt("polish.md")


def _clean_response(text: str) -> str:
    return strip_think(text or "")


def _synthesis_model(model: str) -> str:
    """Use a general chat model for final synthesis, not a code-specialist alias."""
    lowered = (model or "").lower()
    if "coder" in lowered or "code" in lowered:
        return ""
    return model


def _language_instruction_from_state(state: SymbiontState) -> tuple[str, str]:
    language_context = state.get("language_context", {}) or {}
    response_language = str(language_context.get("response_language") or "same_as_user")
    original_query = str(state.get("original_query") or state.get("query") or "")
    if response_language and response_language != "same_as_user":
        instruction = f"User-facing final responses must use this language: {response_language}."
    else:
        instruction = "User-facing final responses must match the original user's language."
    instruction += " Agent-to-agent summaries and structured internal contracts should remain in English."
    return original_query, instruction


def _degraded_context_response(state: SymbiontState) -> str | None:
    """Return an explicit degraded-mode answer when agents failed after context gathering."""
    if not state.get("all_agents_failed"):
        return None

    context_blocks = [
        block for block in state.get("context_blocks", [])
        if getattr(block, "content", "") and getattr(block, "source", "")
    ]
    if not context_blocks:
        return None

    sources: list[str] = []
    evidence: list[str] = []
    for block in context_blocks:
        source = str(getattr(block, "source", "") or "context")
        if source not in sources:
            sources.append(source)
        if len(evidence) >= 4:
            continue
        sample = " ".join(str(getattr(block, "content", "")).split())[:220].strip()
        if sample:
            evidence.append(f"- {source}: {sample}")

    failed_agents = [
        str(getattr(result, "agent_name", "") or "agent")
        for result in state.get("agent_results", [])
        if not getattr(result, "success", False)
    ]
    failure_lines: list[str] = []
    for result in state.get("agent_results", []):
        if getattr(result, "success", False):
            continue
        agent_name = str(getattr(result, "agent_name", "") or "agent")
        metadata = getattr(result, "metadata", {}) or {}
        reason = (
            metadata.get("error")
            or metadata.get("failure_reason")
            or metadata.get("runtime_flag")
            or metadata.get("degraded_reason")
        )
        if isinstance(reason, dict):
            reason = reason.get("reason") or reason.get("safe_action") or str(reason)
        reason_text = str(reason or "sem motivo detalhado").replace("\n", " ")[:240]
        failure_lines.append(f"- {agent_name}: {reason_text}")

    failed_text = ", ".join(dict.fromkeys(failed_agents)) or "agentes selecionados"
    sources_text = ", ".join(sources)
    evidence_text = "\n".join(evidence) if evidence else "- contexto recolhido, sem amostra textual segura"
    failure_text = "\n".join(failure_lines) if failure_lines else "- falha reportada sem detalhe adicional"

    return (
        "Não consegui concluir a resposta final porque todos os agentes selecionados falharam ou expiraram "
        "depois de o contexto local já ter sido recolhido.\n\n"
        f"Agentes afetados: {failed_text}.\n"
        "Motivos reportados:\n"
        f"{failure_text}\n\n"
        f"Fontes de contexto preservadas: {sources_text}.\n\n"
        "Evidência disponível antes da falha:\n"
        f"{evidence_text}\n\n"
        "Estado seguro: não vou inventar conclusões nem substituir a análise por uma resposta genérica. "
        "Recomendo repetir a execução com timeout/capacidade ajustados ou encaminhar esta tarefa para uma "
        "capacidade local apropriada ao tipo de evidência."
    )


def _resolve_agentic_deliberation(state: SymbiontState) -> dict[str, Any]:
    existing = state.get("agentic_deliberation")
    if isinstance(existing, dict) and existing.get("available"):
        return existing
    try:
        from orchestrator.agentic.context import get_agentic_context
        from orchestrator.agentic.store import get_agentic_store

        ctx = get_agentic_context()
        if ctx is None or not ctx.task_id:
            return {"available": False}
        return summarize_agentic_deliberation(get_agentic_store(), ctx.task_id)
    except Exception:
        return {"available": False}


def _agentic_consensus_result(deliberation: dict[str, Any]) -> AgentResult | None:
    text = deliberation_to_synthesis_text(deliberation)
    if not text:
        return None
    consensus = deliberation.get("latest_consensus") if isinstance(deliberation, dict) else None
    confidence = 0.0
    if isinstance(consensus, dict):
        try:
            confidence = float(consensus.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
    return AgentResult(
        task_id="agentic_deliberation",
        agent_name="agentic_consensus",
        output=text,
        success=True,
        confidence=max(0.0, min(1.0, confidence)),
        tokens_used=max(1, len(text) // 4),
        duration_ms=0.0,
        metadata={"source": "agentic_deliberation"},
    )


def _attach_agentic_deliberation(result: dict[str, Any], deliberation: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(deliberation, dict) or not deliberation.get("available"):
        return result
    result["agentic_deliberation"] = deliberation
    trace = result.setdefault("execution_trace", [])
    if isinstance(trace, list) and "synthesize:agentic_deliberation_integrated" not in trace:
        trace.append("synthesize:agentic_deliberation_integrated")
    return result


def create_synthesize_node(
    llm_adapter: Any,
    intelligent_config: "IntelligentPipelineConfig | None" = None,
):
    """Factory that creates the synthesis node with injected LLM adapter."""
    from langchain_core.messages import HumanMessage, SystemMessage

    def synthesize_node(state: SymbiontState) -> dict:
        """Combine agent results into final response, with optional refinement."""
        agent_results = state.get("agent_results", [])
        query = state["query"]
        model = state.get("model_used", "")
        refinement_round = state.get("refinement_round", 0)
        complexity = state.get("complexity", Complexity.NORMAL)
        stream_mode = state.get("stream_mode", False)
        agentic_deliberation = _resolve_agentic_deliberation(state)
        agentic_consensus = _agentic_consensus_result(agentic_deliberation)

        # Fast-path: if dispatch_agents already prepared stream_messages (bypass),
        # just pass them through — no synthesis needed.
        if stream_mode and state.get("stream_messages"):
            return {
                "stream_messages": state["stream_messages"],
                "execution_trace": ["synthesize:stream_bypass_passthrough"],
            }

        # v1.6: Polish mode — refine draft using critic feedback
        if (
            intelligent_config
            and intelligent_config.progressive_refinement_enabled
            and refinement_round == 1
            and complexity == Complexity.DEEP
        ):
            if stream_mode:
                return _stream_polish_synthesize(state, query)
            return _polish_synthesize(state, llm_adapter, model, query)

        successful = [r for r in agent_results if r.success and r.output.strip()]

        if not successful:
            if agentic_consensus is not None:
                if stream_mode:
                    result = _stream_multi_synthesize([agentic_consensus], query, state)
                else:
                    response, tokens, trace = _llm_synthesize([agentic_consensus], query, model, llm_adapter, state)
                    result = {
                        "response": _clean_response(response),
                        "tokens_used": tokens,
                        "execution_trace": [trace],
                    }
                return _attach_agentic_deliberation(result, agentic_deliberation)

            degraded_response = _degraded_context_response(state)
            if degraded_response:
                return {
                    "response": degraded_response,
                    "tokens_used": 0,
                    "execution_trace": ["synthesize:degraded_context_fallback"],
                }
            # No agent results — answer directly with LLM (or prepare for streaming)
            _original_query, language_instruction = _language_instruction_from_state(state)
            messages = [
                {
                    "role": "system",
                    "content": _prompt("direct_fallback.md").format(language_instruction=language_instruction),
                },
                {"role": "user", "content": query},
            ]
            if stream_mode:
                return {
                    "stream_messages": messages,
                    "execution_trace": ["synthesize:stream_llm_fallback"],
                }
            try:
                result = llm_adapter.invoke(
                    [SystemMessage(content=messages[0]["content"]), HumanMessage(content=messages[1]["content"])],
                    model=_synthesis_model(model),
                    temperature=0.2,
                    max_tokens=384,
                )
                return {
                    "response": _clean_response(result.content),
                    "tokens_used": result.response_metadata.get("eval_count", 0),
                    "execution_trace": ["synthesize:llm_fallback"],
                }
            except Exception as exc:
                log.warning("synthesize LLM fallback failed: %s", exc)
                return {
                    "response": "Não foi possível obter informação relevante para a sua pergunta.",
                    "tokens_used": 0,
                    "execution_trace": ["synthesize:no_results"],
                }

        synthesis_sources = [*successful]
        if agentic_consensus is not None:
            synthesis_sources.append(agentic_consensus)

        # Single result - pass through directly (no extra LLM call needed)
        if len(synthesis_sources) == 1:
            result = synthesis_sources[0]
            if stream_mode:
                # Single agent result: no need to stream — already have the answer
                return {
                    "response": result.output,
                    "tokens_used": result.tokens_used,
                    "execution_trace": [f"synthesize:passthrough({result.agent_name})"],
                }
            response = _clean_response(result.output)
            tokens = result.tokens_used
            trace = f"synthesize:passthrough({result.agent_name})"
        else:
            # Multiple results - synthesize with LLM
            if stream_mode:
                return _attach_agentic_deliberation(
                    _stream_multi_synthesize(synthesis_sources, query, state),
                    agentic_deliberation,
                )
            response, tokens, trace = _llm_synthesize(
                synthesis_sources, query, model, llm_adapter, state,
            )

        # v1.6: If progressive refinement enabled for DEEP, this is the draft round
        result_dict: dict[str, Any] = {
            "response": _clean_response(response),
            "tokens_used": tokens,
            "execution_trace": [trace],
        }
        if (
            intelligent_config
            and intelligent_config.progressive_refinement_enabled
            and complexity == Complexity.DEEP
            and refinement_round == 0
        ):
            result_dict["refinement_round"] = 1

        return _attach_agentic_deliberation(result_dict, agentic_deliberation)

    return synthesize_node


def _llm_synthesize(
    successful: list,
    query: str,
    model: str,
    llm_adapter: Any,
    state: SymbiontState,
) -> tuple[str, int, str]:
    """Synthesize multiple results with LLM. Returns (response, tokens, trace)."""
    from langchain_core.messages import HumanMessage, SystemMessage

    sources_text = "\n\n".join(f"[{r.agent_name}]\n{_clean_response(r.output)}" for r in successful)
    total_tokens = sum(r.tokens_used for r in successful)

    try:
        original_query, language_instruction = _language_instruction_from_state(state)
        prompt = _SYNTHESIS_PROMPT.format(
            query=query,
            original_query=original_query,
            language_instruction=language_instruction,
            sources=sources_text,
        )
        messages = [
            SystemMessage(content=prompt),
            HumanMessage(content=query),
        ]
        result = llm_adapter.invoke(
            messages,
            model=_synthesis_model(model),
            temperature=0.2,
            max_tokens=512,
        )
        log.info("synthesize: combined %d sources", len(successful))
        return _clean_response(result.content), total_tokens, f"synthesize:llm({len(successful)} sources)"

    except Exception as e:
        log.warning("synthesize: LLM failed (%s), falling back to concat", e)
        concat = "\n\n---\n\n".join(f"**{r.agent_name}**:\n{_clean_response(r.output)}" for r in successful)
        return concat, total_tokens, "synthesize:concat_fallback"


def _polish_synthesize(
    state: SymbiontState,
    llm_adapter: Any,
    model: str,
    query: str,
) -> dict:
    """Polish pass: refine draft response using critic feedback."""
    from langchain_core.messages import HumanMessage, SystemMessage

    draft = _clean_response(state.get("response", ""))
    issues = state.get("critique_issues", [])
    issues_text = "\n".join(f"- {issue}" for issue in issues) if issues else "- General quality improvements needed"

    try:
        original_query, language_instruction = _language_instruction_from_state(state)
        prompt = _POLISH_PROMPT.format(
            query=query,
            original_query=original_query,
            language_instruction=language_instruction,
            draft=draft,
            issues=issues_text,
        )
        messages = [
            SystemMessage(content=prompt),
            HumanMessage(content=query),
        ]
        result = llm_adapter.invoke(
            messages,
            model=_synthesis_model(model),
            temperature=0.2,
            max_tokens=512,
        )
        log.info("synthesize: polish round complete")
        return {
            "response": _clean_response(result.content),
            "refinement_round": 2,
            "tokens_used": state.get("tokens_used", 0),
            "execution_trace": ["synthesize:polish"],
        }
    except Exception as e:
        log.warning("synthesize: polish failed (%s), keeping draft", e)
        return {
            "refinement_round": 2,
            "execution_trace": ["synthesize:polish_failed"],
        }


def _stream_multi_synthesize(successful: list, query: str, state: SymbiontState) -> dict:
    """Prepare messages for streaming synthesis of multiple results."""
    sources_text = "\n\n".join(f"[{r.agent_name}]\n{_clean_response(r.output)}" for r in successful)
    original_query, language_instruction = _language_instruction_from_state(state)
    prompt = _SYNTHESIS_PROMPT.format(
        query=query,
        original_query=original_query,
        language_instruction=language_instruction,
        sources=sources_text,
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": query},
    ]
    return {
        "stream_messages": messages,
        "tokens_used": sum(r.tokens_used for r in successful),
        "execution_trace": [f"synthesize:stream_llm({len(successful)} sources)"],
    }


def _stream_polish_synthesize(state: SymbiontState, query: str) -> dict:
    """Prepare messages for streaming polish synthesis."""
    draft = _clean_response(state.get("response", ""))
    issues = state.get("critique_issues", [])
    issues_text = "\n".join(f"- {issue}" for issue in issues) if issues else "- General quality improvements needed"
    original_query, language_instruction = _language_instruction_from_state(state)
    prompt = _POLISH_PROMPT.format(
        query=query,
        original_query=original_query,
        language_instruction=language_instruction,
        draft=draft,
        issues=issues_text,
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": query},
    ]
    return {
        "stream_messages": messages,
        "refinement_round": 2,
        "execution_trace": ["synthesize:stream_polish"],
    }
