"""Routing Policy — maps task types / intents to preferred backends.

Instead of always routing to the highest-priority backend, this module
applies configurable rules:
  - classification tasks → fast/CPU backend (low latency, small model)
  - RAG retrieval → aux CPU backend (parallel with GPU work)
  - main response → vLLM/GPU backend (quality)
  - overflow → fallback backend when primary is degraded

All policies are config-driven from [routing_policy] in
config/orc/admission.toml — no hardcoded defaults.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.config import RoutingPolicyConfig

log = logging.getLogger(__name__)


@dataclass
class PolicyDecision:
    """Result of policy evaluation."""

    preferred_backend: str
    preferred_model: str
    reason: str = ""


class RoutingPolicy:
    """Evaluates task type and returns preferred backend/model.

    Rules are defined in config as a list of (task_type → backend, model) mappings.
    If no rule matches, returns the default backend/model.
    """

    def __init__(self, cfg: "RoutingPolicyConfig") -> None:
        self._cfg = cfg
        # Build lookup: task_type → (backend, model)
        self._rules: dict[str, tuple[str, str]] = {}
        for rule in cfg.rules:
            self._rules[rule.task_type] = (rule.backend, rule.model)
        log.debug("RoutingPolicy: loaded %d rules", len(self._rules))

    def resolve(self, task_type: str) -> PolicyDecision:
        """Resolve the preferred backend/model for a given task type.

        Args:
            task_type: One of "classification", "rag_context", "main_response",
                       "local_evidence", "synthesis", "agent", or any custom type.
        """
        rule = self._rules.get(task_type)
        if rule:
            return PolicyDecision(
                preferred_backend=rule[0],
                preferred_model=rule[1],
                reason=f"policy rule for task_type={task_type}",
            )
        # Default fallback
        return PolicyDecision(
            preferred_backend=self._cfg.default_backend,
            preferred_model=self._cfg.default_model,
            reason="default policy (no specific rule)",
        )

    @property
    def rules(self) -> dict[str, tuple[str, str]]:
        """Return all configured rules {task_type: (backend, model)}."""
        return dict(self._rules)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_policy: RoutingPolicy | None = None


def get_routing_policy() -> RoutingPolicy | None:
    """Return the singleton RoutingPolicy (None if not configured)."""
    return _policy


def init_routing_policy(cfg: "RoutingPolicyConfig") -> RoutingPolicy:
    """Initialize the singleton. Called once at startup."""
    global _policy
    _policy = RoutingPolicy(cfg)
    log.info("RoutingPolicy initialized: %d rules, default=%s/%s",
             len(cfg.rules), cfg.default_backend, cfg.default_model)
    return _policy
