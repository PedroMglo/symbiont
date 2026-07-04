"""Direct Answer Guard — blocks prewarming for queries answerable without tools."""

from __future__ import annotations

import re

from orchestrator.prewarming.signals import RequestSignals

# Patterns that strongly indicate "direct answer" (no container needed)
_GREETING_RE = re.compile(
    r"^(ol[áa]|hello|hi|hey|oi|bom dia|boa tarde|boa noite|good morning|good evening)\b",
    re.IGNORECASE,
)
_TRANSLATION_RE = re.compile(
    r"(traduz|translate|em ingl[êe]s|em portugu[êe]s|to english|to portuguese|para ingl[êe]s|para portugu[êe]s)",
    re.IGNORECASE,
)
_KNOWLEDGE_QA_RE = re.compile(
    r"(o que [ée]|what is|define|defini[çc][ãa]o|meaning of|explica[r]?\s+(o que|como|what|how)|explain\s+(what|how))",
    re.IGNORECASE,
)
_MATH_RE = re.compile(
    r"(quanto [ée]|how much is|calculate|calcula|\d+\s*[\+\-\*\/\^]\s*\d+)",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    r"(diferen[çc]a entre|difference between|compare|compara[r]?\s|vs\.?\s|\bversus\b)",
    re.IGNORECASE,
)
_SIMPLE_TASK_RE = re.compile(
    r"^(diz|say|escreve|write|lista|list)\s+.{1,30}$",
    re.IGNORECASE,
)

# Keywords that indicate a tool IS needed (overrides guard)
_TOOL_MARKERS = frozenset({
    # system
    "ram", "vram", "gpu", "cpu", "disco", "disk", "container", "containers",
    "docker", "nvidia", "processos", "processes", "sistema", "uptime",
    # personal
    "email", "emails", "calendário", "calendario", "agenda", "reunião",
    "meeting", "rss", "feeds",
    # code
    "repo", "repositório", "repository", "ficheiro", "file", "git",
    "refactor", "debug", "analisa",
    # research
    "obsidian", "vault", "notas", "notes", "procura", "pesquisa",
    # audio
    "transcreve", "transcribe", "áudio", "audio", "whisper",
})


class DirectAnswerGuard:
    """Fast heuristic to detect queries that don't need any container.

    Runs BEFORE L0 rule routing. If the guard fires, the entire prewarm
    pipeline is skipped (no containers started).

    Design principle: conservative — only blocks when confident.
    A false negative (guard doesn't fire, container starts unnecessarily)
    is cheaper than a false positive (guard fires, needed container not started).
    """

    def is_direct_answer(self, query: str, signals: RequestSignals) -> bool:
        """Return True if query is likely answerable without any container/tool.

        Args:
            query: Raw user query text.
            signals: Pre-extracted signals from SignalExtractor.

        Returns:
            True if prewarming should be skipped entirely.
        """
        # If we have file attachments or code blocks, always allow prewarm
        if signals.has_file or signals.file_extensions or signals.has_code_block:
            return False

        # If pattern matches fired (strong tool signal), allow prewarm
        if signals.pattern_matches:
            return False

        q_lower = query.lower().strip()
        words = q_lower.split()

        # Check if any tool-marker keyword is present in the query
        query_words = {w.strip(".,!?:;\"'()[]{}") for w in words}
        if query_words & _TOOL_MARKERS:
            return False

        # Very short queries with no tool keywords → likely direct answer
        if len(words) <= 5 and not signals.keywords_found:
            return True

        # Greeting
        if _GREETING_RE.search(q_lower):
            return True

        # Translation request (no tool needed)
        if _TRANSLATION_RE.search(q_lower):
            return True

        # Simple math
        if _MATH_RE.search(q_lower):
            return True

        # Simple task (e.g., "diz olá mundo", "say hello")
        if _SIMPLE_TASK_RE.match(q_lower):
            return True

        # Knowledge/explanation question without tool markers
        if _KNOWLEDGE_QA_RE.search(q_lower) and not signals.keywords_found:
            return True

        # Comparison without tool markers
        if _COMPARISON_RE.search(q_lower) and not signals.keywords_found:
            return True

        return False
