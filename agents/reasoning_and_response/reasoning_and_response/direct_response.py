"""Direct response provider for reasoning_and_response."""

from __future__ import annotations

import ast
import logging
import re
from typing import Any

from sharedai.llm.utils import strip_think as _strip_think

from reasoning_and_response.config import get_settings
from reasoning_and_response.synthesis import _call_governed_llm, _language_policy, _prompt
from reasoning_and_response.types import ChatMessage, LLMConfigOverride, RespondResponse

log = logging.getLogger(__name__)

_RESPOND_PROMPT = _prompt("direct_response.md")
_CODE_RESPOND_PROMPT = _prompt("code_response.md")

_CODE_DOMAIN_RE = re.compile(
    r"\b("
    r"python|javascript|typescript|rust|java|go|sql|bash|shell|"
    r"class|classe|function|fun[cç][aã]o|method|m[eé]todo|"
    r"script|module|m[oó]dulo|api|endpoint|test|teste|tests|code|c[oó]digo"
    r")\b",
    re.IGNORECASE,
)
_CODE_ACTION_RE = re.compile(
    r"\b("
    r"create|generate|write|implement|build|fix|debug|refactor|review|test|"
    r"cria|criar|gera|gerar|escreve|implementar|implementa|corrige|depura|"
    r"refatora|refactora|rev[eê]|testa|constr[oó]i|construir"
    r")\b",
    re.IGNORECASE,
)
_CODE_ARTIFACT_RE = re.compile(
    r"```|\b[\w.-]+\.(?:py|pyi|js|ts|tsx|jsx|json|ya?ml|toml|sh|sql)\b",
    re.IGNORECASE,
)
_PYTHON_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(?P<code>[\s\S]*?)```", re.IGNORECASE)
_SELF_DEFAULT_RE = re.compile(r"def\s+\w+\s*\([^)]*=\s*self\.", re.IGNORECASE)
_SUSPICIOUS_SELF_ATTR_RE = re.compile(r"\bself\._\d+\w*")


def _is_code_response_request(query: str) -> bool:
    q = query or ""
    if not q.strip():
        return False
    return bool(
        (_CODE_DOMAIN_RE.search(q) and _CODE_ACTION_RE.search(q))
        or _CODE_ARTIFACT_RE.search(q)
    )


def _python_blocks(text: str) -> list[str]:
    blocks = [match.group("code").strip() for match in _PYTHON_FENCE_RE.finditer(text or "")]
    if blocks:
        return [block for block in blocks if block]
    stripped = (text or "").strip()
    if stripped.startswith(("class ", "def ", "from ", "import ")):
        return [stripped]
    return []


def _python_static_issues(text: str) -> list[str]:
    issues: list[str] = []
    for index, code in enumerate(_python_blocks(text), start=1):
        label = f"python block {index}"
        if _SUSPICIOUS_SELF_ATTR_RE.search(code):
            issues.append(f"{label}: suspicious generated self attribute name")
        if _SELF_DEFAULT_RE.search(code):
            issues.append(f"{label}: default argument references self")
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            issues.append(f"{label}: syntax error at line {exc.lineno}")
            continue
        assigned_attrs: set[str] = set()
        loaded_attrs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = list(node.args.defaults) + list(node.args.kw_defaults)
                if any(
                    isinstance(child, ast.Name) and child.id == "self"
                    for default in defaults
                    if default is not None
                    for child in ast.walk(default)
                ):
                    issues.append(f"{label}: default argument references self")
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name) or node.value.id != "self":
                continue
            if isinstance(node.ctx, ast.Store):
                assigned_attrs.add(node.attr)
            elif isinstance(node.ctx, ast.Load):
                loaded_attrs.add(node.attr)
        undefined = sorted(attr for attr in loaded_attrs if attr.startswith("_") and attr not in assigned_attrs)
        if undefined:
            issues.append(f"{label}: self attributes used before assignment: {', '.join(undefined[:5])}")
    return issues


def _repair_code_response_once(
    *,
    query: str,
    draft: str,
    issues: list[str],
    model: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    language_instruction: str,
) -> str:
    issue_text = "\n".join(f"- {issue}" for issue in issues)
    messages = [
        {
            "role": "system",
            "content": (
                "You repair generated code responses.\n"
                "Fix only the listed issues while preserving the user's requested behavior.\n"
                "Return a complete corrected answer. Do not expose reasoning.\n"
                f"Language policy: {language_instruction}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"User request:\n{query}\n\n"
                f"Issues to fix:\n{issue_text}\n\n"
                f"Draft response:\n{draft}"
            ),
        },
    ]
    return _call_governed_llm(messages, model, base_url, min(float(temperature), 0.1), max_tokens, timeout)


def respond(
    query: str,
    *,
    history: list[ChatMessage] | None = None,
    context: str = "",
    budget_tokens: int | None = None,
    metadata: dict[str, Any] | None = None,
    llm_config: LLMConfigOverride | None = None,
) -> RespondResponse:
    """Generate a direct response for the user-visible query."""

    cfg = get_settings()
    metadata = metadata or {}
    default_system_prompt = _CODE_RESPOND_PROMPT if _is_code_response_request(query) else _RESPOND_PROMPT
    if llm_config and llm_config.model:
        model = llm_config.model
        base_url = llm_config.backend_url
        system_prompt = llm_config.system_prompt or default_system_prompt
        temperature = llm_config.parameters.get("temperature", cfg.llm.temperature)
        max_tokens = budget_tokens or llm_config.parameters.get("max_tokens", cfg.llm.max_tokens)
        timeout = llm_config.parameters.get("timeout", cfg.llm.timeout_seconds)
    else:
        model = cfg.llm.model
        base_url = cfg.llm.base_url
        system_prompt = default_system_prompt
        temperature = cfg.llm.temperature
        max_tokens = budget_tokens or cfg.llm.max_tokens
        timeout = cfg.llm.timeout_seconds

    compact_context = (context or "")[: cfg.response.max_context_chars]
    history_text = _history_text(history or [], cfg.response.max_history_messages)
    original_query, language_instruction = _language_policy(metadata, query)
    system = system_prompt.format(
        query=query,
        original_query=original_query,
        language_instruction=language_instruction,
        context=compact_context or "(none)",
        history=history_text or "(none)",
    )
    messages = [{"role": "system", "content": system}]
    for msg in (history or [])[-cfg.response.max_history_messages:]:
        if msg.content:
            messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": query})

    try:
        content = _call_governed_llm(messages, model, base_url, temperature, max_tokens, timeout)
        if _is_code_response_request(query):
            issues = _python_static_issues(content)
            if issues:
                repaired = _repair_code_response_once(
                    query=query,
                    draft=content,
                    issues=issues,
                    model=model,
                    base_url=base_url,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    language_instruction=language_instruction,
                )
                if not _python_static_issues(repaired):
                    content = repaired
        return RespondResponse(
            response=_strip_think(content),
            model_used=str(model),
            metadata={"provider_mode": "respond", **metadata},
        )
    except Exception as exc:
        log.warning("direct_response: LLM failed: %s", exc)
        fallback = compact_context.strip() or "Nao foi possivel gerar uma resposta neste momento."
        return RespondResponse(
            response=_strip_think(fallback),
            model_used=str(model),
            metadata={"provider_mode": "respond", "fallback_reason": "llm_unavailable", **metadata},
        )


def _history_text(history: list[ChatMessage], max_messages: int) -> str:
    lines: list[str] = []
    for msg in history[-max_messages:]:
        content = msg.content.strip()
        if content:
            lines.append(f"{msg.role}: {content[:800]}")
    return "\n".join(lines)
