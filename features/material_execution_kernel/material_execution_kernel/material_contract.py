"""MaterialContract v0.1 freeze and validation.

The contract is deterministic, scenario-neutral evidence that sits between a
builder plan and file generation. It does not generate content and it never
executes or materializes files.
"""

from __future__ import annotations

import re
from typing import Iterable

from pydantic import ValidationError

from material_execution_kernel.material_builder_client import (
    MaterialPlanProposal,
    MaterialRequirementProposal,
    PlannedMaterialFile,
)
from material_execution_kernel.types import (
    MaterialContract,
    MaterialContractArtifactExpectation,
    MaterialContractCompletionCriterion,
    MaterialContractDependencyStrategy,
    MaterialContractInterface,
    MaterialContractLanguage,
    MaterialContractPlannedFile,
    MaterialContractRequirement,
    MaterialContractValidationProfile,
    MaterialSessionRequest,
)
from material_execution_kernel.validation_plan import effective_required_capabilities


class MaterialContractValidationError(ValueError):
    """Raised when the frozen material contract cannot support file generation."""


def freeze_material_contract(
    *,
    session_id: str,
    request: MaterialSessionRequest,
    plan: MaterialPlanProposal,
) -> MaterialContract:
    """Build and validate a frozen MaterialContract from request and plan evidence."""

    contract_id = f"contract:{session_id}:v0.1"
    requirements = _requirements(request, plan)
    requirement_ids = {item.requirement_id for item in requirements}
    file_id_by_path = {file.path: _file_id(file.path) for file in plan.files}
    planned_files = [
        MaterialContractPlannedFile(
            file_id=file_id_by_path[file.path],
            path=file.path,
            purpose=file.purpose,
            kind=file.kind,
            requirement_ids=_requirement_refs_or_default(
                [*file.requirement_refs, *_misplaced_requirement_refs(file.contract_refs)],
                default=_default_file_requirement_refs(file, requirement_ids),
                requirement_ids=requirement_ids,
            ),
            contract_refs=_contract_refs(file.contract_refs, contract_id=contract_id),
        )
        for file in plan.files
    ]
    validation_profiles = _validation_profiles(
        request=request,
        plan=plan,
        contract_id=contract_id,
        requirement_ids=requirement_ids,
        file_id_by_path=file_id_by_path,
    )
    artifact_expectations = _artifact_expectations(
        request=request,
        plan=plan,
        contract_id=contract_id,
        requirement_ids=requirement_ids,
        file_id_by_path=file_id_by_path,
    )
    completion_criteria = _completion_criteria(
        plan=plan,
        contract_id=contract_id,
        requirement_ids=requirement_ids,
        validation_profiles=validation_profiles,
        artifact_expectations=artifact_expectations,
    )
    interfaces = _interfaces(plan=plan, requirement_ids=requirement_ids, file_id_by_path=file_id_by_path)
    try:
        return MaterialContract(
            contract_id=contract_id,
            session_id=session_id,
            task_id=request.task_id,
            project_root=plan.project_root,
            language=MaterialContractLanguage(
                original_query_language=request.language_context.original_language,
                working_language=request.language_context.working_language,
                internal_contract_language=request.language_context.internal_contract_language,
                final_response_language=request.language_context.final_response_language,
                translation_available=request.language_context.translation_available,
            ),
            requirements=requirements,
            planned_files=planned_files,
            intended_interfaces=interfaces,
            validation_profiles=validation_profiles,
            artifact_expectations=artifact_expectations,
            completion_criteria=completion_criteria,
            dependency_policy=request.constraints.dependency_policy,
            dependency_strategy=_dependency_strategy(plan=plan, contract_id=contract_id, requirement_ids=requirement_ids),
            frozen=True,
            neutrality_notes=[
                "Contract is derived from typed request, capabilities and material plan evidence.",
                "Runtime execution remains owned by the VM-backed sandbox owner.",
            ],
        )
    except ValidationError as exc:
        raise MaterialContractValidationError(str(exc)) from exc


