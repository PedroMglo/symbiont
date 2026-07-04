"""Cheap local responses for trivial conversation control turns."""

from __future__ import annotations

import re


def memory_write_ack(query: str) -> str | None:
    """Return a deterministic acknowledgement for simple chat-memory commands."""
    q = (query or "").strip()
    if not q:
        return None

    normalized = " ".join(q.lower().split())
    command_pattern = re.compile(
        r"^(?:por favor\s+)?(?:"
        r"memoriza|lembra-te|lembra te|guarda isto|guarda que|recorda que|"
        r"nao te esquecas|não te esqueças|remember this|remember that|remember|memorize"
        r")\b",
        re.IGNORECASE,
    )
    if command_pattern.search(normalized) is None:
        return None

    value = ""
    word_match = re.search(r"\b(?:palavra|word)\s+[\"'“”]?([^\"'“”.!?\n]+)", q, re.IGNORECASE)
    if word_match:
        value = word_match.group(1).strip()
    else:
        generic_match = re.search(
            r"\b(?:memoriza|memorize|lembra-te|lembra te|recorda|remember(?: this| that)?)\b[:\s,]*(.+)",
            q,
            re.IGNORECASE,
        )
        if generic_match:
            value = generic_match.group(1).strip(" \t\n\"'“”.!?")

    if value and len(value) <= 80:
        return f"Memorizado: {value}."
    return "Memorizado."


def memory_read_response(query: str, history: list[dict] | None) -> str | None:
    """Answer simple questions about previous user turns from session history."""
    q = " ".join((query or "").lower().split())
    if not q:
        return None

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
    if not any(pattern in q for pattern in patterns):
        return None

    user_turns = [
        str(msg.get("content", "")).strip()
        for msg in (history or [])
        if msg.get("role") == "user" and str(msg.get("content", "")).strip()
    ]
    if not user_turns:
        return "Não encontro mensagens anteriores nesta sessão."

    previous = user_turns[-1]
    ack = memory_write_ack(previous)
    if ack and ack.startswith("Memorizado: "):
        value = ack.removeprefix("Memorizado: ").removesuffix(".")
        return f"Pediste-me para memorizar: {value}."

    return f'Perguntaste-me: "{previous}".'


def simple_greeting_response(query: str) -> str | None:
    """Return a local greeting for one-token salutation turns."""
    q = " ".join((query or "").strip().lower().split())
    if q in {"ola", "olá", "oi", "boas", "hello", "hi", "hey"}:
        return "Olá! Como posso ajudar?"
    return None


def local_conversation_response(query: str) -> str | None:
    """Return a local response for turns that do not need an LLM."""
    return memory_write_ack(query) or simple_greeting_response(query)
