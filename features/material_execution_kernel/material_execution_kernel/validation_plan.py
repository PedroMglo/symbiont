"""Validation profile selection for material sessions.

The kernel owns the decision about which validation evidence is required for a
material task. It does not implement validators; the active sandbox owner still
executes declared profiles. This module only turns typed capabilities and the
normalized goal into scenario-neutral validation profile names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from material_execution_kernel.material_builder_client import MaterialPlanProposal
from material_execution_kernel.types import MaterialSessionRequest


_PROFILE_ORDER = [
    "python-basic",
    "docker-compose-static",
    "python-pytest",
    "python-api",
    "docker-compose-runtime",
    "stateful-postgres",
    "stateful-redis",
    "worker-queue",
    "cli",
]

_CAPABILITY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("python", "python 3", "python3", "pytest")),
    ("tests", ("tests", "unit tests", "integration tests", "pytest")),
    ("api", ("http api", "rest api", "endpoint", "http service", "web service", "health check")),
    ("docker_compose", ("docker compose", "docker-compose", "compose.yml", "docker-compose.yml")),
    ("docker_runtime", ("docker compose up", "compose up", "docker compose build", "containers", "containerized")),
    ("postgres", ("postgres", "postgresql", "persistent state", "persistence")),
    ("redis", ("redis",)),
    ("worker", ("worker", "workers", "background job", "job queue", "queue processing")),
    ("cli", ("cli", "command line", "command-line", "console script")),
)

_CAPABILITY_TO_PROFILES: dict[str, tuple[str, ...]] = {
    "python": ("python-basic",),
    "tests": ("python-pytest",),
    "api": ("python-api",),
    "docker_compose": ("docker-compose-static",),
    "docker_runtime": ("docker-compose-runtime",),
    "postgres": ("stateful-postgres",),
    "redis": ("stateful-redis",),
    "worker": ("worker-queue",),
    "cli": ("cli",),
}


def effective_required_capabilities(request: MaterialSessionRequest) -> list[str]:
    """Merge caller-declared capabilities with generic goal-derived ones."""

    declared = _normalize_capabilities(request.required_capabilities)
    inferred = _infer_capabilities_from_text(request.goal)
    return _specialize_api_capability(_ordered_unique([*declared, *inferred]), request.goal)


def harden_required_validation_profiles(
    request: MaterialSessionRequest,
    plan: MaterialPlanProposal,
) -> MaterialPlanProposal:
    """Return a plan with required validation profiles implied by the task.

    The material builder proposes a plan. The kernel then enforces generic
    requirements derived from the task capabilities and generated file kinds so
    completion cannot happen with weaker evidence than the prompt requires.
    """

    capabilities = effective_required_capabilities(request)
    required = [*plan.required_validation_profiles]

    file_kinds = {item.kind for item in plan.files}
    if "python" in file_kinds or "test" in file_kinds:
        required.append("python-basic")
    if "test" in file_kinds:
        capabilities.append("tests")
    if "compose" in file_kinds:
        capabilities.append("docker_compose")
    if "dockerfile" in file_kinds and "docker_compose" in capabilities:
        capabilities.append("docker_runtime")

    for capability in capabilities:
        required.extend(_CAPABILITY_TO_PROFILES.get(capability, ()))

    # Runtime profiles need either an explicit VM-local validation command or a
    # composed runtime. The kernel must not force Docker Compose when the
    # material contract already supplies a bounded command for the profile.
    runtime_profiles = ("python-api", "stateful-postgres", "stateful-redis", "worker-queue")
    missing_explicit_runtime_command = [
        profile for profile in runtime_profiles if profile in required and profile not in plan.validation_commands
    ]
    if missing_explicit_runtime_command:
        required.append("docker-compose-runtime")
        required.append("docker-compose-static")

    hardened_required = _ordered_profiles(required)
    hardened_optional = [profile for profile in plan.optional_validation_profiles if profile not in hardened_required]
    return MaterialPlanProposal(
        project_root=plan.project_root,
        requirements=plan.requirements,
        files=plan.files,
        intended_interfaces=plan.intended_interfaces,
        required_validation_profiles=hardened_required,
        optional_validation_profiles=_ordered_profiles(hardened_optional),
        validation_commands=plan.validation_commands,
        artifact_expectations=plan.artifact_expectations,
        completion_criteria=plan.completion_criteria,
        dependency_strategy=plan.dependency_strategy,
        architecture_notes=plan.architecture_notes,
        variation_reason=plan.variation_reason,
        model_route=plan.model_route,
    )


def _infer_capabilities_from_text(text: str) -> list[str]:
    normalized = _normalize_text(text)
    capabilities: list[str] = []
    for capability, aliases in _CAPABILITY_ALIASES:
        if any(_contains_alias(normalized, alias) for alias in aliases):
            capabilities.append(capability)
    return _ordered_unique(capabilities)


def _normalize_capabilities(raw: Iterable[str]) -> list[str]:
    capabilities: list[str] = []
    for item in raw:
        token = _normalize_text(str(item)).replace("-", "_").replace(" ", "_")
        if token in {"docker", "compose", "docker_compose"}:
            capabilities.append("docker_compose")
        elif token in {"docker_runtime", "container", "containers", "containerized"}:
            capabilities.append("docker_runtime")
        elif token in {"postgresql", "postgres"}:
            capabilities.append("postgres")
        elif token in {"queue", "queues", "worker", "workers"}:
            capabilities.append("worker")
        elif token:
            capabilities.append(token)
    return _ordered_unique(capabilities)


def _specialize_api_capability(capabilities: list[str], goal: str) -> list[str]:
    if "api" not in capabilities:
        return capabilities
    if _goal_requests_http_api(goal):
        return capabilities
    specialized = [capability for capability in capabilities if capability != "api"]
    if _goal_requests_library_api(goal) and "python" not in specialized:
        specialized.append("python")
    return _ordered_unique(specialized)


def _goal_requests_http_api(goal: str) -> bool:
    normalized = _normalize_text(goal)
    return any(
        _contains_alias(normalized, alias)
        for alias in (
            "http api",
            "rest api",
            "http endpoint",
            "endpoint",
            "route",
            "router",
            "http service",
            "web service",
            "server",
            "health check",
            "openapi",
        )
    )


def _goal_requests_library_api(goal: str) -> bool:
    normalized = _normalize_text(goal)
    return any(
        _contains_alias(normalized, alias)
        for alias in (
            "reusable api",
            "library api",
            "importable api",
            "reusable module",
            "importable module",
            "library",
            "module",
            "package",
        )
    )


def _ordered_profiles(profiles: Iterable[str]) -> list[str]:
    seen = set()
    ordered = []
    for profile in [*_PROFILE_ORDER, *profiles]:
        if profile in profiles and profile not in seen:
            seen.add(profile)
            ordered.append(profile)
    return ordered


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").casefold())


def _contains_alias(text: str, alias: str) -> bool:
    needle = _normalize_text(alias)
    if " " in needle or "-" in needle:
        return needle in text
    return re.search(rf"(?<![a-z0-9_]){re.escape(needle)}(?![a-z0-9_])", text) is not None
