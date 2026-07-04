"""Ollama implementation of the LLMClient protocol."""

from __future__ import annotations

import logging
import re

import httpx
from context_governor import govern_chat_completion

from obsidian_rag.config import settings

log = logging.getLogger(__name__)

_THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)


class OllamaLLMClient:
    """LLMClient backed by an Ollama server.

    Reads ``base_url`` from config when not provided explicitly.
    """

    def __init__(self, *, base_url: str | None = None) -> None:
        self._base_url = base_url or settings.ollama.base_url

    def generate(
        self,
        prompt: str,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 64,
        timeout: float = 30.0,
    ) -> str:
        """Generate text through governed chat completion."""
        try:
            raw = govern_chat_completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                base_url=self._base_url,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                phase="obsidian_rag.generate",
                post=httpx.post,
            )
            return _THINK_PATTERN.sub("", raw).strip()
        except Exception as exc:
            log.warning("LLMClient.generate failed: %s", exc)
            raise

    def chat(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 64,
        timeout: float = 30.0,
    ) -> str:
        """Chat completion through the Context Governor."""
        try:
            raw = govern_chat_completion(
                model=model,
                messages=messages,
                base_url=self._base_url,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                phase="obsidian_rag.chat",
                post=httpx.post,
            )
            return _THINK_PATTERN.sub("", raw).strip()
        except Exception as exc:
            log.warning("LLMClient.chat failed: %s", exc)
            raise

    def health(self) -> bool:
        """Return *True* if the Ollama server is reachable."""
        try:
            resp = httpx.get(f"{self._base_url}/api/tags", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
