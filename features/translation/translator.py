"""Optional local neural translator wrapper."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from context_governor import govern_chat_completion


_OLLAMA_TRANSLATION_CONTRACT_VERSION = "prompt-v5"
_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE: dict[str, str] = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


@dataclass(frozen=True)
class TranslatorConfig:
    enabled: bool
    backend: str
    model_path: Path
    source_lang: str
    target_lang: str
    device: str = "cpu"
    compute_type: str = "int8"
    intra_threads: int = 4
    inter_threads: int = 1
    ollama_base_url: str = ""
    ollama_model: str = ""
    ollama_timeout_seconds: float = 120.0
    ollama_chunk_chars: int = 4000
    ollama_max_tokens: int = 1024


class LocalTranslator:
    def __init__(self, config: TranslatorConfig):
        self.config = config
        self.loaded = False
        self.model_version = "none"
        self.fallback_reason: str | None = None
        self._translator = None
        self._tokenizer = None
        self._load()

    def _load(self) -> None:
        if not self.config.enabled:
            self.fallback_reason = "translation_disabled"
            return
        if self.config.backend == "ollama":
            if not self.config.ollama_base_url:
                self.fallback_reason = "ollama_base_url_missing"
                return
            if not self.config.ollama_model:
                self.fallback_reason = "ollama_model_missing"
                return
            self.loaded = True
            self.fallback_reason = None
            self.model_version = f"ollama:{self.config.ollama_model}:{_OLLAMA_TRANSLATION_CONTRACT_VERSION}"
            return
        if not self.config.model_path.exists():
            self.fallback_reason = "ct2_model_missing"
            return
        try:
            import ctranslate2
            from transformers import AutoTokenizer
        except Exception as exc:
            self.fallback_reason = f"neural_deps_unavailable:{type(exc).__name__}"
            return
        try:
            self._translator = ctranslate2.Translator(
                str(self.config.model_path),
                device=self.config.device,
                compute_type=self.config.compute_type,
                inter_threads=self.config.inter_threads,
                intra_threads=self.config.intra_threads,
            )
            self._tokenizer = AutoTokenizer.from_pretrained(
                str(self.config.model_path),
                local_files_only=True,
            )
            if hasattr(self._tokenizer, "src_lang"):
                self._tokenizer.src_lang = self.config.source_lang
            self.loaded = True
            self.fallback_reason = None
            self.model_version = self.config.model_path.name
        except Exception as exc:
            self._translator = None
            self._tokenizer = None
            self.fallback_reason = "ctranslate2_unavailable"
            if exc:
                self.fallback_reason = f"translator_load_failed:{type(exc).__name__}"

    def translate(self, text: str, *, timeout_ms: int | None = None) -> tuple[str, bool, str | None]:
        if self.config.backend == "ollama":
            return self._translate_ollama(text, timeout_ms=timeout_ms)
        if not self.loaded or self._translator is None or self._tokenizer is None:
            return text, False, self.fallback_reason or "translator_unavailable"
        try:
            # CTranslate2 expects token strings. NLLB generation is constrained
            # with the target language as the first target token.
            input_ids = self._tokenizer.encode(text)
            source_tokens = self._tokenizer.convert_ids_to_tokens(input_ids)
            target_prefix = [self.config.target_lang]
            results = self._translator.translate_batch(
                [source_tokens],
                target_prefix=[target_prefix],
                beam_size=1,
                max_decoding_length=256,
            )
            tokens = list(results[0].hypotheses[0])
            if tokens and tokens[0] == self.config.target_lang:
                tokens = tokens[1:]
            token_ids = self._tokenizer.convert_tokens_to_ids(tokens)
            translated = self._tokenizer.decode(token_ids, skip_special_tokens=True).strip()
            if not translated:
                return text, False, "empty_translation"
            return translated, True, None
        except Exception as exc:
            return text, False, f"translation_failed:{type(exc).__name__}"

    def _translate_ollama(self, text: str, *, timeout_ms: int | None = None) -> tuple[str, bool, str | None]:
        if not self.loaded:
            return text, False, self.fallback_reason or "translator_unavailable"
        try:
            import httpx
        except Exception as exc:
            return text, False, f"ollama_deps_unavailable:{type(exc).__name__}"

        timeout_seconds = self.config.ollama_timeout_seconds
        if timeout_ms is not None and timeout_ms > 0:
            timeout_seconds = max(timeout_seconds, timeout_ms / 1000)
        chunks = self._chunk_text(text, max_chars=max(500, self.config.ollama_chunk_chars))
        translated_chunks: list[str] = []
        try:
            with httpx.Client(
                base_url=self.config.ollama_base_url.rstrip("/"),
                timeout=httpx.Timeout(timeout_seconds),
                verify=os.environ.get("SSL_CERT_FILE") or True,
            ) as client:
                for chunk in chunks:
                    translated_chunks.append(self._translate_ollama_chunk(client, chunk))
        except Exception as exc:
            return text, False, f"translation_failed:{type(exc).__name__}"
        translated = "\n\n".join(part.strip() for part in translated_chunks if part.strip()).strip()
        if not translated:
            return text, False, "empty_translation"
        return translated, True, None

    def _translate_ollama_chunk(self, client: object, chunk: str) -> str:
        source = _language_label(self.config.source_lang)
        target = _language_label(self.config.target_lang)
        system_prompt = _prompt("ollama_translation_system.md").format(source=source, target=target)
        auth_headers = {}
        api_key = os.environ.get("OLLAMA_API_KEY")
        if api_key:
            auth_headers["Authorization"] = f"Bearer {api_key}"
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {"role": "user", "content": chunk},
        ]

        def _post(_url: str, *, json: dict, headers: dict | None = None, timeout: float | None = None):
            request_headers = dict(headers or {})
            request_headers.update(auth_headers)
            return client.post("/api/chat", headers=request_headers, json=json)

        return govern_chat_completion(
            model=self.config.ollama_model,
            messages=messages,
            base_url=self.config.ollama_base_url,
            temperature=0.0,
            max_tokens=self.config.ollama_max_tokens,
            timeout=self.config.ollama_timeout_seconds,
            phase="translation.ollama",
            post=_post,
        )

    @staticmethod
    def _chunk_text(text: str, *, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for paragraph in text.split("\n\n"):
            part_len = len(paragraph) + (2 if current else 0)
            if current and current_len + part_len > max_chars:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            if len(paragraph) > max_chars:
                for start in range(0, len(paragraph), max_chars):
                    segment = paragraph[start : start + max_chars]
                    if current:
                        chunks.append("\n\n".join(current))
                        current = []
                        current_len = 0
                    chunks.append(segment)
                continue
            current.append(paragraph)
            current_len += part_len
        if current:
            chunks.append("\n\n".join(current))
        return chunks


def _language_label(value: str) -> str:
    normalized = value.strip()
    labels = {
        "por_Latn": "Portuguese",
        "pt": "Portuguese",
        "pt-PT": "Portuguese",
        "eng_Latn": "English",
        "en": "English",
    }
    return labels.get(normalized, normalized or "the source language")