def _requirements(
    request: MaterialSessionRequest,
    plan: MaterialPlanProposal,
) -> list[MaterialContractRequirement]:
    if plan.requirements:
        requirements = [_requirement_from_proposal(item) for item in plan.requirements]
    else:
        requirements = [
            MaterialContractRequirement(
                requirement_id="req:user_goal",
                description="Satisfy the normalized user goal for the material artifact.",
                source="user",
            )
        ]
        for capability in effective_required_capabilities(request):
            requirements.append(
                MaterialContractRequirement(
                    requirement_id=f"req:capability:{_slug(capability)}",
                    description=f"Provide material evidence for requested capability: {capability}.",
                    source="capability",
                    capability_refs=[capability],
                )
            )
        if plan.required_validation_profiles or plan.optional_validation_profiles:
            requirements.append(
                MaterialContractRequirement(
                    requirement_id="req:validation",
                    description="Provide validation evidence through declared sandbox validation profiles.",
                    source="derived",
                )
            )
    return _requirements_with_plan_refs(requirements, plan)


def _requirement_from_proposal(proposal: MaterialRequirementProposal) -> MaterialContractRequirement:
    return MaterialContractRequirement(
        requirement_id=proposal.requirement_id,
        description=proposal.description,
        source=proposal.source if proposal.source in {"user", "derived", "capability", "constraint"} else "derived",
        capability_refs=list(proposal.capability_refs),
    )


def _requirements_with_plan_refs(
    requirements: list[MaterialContractRequirement],
    plan: MaterialPlanProposal,
) -> list[MaterialContractRequirement]:
    deduped = _dedupe_requirements(requirements)
    if plan.requirements:
        return deduped
    for ref in _plan_requirement_refs(plan):
        requirement_id = _requirement_id_from_ref(ref)
        if not requirement_id:
            continue
        requirement_index = _canonical_ref_index(
            [item.requirement_id for item in deduped],
            requirement_aliases=True,
        )
        if _normalize_requirement_ref(ref, requirement_index, fallback=""):
            continue
        if _normalize_requirement_ref(requirement_id, requirement_index, fallback=""):
            continue
        deduped.append(
            MaterialContractRequirement(
                requirement_id=requirement_id,
                description=f"Derived material requirement referenced by the plan: {requirement_id}.",
                source="derived",
            )
        )
    return _dedupe_requirements(deduped)


def _plan_requirement_refs(plan: MaterialPlanProposal) -> list[str]:
    refs: list[str] = []
    for file in plan.files:
        refs.extend(file.requirement_refs)
        refs.extend(_misplaced_requirement_refs(file.contract_refs))
    for interface in plan.intended_interfaces:
        refs.extend(interface.requirement_refs)
    for command in plan.validation_commands.values():
        refs.extend(command.requirement_refs)
        refs.extend(_misplaced_requirement_refs(command.contract_refs))
    for artifact in plan.artifact_expectations:
        refs.extend(artifact.requirement_refs)
        refs.extend(_misplaced_requirement_refs(artifact.contract_refs))
    for criterion in plan.completion_criteria:
        refs.extend(criterion.requirement_refs)
        refs.extend(_misplaced_requirement_refs(criterion.contract_refs))
    refs.extend(plan.dependency_strategy.requirement_refs)
    refs.extend(_misplaced_requirement_refs(plan.dependency_strategy.contract_refs))
    return _dedupe(refs)


def _requirement_id_from_ref(ref: str) -> str:
    value = str(ref).strip()
    if not value:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_.:-]{3,128}", value):
        return value
    slug = _slug(value)
    if len(slug) < 3:
        slug = f"req:{slug or 'item'}"
    return slug[:128]


