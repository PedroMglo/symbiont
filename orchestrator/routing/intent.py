"""Keyword-based intent classification with optional LLM fallback.

Extraído e adaptado de obsidian_rag/retrieval/router.py.
"""

from __future__ import annotations

import logging

from orchestrator.types import Intent

log = logging.getLogger(__name__)


def _last_assistant_snippet(history: list[dict] | None, max_chars: int) -> str:
    """Return a trimmed snippet of the most recent assistant turn, or ''."""
    if not history:
        return ""
    for turn in reversed(history):
        if turn.get("role") == "assistant":
            content = (turn.get("content") or "").strip()
            if content:
                return content[:max_chars]
    return ""


def is_anaphoric(query: str) -> bool:
    """True if the query references a previous turn (pronoun/demonstrative/continuation).

    Signal sets are configured in [classify] (anaphora_words / anaphora_patterns)
    — never hardcoded. Returns False if config is unavailable.
    """
    from orchestrator.config import get_settings

    cfg = get_settings().classify
    q_lower = query.lower()
    if any(pat in q_lower for pat in cfg.anaphora_patterns):
        return True
    words = {w.strip(".,!?:;\"'()[]{}") for w in q_lower.split()}
    words.discard("")
    return bool(words & cfg.anaphora_words)


def expand_query_with_history(query: str, history: list[dict] | None) -> str:
    """Prepend the previous assistant turn to an anaphoric query for classification.

    The expansion is only used to improve keyword matching — the original query is
    still what gets answered. Returns the original query when not anaphoric or when
    there is no usable history.
    """
    if not history or not is_anaphoric(query):
        return query
    snippet = _last_assistant_snippet(history, max_chars=400)
    if not snippet:
        return query
    return f"{snippet}\n{query}"

# ---------------------------------------------------------------------------
# Keyword sets (PT + EN)
# ---------------------------------------------------------------------------

_LOCAL_SIGNALS = frozenset({
    "meu", "minha", "meus", "minhas", "nosso", "nossa",
    "my", "our", "mine",
    "obsidian", "vault", "notas", "notes",
    "repo", "repositório", "repository", "projeto", "project",
    "ficheiro", "ficheiros", "file", "files",
    "configuração", "config", "setup",
    "documentos", "documents", "docs",
    "indexado", "indexed", "local",
    "pipeline", "codebase", "workspace",
    "modelfile", "modelfiles",
    "instalado", "instalados", "installed",
    "configurado", "configurados", "configured",
    "alias", "aliases", "funções", "functions",
})

_RESEARCH_ACTION_SIGNALS = frozenset({
    "procura", "procurar", "pesquisa", "pesquisar", "encontra", "encontrar",
    "resume", "resumir", "documentacao", "documentação", "docs", "notas",
    "evidencia", "evidência", "contexto", "rag", "qdrant", "vault",
})

_PERSONAL_CONTEXT_SIGNALS = frozenset({
    "prefiro", "preferência", "preferencias", "preferências", "preferencia",
    "guarda", "guardar", "lembra-te", "lembra", "memoriza", "recorda",
    "meu contexto pessoal", "minhas preferencias", "minhas preferências",
})

_NEGATIVE_CONTEXT_PATTERNS = (
    "nao procures", "não procures", "nao uses rag", "não uses rag",
    "nao usar rag", "não usar rag", "sem rag", "sem ferramentas",
    "sem contexto externo", "nao analises codigo", "não analises código",
)

_MULTI_CAPABILITY_TASK_SIGNALS = frozenset({
    "combina", "combinar", "audita", "auditar", "diagnostica", "diagnosticar",
    "compara", "comparar", "plano", "completo", "todos", "todas",
    "migração", "migracao", "drift", "slo", "slos", "evidencias",
    "evidências", "rollback", "correcoes", "correções",
})

_GRAPH_SIGNALS = frozenset({
    "depende", "dependência", "dependências", "depends", "dependency",
    "chama", "chamada", "calls", "called",
    "importa", "imports", "importação",
    "fluxo", "flow", "pipeline", "cadeia", "chain",
    "arquitectura", "arquitetura", "architecture", "structure", "estrutura",
    "impacto", "impact", "afeta", "affects",
    "relação", "relações", "relation", "relations", "relationship",
    "componente", "componentes", "component", "components",
    "módulo", "módulos", "module", "modules",
    "vizinhos", "neighbors", "neighbour",
    "comunidade", "community",
    "grafo", "graph",
    "upstream", "downstream", "montante", "jusante",
})

