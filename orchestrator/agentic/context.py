"""Request-local correlation context for agentic ledger recording."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class AgenticContext:
    task_id: str
    trace_id: str
    request_id: str
    session_id: str | None = None
    mode: str = "supervised"


_CONTEXT: ContextVar[AgenticContext | None] = ContextVar("agentic_context", default=None)


def get_agentic_context() -> AgenticContext | None:
    return _CONTEXT.get()


def set_agentic_context(ctx: AgenticContext | None) -> Token:
    return _CONTEXT.set(ctx)


def reset_agentic_context(token: Token) -> None:
    _CONTEXT.reset(token)


@contextmanager
def agentic_context(ctx: AgenticContext | None) -> Iterator[None]:
    token = set_agentic_context(ctx)
    try:
        yield
    finally:
        reset_agentic_context(token)
