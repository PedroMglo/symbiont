"""LLMClient protocol — backend-agnostic interface for LLM generation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class BatchRequest:
    """A single request within a batch of LLM calls."""

    messages: list[dict]
    model: str
    temperature: float = 0.7
    max_tokens: int | None = None
    request_id: str = ""


@dataclass
class BatchResult:
    """Result of a single request within a batch."""

    text: str
    request_id: str
    model: str
    success: bool = True
    error: str | None = None
    latency_ms: float = 0.0
    tokens_used: int = 0


@runtime_checkable
class LLMClient(Protocol):
    """Backend-agnostic LLM interface."""

    def generate(
        self,
        prompt: str,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str: ...

    def chat(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str: ...

    def chat_stream(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ):
        """Yield chunks of text as they arrive. Optional — may raise NotImplementedError."""
        ...

    async def chat_async(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Async chat — defaults to thread-wrapped sync."""
        return await asyncio.to_thread(
            self.chat, messages, model, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout,
        )

    async def chat_batch(
        self,
        requests: list[BatchRequest],
    ) -> list[BatchResult]:
        """Batch multiple chat requests. Defaults to sequential execution."""
        results: list[BatchResult] = []
        for req in requests:
            import time
            t0 = time.perf_counter()
            try:
                text = await self.chat_async(
                    req.messages, req.model,
                    temperature=req.temperature, max_tokens=req.max_tokens,
                )
                results.append(BatchResult(
                    text=text, request_id=req.request_id, model=req.model,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                ))
            except Exception as e:
                results.append(BatchResult(
                    text="", request_id=req.request_id, model=req.model,
                    success=False, error=str(e),
                    latency_ms=(time.perf_counter() - t0) * 1000,
                ))
        return results

    def health(self) -> bool: ...

    def list_models(self) -> list[str]:
        """Return list of available model names. Return [] if not supported."""
        return []