_SYSTEM_SIGNALS = frozenset({
    "ram", "memória", "memory", "vram",
    "gpu", "cpu", "processador", "processor",
    "disco", "disk", "storage", "armazenamento",
    "temperatura", "temperature", "temp",
    "processos", "processes",
    "sistema", "system",
    "kernel", "driver", "drivers",
    "nvidia", "amd", "cuda",
    "swap", "hardware",
    "máquina", "machine", "pc", "computador", "computer",
    "uptime", "carga", "load",
    "espaço", "space",
    "rede", "network", "ip",
    "bateria", "battery",
})

_CODE_SIGNALS = frozenset({
    "função", "funcao", "function", "method", "método",
    "classe", "class", "refactor", "refactora", "refatorar",
    "bug", "bugs", "debug", "debugging", "depurar",
    "implementa", "implement", "escreve", "write", "analisa", "analisar",
    "verifica", "verificar", "cobre", "cobrem", "cobertura",
    "código", "codigo", "code", "script", "scripts", "programa", "program",
    "async", "await", "loop", "recursão", "recursion",
    "api", "endpoint", "endpoints", "rest", "grpc",
    "teste", "testes", "test", "tests", "testa", "testing",
    "compila", "compile", "build", "docker", "makefile",
    "erro", "error", "exception", "traceback",
    "python", "javascript", "typescript", "rust", "go", "java",
    "sql", "bash", "shell", "zsh",
    "git", "commit", "branch", "merge", "pull", "push",
    "repositório", "repositórios", "repository", "repositories",
})

_CODE_CREATION_SIGNALS = frozenset({
    "cria", "criar", "create",
    "gera", "gerar", "generate",
    "escreve", "write",
    "implementa", "implementar", "implement",
    "build", "constrói", "constroi", "construir",
})

_LOCAL_SCOPE_HINTS = frozenset({
    "meu", "minha", "meus", "minhas", "nosso", "nossa",
    "my", "our", "mine",
    "repo", "repositório", "repository",
    "projeto", "project",
    "ficheiro", "ficheiros", "file", "files",
    "workspace", "codebase",
    "módulo", "módulos", "module", "modules",
})

_AUDIO_ACTION_SIGNALS = frozenset({
    "transcreve", "transcrever", "transcrição", "transcricao",
    "transcribe", "transcription", "transcribing",
    "legendas", "subtitles", "legenda", "subtitle",
})

_AUDIO_EXTENSIONS = frozenset({
    "mp3", "wav", "m4a", "flac", "ogg", "mp4", "mkv", "webm", "mov", "avi",
})

_AUDIO_PATTERNS = (
    "transcreve o", "transcreve este", "transcreve a", "transcreve as",
    "transcreve os", "transcrever o", "transcrever este",
    "transcreve o seguinte", "transcreve o audio",
    "transcreve todos", "transcreve tudo",
    "transcribe the", "transcribe this", "transcribe all",
    "gera legendas", "gerar legendas", "generate subtitles",
)

_AUDIO_TOPIC_SIGNALS = frozenset({
    "áudio", "audio", "áudios", "audios", "whisper",
})

_SYSTEM_PATTERNS = (
    "quanto de ram", "quanta ram", "quanta memória", "memória livre",
    "espaço em disco", "espaço livre", "uso do disco",
    "temperatura do", "temperatura da",
    "está a usar gpu", "está a usar a gpu",
    "o que está a correr", "o que está a consumir",
    "processos activos", "processos ativos",
    "carga do sistema", "uso de cpu",
    "how much memory", "how much ram", "free memory", "free ram",
    "disk space", "disk usage", "free space",
    "temperature of", "gpu temperature", "cpu temperature",
    "using my gpu", "what is running", "what is using",
    "system load", "cpu usage", "memory usage",
)

_SYSTEM_FALSE_POSITIVES = (
    "machine learning", "system design", "system prompt",
    "operating system", "file system", "type system",
    "memory model", "memory management", "memory leak",
    "memory safety", "space complexity", "disk image",
    "network protocol", "network layer", "ip address",
    "load balancing", "load balancer",
)

# Signals that indicate a GENERAL/contextual query (email, news, calendar).
# When these are present, weak system signals (like "pc") are suppressed.
_GENERAL_CONTEXT_SIGNALS = frozenset({
    "email", "emails", "correio", "mail", "mails",
    "notícias", "noticias", "news", "feeds", "rss",
    "calendário", "calendario", "agenda", "eventos", "events",
    "resumo", "briefing", "summary",
})

# System words that are weak (contextual/ambient, not actual system queries)
_WEAK_SYSTEM_SIGNALS = frozenset({
    "pc", "computador", "computer", "máquina", "machine",
})

_GRAPH_PATTERNS = (
    "como funciona", "como é que", "o que chama", "quem chama",
    "o que depende", "quem depende", "qual o fluxo", "qual é o fluxo",
    "que relação", "como se liga", "como interage",
    "o que acontece se mudar", "impacto de alterar",
    "este projeto", "este repo", "este módulo", "este pipeline",
    "o meu pipeline", "o meu repo", "o meu projeto",
    "how does", "what calls", "what depends", "call chain", "call flow",
    "depends on", "used by", "calls to",
    "this project", "this repo", "this module", "this pipeline",
    "my pipeline", "my repo", "my project",
)