def _dependency_strategy(
    *,
    plan: MaterialPlanProposal,
    contract_id: str,
    requirement_ids: set[str],
) -> MaterialContractDependencyStrategy:
    proposal = plan.dependency_strategy
    declared_files = list(proposal.declared_dependency_files) or [
        file.path for file in plan.files if _looks_like_dependency_manifest(file)
    ]
    refs = _requirement_refs_or_default(
        [*proposal.requirement_refs, *_misplaced_requirement_refs(proposal.contract_refs)],
        default=_dependency_requirement_refs(requirement_ids),
        requirement_ids=requirement_ids,
    )
    contract_refs = list(proposal.contract_refs)
    contract_refs = _contract_refs(contract_refs, contract_id=contract_id)
    if not refs and not contract_refs:
        contract_refs = [contract_id]
    return MaterialContractDependencyStrategy(
        declared_dependency_files=_dedupe(declared_files),
        external_dependencies=_dedupe(_normalize_dependency_name(item) for item in proposal.external_dependencies),
        install_profiles=_dedupe(proposal.install_profiles),
        lockfiles=_dedupe(proposal.lockfiles),
        native_builds_required=proposal.native_builds_required,
        network_required=proposal.network_required if proposal.network_required in {"none", "dependency-cache", "external"} else "none",
        requirement_ids=refs,
        contract_refs=contract_refs,
    )


def _validation_profiles(
    *,
    request: MaterialSessionRequest,
    plan: MaterialPlanProposal,
    contract_id: str,
    requirement_ids: set[str],
    file_id_by_path: dict[str, str],
) -> list[MaterialContractValidationProfile]:
    del request
    profiles = _dedupe([*plan.required_validation_profiles, *plan.optional_validation_profiles])
    if not profiles:
        profiles = ["artifact"]
    validation_profiles: list[MaterialContractValidationProfile] = []
    for profile in profiles:
        command = plan.validation_commands.get(profile)
        requirement_refs = _requirement_refs_or_default(
            [
                *(command.requirement_refs if command else []),
                *(_misplaced_requirement_refs(command.contract_refs) if command else []),
            ],
            default=_default_validation_requirement_refs(profile, requirement_ids),
            requirement_ids=requirement_ids,
        )
        contract_refs = _contract_refs(command.contract_refs, contract_id=contract_id) if command else []
        if not requirement_refs and not contract_refs:
            contract_refs = [contract_id]
        validation_profiles.append(
            MaterialContractValidationProfile(
                validation_id=_validation_id(profile),
                profile=profile,
                requirement_ids=requirement_refs,
                contract_refs=contract_refs,
                file_ids=_file_ids_for_profile(profile, file_id_by_path),
                command_ref=_validation_id(profile) if command else None,
            )
        )
    return validation_profiles


def _artifact_expectations(
    *,
    request: MaterialSessionRequest,
    plan: MaterialPlanProposal,
    contract_id: str,
    requirement_ids: set[str],
    file_id_by_path: dict[str, str],
) -> list[MaterialContractArtifactExpectation]:
    if plan.artifact_expectations:
        return [
            MaterialContractArtifactExpectation(
                artifact_id=item.artifact_id,
                root=item.root,
                purpose=item.purpose,
                requirement_ids=_requirement_refs_or_default(
                    [*item.requirement_refs, *_misplaced_requirement_refs(item.contract_refs)],
                    default=[],
                    requirement_ids=requirement_ids,
                ),
                contract_refs=_contract_refs(item.contract_refs, contract_id=contract_id) or [contract_id],
                file_ids=_paths_to_file_ids(item.file_refs, file_id_by_path),
            )
            for item in plan.artifact_expectations
        ]
    root = request.constraints.expected_artifact_root or plan.project_root
    refs = ["req:user_goal"] if "req:user_goal" in requirement_ids else []
    return [
        MaterialContractArtifactExpectation(
            artifact_id="artifact:project",
            root=root,
            purpose="Package the validated material project root as the session artifact.",
            requirement_ids=refs,
            contract_refs=[] if refs else [contract_id],
            file_ids=list(file_id_by_path.values()),
        )
    ]


