"""LLM post-processing for transcription results.

After audio transcription completes, this module uses Ollama to:
1. Correct common ASR errors (hallucinations, repeated words, garbled text)
2. Generate a structured summary (key points, action items, decisions)
3. Detect and fix language-specific issues (PT-PT corrections)

Runs asynchronously after the main pipeline — does NOT block transcription.
Uses the symbiont's Ollama instance (host.docker.internal:11434).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from sharedai.llm.backend_url import ollama_generate_url, validated_base_url
from sharedai.llm.ollama_payload import build_generate_payload, parse_generate_content
from audio_transcribe.scratch import assert_scratch_path
from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


logger = logging.getLogger(__name__)


# Ollama endpoint (same as symbiont uses)
_OLLAMA_URL = validated_base_url(
    os.environ.get(
        "OLLAMA_BASE_URL",
        "https://host.docker.internal:11434" if os.path.exists("/.dockerenv") else "https://localhost:11434",
    ),
)[0]

# Model for post-processing (fast, good at text correction)
_LLM_MODEL = os.environ.get("LLM_POST_MODEL", "qwen3:8b")

# Maximum text length to send to LLM (avoid context overflow)
_MAX_TEXT_LENGTH = 12000

# Prompts
_CORRECTION_PROMPT = _prompt("correction.md")

_SUMMARY_PROMPT = _prompt("summary.md")
async def correct_transcription(text: str) -> str:
    """Use LLM to correct ASR errors in transcription text.

    Returns corrected text, or original text if LLM is unavailable.
    """
    if not text or len(text.strip()) < 20:
        return text

    # Truncate if too long (process in chunks for very long texts)
    input_text = text[:_MAX_TEXT_LENGTH]

    try:
        response = await _call_ollama(
            _CORRECTION_PROMPT.format(text=input_text),
            temperature=0.1,
            max_tokens=_MAX_TEXT_LENGTH + 500,
        )
        if response and len(response.strip()) > 10:
            return response.strip()
        return text
    except Exception as exc:
        logger.warning(f"LLM correction failed (non-critical): {exc}")
        return text


async def generate_summary(text: str) -> dict[str, Any]:
    """Generate structured summary from transcription text.

    Returns summary dict, or empty dict if LLM is unavailable.
    """
    if not text or len(text.strip()) < 50:
        return {}

    input_text = text[:_MAX_TEXT_LENGTH]

    try:
        response = await _call_ollama(
            _SUMMARY_PROMPT.format(text=input_text),
            temperature=0.3,
            max_tokens=2048,
        )
        if not response:
            return {}

        # Parse JSON response (LLM may wrap in markdown code block)
        cleaned = response.strip()
        if cleaned.startswith("```"):
            # Remove markdown code fences
            lines = cleaned.split("\n")
            cleaned = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            )

        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.debug("LLM returned non-JSON summary response")
        return {"summary": response.strip()[:500]} if response else {}
    except Exception as exc:
        logger.warning(f"LLM summary generation failed (non-critical): {exc}")
        return {}


async def post_process_job(job_output_dir: str) -> dict[str, Any]:
    """Run full LLM post-processing on a completed job.

    Reads transcripts from the job output, applies correction and summarization,
    and saves enhanced outputs.

    Returns dict with processing results.
    """
    job_dir = assert_scratch_path(job_output_dir, label="streaming job output")
    transcripts_dir = job_dir / "transcripts"
    results: dict[str, Any] = {"corrected": False, "summary": {}}

    # Read the clean transcript text
    text = ""
    txt_file = transcripts_dir / "transcript.txt"
    if txt_file.exists():
        text = txt_file.read_text(encoding="utf-8")
    else:
        # Try raw JSON
        raw_file = transcripts_dir / "transcript_raw.json"
        if raw_file.exists():
            raw_data = json.loads(raw_file.read_text(encoding="utf-8"))
            segments = raw_data.get("segments", [])
            text = " ".join(seg.get("text", "") for seg in segments)

    if not text or len(text.strip()) < 50:
        logger.info("Transcript too short for LLM post-processing")
        return results

    # 1. Correction
    corrected_text = await correct_transcription(text)
    if corrected_text != text:
        results["corrected"] = True
        # Save corrected version
        corrected_file = transcripts_dir / "transcript_corrected.txt"
        corrected_file.write_text(corrected_text, encoding="utf-8")
        logger.info(f"Saved LLM-corrected transcript: {corrected_file.name}")

    # 2. Summary
    summary = await generate_summary(corrected_text or text)
    if summary:
        results["summary"] = summary
        # Save summary
        summary_file = transcripts_dir / "summary_llm.json"
        summary_file.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"Saved LLM summary: {summary_file.name}")

    return results


async def _call_ollama(
    prompt: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> str | None:
    """Call Ollama API for text generation."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            ollama_generate_url(_OLLAMA_URL),
            json=build_generate_payload(
                model=_LLM_MODEL,
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )
        resp.raise_for_status()
        return parse_generate_content(resp.json())
