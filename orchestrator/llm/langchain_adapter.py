"""LangChain BaseChatModel adapter wrapping the symbiont's LLM client.

Preserves all existing multi-backend routing, health checks, timing,
observability and fallback logic while exposing a LangChain-compatible
interface for LangGraph nodes.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

log = logging.getLogger(__name__)


def _messages_to_dicts(messages: list[BaseMessage]) -> list[dict[str, str]]:
    """Convert LangChain messages to the dict format expected by our LLM client."""
    result: list[dict[str, str]] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            role = "system"
        elif isinstance(msg, HumanMessage):
            role = "user"
        elif isinstance(msg, AIMessage):
            role = "assistant"
        else:
            role = "user"
        result.append({"role": role, "content": msg.content})
    return result


class SymbiontChatModel(BaseChatModel):
    """LangChain-compatible wrapper around the symbiont's LLMRouter.

    Usage::

        from orchestrator.llm.langchain_adapter import SymbiontChatModel

        adapter = SymbiontChatModel(llm_client=router, model="qwen3:8b")
        # Use in LangGraph nodes as a standard BaseChatModel
        result = adapter.invoke([HumanMessage(content="hello")])
    """

    llm_client: Any
    """The symbiont LLMRouter (or any object with .chat_instrumented())."""

    model: str = ""
    """Default model to use. Can be overridden per-call via model_kwargs."""

    temperature: float = 0.7
    max_tokens: int | None = None
    timeout: float | None = None
    num_ctx: Optional[int] = None
    use_native_ollama: bool = True

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return "symbiont-llm"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Call the symbiont LLM client and return a ChatResult."""
        msg_dicts = _messages_to_dicts(messages)
        model = kwargs.pop("model", None) or self.model
        temperature = kwargs.pop("temperature", self.temperature)
        max_tokens = kwargs.pop("max_tokens", self.max_tokens)
        timeout = kwargs.pop("timeout", self.timeout)
        num_ctx = kwargs.pop("num_ctx", self.num_ctx)

        # Prefer instrumented variant for full metadata
        chat_fn = getattr(self.llm_client, "chat_instrumented", None)
        if chat_fn is not None:
            call_kwargs: dict[str, Any] = {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout": timeout,
                "use_native_ollama": self.use_native_ollama,
            }
            if num_ctx is not None:
                call_kwargs["num_ctx"] = num_ctx
            # Pass intent/complexity if provided
            if "intent" in kwargs:
                call_kwargs["intent"] = kwargs.pop("intent")
            if "complexity" in kwargs:
                call_kwargs["complexity"] = kwargs.pop("complexity")

            result_obj = chat_fn(msg_dicts, model, **call_kwargs)
            text = result_obj.text
            generation_info = {
                "model": result_obj.model,
                "backend": result_obj.backend,
                "latency_ms": result_obj.latency_ms,
                "finish_reason": result_obj.finish_reason,
                "cold_start": result_obj.cold_start,
            }
            if result_obj.usage:
                generation_info["usage"] = {
                    "prompt_tokens": result_obj.usage.prompt_tokens,
                    "completion_tokens": result_obj.usage.completion_tokens,
                    "total_tokens": result_obj.usage.total_tokens,
                }
        else:
            # Fallback to plain .chat()
            text = self.llm_client.chat(
                msg_dicts, model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            generation_info = {"model": model}

        message = AIMessage(content=text)
        generation = ChatGeneration(message=message, generation_info=generation_info)
        return ChatResult(generations=[generation])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Stream responses if the client supports it."""
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        msg_dicts = _messages_to_dicts(messages)
        model = kwargs.pop("model", None) or self.model
        temperature = kwargs.pop("temperature", self.temperature)
        max_tokens = kwargs.pop("max_tokens", self.max_tokens)
        timeout = kwargs.pop("timeout", self.timeout)
        num_ctx = kwargs.pop("num_ctx", self.num_ctx)

        stream_fn = getattr(self.llm_client, "chat_stream_instrumented", None)
        if stream_fn is None:
            # Fallback to non-streaming
            result = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(content=result.generations[0].text)
            )
            yield chunk
            return

        call_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        if num_ctx is not None:
            call_kwargs["num_ctx"] = num_ctx

        stream_result = stream_fn(msg_dicts, model, **call_kwargs)
        for text_chunk in stream_result:
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(content=text_chunk)
            )
            if run_manager:
                run_manager.on_llm_new_token(text_chunk)
            yield chunk
