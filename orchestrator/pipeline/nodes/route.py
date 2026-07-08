"""Route node — deterministic routing based on intent x complexity.

Maps classified intent to context sources and agents, selects model/profile.
Also provides the conditional edge function that decides the next graph step.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from orchestrator.capabilities.workspace import match_workspace_capability
from orchestrator.config import get_settings
from orchestrator.observability.capability_trace import emit_capability_event
from orchestrator.pipeline.state import SymbiontState
from orchestrator.routing.context_requirements import (
    context_sources_for_query,
    needs_system_context,
    requires_context_gather,
    requires_local_evidence,
)
from orchestrator.routing.context_router import ConfigContextRouter
from orchestrator.routing.model_router import ConfigModelRouter
from orchestrator.types import Complexity, Intent

log = logging.getLogger(__name__)

_context_router = ConfigContextRouter()
_model_router = ConfigModelRouter()


def _confidence_threshold() -> float:
    """Base confidence threshold below which routing delegates to the LLM fallback."""
    return get_settings().classify.confidence_threshold


def _history_aware_threshold() -> float:
    """Higher threshold applied when an active session provides conversation history."""
    return get_settings().classify.history_aware_threshold


def _is_conversation_memory_query(query: str) -> bool:
    """Return True for questions that only need the chat history, not LLM routing."""
    q = " ".join((query or "").lower().split())
    if not q:
        return False
    patterns = (
        "o que te perguntei",
        "o que eu perguntei",
        "que te perguntei",
        "o que te disse",
        "que te disse",
        "o que eu disse",
        "qual foi a palavra",
        "qual foi o codigo",
        "qual foi o código",
        "palavra que te pedi",
        "codigo que te pedi",
        "código que te pedi",
        "que te pedi para memorizar",
        "pedi para memorizar",
        "mensagem anterior",
        "mensagens atras",
        "mensagem atras",
        "2 mensagem",
        "2 mensagens",
        "duas mensagens",
        "pergunta anterior",
        "perguntei antes",
        "disse antes",
        "what did i ask",
        "what did i tell",
        "previous message",
        "last message",
        "two messages ago",
        "earlier message",
    )
    return any(pattern in q for pattern in patterns)


def _is_conversation_memory_write(query: str) -> bool:
    """Return True for commands that ask the assistant to remember chat facts."""
    q = " ".join((query or "").lower().split())
    if not q:
        return False
    return re.search(
        r"^(?:por favor\s+)?(?:"
        r"memoriza|lembra-te|lembra te|guarda isto|guarda que|recorda que|"
        r"nao te esquecas|não te esqueças|remember this|remember that|remember|memorize"
        r")\b",
        q,
    ) is not None


def _is_simple_greeting(query: str) -> bool:
    q = " ".join((query or "").strip().lower().split())
    return q in {"ola", "olá", "oi", "boas", "hello", "hi", "hey"}


_CODE_GENERATION_ACTION_RE = re.compile(
    r"\b("
    r"create|generate|write|implement|build|"
    r"cria|criar|gera|gerar|escreve|implementar|implementa|"
    r"constr[oó]i|construir"
    r")\b",
    re.IGNORECASE,
)
_CODE_DOMAIN_RE = re.compile(
    r"\b("
    r"python|javascript|typescript|rust|java|go|sql|bash|shell|"
    r"class|classe|function|fun[cç][aã]o|method|m[eé]todo|"
    r"script|module|m[oó]dulo|api|endpoint|test|teste|tests|code|c[oó]digo"
    r")\b",
    re.IGNORECASE,
)
_LOCAL_CODE_SCOPE_RE = re.compile(
    r"\b("
    r"this repo|this repository|this project|this codebase|this workspace|this module|this file|"
    r"current repo|current repository|current project|current codebase|current workspace|current module|current file|"
    r"my repo|my repository|my project|my codebase|my workspace|my module|my file|"
    r"existing code|existing project|existing module|existing file|"
    r"este repo|este reposit[oó]rio|este projeto|este projecto|este m[oó]dulo|este ficheiro|"
    r"neste repo|neste reposit[oó]rio|neste projeto|neste projecto|neste m[oó]dulo|neste ficheiro|"
    r"meu repo|meu reposit[oó]rio|minha codebase|meu workspace|minha workspace|"
    r"c[oó]digo existente|projeto existente|projecto existente|m[oó]dulo existente|ficheiro existente"
    r")\b",
    re.IGNORECASE,
)
_FILE_REFERENCE_RE = re.compile(
    r"(?<!\w)(?:\./|\.\./|/|~\/)|\b[\w.-]+\.(?:py|pyi|js|ts|tsx|jsx|json|ya?ml|toml|md|sh|sql|cfg|ini)\b",
    re.IGNORECASE,
)
_QUOTED_PATH_RE = re.compile(r"(?P<quote>['\"`])(?P<path>(?:/|~/?|\./|\.\./)[^'\"`]+)(?P=quote)")
_PATH_TOKEN_RE = re.compile(r"(?<!\w)(?:/|~/?|\./|\.\./)(?:[^\s\"'`<>]|\\ )+")
_TRAILING_PATH_CHARS = ".,;:)]}>"


def _extract_prompt_paths(prompt: str, *, base_cwd: str | None = None) -> list[str]:
    """Extract explicit local path mentions for routing only."""
    found: list[tuple[int, str]] = []
    for match in _QUOTED_PATH_RE.finditer(prompt or ""):
        found.append((match.start(), match.group("path").strip()))
    for match in _PATH_TOKEN_RE.finditer(prompt or ""):
        found.append((match.start(), match.group(0).strip()))

    resolved: list[str] = []
    seen: set[str] = set()
    for _, raw in sorted(found, key=lambda item: item[0]):
        value = raw.rstrip(_TRAILING_PATH_CHARS).replace("\\ ", " ")
        if not value or value in seen:
            continue
        seen.add(value)
        if base_cwd and value.startswith(("./", "../")):
            try:
                value = str((Path(base_cwd).expanduser() / value).resolve())
            except OSError:
                value = str(Path(base_cwd).expanduser() / value)
        resolved.append(value)
    return resolved


def _is_standalone_code_generation_request(query: str, intent: Intent | str) -> bool:
    """Return True for code creation prompts that do not ask about local evidence."""
    if not _matches_enum(intent, Intent.CODE):
        return False
    q = " ".join((query or "").split())
    if not q:
        return False
    if not (_CODE_GENERATION_ACTION_RE.search(q) and _CODE_DOMAIN_RE.search(q)):
        return False
    if _is_workspace_bound_task(q):
        return False
    if _explicit_workspace_paths_from_query(q):
        return False
    if _LOCAL_CODE_SCOPE_RE.search(q) or _FILE_REFERENCE_RE.search(q):
        return False
    return True


def _is_workspace_bound_task(query: str) -> bool:
    """Return True when the user explicitly scopes evidence to a local workspace."""
    q = " ".join((query or "").lower().split())
    if not q:
        return False

    explicit_workspace_paths = _explicit_workspace_paths_from_query(query)
    if explicit_workspace_paths:
        return True

    local_scope_terms = (
        "pasta atual",
        "diretório atual",
        "diretorio atual",
        "nesta pasta",
        "neste diretório",
        "neste diretorio",
        "current folder",
        "current directory",
        "working directory",
        "local workspace",
        "workspace local",
    )
    evidence_scope_terms = (
        "todas as evidências estão dentro",
        "todas as evidencias estao dentro",
        "all evidence is inside",
        "all evidence lives inside",
        "evidence is inside",
        "evidências dentro",
        "evidencias dentro",
        "ficheiros locais",
        "local files",
    )
    task_terms = (
        "task.md",
        "readme.md",
    )
    has_task_reference = any(term in q for term in task_terms)
    return (
        any(term in q for term in evidence_scope_terms)
        or (
            any(term in q for term in local_scope_terms)
            and has_task_reference
        )
        or (
            bool(_explicit_workspace_paths_from_query(query))
            and has_task_reference
        )
    )


def _clean_absolute_path_token(raw: str) -> str:
    return (raw or "").strip().strip(".,;:)】]}>'\"`")


def _container_visible_path_candidates(raw_path: str) -> list[Path]:
    raw = (raw_path or "").strip()
    if not raw or "\x00" in raw:
        return []
    path = Path(raw).expanduser()
    candidates = [path]
    host_home = os.environ.get("HOST_HOME_PREFIX", "").rstrip("/")
    if host_home and raw == host_home:
        candidates.append(Path("/host_home"))
    elif host_home and raw.startswith(f"{host_home}/"):
        candidates.append(Path("/host_home") / raw[len(host_home) + 1 :])
    parts = path.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "home":
        candidates.append(Path("/host_home").joinpath(*parts[3:]))
    return candidates


def _container_visible_workspace_path(raw_path: str) -> Path | None:
    for candidate in _container_visible_path_candidates(raw_path):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir():
            return resolved
    return None


def _explicit_workspace_paths_from_query(query: str) -> list[Path]:
    """Return visible local directories explicitly named in the prompt."""
    paths: list[Path] = []
    seen: set[str] = set()
    for raw_path in _extract_prompt_paths(query or ""):
        raw = _clean_absolute_path_token(raw_path)
        if not raw:
            continue
        workspace = _container_visible_workspace_path(raw)
        if workspace is None:
            continue
        key = str(workspace)
        if key in seen:
            continue
        seen.add(key)
        paths.append(workspace)
    return paths


def _workspace_paths_for_state(state: SymbiontState) -> list[Path]:
    paths = _explicit_workspace_paths_from_query(str(state.get("query") or ""))
    cwd_workspace = _container_visible_workspace_path(str(state.get("client_cwd") or ""))
    if cwd_workspace is not None and str(cwd_workspace) not in {str(path) for path in paths}:
        paths.append(cwd_workspace)
    return paths


def _visible_task_md_from_workspace(state: SymbiontState, *, max_chars: int = 20_000) -> str:
    for workspace in _workspace_paths_for_state(state):
        task_path = workspace / "TASK.md"
        try:
            if task_path.is_file():
                return task_path.read_text(encoding="utf-8", errors="replace")[:max_chars].strip()
        except OSError:
            continue
    return ""


def _workspace_task_augmented_query(query: str, state: SymbiontState) -> str:
    """Append visible TASK.md text for routing when the user asked to solve it."""
    if not _is_workspace_bound_task(query):
        return query
    task_text = _visible_task_md_from_workspace(state)
    if not task_text:
        return query
    return f"{query}\n\n[Visible workspace TASK.md]\n{task_text}"


def _matches_enum(value: Any, expected: Intent | Complexity) -> bool:
    """Compare enum/string state values without making routing depend on type coercion."""
    raw = value.value if hasattr(value, "value") else value
    raw_text = str(raw).strip().lower()
    expected_value = str(expected.value).lower()
    expected_name = str(expected.name).lower()
    expected_qualified = f"{expected.__class__.__name__}.{expected.name}".lower()
    return raw_text in {expected_value, expected_name, expected_qualified}

# Ordered complexity levels for threshold comparison
COMPLEXITY_ORDER: dict[str, int] = {
    "SIMPLE": 0,
    "MEDIUM": 1,
    "NORMAL": 1,
    "COMPLEX": 2,
    "DEEP": 3,
}

# ---------------------------------------------------------------------------
# Intent -> Agent mapping (deterministic)
# ---------------------------------------------------------------------------
_INTENT_AGENTS: dict[Intent, list[str]] = {
    Intent.GENERAL: ["reasoning_and_response"],
    Intent.LOCAL: ["reasoning_and_response"],
    Intent.RESEARCH: ["reasoning_and_response"],
    Intent.PERSONAL_CONTEXT: ["reasoning_and_response"],
    Intent.CODE: ["reasoning_and_response"],
    Intent.SYSTEM: ["reasoning_and_response"],
    Intent.GRAPH: ["reasoning_and_response"],
    Intent.AUDIO: ["audio_transcribe"],
    Intent.LOCAL_AND_GRAPH: ["reasoning_and_response"],
    Intent.SYSTEM_AND_LOCAL: ["reasoning_and_response"],
    Intent.CLARIFY: [],
}


def _dispatchable_agent_names() -> set[str]:
    try:
        names = set(get_settings().dispatch.agent_endpoints)
    except Exception:
        names = {"reasoning_and_response", "audio_transcribe"}
    return names or {"reasoning_and_response"}


def _fallback_dispatch_agent(valid_agents: set[str]) -> str | None:
    if "reasoning_and_response" in valid_agents:
        return "reasoning_and_response"
    return next(iter(valid_agents), None)


def _sanitize_dispatch_agents(candidates: list[str]) -> tuple[list[str], list[str]]:
    if not candidates:
        return [], []
    valid_agents = _dispatchable_agent_names()
    selected: list[str] = []
    dropped: list[str] = []
    for candidate in candidates:
        name = str(candidate)
        if name in valid_agents:
            if name not in selected:
                selected.append(name)
        elif name not in dropped:
            dropped.append(name)
    if selected:
        return selected, dropped
    fallback = _fallback_dispatch_agent(valid_agents)
    return ([fallback] if fallback else []), dropped


def route_node(state: SymbiontState) -> dict:
    """Deterministic routing: select agents, context sources, and model.

    Model selection is fully internal — based on intent × complexity analysis.
    No user override is accepted.
    """
    intent = state["intent"]
    complexity = state["complexity"]

    query = _workspace_task_augmented_query(state.get("query", ""), state)

    workspace_bound = _is_workspace_bound_task(query)
    workspace_route = match_workspace_capability(query, workspace_bound=workspace_bound)

    if workspace_bound:
        context_sources = list(workspace_route.context_sources) if workspace_route else []
    elif _is_standalone_code_generation_request(query, intent):
        context_sources = []
    else:
        context_sources = context_sources_for_query(query, _context_router.route(intent, complexity))
    local_evidence_required = requires_local_evidence(query)

    # Agent selection — preserve the decomposition provider's choice when a dynamic
    # execution plan exists; otherwise use the deterministic intent mapping.
    existing_agents = state.get("selected_agents") or []
    from_decomposer = any(
        str(trace).startswith("decompose:")
        for trace in state.get("execution_trace", [])
    )
    from_fallback = bool(state.get("fallback_used")) and bool(existing_agents)
    if existing_agents and (state.get("execution_plan") or from_decomposer or from_fallback):
        selected_agents = list(existing_agents)
        agent_source = "llm_fallback" if from_fallback and not from_decomposer else "decomposer"
    else:
        selected_agents = list(_INTENT_AGENTS.get(intent, ["reasoning_and_response"]))
        agent_source = "intent_map"
        if workspace_route and workspace_route.agent_source != "intent_map":
            selected_agents = list(workspace_route.selected_agents)
            agent_source = workspace_route.agent_source
    selected_agents, dropped_agents = _sanitize_dispatch_agents(selected_agents)
    if dropped_agents:
        agent_source = f"{agent_source}:dispatchable"

    # Model selection — always from internal routing (no user override).
    # Conversation-memory reads/writes only need short chat history, so keep
    # them on the fast profile even when the heuristic classifier says complex.
    if _matches_enum(intent, Intent.GENERAL) and (
        _is_conversation_memory_query(query) or _is_conversation_memory_write(query)
    ):
        selection = _model_router.select_with_profile(Intent.GENERAL, Complexity.SIMPLE)
    else:
        selection = _model_router.select_with_profile(intent, complexity)
    model_used = selection.model
    profile_key = selection.profile_key

    log.info(
        "route: agents=%s (%s) sources=%s model=%s profile=%s",
        selected_agents, agent_source, context_sources, model_used, profile_key,
    )
    emit_capability_event(
        "route_decision",
        intent=getattr(intent, "value", str(intent)),
        complexity=getattr(complexity, "value", str(complexity)),
        selected_agents=selected_agents,
        dropped_agents=dropped_agents,
        context_sources=context_sources,
        local_evidence_required=local_evidence_required,
        model_used=model_used,
        profile_key=profile_key,
        agent_source=agent_source,
        client_cwd=state.get("client_cwd", ""),
    )

    return {
        "query": query,
        "selected_agents": selected_agents,
        "context_sources": context_sources,
        "local_evidence_required": local_evidence_required,
        "model_used": model_used,
        "profile_key": profile_key,
        "fallback_used": False,
        "execution_trace": (
            [f"route:{agent_source}->{selected_agents}"]
            + (["route:standalone_code_generation_context_skipped"] if _is_standalone_code_generation_request(query, intent) else [])
            + (["route:local_evidence_required"] if local_evidence_required else [])
        )
        + ([f"route:dropped_non_dispatchable->{dropped_agents}"] if dropped_agents else []),
    }


# ---------------------------------------------------------------------------
# Complexity comparison helper
# ---------------------------------------------------------------------------

def _complexity_meets_threshold(complexity: Complexity | str, threshold: str) -> bool:
    """Check if complexity meets or exceeds the given threshold."""
    if hasattr(complexity, "value"):
        c_key = complexity.value.upper()
    else:
        c_key = str(complexity).upper()
    c_val = COMPLEXITY_ORDER.get(c_key, 1)
    t_val = COMPLEXITY_ORDER.get(threshold.upper(), 2)
    return c_val >= t_val


def _dynamic_decompose_enabled(complexity: Complexity | str) -> bool:
    cfg = get_settings()
    if cfg.dynamic_routing.mode not in ("dynamic", "ab_test"):
        return False
    return _matches_enum(complexity, Complexity.COMPLEX) or _matches_enum(complexity, Complexity.DEEP)


def _has_sensitive_sources(context_blocks: list) -> bool:
    """Check if any context block has a source in the denylist."""
    _DENYLIST_SOURCES = {"secrets", "credentials", "tokens", "passwords", "api_keys"}
    for block in context_blocks:
        source = getattr(block, "source", None) or ""
        if source.lower() in _DENYLIST_SOURCES:
            return True
    return False


def _emit_gemilyni_routing(
    *,
    state: SymbiontState,
    selected_path: str,
    reason: str,
    complexity: str,
    complexity_threshold: str,
    intent: str,
    blocked_by_policy: bool,
    externalizable: bool,
    execution_enabled: bool,
) -> None:
    """Emit gemilyni routing decision event (best-effort)."""
    try:
        from orchestrator.observability.gemilyni import emit_routing_decision

        run_id = state.get("request_id", "") or state.get("session_id", "") or ""
        emit_routing_decision(
            run_id=run_id,
            trace_id=state.get("trace_id"),
            selected_path=selected_path,
            reason=reason,
            complexity=complexity,
            complexity_threshold=complexity_threshold,
            intent=intent,
            blocked_by_policy=blocked_by_policy,
            externalizable=externalizable,
            execution_enabled=execution_enabled,
        )
    except Exception:
        pass  # Observability never breaks execution


# ---------------------------------------------------------------------------
# Conditional edge function (used by the graph to decide next step)
# ---------------------------------------------------------------------------

def create_route_decision(execution_config: Any = None):
    """Factory that creates a route_decision function aware of execution config.

    If execution_config is None or disabled, returns the default route_decision.
    """
    def _route_decision(state: SymbiontState) -> str:
        """Conditional edge after classify: pick next node based on confidence.

        Returns:
            "direct_respond" - trivial query, skip context/agents
            "llm_fallback"   - low confidence, ask LLM to route
            "decompose"      - v1.5: complex query in dynamic mode, decompose first
            "execute"        - external execution via Gemini workers
            "gather"         - high confidence, proceed with deterministic routing
        """
        intent = state["intent"]
        confidence = state.get("confidence", 0.5)
        complexity = state.get("complexity", Complexity.NORMAL)
        history = state.get("history")

        query = state.get("query", "")
        workspace_bound = _is_workspace_bound_task(query)
        if workspace_bound:
            return "gather"
        if (
            _is_conversation_memory_query(query)
            or _is_conversation_memory_write(query)
            or _is_simple_greeting(query)
        ):
            return "direct_respond"

        if requires_context_gather(query):
            return "gather"
        if (
            needs_system_context(query)
            or _matches_enum(intent, Intent.SYSTEM)
            or _matches_enum(intent, Intent.SYSTEM_AND_LOCAL)
        ):
            return "gather"

        # Context-aware fallback: with an active session, anything below the
        # history-aware threshold goes to the LLM router so it can resolve
        # follow-up references before any direct-response shortcut fires.
        if history and confidence < _history_aware_threshold():
            return "llm_fallback"

        # Trivial: greetings, very simple questions with no info need
        if _matches_enum(intent, Intent.CLARIFY):
            return "direct_respond"
        if _matches_enum(complexity, Complexity.SIMPLE) and _matches_enum(intent, Intent.GENERAL):
            query_words = state.get("query", "").lower().split()
            if len(query_words) <= 3:
                return "direct_respond"
            if confidence >= 0.8:
                return "direct_respond"
        if _is_standalone_code_generation_request(query, intent):
            return "gather"

        # --- Execution layer routing ---
        if execution_config is not None and execution_config.enabled:
            intent_str = str(intent.value).upper() if hasattr(intent, "value") else str(intent).upper()
            complexity_str = str(complexity.value).upper() if hasattr(complexity, "value") else str(complexity).upper()

            # Check all conditions for external execution
            blocked_by_policy = False
            reason = ""

            # 1. Intent must not be blocked
            if intent_str in execution_config.blocked_intents:
                blocked_by_policy = True
                reason = "blocked_intent"

            # 2. Complexity must meet threshold
            elif not _complexity_meets_threshold(complexity, execution_config.complexity_threshold):
                reason = "complexity_below_threshold"

            # 3. Check for sensitive sources in context
            elif _has_sensitive_sources(state.get("context_blocks", [])):
                blocked_by_policy = True
                reason = "sensitive_context_sources"

            else:
                # All conditions met — route to execute
                _emit_gemilyni_routing(
                    state=state,
                    selected_path="execute",
                    reason="complexity_above_threshold_and_externalizable",
                    complexity=complexity_str,
                    complexity_threshold=execution_config.complexity_threshold,
                    intent=intent_str,
                    blocked_by_policy=False,
                    externalizable=True,
                    execution_enabled=True,
                )
                return "execute"

            # Blocked — log the reason and fall through to local paths
            if blocked_by_policy or reason:
                log.info(
                    "Execution blocked: reason=%s intent=%s complexity=%s",
                    reason, intent_str, complexity_str,
                )
                _emit_gemilyni_routing(
                    state=state,
                    selected_path="local",
                    reason=reason,
                    complexity=complexity_str,
                    complexity_threshold=execution_config.complexity_threshold,
                    intent=intent_str,
                    blocked_by_policy=blocked_by_policy,
                    externalizable=False,
                    execution_enabled=True,
                )

        # v1.5: Dynamic routing for COMPLEX+ with mode="dynamic". Prefer this
        # before the LLM fallback so complex workflows do not wait on a routing
        # LLM just to choose the decomposition path.
        if _dynamic_decompose_enabled(complexity):
            return "decompose"

        # Low confidence - delegate to LLM
        if confidence < _confidence_threshold():
            return "llm_fallback"

        # Default: deterministic routing
        return "gather"

    return _route_decision


# Default route_decision (without execution config) for backward compat
def route_decision(state: SymbiontState) -> str:
    """Conditional edge after classify: pick next node based on confidence.

    Returns:
        "direct_respond" - trivial query, skip context/agents
        "llm_fallback"   - low confidence, ask LLM to route
        "decompose"      - v1.5: complex query in dynamic mode, decompose first
        "gather"         - high confidence, proceed with deterministic routing
    """
    intent = state["intent"]
    confidence = state.get("confidence", 0.5)
    complexity = state.get("complexity", Complexity.NORMAL)
    history = state.get("history")

    # Audio transcription — always route to gather (specific action, no LLM needed)
    if _matches_enum(intent, Intent.AUDIO):
        return "gather"

    query = state.get("query", "")
    workspace_bound = _is_workspace_bound_task(query)
    if workspace_bound:
        return "gather"
    if requires_context_gather(query):
        return "gather"
    if (
        needs_system_context(query)
        or _matches_enum(intent, Intent.SYSTEM)
        or _matches_enum(intent, Intent.SYSTEM_AND_LOCAL)
    ):
        return "gather"

    if (
        _is_conversation_memory_query(query)
        or _is_conversation_memory_write(query)
        or _is_simple_greeting(query)
    ):
        return "direct_respond"

    # Context-aware fallback: with an active session, anything below the (higher)
    # history-aware threshold is delegated to the LLM router so it can resolve
    # follow-up references before any direct-response shortcut fires.
    if history and confidence < _history_aware_threshold():
        return "llm_fallback"

    # Trivial: greetings, very simple questions with no info need
    if _matches_enum(intent, Intent.CLARIFY):
        return "direct_respond"
    if _matches_enum(complexity, Complexity.SIMPLE) and _matches_enum(intent, Intent.GENERAL):
        # Short general queries (greetings, thanks, etc.) go direct
        query_words = state.get("query", "").lower().split()
        if len(query_words) <= 3:
            return "direct_respond"
        if confidence >= 0.8:
            return "direct_respond"

    # General knowledge questions (no local/code/system context needed) → direct LLM
    # The direct_respond node already includes session history in its prompt, so
    # short follow-ups with history will be resolved by the LLM seeing prior turns.
    if _matches_enum(intent, Intent.GENERAL) and _matches_enum(complexity, Complexity.NORMAL):
        return "direct_respond"
    if _is_standalone_code_generation_request(query, intent):
        return "gather"

    # v1.5: Dynamic routing for COMPLEX+ with mode="dynamic"
    if _dynamic_decompose_enabled(complexity):
        return "decompose"

    # Low confidence - delegate to LLM
    if confidence < _confidence_threshold():
        return "llm_fallback"

    # Default: deterministic routing
    return "gather"
