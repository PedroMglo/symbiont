"""Benchmark runner — measures model performance with fixed prompts."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from orchestrator.config import get_settings

if TYPE_CHECKING:
    from orchestrator.llm.openai_compat import OpenAICompatibleLLMClient

log = logging.getLogger(__name__)

# Fixed benchmark prompts per task type
_BENCHMARK_PROMPTS = {
    "short": "Olá! Diz-me a capital de Portugal numa frase curta.",
    "code": "Escreve uma função Python que calcula o n-ésimo número de Fibonacci de forma iterativa. Inclui type hints.",
    "reasoning": "Explica passo a passo porque é que o algoritmo quicksort tem complexidade média O(n log n) mas pior caso O(n²). Dá um exemplo concreto de cada caso.",
}


class BenchmarkRunner:
    """Runs benchmark prompts against configured models and collects timing data."""

    def __init__(self) -> None:
        self._cfg = get_settings()

    def _get_models_to_test(self, model_filter: str | None = None) -> list[str]:
        """Get list of models to benchmark."""
        if model_filter:
            return [model_filter]

        # All enabled backend models (deduplicated)
        models: list[str] = []
        seen: set[str] = set()
        for b in self._cfg.llm.backends:
            if not b.enabled:
                continue
            for m in b.models:
                if m not in seen and m != "nomic-embed-text":
                    seen.add(m)
                    models.append(m)
        return models

    def _get_tasks(self, task_filter: str) -> dict[str, str]:
        """Get benchmark prompts based on filter."""
        if task_filter == "all":
            return dict(_BENCHMARK_PROMPTS)
        if task_filter in _BENCHMARK_PROMPTS:
            return {task_filter: _BENCHMARK_PROMPTS[task_filter]}
        return dict(_BENCHMARK_PROMPTS)

    def run(
        self,
        *,
        model_filter: str | None = None,
        task_filter: str = "all",
    ) -> list[dict[str, Any]]:
        """Run benchmarks and return results."""
        from orchestrator.llm.openai_compat import OpenAICompatibleLLMClient

        models = self._get_models_to_test(model_filter)
        tasks = self._get_tasks(task_filter)

        # Find the enabled Ollama backend
        backend_cfg = next(
            (b for b in self._cfg.llm.backends if b.enabled and b.name == "ollama"),
            None,
        )
        if backend_cfg is None:
            backend_cfg = next((b for b in self._cfg.llm.backends if b.enabled), None)
        if backend_cfg is None:
            log.error("BenchmarkRunner: no enabled backend found")
            return []

        client = OpenAICompatibleLLMClient(backend_cfg)
        results: list[dict[str, Any]] = []

        for model in models:
            for task_name, prompt in tasks.items():
                result = self._run_single(client, model, task_name, prompt)
                results.append(result)

        return results

    def _run_single(
        self,
        client: "OpenAICompatibleLLMClient",
        model: str,
        task_name: str,
        prompt: str,
    ) -> dict[str, Any]:
        """Run a single benchmark and capture timing."""
        messages = [{"role": "user", "content": prompt}]

        try:
            result_obj = client.chat_instrumented(
                messages,
                model,
                temperature=0.3,
                max_tokens=512,
                timeout=120.0,
                use_native_ollama=True,
            )

            timing = result_obj.ollama_timing
            return {
                "model": model,
                "task": task_name,
                "total_latency_ms": result_obj.latency_ms,
                "first_token_ms": None,  # non-streaming
                "model_load_ms": timing.load_duration_ms,
                "prompt_eval_ms": timing.prompt_eval_duration_ms,
                "generation_ms": timing.eval_duration_ms,
                "prompt_tokens": timing.prompt_eval_count,
                "output_tokens": timing.eval_count,
                "prompt_tokens_per_second": timing.prompt_tokens_per_second,
                "generation_tokens_per_second": timing.generation_tokens_per_second,
                "total_tokens_per_second": timing.total_tokens_per_second,
                "response_length": len(result_obj.text),
                "success": True,
                "error": None,
            }
        except Exception as exc:
            log.warning("BenchmarkRunner: %s/%s failed: %s", model, task_name, exc)
            return {
                "model": model,
                "task": task_name,
                "total_latency_ms": 0,
                "first_token_ms": None,
                "model_load_ms": None,
                "prompt_eval_ms": None,
                "generation_ms": None,
                "prompt_tokens": None,
                "output_tokens": None,
                "prompt_tokens_per_second": None,
                "generation_tokens_per_second": None,
                "total_tokens_per_second": None,
                "response_length": 0,
                "success": False,
                "error": str(exc),
            }