def _completion_criteria(
    *,
    plan: MaterialPlanProposal,
    contract_id: str,
    requirement_ids: set[str],
    validation_profiles: list[MaterialContractValidationProfile],
    artifact_expectations: list[MaterialContractArtifactExpectation],
) -> list[MaterialContractCompletionCriterion]:
    validation_ids = {item.validation_id for item in validation_profiles}
    artifact_ids = {item.artifact_id for item in artifact_expectations}
    if plan.completion_criteria:
        return [
            MaterialContractCompletionCriterion(
                criterion_id=item.criterion_id,
                description=item.description,
                requirement_ids=_requirement_refs_or_default(
                    [*item.requirement_refs, *_misplaced_requirement_refs(item.contract_refs)],
                    default=[],
                    requirement_ids=requirement_ids,
                ),
                validation_ids=_validation_refs(item.validation_refs, validation_ids),
                artifact_ids=_artifact_refs(item.artifact_refs, artifact_ids),
                contract_refs=_contract_refs(item.contract_refs, contract_id=contract_id) or [contract_id],
            )
            for item in plan.completion_criteria
        ]
    refs = ["req:user_goal"] if "req:user_goal" in requirement_ids else []
    return [
        MaterialContractCompletionCriterion(
            criterion_id="criterion:required_validations",
            description="All required validation profiles pass inside the governed sandbox.",
            requirement_ids=list(refs),
            validation_ids=[item.validation_id for item in validation_profiles],
            artifact_ids=[],
            contract_refs=[] if refs else [contract_id],
        ),
        MaterialContractCompletionCriterion(
            criterion_id="criterion:artifact",
            description="A package artifact is produced with hash evidence.",
            requirement_ids=list(refs),
            validation_ids=[],
            artifact_ids=[item.artifact_id for item in artifact_expectations],
            contract_refs=[] if refs else [contract_id],
        ),
    ]


def _interfaces(
    *,
    plan: MaterialPlanProposal,
    requirement_ids: set[str],
    file_id_by_path: dict[str, str],
) -> list[MaterialContractInterface]:
    if plan.intended_interfaces:
        return [
            MaterialContractInterface(
                interface_id=item.interface_id,
                kind=item.kind if item.kind in {"api", "cli", "worker", "service", "library", "data", "artifact", "other"} else "other",
                name=item.name,
                purpose=item.purpose,
                requirement_ids=_requirement_refs_or_default(
                    item.requirement_refs,
                    default=_default_interface_refs(item.kind, requirement_ids),
                    requirement_ids=requirement_ids,
                ),
                file_ids=_paths_to_file_ids(item.file_refs, file_id_by_path),
            )
            for item in plan.intended_interfaces
        ]
    return _derived_interfaces(plan=plan, requirement_ids=requirement_ids, file_id_by_path=file_id_by_path)


def _derived_interfaces(
    *,
    plan: MaterialPlanProposal,
    requirement_ids: set[str],
    file_id_by_path: dict[str, str],
) -> list[MaterialContractInterface]:
    interfaces: list[MaterialContractInterface] = []
    notes = " ".join(plan.architecture_notes).casefold()
    interface_terms = {
        "api": ("api", "http", "endpoint", "route"),
        "cli": ("cli", "command-line", "command line", "console"),
        "worker": ("worker", "queue", "background"),
    }
    for kind, terms in interface_terms.items():
        paths = [
            file.path
            for file in plan.files
            if _file_can_provide_runtime_interface(kind, file)
            and any(term in f"{file.path} {file.purpose}".casefold() for term in terms)
        ]
        requested_runtime_surface = _runtime_interface_requested(kind, requirement_ids)
        if not paths and not (requested_runtime_surface and any(term in notes for term in terms)):
            continue
        interfaces.append(
            MaterialContractInterface(
                interface_id=f"interface:{kind}",
                kind=kind,
                name=f"{kind} surface",
                purpose=f"Expose the requested {kind} capability through generated project files.",
                requirement_ids=_default_interface_refs(kind, requirement_ids),
                file_ids=_paths_to_file_ids(paths, file_id_by_path),
            )
        )
    return interfaces


def _file_can_provide_runtime_interface(kind: str, file: PlannedMaterialFile) -> bool:
    """Return whether a planned file can plausibly expose a runtime surface."""

    file_kind = str(file.kind or "").casefold()
    path = str(file.path or "").casefold()
    text = f"{path} {file.purpose}".casefold()
    non_runtime_kinds = {
        "asset",
        "config",
        "data",
        "document",
        "docs",
        "markdown",
        "test",
        "text",
    }
    if file_kind in non_runtime_kinds:
        return False
    if kind == "cli":
        return (
            "cli" in path
            or "__main__.py" in path
            or "entrypoint" in text
            or "command-line interface" in text
            or "command line interface" in text
            or "console script" in text
        )
    return True


