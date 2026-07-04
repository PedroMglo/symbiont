"""Configuration loader for the translation feature."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class TranslationConfig:
    enabled: bool = True
    mode: str = "shadow"
    hunspell_aff_path: Path = BASE_DIR / "data" / "hunspell" / "index.aff"
    hunspell_dic_path: Path = BASE_DIR / "data" / "hunspell" / "index.dic"
    autocorrect_enabled: bool = True
    autocorrect_threshold: float = 0.92
    max_edit_distance: int = 2
    pt_to_en_path: Path = BASE_DIR / "data" / "glossaries" / "pt_pt_to_en.yml"
    en_to_pt_path: Path = BASE_DIR / "data" / "glossaries" / "en_to_pt_pt.yml"
    pt_br_blocklist_path: Path = BASE_DIR / "data" / "rules" / "pt_br_blocklist.yml"
    cache_enabled: bool = True
    cache_path: Path = BASE_DIR / "cache" / "i18n_cache.sqlite"
    cache_ttl_seconds: int = 604800
    translation_enabled: bool = True
    translation_backend: str = "ctranslate2"
    ct2_model_path: Path = BASE_DIR / "models" / "nllb-200-distilled-600M-ct2-int8"
    source_lang: str = "por_Latn"
    target_lang: str = "eng_Latn"
    device: str = "cpu"
    compute_type: str = "int8"
    intra_threads: int = 4
    inter_threads: int = 1
    ollama_base_url: str = ""
    ollama_model: str = ""
    ollama_timeout_seconds: float = 120.0
    ollama_chunk_chars: int = 4000
    ollama_max_tokens: int = 1024
    min_translate_chars: int = 20
    max_protected_ratio: float = 0.40


def _config_path() -> Path:
    explicit = os.environ.get("I18N_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return BASE_DIR / "config.toml"


def _resolve(raw: str | Path) -> Path:
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _load_raw() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_config() -> TranslationConfig:
    raw = _load_raw()
    i18n = raw.get("i18n", {})
    spell = raw.get("spellcheck", {})
    glossary = raw.get("glossary", {})
    translation = raw.get("translation", {})
    cache = raw.get("cache", {})
    policy = raw.get("policy", {})
    return TranslationConfig(
        enabled=bool(i18n.get("enabled", True)),
        mode=str(i18n.get("mode", "shadow")),
        hunspell_aff_path=_resolve(spell.get("aff_path", "data/hunspell/index.aff")),
        hunspell_dic_path=_resolve(spell.get("dic_path", "data/hunspell/index.dic")),
        autocorrect_enabled=bool(spell.get("autocorrect_enabled", True)),
        autocorrect_threshold=float(spell.get("autocorrect_threshold", 0.92)),
        max_edit_distance=int(spell.get("max_edit_distance", 2)),
        pt_to_en_path=_resolve(glossary.get("pt_to_en_path", "data/glossaries/pt_pt_to_en.yml")),
        en_to_pt_path=_resolve(glossary.get("en_to_pt_path", "data/glossaries/en_to_pt_pt.yml")),
        pt_br_blocklist_path=_resolve(glossary.get("pt_br_blocklist_path", "data/rules/pt_br_blocklist.yml")),
        cache_enabled=bool(cache.get("enabled", True)),
        cache_path=_resolve(cache.get("path", "cache/i18n_cache.sqlite")),
        cache_ttl_seconds=int(cache.get("ttl_seconds", 604800)),
        translation_enabled=bool(translation.get("enabled", True)),
        translation_backend=str(translation.get("backend", "ctranslate2")).lower(),
        ct2_model_path=_resolve(translation.get("ct2_model_path", "models/nllb-200-distilled-600M-ct2-int8")),
        source_lang=str(translation.get("source_lang", "por_Latn")),
        target_lang=str(translation.get("target_lang", "eng_Latn")),
        device=str(translation.get("device", "cpu")),
        compute_type=str(translation.get("compute_type", "int8")),
        intra_threads=int(translation.get("intra_threads", 4)),
        inter_threads=int(translation.get("inter_threads", 1)),
        ollama_base_url=str(translation.get("ollama_base_url") or os.environ.get("OLLAMA_BASE_URL") or ""),
        ollama_model=str(translation.get("ollama_model") or os.environ.get("TRANSLATION_OLLAMA_MODEL") or ""),
        ollama_timeout_seconds=float(
            os.environ.get("TRANSLATION_OLLAMA_TIMEOUT_SECONDS")
            or translation.get("ollama_timeout_seconds", 120.0)
        ),
        ollama_chunk_chars=int(translation.get("ollama_chunk_chars", 4000)),
        ollama_max_tokens=int(translation.get("ollama_max_tokens", 1024)),
        min_translate_chars=int(policy.get("min_translate_chars", 20)),
        max_protected_ratio=float(policy.get("max_protected_ratio", 0.40)),
    )


_config: TranslationConfig | None = None


def get_config() -> TranslationConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    global _config
    _config = None
