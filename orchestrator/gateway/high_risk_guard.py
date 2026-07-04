"""Gateway policy guard for destructive requests against sensitive targets."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class HighRiskBlock:
    reason: str
    response: str


_DESTRUCTIVE_PATTERNS = (
    r"\brm\s+-[^\n]*r[f]?\b",
    r"\bdelete\b",
    r"\bremove\b",
    r"\bwipe\b",
    r"\berase\b",
    r"\bdestroy\b",
    r"\boverwrite\b",
    r"\bmutate\b",
    r"\bapply\b",
    r"\bapaga(?:r|ste)?\b",
    r"\bapag(?:a|ar|ue|uem)\b",
    r"\belimina(?:r|ste)?\b",
    r"\bremove(?:r)?\b",
    r"\bsobrescrev(?:er|e)\b",
    r"\brestaur(?:a|ar)\b",
    r"\baplica(?:r)?\b",
    r"\bexecuta(?:r)?\b",
)

_SENSITIVE_TARGET_PATTERNS = (
    r"\breal\s+storage\b",
    r"\bstorage\s+real\b",
    r"\bmanaged\s+storage\b",
    r"\bexternal\s+storage\b",
    r"\barmazenamento\s+real\b",
    r"\barmazenamento\s+externo\b",
    r"\bhost\s+files?\b",
    r"\bficheiros?\s+do\s+host\b",
    r"\b/run/secrets\b",
    r"\bsecrets?\b",
    r"\bsegredos?\b",
    r"\bcredentials?\b",
    r"\bcredenciais?\b",
    r"\bdocker\s+socket\b",
    r"\bmounts?\b",
    r"\bprivileged\s+system\b",
    r"\bsistema\s+privilegiado\b",
)

_SAFE_PLANNING_PATTERNS = (
    r"\bdry[- ]run\b",
    r"\bplan(?:o|ear|ning)?\b",
    r"\bsimula(?:r|cao|ção)?\b",
    r"\bsandbox\b",
    r"\bsem\s+apply\b",
    r"\bsem\s+aplicar\b",
)

_ALIAS_CONTEXT_MARKERS = (
    "[Contexto local read-only recolhido pelo alias @",
    "[Contexto local read-only recolhido pelo alias @".lower(),
    "[Contexto local read-only recolhido pelo alias @".replace("í", "i").lower(),
)


def block_high_risk_mutation(query: str) -> HighRiskBlock | None:
    """Return a deterministic block for destructive requests against sensitive targets.

    The orchestrator owns policy gates. This guard does not execute, parse, or
    replace storage behavior; it prevents the generic responder from turning a
    high-risk natural-language request into execution instructions.
    """
    text = _normalize(_strip_injected_context(query))
    risky_segments = [
        segment
        for segment in _risk_segments(text)
        if any(re.search(pattern, segment) for pattern in _DESTRUCTIVE_PATTERNS)
        and any(re.search(pattern, segment) for pattern in _SENSITIVE_TARGET_PATTERNS)
    ]
    if not risky_segments:
        return None

    planning_only = any(
        any(re.search(pattern, segment) for pattern in _SAFE_PLANNING_PATTERNS)
        for segment in risky_segments
    )
    asks_to_apply = any(_asks_to_apply_now(segment) for segment in risky_segments)
    if planning_only and not asks_to_apply:
        return None

    return HighRiskBlock(
        reason="destructive_sensitive_target",
        response=(
            "Pedido bloqueado pela policy: apagar, sobrescrever, restaurar ou aplicar "
            "mudancas em storage real, ficheiros do host, segredos ou estado privilegiado "
            "exige aprovacao explicita/preapproval e deve passar pelo owner apropriado com "
            "dry-run e validacao em sandbox. Posso ajudar a transformar isto num plano seguro "
            "sem executar nem sugerir comandos destrutivos."
        ),
    )


def _strip_injected_context(query: str) -> str:
    """Keep policy intent checks scoped to the user's prompt, not alias evidence."""
    text = query or ""
    normalized = _normalize(text)
    cut_points: list[int] = []
    for marker in _ALIAS_CONTEXT_MARKERS:
        marker_normalized = _normalize(marker)
        position = normalized.find(marker_normalized)
        if position >= 0:
            cut_points.append(position)
    if not cut_points:
        return text
    return text[: min(cut_points)]


def _risk_segments(text: str) -> list[str]:
    """Split intent text so unrelated constraints do not combine into a risk."""
    return [
        segment.strip()
        for segment in re.split(r"[\n\r.;!?]+", text)
        if segment.strip()
    ]


def _asks_to_apply_now(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "aplica",
            "apply",
            "executa",
            "execute",
            "procede",
            "proceed",
            "agora",
            "now",
        )
    )


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return without_marks.lower()