def _runtime_interface_requested(kind: str, requirement_ids: set[str]) -> bool:
    wanted = {
        _canonical_ref(kind),
        _canonical_ref(f"req:{kind}"),
        _canonical_ref(f"req:capability:{kind}"),
        _canonical_ref(f"capability:{kind}"),
    }
    aliases: set[str] = set()
    for requirement_id in requirement_ids:
        aliases.update(_canonical_requirement_aliases(requirement_id))
    return bool(wanted & aliases)


def _dedupe_requirements(requirements: list[MaterialContractRequirement]) -> list[MaterialContractRequirement]:
    seen: set[str] = set()
    deduped: list[MaterialContractRequirement] = []
    for requirement in requirements:
        if requirement.requirement_id in seen:
            continue
        seen.add(requirement.requirement_id)
        deduped.append(requirement)
    return deduped


def _default_file_requirement_refs(file: PlannedMaterialFile, requirement_ids: set[str]) -> list[str]:
    text = f"{file.path} {file.purpose} {file.kind}".casefold()
    refs: list[str] = []
    for capability in ("api", "cli", "worker", "postgres", "redis", "tests", "python"):
        req = f"req:capability:{_slug(capability)}"
        if req in requirement_ids and (capability in text or _capability_alias(capability) in text):
            refs.append(req)
    if file.kind == "test" and "req:capability:tests" in requirement_ids:
        refs.append("req:capability:tests")
    if file.kind == "python" and "req:capability:python" in requirement_ids:
        refs.append("req:capability:python")
    if not refs and "req:user_goal" in requirement_ids:
        refs.append("req:user_goal")
    if not refs and len(requirement_ids) == 1:
        refs.extend(requirement_ids)
    return _dedupe(refs)


def _dependency_requirement_refs(requirement_ids: set[str]) -> list[str]:
    refs = [
        requirement_id
        for requirement_id in requirement_ids
        if requirement_id in {"req:capability:python", "req:capability:node", "req:validation"}
    ]
    if not refs and "req:user_goal" in requirement_ids:
        refs.append("req:user_goal")
    if not refs and len(requirement_ids) == 1:
        refs.extend(requirement_ids)
    return _dedupe(refs)


def _looks_like_dependency_manifest(file: PlannedMaterialFile) -> bool:
    path = file.path.rsplit("/", 1)[-1].casefold()
    return path in {
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "package.json",
        "package-lock.json",
        "poetry.lock",
        "uv.lock",
        "pdm.lock",
    }


def _default_validation_requirement_refs(profile: str, requirement_ids: set[str]) -> list[str]:
    refs = []
    for capability in _capabilities_for_profile(profile):
        req = f"req:capability:{_slug(capability)}"
        if req in requirement_ids:
            refs.append(req)
    if not refs and "req:validation" in requirement_ids:
        refs.append("req:validation")
    if not refs and "req:user_goal" in requirement_ids:
        refs.append("req:user_goal")
    if not refs and len(requirement_ids) == 1:
        refs.extend(requirement_ids)
    if not refs:
        refs.extend(sorted(requirement_ids))
    return _dedupe(refs)


def _default_interface_refs(kind: str, requirement_ids: set[str]) -> list[str]:
    req = f"req:capability:{_slug(kind)}"
    if req in requirement_ids:
        return [req]
    if "req:user_goal" in requirement_ids:
        return ["req:user_goal"]
    if len(requirement_ids) == 1:
        return list(requirement_ids)
    return []


def _capabilities_for_profile(profile: str) -> tuple[str, ...]:
    return {
        "artifact": ("artifact",),
        "cli": ("cli",),
        "docker-compose-runtime": ("docker_compose", "docker_runtime"),
        "docker-compose-static": ("docker_compose",),
        "node-basic": ("node",),
        "python-api": ("api", "python"),
        "python-basic": ("python",),
        "python-pytest": ("tests", "python"),
        "stateful-postgres": ("postgres",),
        "stateful-redis": ("redis",),
        "worker-queue": ("worker",),
    }.get(profile, ())


def _capability_alias(capability: str) -> str:
    return {"tests": "test", "postgres": "database"}.get(capability, capability)


