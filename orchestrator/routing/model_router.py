"""Model selection based on intent × complexity × resource availability.

v2: Reads routing profiles from models.json (orchestration.routing.profiles)
    instead of config dataclass. No user-facing model selection — the
    symbiont picks the best model internally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from orchestrator.types import Complexity, Intent

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routing table: (intent, complexity) → profile key
# ---------------------------------------------------------------------------
# Keys: "default", "fast", "code", "deep"
_ROUTE_TABLE: dict[tuple[Intent, Complexity], str] = {
    # General
    (Intent.GENERAL, Complexity.SIMPLE): "fast",
    (Intent.GENERAL, Complexity.NORMAL): "default",
    (Intent.GENERAL, Complexity.COMPLEX): "default",
    (Intent.GENERAL, Complexity.DEEP): "deep",
    # Local / notes
    (Intent.LOCAL, Complexity.SIMPLE): "default",
    (Intent.LOCAL, Complexity.NORMAL): "default",
    (Intent.LOCAL, Complexity.COMPLEX): "default",
    (Intent.LOCAL, Complexity.DEEP): "deep",
    # Research / personal context
    (Intent.RESEARCH, Complexity.SIMPLE): "default",
    (Intent.RESEARCH, Complexity.NORMAL): "default",
    (Intent.RESEARCH, Complexity.COMPLEX): "default",
    (Intent.RESEARCH, Complexity.DEEP): "deep",
    (Intent.PERSONAL_CONTEXT, Complexity.SIMPLE): "default",
    (Intent.PERSONAL_CONTEXT, Complexity.NORMAL): "default",
    (Intent.PERSONAL_CONTEXT, Complexity.COMPLEX): "default",
    (Intent.PERSONAL_CONTEXT, Complexity.DEEP): "deep",
    # Code
    (Intent.CODE, Complexity.SIMPLE): "code",
    (Intent.CODE, Complexity.NORMAL): "code",
    (Intent.CODE, Complexity.COMPLEX): "code",
    (Intent.CODE, Complexity.DEEP): "code",
    # System
    (Intent.SYSTEM, Complexity.SIMPLE): "fast",
    (Intent.SYSTEM, Complexity.NORMAL): "default",
    (Intent.SYSTEM, Complexity.COMPLEX): "default",
    (Intent.SYSTEM, Complexity.DEEP): "default",
    # Graph / architecture
    (Intent.GRAPH, Complexity.SIMPLE): "default",
    (Intent.GRAPH, Complexity.NORMAL): "default",
    (Intent.GRAPH, Complexity.COMPLEX): "deep",
    (Intent.GRAPH, Complexity.DEEP): "deep",
    # Combined
    (Intent.LOCAL_AND_GRAPH, Complexity.SIMPLE): "default",
    (Intent.LOCAL_AND_GRAPH, Complexity.NORMAL): "default",
    (Intent.LOCAL_AND_GRAPH, Complexity.COMPLEX): "deep",
    (Intent.LOCAL_AND_GRAPH, Complexity.DEEP): "deep",
    (Intent.SYSTEM_AND_LOCAL, Complexity.SIMPLE): "default",
    (Intent.SYSTEM_AND_LOCAL, Complexity.NORMAL): "default",
    (Intent.SYSTEM_AND_LOCAL, Complexity.COMPLEX): "default",
    (Intent.SYSTEM_AND_LOCAL, Complexity.DEEP): "default",
    # Clarify — fast response to ask for clarification
    (Intent.CLARIFY, Complexity.SIMPLE): "fast",
    (Intent.CLARIFY, Complexity.NORMAL): "fast",
    (Intent.CLARIFY, Complexity.COMPLEX): "fast",
    (Intent.CLARIFY, Complexity.DEEP): "fast",
}


class ConfigModelRouter:
    """Selects model from registry routing profiles based on intent × complexity.

    Model selection is fully internal — users cannot override or choose models.
    The routing table maps (intent, complexity) to a profile key, which is then
    resolved to an actual model name via models.json routing profiles.
    """

    def select(self, intent: Intent, complexity: Complexity) -> str:
        """Select the best model for the given intent and complexity."""
        from orchestrator.registry import get_registry

        reg = get_registry()
        key = _ROUTE_TABLE.get((intent, complexity), "default")
        model = reg.get_model_for_profile(key)

        if not model:
            # Fallback to default profile
            model = reg.get_model_for_profile("default")

        log.debug("ModelRouter: %s×%s → %s (%s)", intent.value, complexity.value, model, key)
        return model

    def select_with_profile(self, intent: Intent, complexity: Complexity) -> "ModelSelection":
        """Select model and return with profile key for inference/budget configuration."""
        from orchestrator.registry import get_registry

        reg = get_registry()
        key = _ROUTE_TABLE.get((intent, complexity), "default")
        model = reg.get_model_for_profile(key)

        if not model:
            model = reg.get_model_for_profile("default")

        log.debug("ModelRouter: %s×%s → %s (profile=%s)", intent.value, complexity.value, model, key)
        return ModelSelection(model=model, profile_key=key)

    def select_model_profile(
        self,
        profile_key: str | None,
        *,
        fallback_profile: str | None = "default",
        required_capabilities: tuple[str, ...] = (),
    ) -> "ModelProfileSelection | None":
        """Resolve a capability/model profile to a concrete model/backend.

        This is intentionally generic runtime routing: profiles come from the
        central LLM config/registry, while agents/features keep their own task
        semantics behind their APIs.
        """
        requested_profile = str(profile_key or "").strip()
        if not requested_profile:
            return None

        from orchestrator.config import get_settings

        settings = get_settings()
        profiles = {profile.alias: profile for profile in settings.llm.model_profiles}
        profile = profiles.get(requested_profile)
        fallback_reason = ""

        if profile is None:
            if fallback_profile is None:
                return None
            profile = profiles.get(fallback_profile)
            if profile is None:
                return None
            fallback_reason = "unknown_profile"
        elif not profile.enabled:
            if fallback_profile is None:
                return None
            profile = profiles.get(fallback_profile)
            if profile is None or not profile.enabled:
                return None
            fallback_reason = "disabled_profile"

        capabilities = tuple(required_capabilities or profile.required_capabilities)
        model, backend = self._select_model_from_profile(profile, settings.llm.backends, capabilities)
        if not model and fallback_profile and profile.alias != fallback_profile:
            fallback = profiles.get(fallback_profile)
            if fallback and fallback.enabled:
                model, backend = self._select_model_from_profile(fallback, settings.llm.backends, tuple(fallback.required_capabilities))
                if model:
                    fallback_reason = fallback_reason or "profile_unavailable"
                    profile = fallback
                    capabilities = tuple(fallback.required_capabilities)
        if not model:
            return None

        return ModelProfileSelection(
            requested_profile=requested_profile,
            profile_key=profile.alias,
            model=model,
            backend_name=str(getattr(backend, "name", "")) if backend is not None else "",
            backend_type=self._backend_type(backend),
            backend_url=self._backend_url(backend),
            preferred_models=tuple(profile.preferred_models),
            fallback_model=profile.fallback_model,
            required_capabilities=capabilities,
            fallback_used=bool(fallback_reason or profile.alias != requested_profile or model not in profile.preferred_models[:1]),
            fallback_reason=fallback_reason,
        )

    def _select_model_from_profile(
        self,
        profile: Any,
        backends: tuple[Any, ...],
        required_capabilities: tuple[str, ...],
    ) -> tuple[str, Any | None]:
        candidates = [model for model in profile.preferred_models if model]
        if profile.fallback_model and profile.fallback_model not in candidates:
            candidates.append(profile.fallback_model)

        for model in candidates:
            backend = self._first_backend_for_model(model, backends, required_capabilities)
            if backend is not None:
                return model, backend
        if candidates:
            return candidates[0], None
        return "", None

    @staticmethod
    def _first_backend_for_model(
        model: str,
        backends: tuple[Any, ...],
        required_capabilities: tuple[str, ...],
    ) -> Any | None:
        required = {capability for capability in required_capabilities if capability}
        for backend in sorted(backends, key=lambda item: int(getattr(item, "priority", 1000))):
            if not getattr(backend, "enabled", False):
                continue
            models = set(getattr(backend, "models", ()) or ())
            if model not in models:
                continue
            capabilities = set(getattr(backend, "capabilities", ()) or ())
            if required and not required.issubset(capabilities):
                continue
            return backend
        return None

    @staticmethod
    def _backend_type(backend: Any | None) -> str:
        if backend is None:
            return ""
        return "ollama" if getattr(backend, "name", "") == "ollama" else "openai"

    @staticmethod
    def _backend_url(backend: Any | None) -> str:
        if backend is None:
            return ""
        url = str(getattr(backend, "base_url", "") or "").rstrip("/")
        if getattr(backend, "name", "") == "ollama" and url.endswith("/v1"):
            return url[:-3].rstrip("/")
        return url


@dataclass
class ModelSelection:
    """Result of model selection — includes profile key."""

    model: str
    profile_key: str  # "fast", "default", "code", "deep"


@dataclass
class ModelProfileSelection:
    """Resolved model profile for capability-driven runtime calls."""

    requested_profile: str
    profile_key: str
    model: str
    backend_name: str = ""
    backend_type: str = ""
    backend_url: str = ""
    preferred_models: tuple[str, ...] = ()
    fallback_model: str = ""
    required_capabilities: tuple[str, ...] = ()
    fallback_used: bool = False
    fallback_reason: str = ""

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "requested_profile": self.requested_profile,
            "profile_key": self.profile_key,
            "model": self.model,
            "backend_name": self.backend_name,
            "backend_type": self.backend_type,
            "backend_url": self.backend_url,
            "preferred_models": list(self.preferred_models),
            "fallback_model": self.fallback_model,
            "required_capabilities": list(self.required_capabilities),
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
        }
