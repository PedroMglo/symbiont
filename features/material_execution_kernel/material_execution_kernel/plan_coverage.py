"""Scenario-neutral material plan coverage checks.

The kernel uses these checks before materializing files. They do not invent
files, dependencies, commands or framework choices; they only decide whether a
plan contains enough evidence to be worth executing inside the sandbox.
"""

from __future__ import annotations

from collections.abc import Iterable

from material_execution_kernel.material_builder_client import (
    MaterialPlanProposal,
    PlanCoverageIssueProposal,
)
from material_execution_kernel.types import MaterialSessionRequest
from material_execution_kernel.validation_plan import effective_required_capabilities


_DEPENDENCY_MANIFEST_NAMES = {
    "requirements.txt",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "environment.yml",
    "environment.yaml",
    "pipfile",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "dockerfile",
    "containerfile",
}

_DEPENDENCY_NOTE_TERMS = (
    "dependency",
    "dependencies",
    "package manager",
    "install strategy",
    "runtime image",
    "base image",
    "vendored",
    "standard library only",
    "stdlib only",
)

_CAPABILITY_EVIDENCE_TERMS = {
    "api": (
        "api",
        "http api",
        "http service",
        "http endpoint",
        "endpoint",
        "route",
        "router",
    ),
    "cli": (
        "cli",
        "command-line",
        "command line",
        "console command",
        "terminal command",
        "console interface",
    ),
    "worker": (
        "worker",
        "queue worker",
        "queue consumer",
        "background worker",
        "job processor",
        "task processor",
    ),
}


def plan_coverage_issues(
    request: MaterialSessionRequest,
    plan: MaterialPlanProposal,
) -> list[PlanCoverageIssueProposal]:
    capabilities = set(effective_required_capabilities(request))
    profiles = set(plan.required_validation_profiles)
    file_paths = [item.path for item in plan.files]
    file_kinds = {item.kind for item in plan.files}
    notes = " \n".join(plan.architecture_notes).casefold()
    commands = set(plan.validation_commands)

    issues: list[PlanCoverageIssueProposal] = []
    if _needs_python_dependency_strategy(profiles, file_kinds) and not _has_dependency_strategy(file_paths, notes):
        issues.append(
            PlanCoverageIssueProposal(
                issue_type="missing_dependency_strategy",
                severity="blocking_completion",
                message=(
                    "The plan requires Python runtime/test validation but does not declare how "
                    "runtime dependencies are provided."
                ),
                details={
                    "required_profiles": sorted(profiles),
                    "file_kinds": sorted(file_kinds),
                },
                acceptance=[
                    "declare a dependency/runtime strategy in a generated project file or architecture note",
                    "keep dependency evidence generic and driven by the selected project architecture",
                ],
            )
        )
    if _requires_compose_contract(profiles) and not _has_compose_or_runtime_contract(file_paths, notes, commands):
        issues.append(
            PlanCoverageIssueProposal(
                issue_type="missing_runtime_service_contract",
                severity="blocking_completion",
                message=(
                    "The plan requires containerized or stateful runtime validation but does not "
                    "declare a runnable service/runtime contract."
                ),
                details={"required_profiles": sorted(profiles)},
                acceptance=[
                    "include a compose/runtime contract or an explicit VM-local runtime validation command",
                    "do not rely on host Docker socket or host paths",
                ],
            )
        )
    if "cli" in profiles and not _has_capability_evidence(file_paths, notes, commands, "cli"):
        issues.append(_missing_surface_issue("missing_cli_contract", "cli", profiles))
    if "worker-queue" in profiles and not _has_capability_evidence(file_paths, notes, commands, "worker"):
        issues.append(_missing_surface_issue("missing_worker_contract", "worker", profiles))
    if "python-api" in profiles and not _has_capability_evidence(file_paths, notes, commands, "api"):
        issues.append(_missing_surface_issue("missing_api_contract", "api", profiles))
    if _needs_test_contract(profiles, capabilities) and not _has_test_evidence(plan.files, notes, commands):
        issues.append(
            PlanCoverageIssueProposal(
                issue_type="missing_test_contract",
                severity="blocking_completion",
                message=(
                    "The plan requires test validation but does not declare a generated test "
                    "surface or an equivalent sandbox validation contract."
                ),
                details={"required_profiles": sorted(profiles), "capabilities": sorted(capabilities)},
                acceptance=[
                    "declare generated test files or a VM-local test validation command",
                    "keep tests derived from requested behavior rather than from benchmark-specific fixtures",
                ],
            )
        )
    if _needs_stateful_contract(profiles, capabilities) and not _has_stateful_evidence(file_paths, notes, commands):
        issues.append(
            PlanCoverageIssueProposal(
                issue_type="missing_stateful_service_contract",
                severity="blocking_completion",
                message=(
                    "The plan requires stateful service validation but does not declare how "
                    "stateful services are configured or exercised."
                ),
                details={"required_profiles": sorted(profiles), "capabilities": sorted(capabilities)},
                acceptance=[
                    "declare VM-local stateful service configuration or validation commands",
                    "keep service credentials synthetic and sandbox-local",
                ],
            )
        )
    return issues


