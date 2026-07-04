"""Model capability detection — auto-detect what a model supports from its name.

v1.4 — Model-Agnostic Intelligence Layer.

Detection strategy (ordered by priority):
1. Config override from [llm.model_capabilities] in config/orc/llm.toml
2. Pattern match against known model families
3. Conservative default (assumes minimal capabilities)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

# Type aliases for capability levels
ReasoningStrength = Literal["weak", "moderate", "strong"]
InstructionFollowing = Literal["basic", "good", "excellent"]
SpeedClass = Literal["fast", "medium", "slow"]


@dataclass(frozen=True)
class ModelCapabilities:
    """Capabilities of a specific model — informs prompt building and budgeting."""

    context_window: int
    supports_function_calling: bool
    supports_json_mode: bool
    supports_system_prompt: bool
    supports_thinking: bool
    reasoning_strength: ReasoningStrength
    instruction_following: InstructionFollowing
    speed_class: SpeedClass
    quantization: str | None = None


CONSERVATIVE_DEFAULT = ModelCapabilities(
    context_window=4096,
    supports_function_calling=False,
    supports_json_mode=False,
    supports_system_prompt=True,
    supports_thinking=False,
    reasoning_strength="weak",
    instruction_following="good",
    speed_class="medium",
    quantization=None,
)


# ---------------------------------------------------------------------------
# Known model patterns — (regex, base capabilities)
# Size suffix (:1b, :4b, :8b, etc.) is handled separately to adjust
# reasoning_strength and speed_class.
# ---------------------------------------------------------------------------

_SIZE_RE = re.compile(r":(\d+)b")

_KNOWN_PATTERNS: list[tuple[re.Pattern[str], ModelCapabilities]] = [
    # Qwen3 family — supports thinking, function calling
    (re.compile(r"qwen3", re.IGNORECASE), ModelCapabilities(
        context_window=32768,
        supports_function_calling=True,
        supports_json_mode=True,
        supports_system_prompt=True,
        supports_thinking=True,
        reasoning_strength="strong",
        instruction_following="excellent",
        speed_class="medium",
    )),
    # Qwen2.5-coder — code specialist, function calling
    (re.compile(r"qwen2\.?5-coder", re.IGNORECASE), ModelCapabilities(
        context_window=32768,
        supports_function_calling=True,
        supports_json_mode=True,
        supports_system_prompt=True,
        supports_thinking=False,
        reasoning_strength="moderate",
        instruction_following="excellent",
        speed_class="medium",
    )),
    # Qwen2.5 general
    (re.compile(r"qwen2\.?5", re.IGNORECASE), ModelCapabilities(
        context_window=32768,
        supports_function_calling=True,
        supports_json_mode=True,
        supports_system_prompt=True,
        supports_thinking=False,
        reasoning_strength="moderate",
        instruction_following="excellent",
        speed_class="medium",
    )),
    # DeepSeek-R1 — reasoning specialist
    (re.compile(r"deepseek-r1", re.IGNORECASE), ModelCapabilities(
        context_window=32768,
        supports_function_calling=False,
        supports_json_mode=False,
        supports_system_prompt=True,
        supports_thinking=True,
        reasoning_strength="strong",
        instruction_following="good",
        speed_class="slow",
    )),
    # DeepSeek-V3/V2
    (re.compile(r"deepseek-v[23]", re.IGNORECASE), ModelCapabilities(
        context_window=32768,
        supports_function_calling=True,
        supports_json_mode=True,
        supports_system_prompt=True,
        supports_thinking=False,
        reasoning_strength="strong",
        instruction_following="excellent",
        speed_class="medium",
    )),
    # Gemma3 family
    (re.compile(r"gemma3", re.IGNORECASE), ModelCapabilities(
        context_window=8192,
        supports_function_calling=False,
        supports_json_mode=False,
        supports_system_prompt=True,
        supports_thinking=False,
        reasoning_strength="moderate",
        instruction_following="good",
        speed_class="fast",
    )),
    # Gemma2 family
    (re.compile(r"gemma2", re.IGNORECASE), ModelCapabilities(
        context_window=8192,
        supports_function_calling=False,
        supports_json_mode=False,
        supports_system_prompt=True,
        supports_thinking=False,
        reasoning_strength="moderate",
        instruction_following="good",
        speed_class="fast",
    )),
    # Llama3/3.1/3.2
    (re.compile(r"llama3", re.IGNORECASE), ModelCapabilities(
        context_window=8192,
        supports_function_calling=True,
        supports_json_mode=True,
        supports_system_prompt=True,
        supports_thinking=False,
        reasoning_strength="moderate",
        instruction_following="good",
        speed_class="medium",
    )),
    # Phi3/Phi4
    (re.compile(r"phi[34]", re.IGNORECASE), ModelCapabilities(
        context_window=4096,
        supports_function_calling=True,
        supports_json_mode=True,
        supports_system_prompt=True,
        supports_thinking=False,
        reasoning_strength="moderate",
        instruction_following="good",
        speed_class="fast",
    )),
    # Mistral/Mixtral
    (re.compile(r"mistral|mixtral", re.IGNORECASE), ModelCapabilities(
        context_window=32768,
        supports_function_calling=True,
        supports_json_mode=True,
        supports_system_prompt=True,
        supports_thinking=False,
        reasoning_strength="moderate",
        instruction_following="good",
        speed_class="medium",
    )),
    # Command-R
    (re.compile(r"command-r", re.IGNORECASE), ModelCapabilities(
        context_window=131072,
        supports_function_calling=True,
        supports_json_mode=True,
        supports_system_prompt=True,
        supports_thinking=False,
        reasoning_strength="strong",
        instruction_following="excellent",
        speed_class="slow",
    )),
]


def _infer_speed_class(size_b: int) -> SpeedClass:
    if size_b <= 4:
        return "fast"
    if size_b <= 14:
        return "medium"
    return "slow"


def _infer_reasoning(size_b: int, base: ReasoningStrength) -> ReasoningStrength:
    if size_b <= 2:
        return "weak"
    if size_b <= 4 and base != "strong":
        return "weak" if base == "weak" else "moderate"
    return base


class CapabilityDetector:
    """Detects model capabilities from name patterns and config overrides."""

    def __init__(self, overrides: dict[str, ModelCapabilities] | None = None) -> None:
        self._overrides = overrides or {}
        self._cache: dict[str, ModelCapabilities] = {}

    def detect(self, model_name: str) -> ModelCapabilities:
        """Detect capabilities for a model. Results are cached."""
        if model_name in self._cache:
            return self._cache[model_name]

        caps = self._detect_uncached(model_name)
        self._cache[model_name] = caps
        log.debug(
            "CapabilityDetector: %s → ctx=%d, fc=%s, thinking=%s, reasoning=%s, speed=%s",
            model_name, caps.context_window, caps.supports_function_calling,
            caps.supports_thinking, caps.reasoning_strength, caps.speed_class,
        )
        return caps

    def _detect_uncached(self, model_name: str) -> ModelCapabilities:
        # 1. Config override
        if model_name in self._overrides:
            return self._overrides[model_name]

        # 2. Pattern match
        for pattern, base_caps in _KNOWN_PATTERNS:
            if pattern.search(model_name):
                return self._adjust_for_size(model_name, base_caps)

        # 3. Conservative default
        return self._adjust_for_size(model_name, CONSERVATIVE_DEFAULT)

    def _adjust_for_size(self, model_name: str, base: ModelCapabilities) -> ModelCapabilities:
        """Adjust speed_class and reasoning based on model size suffix."""
        m = _SIZE_RE.search(model_name)
        if not m:
            return base

        size_b = int(m.group(1))
        return ModelCapabilities(
            context_window=base.context_window,
            supports_function_calling=base.supports_function_calling,
            supports_json_mode=base.supports_json_mode,
            supports_system_prompt=base.supports_system_prompt,
            supports_thinking=base.supports_thinking,
            reasoning_strength=_infer_reasoning(size_b, base.reasoning_strength),
            instruction_following=base.instruction_following,
            speed_class=_infer_speed_class(size_b),
            quantization=base.quantization,
        )