def _file_ids_for_profile(profile: str, file_id_by_path: dict[str, str]) -> list[str]:
    if profile == "artifact":
        return list(file_id_by_path.values())
    return []


def _paths_to_file_ids(paths: Iterable[str], file_id_by_path: dict[str, str]) -> list[str]:
    return [file_id_by_path.get(path, _file_id(path)) for path in paths]


def _validation_refs(refs: Iterable[str], validation_ids: set[str]) -> list[str]:
    normalized: list[str] = []
    validation_index = _canonical_ref_index(validation_ids)
    for ref in refs:
        if ref in validation_ids:
            normalized.append(ref)
            continue
        validation_ref = _validation_id(ref)
        if validation_ref in validation_ids:
            normalized.append(validation_ref)
            continue
        normalized.append(_normalize_contract_ref(ref, validation_index, fallback=validation_ref))
    return _dedupe(normalized)


def _artifact_refs(refs: Iterable[str], artifact_ids: set[str]) -> list[str]:
    artifact_index = _canonical_ref_index(artifact_ids)
    return _dedupe(_normalize_contract_ref(ref, artifact_index, fallback=ref) for ref in refs if ref)


def _contract_refs(refs: Iterable[str], *, contract_id: str) -> list[str]:
    return _dedupe(ref for ref in refs if ref == contract_id)


def _misplaced_requirement_refs(refs: Iterable[str]) -> list[str]:
    return _dedupe(ref for ref in refs if _looks_like_requirement_ref(ref))


def _looks_like_requirement_ref(ref: str) -> bool:
    canonical = _canonical_ref(ref)
    return (
        canonical.startswith("req")
        or canonical.startswith("requirement")
        or canonical.startswith("capability")
    ) and not canonical.startswith("contract")


def _refs_or_default(refs: Iterable[str], *, default: list[str]) -> list[str]:
    refs = [ref for ref in refs if ref]
    return _dedupe(refs or default)


def _requirement_refs_or_default(
    refs: Iterable[str],
    *,
    default: list[str],
    requirement_ids: set[str],
) -> list[str]:
    selected = _refs_or_default(refs, default=default)
    requirement_index = _canonical_ref_index(requirement_ids, requirement_aliases=True)
    return _dedupe(_normalize_requirement_ref(ref, requirement_index, fallback=ref) for ref in selected)


def _canonical_ref_index(values: Iterable[str], *, requirement_aliases: bool = False) -> dict[str, str | None]:
    index: dict[str, str | None] = {}
    for value in values:
        aliases = _canonical_requirement_aliases(value) if requirement_aliases else (_canonical_ref(value),)
        for canonical in aliases:
            if canonical in index and index[canonical] != value:
                index[canonical] = None
                continue
            index[canonical] = value
    return index


def _normalize_contract_ref(ref: str, canonical_index: dict[str, str | None], *, fallback: str) -> str:
    match = canonical_index.get(_canonical_ref(ref))
    return match if match else fallback


def _normalize_requirement_ref(ref: str, canonical_index: dict[str, str | None], *, fallback: str) -> str:
    for canonical in _canonical_requirement_aliases(ref):
        match = canonical_index.get(canonical)
        if match:
            return match
    return fallback


def _canonical_ref(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _canonical_requirement_aliases(value: str) -> tuple[str, ...]:
    canonical = _canonical_ref(value)
    aliases = [canonical]
    for prefix in ("reqcapability", "requirementcapability", "capability"):
        if canonical.startswith(prefix) and len(canonical) > len(prefix):
            capability = canonical[len(prefix) :]
            aliases.append(capability)
            aliases.append(f"req{capability}")
    for prefix in ("requirement", "req"):
        if canonical.startswith(prefix) and len(canonical) > len(prefix):
            aliases.append(canonical[len(prefix) :])
    return tuple(_dedupe(aliases))


def _validation_id(profile: str) -> str:
    return f"validation:{_slug(profile)}"


def _normalize_dependency_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().casefold())


def _file_id(path: str) -> str:
    return f"file:{_slug(path)}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.:]+", "_", str(value).strip()).strip("_").lower()
    return slug or "item"


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