def _needs_python_dependency_strategy(profiles: set[str], file_kinds: set[str]) -> bool:
    if not ({"python", "test"} & file_kinds):
        return False
    return bool(
        profiles
        & {
            "python-pytest",
            "python-api",
            "stateful-postgres",
            "stateful-redis",
            "worker-queue",
        }
    )


def _has_dependency_strategy(paths: Iterable[str], notes: str) -> bool:
    if any(_basename(path) in _DEPENDENCY_MANIFEST_NAMES for path in paths):
        return True
    return any(term in notes for term in _DEPENDENCY_NOTE_TERMS)


def _requires_compose_contract(profiles: set[str]) -> bool:
    return bool(
        profiles
        & {
            "docker-compose-static",
            "docker-compose-runtime",
            "stateful-postgres",
            "stateful-redis",
            "worker-queue",
        }
    )


def _has_compose_or_runtime_contract(paths: Iterable[str], notes: str, commands: set[str]) -> bool:
    if any(_basename(path) in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"} for path in paths):
        return True
    if {
        "docker-compose-static",
        "docker-compose-runtime",
        "stateful-postgres",
        "stateful-redis",
        "worker-queue",
    } & commands:
        return True
    return "compose" in notes or "container runtime" in notes or "service runtime" in notes


def _needs_stateful_contract(profiles: set[str], capabilities: set[str]) -> bool:
    return bool((profiles & {"stateful-postgres", "stateful-redis"}) or (capabilities & {"postgres", "redis"}))


def _has_stateful_evidence(paths: Iterable[str], notes: str, commands: set[str]) -> bool:
    normalized_paths = " \n".join(paths).casefold()
    if any(term in normalized_paths for term in ("postgres", "postgresql", "redis", "database", "db")):
        return True
    if commands & {"stateful-postgres", "stateful-redis"}:
        return True
    return any(term in notes for term in ("postgres", "postgresql", "redis", "database", "stateful service"))


def _has_capability_evidence(paths: Iterable[str], notes: str, commands: set[str], capability: str) -> bool:
    normalized_paths = " \n".join(paths).casefold()
    evidence_terms = _CAPABILITY_EVIDENCE_TERMS.get(capability, (capability,))
    if any(term in normalized_paths or term in notes for term in evidence_terms):
        return True
    command_profiles = {
        "api": {"python-api"},
        "cli": {"cli"},
        "worker": {"worker-queue"},
    }
    return bool(commands & command_profiles.get(capability, set()))


def _needs_test_contract(profiles: set[str], capabilities: set[str]) -> bool:
    return "python-pytest" in profiles or "tests" in capabilities


def _has_test_evidence(files: Iterable[object], notes: str, commands: set[str]) -> bool:
    for item in files:
        path = str(getattr(item, "path", "")).casefold()
        kind = str(getattr(item, "kind", "")).casefold()
        purpose = str(getattr(item, "purpose", "")).casefold()
        if kind == "test":
            return True
        if "/test" in path or path.startswith("test") or "_test." in path or ".test." in path:
            return True
        if "test" in purpose or "validation case" in purpose:
            return True
    if "python-pytest" in commands:
        return True
    return any(term in notes for term in ("test surface", "test suite", "unit test", "integration test"))


def _missing_surface_issue(issue_type: str, capability: str, profiles: set[str]) -> PlanCoverageIssueProposal:
    return PlanCoverageIssueProposal(
        issue_type=issue_type,
        severity="blocking_completion",
        message=f"The plan requires {capability} validation but does not declare a {capability} surface.",
        details={"capability": capability, "required_profiles": sorted(profiles)},
        acceptance=[
            f"declare a generated {capability} surface or a VM-local validation command",
            "keep the surface derived from the user's requirements, not from benchmark shortcuts",
        ],
    )


def _basename(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()
