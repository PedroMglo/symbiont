"""Level 2 — Micro-classifier using lightweight LLM (only when ambiguous)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
from sharedai.llm.backend_url import ollama_generate_url
from sharedai.llm.ollama_payload import build_generate_payload, parse_generate_content

from orchestrator.prewarming.feature_catalog import FeatureCatalog
from orchestrator.prewarming.state import PrewarmPrediction

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


log = logging.getLogger(__name__)

_CLASSIFY_PROMPT = _prompt("classify.md")


class MicroClassifier:
    """Lightweight LLM classifier for ambiguous routing cases.

    Only called when:
    - Rules gave no strong match (nothing above high threshold)
    - Embedding scores are clustered (top-2 within ambiguity gap)

    Uses qwen3:0.6b (or configured model) for fast classification with strict
    JSON output. Timeout-capped to avoid adding latency.
    """

    def __init__(
        self,
        catalog: FeatureCatalog,
        *,
        ollama_base_url: str,
        model: str,
        timeout_ms: int,
    ) -> None:
        self._catalog = catalog
        self._ollama_url = ollama_base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_ms / 1000.0

    async def classify(
        self,
        query: str,
        candidates: list[str] | None = None,
    ) -> list[PrewarmPrediction]:
        """Classify which services a query needs using a micro-LLM.

        Args:
            query: The user query text.
            candidates: Optional list of feature IDs to consider (narrows the scope).

        Returns:
            List of predictions from the classifier, or empty on failure/timeout.
        """
        # Build service list for prompt
        if candidates:
            services = [
                fid for fid in candidates
                if self._catalog.is_prewarm_target(fid)
            ]
        else:
            services = [
                fid for fid, feat in self._catalog.get_prewarm_targets().items()
                if feat.keywords
            ]

        if not services:
            return []

        service_descriptions = []
        for fid in services:
            feat = self._catalog.get(fid)
            if feat:
                details = feat.description or feat.display_name
                if feat.capabilities:
                    details = f"{details}; capabilities: {', '.join(feat.capabilities)}"
                if feat.inputs:
                    details = f"{details}; inputs: {', '.join(feat.inputs)}"
                service_descriptions.append(f"- {fid}: {details}")

        prompt = _CLASSIFY_PROMPT.format(
            services="\n".join(service_descriptions),
            query=query,
        )

        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    ollama_generate_url(self._ollama_url),
                    json=build_generate_payload(
                        model=self._model,
                        prompt=prompt,
                        temperature=0.1,
                        max_tokens=150,
                        num_ctx=512,
                    ),
                )

                if resp.status_code != 200:
                    log.debug("Micro-classifier LLM returned %d", resp.status_code)
                    return []

                raw_response = parse_generate_content(resp.json()).strip()

        except (httpx.TimeoutException, httpx.HTTPError) as e:
            elapsed = (time.time() - start) * 1000
            log.debug("Micro-classifier timeout/error after %.0fms: %s", elapsed, e)
            return []

        # Parse JSON response
        return self._parse_response(raw_response, services)

    def _parse_response(self, raw: str, valid_services: list[str]) -> list[PrewarmPrediction]:
        """Parse the LLM's JSON response into predictions."""
        # Try to extract JSON from response (handle markdown code blocks)
        json_str = raw
        if "```" in raw:
            # Extract content between code fences
            parts = raw.split("```")
            if len(parts) >= 2:
                json_str = parts[1]
                if json_str.startswith("json"):
                    json_str = json_str[4:]
                json_str = json_str.strip()

        # Also try to find a JSON array directly
        if not json_str.startswith("["):
            start = json_str.find("[")
            end = json_str.rfind("]")
            if start != -1 and end != -1:
                json_str = json_str[start:end + 1]

        try:
            items = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            log.debug("Micro-classifier returned invalid JSON: %s", raw[:200])
            return []

        if not isinstance(items, list):
            return []

        predictions: list[PrewarmPrediction] = []
        valid_set = set(valid_services)

        for item in items:
            if not isinstance(item, dict):
                continue
            fid = item.get("feature", item.get("service", ""))
            confidence = item.get("confidence", 0.0)

            if not isinstance(fid, str) or fid not in valid_set:
                continue
            if not isinstance(confidence, (int, float)) or confidence <= 0.3:
                continue

            predictions.append(PrewarmPrediction(
                feature_id=fid,
                confidence=min(1.0, float(confidence)),
                source="classifier",
                reason="micro_llm",
            ))

        predictions.sort(key=lambda p: p.confidence, reverse=True)
        return predictions
