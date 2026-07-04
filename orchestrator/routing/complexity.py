"""Query complexity classification.

Adaptado de obsidian_rag/retrieval/rag.py _estimate_complexity().
"""

from __future__ import annotations

from orchestrator.types import Complexity

# Indicators of deep reasoning
_DEEP_SIGNALS = frozenset({
    "analisa", "análise", "analyze", "analysis",
    "compara", "compare", "comparação", "comparison",
    "prós", "contras", "pros", "cons", "trade-off", "tradeoff",
    "porquê", "why", "razão", "reason",
    "debug", "debugging", "depura", "depurar",
    "arquitectura", "arquitetura", "architecture", "design",
    "raciocínio", "reasoning", "chain-of-thought",
    "passo a passo", "step by step",
    "explica em detalhe", "explain in detail",
    "avalia", "evaluate", "evaluation",
})

_CODE_COMPLEXITY_SIGNALS = frozenset({
    "refactor", "refactora", "refatorar", "rewrite", "reescreve",
    "implementa", "implement", "escreve", "write",
    "cria", "create", "gera", "generate",
    "converte", "convert", "migra", "migrate",
    "optimiza", "optimize",
})


class HeuristicComplexityClassifier:
    """Keyword + length heuristic for complexity estimation."""

    def classify(self, query: str) -> Complexity:
        q_lower = query.lower()
        words = [w for w in q_lower.split() if w.strip()]
        word_set = {w.strip(".,!?:;\"'()[]{}") for w in words}
        word_count = len(words)

        has_deep = bool(word_set & _DEEP_SIGNALS)
        has_code_gen = bool(word_set & _CODE_COMPLEXITY_SIGNALS)
        has_boolean = any(op in q_lower for op in (" and ", " or ", " not ", " && ", " || ", " e ", " ou "))
        multi_question = q_lower.count("?") > 1

        if has_deep or (has_boolean and word_count > 10) or multi_question:
            return Complexity.DEEP

        if has_code_gen or word_count > 8:
            return Complexity.COMPLEX

        if word_count <= 3:
            return Complexity.SIMPLE

        return Complexity.NORMAL
