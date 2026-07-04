"""OpenAI-compatible LLM client — works with Ollama /v1, vLLM, llama.cpp, LM Studio, TGI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Iterator

import httpx

if TYPE_CHECKING:
    from orchestrator.config import BackendConfig
    from orchestrator.observability.models import LLMChatResult

from sharedai.llm.utils import _THINK_PATTERN, mask_url, strip_think

from orchestrator.llm.base import BatchRequest, BatchResult

log = logging.getLogger(__name__)

# Short timeout for health / model-list probes. Keep this low because health
# reports probe every enabled backend, including optional containers that may be
# intentionally stopped in the core profile.
try:
    _PROBE_TIMEOUT = max(0.2, float(os.getenv("ORC_LLM_PROBE_TIMEOUT_SECONDS", "1.5")))
except ValueError:
    _PROBE_TIMEOUT = 1.5
# Low-level fallback used only when this backend client is called directly
# instead of through LLMRouter, which supplies profile-derived num_predict.
_DIRECT_CLIENT_MAX_TOKENS = 4096


def _resolve_direct_max_tokens(max_tokens: int | None) -> int:
    return _DIRECT_CLIENT_MAX_TOKENS if max_tokens is None else max_tokens


def _vllm_max_output_tokens() -> int:
    raw = os.environ.get("VLLM_MAX_OUTPUT_TOKENS", "384")
    try:
        return max(16, int(raw))
    except ValueError:
        log.warning("Invalid VLLM_MAX_OUTPUT_TOKENS=%r; using 384", raw)
        return 384


def _cap_vllm_output_tokens(backend_name: str, max_tokens: int | None) -> int:
    max_tokens = _resolve_direct_max_tokens(max_tokens)
    if backend_name != "vllm":
        return max_tokens
    return min(max_tokens, _vllm_max_output_tokens())


class OpenAICompatibleLLMClient:
    """LLMClient backed by any OpenAI-compatible HTTP server.

    Supports: Ollama /v1, vLLM, llama.cpp server, LM Studio, TGI v2+.
    API key is read lazily from the env var named in ``BackendConfig.api_key_env``.
    """

    def __init__(self, config: "BackendConfig") -> None:
        self._cfg = config
        self._base_url = config.base_url.rstrip("/")
        self._masked_url = mask_url(self._base_url)
        # Cached health state with TTL
        self._last_health: bool = False
        self._health_ts: float = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _api_key(self) -> str:
        if not self._cfg.api_key_env:
            return ""
        return os.environ.get(self._cfg.api_key_env, "")

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        key = self._api_key()
        if self._cfg.api_key_env and not key:
            raise RuntimeError(
                f"Missing required backend API key env var: {self._cfg.api_key_env}"
            )
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _request_timeout(self, timeout: float | None) -> float:
        return float(self._cfg.request_timeout if timeout is None else timeout)

    def _stream_timeout(self, timeout: float | None) -> float:
        return float(self._cfg.stream_timeout if timeout is None else timeout)

    # ------------------------------------------------------------------
    # LLMClient protocol
    # ------------------------------------------------------------------

    def generate(
        self,
        prompt: str,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Single-turn generation via chat (OpenAI-compatible)."""
        return self.chat(
            [{"role": "user", "content": prompt}],
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    def chat(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        max_tokens = _cap_vllm_output_tokens(self._cfg.name, max_tokens)
        timeout = self._request_timeout(timeout)
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Disable thinking mode for Qwen3 on vLLM
        if self._cfg.name == "vllm":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            resp = httpx.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.error(
                "OpenAICompatibleLLMClient[%s]: chat failed HTTP %s",
                self._cfg.name,
                exc.response.status_code,
            )
            raise
        except httpx.RequestError as exc:
            log.error(
                "OpenAICompatibleLLMClient[%s]: chat request error: %s",
                self._cfg.name,
                type(exc).__name__,
            )
            raise

        data = resp.json()
        raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return strip_think(raw)

    def chat_stream(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        """Stream chat tokens via SSE (OpenAI /v1/chat/completions format)."""
        max_tokens = _cap_vllm_output_tokens(self._cfg.name, max_tokens)
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Disable thinking mode for Qwen3 on vLLM
        if self._cfg.name == "vllm":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        stream_timeout = httpx.Timeout(
            connect=5.0,
            read=self._stream_timeout(timeout),
            write=30.0,
            pool=10.0,
        )
        buffer = ""
        try:
            with httpx.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=stream_timeout,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if not content:
                        continue
                    # Strip think tags across chunks
                    buffer += content
                    # Emit only complete non-think segments
                    clean, buffer = _flush_think_buffer(buffer)
                    if clean:
                        yield clean
        except httpx.RequestError as exc:
            log.error(
                "OpenAICompatibleLLMClient[%s]: stream interrupted: %s",
                self._cfg.name,
                type(exc).__name__,
            )
            raise

        # Flush remaining buffer (no open think tag)
        if buffer and "<think>" not in buffer:
            clean = strip_think(buffer)
            if clean:
                yield clean

    # ------------------------------------------------------------------
    # Async methods (v1.2 — Pipeline Parallelism)
    # ------------------------------------------------------------------

    async def chat_async(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """True async chat using shared connection pool."""
        from orchestrator.llm.http_pool import get_async_client

        max_tokens = _cap_vllm_output_tokens(self._cfg.name, max_tokens)
        timeout = self._request_timeout(timeout)
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        client = await get_async_client()
        try:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.error(
                "OpenAICompatibleLLMClient[%s]: chat_async failed HTTP %s",
                self._cfg.name, exc.response.status_code,
            )
            raise
        except httpx.RequestError as exc:
            log.error(
                "OpenAICompatibleLLMClient[%s]: chat_async request error: %s",
                self._cfg.name, type(exc).__name__,
            )
            raise

        data = resp.json()
        raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return strip_think(raw)

    async def chat_batch(self, requests: list[BatchRequest]) -> list[BatchResult]:
        """Batch multiple chat requests concurrently via async pool."""
        if not requests:
            return []

        has_batch_cap = "batch" in self._cfg.capabilities

        if has_batch_cap:
            return await self._batch_concurrent(requests)
        return await self._batch_sequential(requests)

    async def _batch_concurrent(self, requests: list[BatchRequest]) -> list[BatchResult]:
        """Fire all requests concurrently (for vLLM and batch-capable backends)."""
        tasks = [self._single_batch_call(req) for req in requests]
        return list(await asyncio.gather(*tasks))

    async def _batch_sequential(self, requests: list[BatchRequest]) -> list[BatchResult]:
        """Process one at a time (for Ollama and sequential backends)."""
        results: list[BatchResult] = []
        for req in requests:
            results.append(await self._single_batch_call(req))
        return results

    async def _single_batch_call(self, req: BatchRequest) -> BatchResult:
        """Execute a single batch request and wrap into BatchResult."""
        t0 = time.perf_counter()
        try:
            text = await self.chat_async(
                req.messages, req.model,
                temperature=req.temperature, max_tokens=req.max_tokens,
            )
            return BatchResult(
                text=text, request_id=req.request_id, model=req.model,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return BatchResult(
                text="", request_id=req.request_id, model=req.model,
                success=False, error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    # ------------------------------------------------------------------
    # Health & model discovery
    # ------------------------------------------------------------------

    def health(self) -> bool:
        """Check backend availability — tries GET /v1/models, then /health."""
        now = time.monotonic()
        if now - self._health_ts < self._cfg.request_timeout:
            # Use module-level TTL from router; this per-client cache is a safety net only
            pass

        ok = self._probe_health()
        self._last_health = ok
        self._health_ts = now
        return ok

    def _probe_health(self) -> bool:
        """Attempt GET /v1/models; fall back to GET /health for TGI-style backends."""
        try:
            resp = httpx.get(
                f"{self._base_url}/models",
                headers=self._headers(),
                timeout=_PROBE_TIMEOUT,
            )
            if resp.status_code < 500:
                return True
        except httpx.RequestError:
            return False
        except Exception:
            pass
        # Fallback: /health endpoint (TGI, custom servers)
        try:
            resp = httpx.get(
                f"{self._base_url.removesuffix('/v1')}/health",
                timeout=_PROBE_TIMEOUT,
            )
            return resp.status_code < 500
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """Return model IDs available on this backend via GET /v1/models."""
        try:
            resp = httpx.get(
                f"{self._base_url}/models",
                headers=self._headers(),
                timeout=_PROBE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return [m.get("id", m.get("name", "")) for m in data.get("data", []) if m.get("id") or m.get("name")]
        except Exception as exc:
            log.debug(
                "OpenAICompatibleLLMClient[%s]: list_models failed: %s",
                self._cfg.name,
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # Instrumented variants (used by LLMRouter for observability)
    # ------------------------------------------------------------------

    def chat_instrumented(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
        num_ctx: int | None = None,
        use_native_ollama: bool = False,
    ) -> "LLMChatResult":
        """Like chat() but returns LLMChatResult with full metadata.

        If use_native_ollama=True and backend is Ollama, uses /api/chat for
        native timing data (load_duration, prompt_eval_duration, eval_duration).
        """
        from orchestrator.observability.models import LLMChatResult, LLMUsage

        max_tokens = _cap_vllm_output_tokens(self._cfg.name, max_tokens)
        timeout = self._request_timeout(timeout)
        if use_native_ollama and self._cfg.name == "ollama":
            return self._chat_ollama_native(messages, model, temperature=temperature, max_tokens=max_tokens, timeout=timeout, num_ctx=num_ctx)

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Disable thinking mode for Qwen3 on vLLM to avoid wasting tokens on <think> blocks
        if self._cfg.name == "vllm":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if num_ctx is not None:
            payload["options"] = {"num_ctx": num_ctx}

        t0 = time.monotonic()
        try:
            resp = httpx.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=timeout,
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError):
            raise

        elapsed_ms = (time.monotonic() - t0) * 1000
        data = resp.json()

        # Extract text
        choice = data.get("choices", [{}])[0]
        raw = choice.get("message", {}).get("content", "")
        text = strip_think(raw)

        # Extract usage
        usage_data = data.get("usage")
        if usage_data:
            usage = LLMUsage.from_backend(usage_data)
        else:
            usage = LLMUsage.estimated(
                "".join(m.get("content", "") for m in messages), text
            )

        # Finish reason
        finish_reason = choice.get("finish_reason")

        return LLMChatResult(
            text=text,
            model=model,
            backend=self._cfg.name,
            usage=usage,
            latency_ms=round(elapsed_ms, 1),
            finish_reason=finish_reason,
        )

    def _chat_ollama_native(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
        num_ctx: int | None = None,
    ) -> "LLMChatResult":
        """Call Ollama native /api/chat for detailed timing metrics."""
        from orchestrator.observability.models import LLMChatResult, LLMUsage, OllamaTiming

        # Ollama native endpoint is at base_url without /v1
        max_tokens = _resolve_direct_max_tokens(max_tokens)
        timeout = self._request_timeout(timeout)
        native_base = self._base_url.replace("/v1", "")
        options: dict = {"temperature": temperature, "num_predict": max_tokens}
        if num_ctx is not None:
            options["num_ctx"] = num_ctx

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": options,
        }

        t0 = time.monotonic()
        try:
            resp = httpx.post(
                f"{native_base}/api/chat",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError):
            raise

        elapsed_ms = (time.monotonic() - t0) * 1000
        data = resp.json()

        # Extract text
        raw = data.get("message", {}).get("content", "")
        text = strip_think(raw)

        # Extract Ollama native timing
        ollama_timing = OllamaTiming.from_response(data)

        # Build usage from Ollama data
        prompt_tokens = ollama_timing.prompt_eval_count
        completion_tokens = ollama_timing.eval_count
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
        usage = LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens if total_tokens else None,
            usage_source="backend" if prompt_tokens else "estimated",
        )
        if not prompt_tokens:
            usage = LLMUsage.estimated(
                "".join(m.get("content", "") for m in messages), text
            )

        return LLMChatResult(
            text=text,
            model=model,
            backend=self._cfg.name,
            usage=usage,
            latency_ms=round(elapsed_ms, 1),
            finish_reason="stop",
            ollama_timing=ollama_timing,
        )

    def chat_stream_instrumented(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | None = None,
        num_ctx: int | None = None,
    ) -> "InstrumentedStreamResult":
        """Like chat_stream() but wraps the generator to capture metrics.

        Returns an InstrumentedStreamResult that is iterable (yields str chunks)
        and exposes .result after iteration completes.
        """

        max_tokens = _cap_vllm_output_tokens(self._cfg.name, max_tokens)
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Disable thinking mode for Qwen3 on vLLM
        if self._cfg.name == "vllm":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if num_ctx is not None:
            payload["options"] = {"num_ctx": num_ctx}

        stream_timeout = httpx.Timeout(
            connect=5.0,
            read=self._stream_timeout(timeout),
            write=30.0,
            pool=10.0,
        )

        return InstrumentedStreamResult(
            url=f"{self._base_url}/chat/completions",
            payload=payload,
            headers=self._headers(),
            timeout=stream_timeout,
            model=model,
            backend_name=self._cfg.name,
            prompt_text="".join(m.get("content", "") for m in messages),
        )


class InstrumentedStreamResult:
    """Wraps a streaming LLM call, yielding chunks while capturing metadata."""

    def __init__(
        self,
        url: str,
        payload: dict,
        headers: dict,
        timeout: httpx.Timeout,
        model: str,
        backend_name: str,
        prompt_text: str,
    ) -> None:
        self._url = url
        self._payload = payload
        self._headers = headers
        self._timeout = timeout
        self._model = model
        self._backend_name = backend_name
        self._prompt_text = prompt_text
        self.result: "LLMChatResult | None" = None

    def __iter__(self) -> Iterator[str]:
        from orchestrator.observability.models import LLMChatResult, LLMUsage

        t0 = time.monotonic()
        first_token_t: float | None = None
        buffer = ""
        full_text = ""
        chunks_count = 0
        finish_reason: str | None = None
        usage_data: dict | None = None

        try:
            with httpx.stream(
                "POST", self._url,
                json=self._payload,
                headers=self._headers,
                timeout=self._timeout,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # Some backends send usage in the final chunk
                    if "usage" in chunk and chunk["usage"]:
                        usage_data = chunk["usage"]

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    content = delta.get("content")

                    # Capture finish_reason from last chunk
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                    if not content:
                        continue

                    if first_token_t is None:
                        first_token_t = time.monotonic()
                    chunks_count += 1

                    buffer += content
                    clean, buffer = _flush_think_buffer(buffer)
                    if clean:
                        full_text += clean
                        yield clean
        except httpx.RequestError as exc:
            log.error("InstrumentedStream[%s]: interrupted: %s", self._backend_name, type(exc).__name__)
            raise

        # Flush remaining
        if buffer and "<think>" not in buffer:
            clean = strip_think(buffer)
            if clean:
                full_text += clean
                yield clean

        elapsed_ms = (time.monotonic() - t0) * 1000
        first_token_ms = ((first_token_t - t0) * 1000) if first_token_t else None

        # Build usage
        if usage_data:
            usage = LLMUsage.from_backend(usage_data)
        else:
            usage = LLMUsage.estimated(self._prompt_text, full_text)

        self.result = LLMChatResult(
            text=full_text,
            model=self._model,
            backend=self._backend_name,
            usage=usage,
            latency_ms=round(elapsed_ms, 1),
            first_token_latency_ms=round(first_token_ms, 1) if first_token_ms else None,
            finish_reason=finish_reason,
            chunks_count=chunks_count,
        )


# ---------------------------------------------------------------------------
# Think-tag streaming helper
# ---------------------------------------------------------------------------

def _flush_think_buffer(buf: str) -> tuple[str, str]:
    """Emit clean content from buffer, holding back any open <think> block.

    Returns (to_emit, remaining_buffer).
    """
    # Complete think blocks — strip them
    buf = _THINK_PATTERN.sub("", buf)
    # Partial open think tag at end — hold back
    open_idx = buf.rfind("<think>")
    if open_idx != -1:
        # There's an open <think> with no closing </think>
        return buf[:open_idx], buf[open_idx:]
    return buf, ""