# ---------------------------------------------------------------------------
# Heuristic classifier
# ---------------------------------------------------------------------------

class HeuristicIntentClassifier:
    """Keyword-based intent classification."""

    def classify(self, query: str, *, history: list[dict] | None = None) -> Intent:
        # Expand anaphoric follow-ups ("explica isso", "e o ponto 2") with the
        # previous assistant turn so keyword matching stays context-aware.
        query = expand_query_with_history(query, history)
        q_lower = query.lower()
        words = {w.strip(".,!?:;\"'()[]{}") for w in q_lower.split()}
        words.discard("")

        # --- Audio detection (highest priority — specific action) ---
        has_audio = bool(words & _AUDIO_ACTION_SIGNALS) or any(p in q_lower for p in _AUDIO_PATTERNS)
        # Also detect audio file extensions in the query (e.g. ".mp3", ".wav")
        if not has_audio:
            has_audio = any(f".{ext}" in q_lower for ext in _AUDIO_EXTENSIONS)
        if has_audio:
            return Intent.AUDIO

        has_local = bool(words & _LOCAL_SIGNALS)
        has_graph = bool(words & _GRAPH_SIGNALS) or any(p in q_lower for p in _GRAPH_PATTERNS)
        has_system = bool(words & _SYSTEM_SIGNALS) or any(p in q_lower for p in _SYSTEM_PATTERNS)
        has_code = bool(words & _CODE_SIGNALS)
        has_research = (
            bool(words & _RESEARCH_ACTION_SIGNALS)
            or any(term in q_lower for term in ("nas notas", "no meu vault", "sobre agentic runtime"))
        )
        has_personal_context = bool(words & _PERSONAL_CONTEXT_SIGNALS) or any(
            term in q_lower for term in _PERSONAL_CONTEXT_SIGNALS
        )
        negative_context_request = any(pattern in q_lower for pattern in _NEGATIVE_CONTEXT_PATTERNS)
        if negative_context_request:
            has_local = False
            has_research = False
            has_personal_context = False
            has_code = False

        domain_count = sum(bool(value) for value in (has_research, has_personal_context, has_code, has_system, has_graph))
        if bool(words & _MULTI_CAPABILITY_TASK_SIGNALS) and (domain_count >= 2 or len(words) >= 8):
            return Intent.GENERAL

        # Suppress system false positives
        if has_system and any(fp in q_lower for fp in _SYSTEM_FALSE_POSITIVES):
            fp_words: set[str] = set()
            for fp in _SYSTEM_FALSE_POSITIVES:
                if fp in q_lower:
                    fp_words.update(fp.split())
            remaining = (words & _SYSTEM_SIGNALS) - fp_words
            remaining_patterns = any(
                p in q_lower for p in _SYSTEM_PATTERNS
                if not any(fp in q_lower and p in fp for fp in _SYSTEM_FALSE_POSITIVES)
            )
            has_system = bool(remaining) or remaining_patterns

        # Suppress weak system signals when general context signals are present
        # e.g. "acabei de ligar o PC, mostra emails e notícias" → GENERAL, not SYSTEM
        if has_system:
            system_words = words & _SYSTEM_SIGNALS
            general_ctx = words & _GENERAL_CONTEXT_SIGNALS
            if general_ctx and system_words and system_words <= _WEAK_SYSTEM_SIGNALS:
                # Only weak system signals + strong general context → suppress system
                if not any(p in q_lower for p in _SYSTEM_PATTERNS):
                    has_system = False

        # Priority: system > code > graph > local > general
        if has_system and has_local:
            return Intent.SYSTEM_AND_LOCAL
        if has_system:
            return Intent.SYSTEM
        if has_personal_context:
            return Intent.PERSONAL_CONTEXT
        code_creation_request = bool(words & _CODE_CREATION_SIGNALS)
        if has_code and has_graph and code_creation_request and not (has_local or (words & _LOCAL_SCOPE_HINTS)):
            return Intent.CODE
        if has_code and has_graph:
            return Intent.LOCAL_AND_GRAPH
        if has_code:
            return Intent.CODE
        if has_research:
            return Intent.RESEARCH
        if has_local and has_graph:
            return Intent.LOCAL_AND_GRAPH
        if has_graph:
            project_hints = {"meu", "minha", "nosso", "my", "our", "repo", "projeto", "project"}
            if words & project_hints:
                return Intent.LOCAL_AND_GRAPH
            return Intent.GENERAL
        if has_local:
            return Intent.LOCAL

        return Intent.GENERAL
