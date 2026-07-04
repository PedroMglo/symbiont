"""Runtime schema adapter for the generated Resource Governor policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config.resource_governor_policy import build_effective_policy_payload
from orchestrator.resource_governor.schemas import EffectivePolicy


def build_effective_policy(
    config: dict[str, Any] | None = None,
    *,
    resolved_config: dict[str, Any] | None = None,
    policy_path: str | Path | None = None,
) -> EffectivePolicy:
    """Build and validate the effective Resource Governor policy."""

    return EffectivePolicy.model_validate(
        build_effective_policy_payload(
            config,
            resolved_config=resolved_config,
            policy_path=policy_path,
        )
    )
