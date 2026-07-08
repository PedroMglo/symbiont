"""Material builder proposal logic.

The current implementation intentionally has no static generation shortcut. It
can validate explicit contract blueprints supplied by a caller, and otherwise
returns a typed blocked state until an LLM proposal backend is wired through the
material lanes.
"""

from __future__ import annotations

from typing import Any

from material_builder.config import get_settings
from material_builder.llm_io import (
    MaterialLLMError,
    critique_repair_with_llm,
    generate_files_with_llm,
    generate_patch_with_llm,
    generate_plan_with_llm,
    repair_plan_with_llm,
)
from material_builder.types import (
    GeneratedFileProposal,
    MaterialFileSpec,
    MaterialFileGenerationRequest,
    MaterialFileGenerationResponse,
    MaterialPatchGenerationRequest,
    MaterialPatchGenerationResponse,
    MaterialPlan,
    MaterialPlanRepairRequest,
    MaterialPlanRepairResponse,
    MaterialPlanRequest,
    MaterialPlanResponse,
    MaterialRepairCriticRequest,
    MaterialRepairCriticResponse,
    PatchSetProposal,
    ReplacementProposal,
)


class MaterialBuilderBlocked(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)


def create_plan(request: MaterialPlanRequest) -> MaterialPlanResponse:
    blueprint = request.constraints.get("plan_blueprint")
    if not isinstance(blueprint, dict):
        llm = get_settings().llm_plan
        if not llm.configured:
            raise MaterialBuilderBlocked(
                "material_builder_backend_unavailable",
                (
                    "material_builder has no LLM proposal backend configured and no explicit "
                    "plan_blueprint was provided"
                ),
                details={
                    "task_id": request.task_id,
                    "static_generation_shortcut_used": False,
                    "required_capabilities": request.required_capabilities,
                },
            )
        try:
            return generate_plan_with_llm(request, llm, repair_llm=get_settings().llm_repair)
        except MaterialLLMError as exc:
            raise MaterialBuilderBlocked(exc.code, str(exc), details=exc.details) from exc
    plan_payload = {
        "project_root": blueprint.get("project_root"),
        "requirements": blueprint.get("requirements", []),
        "files": blueprint.get("files"),
        "intended_interfaces": blueprint.get("intended_interfaces", []),
        "required_validation_profiles": blueprint.get("required_validation_profiles", []),
        "optional_validation_profiles": blueprint.get("optional_validation_profiles", []),
        "validation_commands": blueprint.get("validation_commands", {}),
        "artifact_expectations": blueprint.get("artifact_expectations", []),
        "completion_criteria": blueprint.get("completion_criteria", []),
        "dependency_strategy": blueprint.get("dependency_strategy", {}),
        "architecture_notes": blueprint.get("architecture_notes", []),
        "variation_reason": blueprint.get("variation_reason"),
    }
    plan = MaterialPlan.model_validate(plan_payload)
    file_contents = blueprint.get("file_contents", {})
    if file_contents is not None and not isinstance(file_contents, dict):
        raise MaterialBuilderBlocked(
            "invalid_contract_blueprint",
            "plan_blueprint.file_contents must be an object mapping relative paths to text content",
        )
    return MaterialPlanResponse(
        plan=plan,
        generation_backend="contract_blueprint",
        file_contents={str(path): str(content) for path, content in dict(file_contents).items()},
        notes=["contract_blueprint accepted; no static generation shortcut was used"],
    )


def repair_plan(request: MaterialPlanRepairRequest) -> MaterialPlanRepairResponse:
    repair_blueprint = request.constraints.get("plan_repair_blueprint")
    if isinstance(repair_blueprint, dict):
        plan = MaterialPlan.model_validate(
            {
                "project_root": repair_blueprint.get("project_root"),
                "requirements": repair_blueprint.get("requirements", []),
                "files": repair_blueprint.get("files"),
                "intended_interfaces": repair_blueprint.get("intended_interfaces", []),
                "required_validation_profiles": repair_blueprint.get("required_validation_profiles", []),
                "optional_validation_profiles": repair_blueprint.get("optional_validation_profiles", []),
                "validation_commands": repair_blueprint.get("validation_commands", {}),
                "artifact_expectations": repair_blueprint.get("artifact_expectations", []),
                "completion_criteria": repair_blueprint.get("completion_criteria", []),
                "dependency_strategy": repair_blueprint.get("dependency_strategy", {}),
                "architecture_notes": repair_blueprint.get("architecture_notes", []),
                "variation_reason": repair_blueprint.get("variation_reason"),
            }
        )
        plan, completion_notes = _complete_plan_contracts_from_issues(request, plan)
        return MaterialPlanRepairResponse(
            plan=plan,
            generation_backend="contract_blueprint",
            static_generation_shortcut_used=False,
            notes=[
                "contract_blueprint repair accepted; no static generation shortcut was used",
                *completion_notes,
            ],
        )
    llm = get_settings().llm_repair
    if not llm.configured:
        raise MaterialBuilderBlocked(
            "material_builder_backend_unavailable",
            (
                "material_builder has no LLM plan repair backend configured and no explicit "
                "plan_repair_blueprint was provided"
            ),
            details={
                "task_id": request.task_id,
                "session_id": request.session_id,
                "static_generation_shortcut_used": False,
                "coverage_issue_types": [issue.issue_type for issue in request.coverage_issues],
            },
        )
    try:
        response = repair_plan_with_llm(request, llm, schema_repair_llm=get_settings().llm_repair)
    except MaterialLLMError as exc:
        raise MaterialBuilderBlocked(exc.code, str(exc), details=exc.details) from exc
    plan, completion_notes = _complete_plan_contracts_from_issues(request, response.plan)
    if completion_notes:
        return MaterialPlanRepairResponse(
            plan=plan,
            generation_backend=response.generation_backend,
            static_generation_shortcut_used=False,
            notes=[*response.notes, *completion_notes],
            model_route=response.model_route,
        )
    return response


def generate_files(request: MaterialFileGenerationRequest) -> MaterialFileGenerationResponse:
    if not request.file_contents:
        llm = get_settings().llm_file
        if not llm.configured:
            raise MaterialBuilderBlocked(
                "material_builder_backend_unavailable",
                (
                    "material_builder has no LLM file generation backend configured and no "
                    "explicit file_contents were provided"
                ),
                details={"task_id": request.task_id, "static_generation_shortcut_used": False},
        )
        try:
            files, lane_metrics = generate_files_with_llm(request, llm)
            return MaterialFileGenerationResponse(
                files=files,
                generation_backend="llm",
                static_generation_shortcut_used=False,
                model_route=llm.route,
                lane_metrics=lane_metrics,
            )
        except MaterialLLMError as exc:
            raise MaterialBuilderBlocked(exc.code, str(exc), details=exc.details) from exc
    planned_paths = {file.path for file in request.plan.files}
    supplied_paths = set(request.file_contents)
    missing = sorted(planned_paths - supplied_paths)
    if missing:
        raise MaterialBuilderBlocked(
            "chunk_missing",
            "file_contents did not include all planned file paths",
            details={"missing_paths": missing},
        )
    files = [
        GeneratedFileProposal.from_content(
            path=file.path,
            content=request.file_contents[file.path],
            kind=file.kind,
            source_plan_ref=request.source_plan_ref,
        )
        for file in request.plan.files
    ]
    return MaterialFileGenerationResponse(
        files=files,
        generation_backend="contract_blueprint",
        static_generation_shortcut_used=False,
    )


def propose_patch(request: MaterialPatchGenerationRequest) -> MaterialPatchGenerationResponse:
    for blueprint in request.patch_set_blueprints:
        if blueprint.issue_id != request.issue_id:
            continue
        _validate_patch_set_matches_request(request, blueprint)
        return MaterialPatchGenerationResponse(
            patch_set=blueprint,
            generation_backend="contract_blueprint",
            static_generation_shortcut_used=False,
        )
    for blueprint in request.replacement_blueprints:
        if blueprint.issue_id != request.issue_id and blueprint.target_path != request.target_path:
            continue
        if blueprint.target_path != request.target_path:
            raise MaterialBuilderBlocked(
                "replacement_target_mismatch",
                "replacement blueprint target_path does not match the requested repair target",
                details={"expected_path": request.target_path, "actual_path": blueprint.target_path},
            )
        if blueprint.expected_current_sha256 != request.expected_current_sha256:
            raise MaterialBuilderBlocked(
                "replacement_checksum_mismatch",
                "replacement blueprint expected_current_sha256 does not match the current target hash",
                details={
                    "target_path": request.target_path,
                    "expected_current_sha256": request.expected_current_sha256,
                    "actual_current_sha256": blueprint.expected_current_sha256,
                },
            )
        return MaterialPatchGenerationResponse(
            replacement=blueprint,
            generation_backend="contract_blueprint",
            static_generation_shortcut_used=False,
        )
    for blueprint in request.regeneration_blueprints:
        if blueprint.issue_id != request.issue_id:
            continue
        if request.target_path not in blueprint.target_paths:
            raise MaterialBuilderBlocked(
                "regeneration_target_mismatch",
                "regeneration blueprint does not include the requested repair target",
                details={"target_path": request.target_path, "target_paths": blueprint.target_paths},
            )
        return MaterialPatchGenerationResponse(
            regeneration=blueprint,
            generation_backend="contract_blueprint",
            static_generation_shortcut_used=False,
        )
    for blueprint in request.patch_blueprints:
        if blueprint.issue_id != request.issue_id and blueprint.target_path != request.target_path:
            continue
        if blueprint.target_path != request.target_path:
            raise MaterialBuilderBlocked(
                "patch_target_mismatch",
                "patch blueprint target_path does not match the requested repair target",
                details={"expected_path": request.target_path, "actual_path": blueprint.target_path},
            )
        if blueprint.expected_current_sha256 != request.expected_current_sha256:
            raise MaterialBuilderBlocked(
                "patch_checksum_mismatch",
                "patch blueprint expected_current_sha256 does not match the current target hash",
                details={
                    "target_path": request.target_path,
                    "expected_current_sha256": request.expected_current_sha256,
                    "actual_current_sha256": blueprint.expected_current_sha256,
                },
            )
        return MaterialPatchGenerationResponse(
            patch=blueprint,
            generation_backend="contract_blueprint",
            static_generation_shortcut_used=False,
        )
    if (
        request.patch_blueprints
        or request.patch_set_blueprints
        or request.replacement_blueprints
        or request.regeneration_blueprints
    ):
        raise MaterialBuilderBlocked(
            "patch_blueprint_missing",
            "no repair blueprint matched the requested issue or target path",
            details={"issue_id": request.issue_id, "target_path": request.target_path},
        )
    llm = get_settings().llm_patch
    if not llm.configured:
        raise MaterialBuilderBlocked(
            "material_builder_backend_unavailable",
            (
                "material_builder has no LLM patch proposal backend configured and no "
                "explicit patch_blueprints were provided"
            ),
            details={"task_id": request.task_id, "issue_id": request.issue_id, "static_generation_shortcut_used": False},
        )
    try:
        repair, lane_metrics = generate_patch_with_llm(request, llm)
        if isinstance(repair, ReplacementProposal):
            return MaterialPatchGenerationResponse(
                replacement=repair,
                generation_backend="llm",
                static_generation_shortcut_used=False,
                model_route=llm.route,
                lane_metrics=lane_metrics,
            )
        if isinstance(repair, PatchSetProposal):
            return MaterialPatchGenerationResponse(
                patch_set=repair,
                generation_backend="llm",
                static_generation_shortcut_used=False,
                model_route=llm.route,
                lane_metrics=lane_metrics,
            )
        return MaterialPatchGenerationResponse(
            patch=repair,
            generation_backend="llm",
            static_generation_shortcut_used=False,
            model_route=llm.route,
            lane_metrics=lane_metrics,
        )
    except MaterialLLMError as exc:
        raise MaterialBuilderBlocked(exc.code, str(exc), details=exc.details) from exc


def critique_repair(request: MaterialRepairCriticRequest) -> MaterialRepairCriticResponse:
    llm = get_settings().llm_critic
    if not llm.configured:
        raise MaterialBuilderBlocked(
            "material_builder_critic_unavailable",
            "material_builder has no configured material critic lane",
            details={"task_id": request.task_id, "issue_id": request.issue_id, "static_generation_shortcut_used": False},
        )
    try:
        return critique_repair_with_llm(request, llm)
    except MaterialLLMError as exc:
        raise MaterialBuilderBlocked(exc.code, str(exc), details=exc.details) from exc


def _validate_patch_set_matches_request(
    request: MaterialPatchGenerationRequest,
    blueprint: PatchSetProposal,
) -> None:
    resolution = request.target_resolution
    allowed_targets = {request.target_path}
    if resolution is not None:
        allowed_targets.update(resolution.related_targets)
        allowed_targets.update(resolution.candidate_targets)
    patch_targets = {patch.target_path for patch in blueprint.patches}
    if request.target_path not in patch_targets:
        raise MaterialBuilderBlocked(
            "patch_set_primary_target_missing",
            "patch set blueprint must include the requested primary repair target",
            details={"target_path": request.target_path, "patch_targets": sorted(patch_targets)},
        )
    unexpected = sorted(patch_targets - allowed_targets)
    if unexpected:
        raise MaterialBuilderBlocked(
            "patch_set_target_not_resolved",
            "patch set blueprint touched targets outside the typed target resolution",
            details={
                "target_path": request.target_path,
                "unexpected_targets": unexpected,
                "allowed_targets": sorted(allowed_targets),
            },
        )


_CONTRACT_COMPLETION_FILES = {
    "missing_api_contract": (
        "api.py",
        "HTTP API endpoint surface derived from requested behavior",
        "python",
    ),
    "missing_cli_contract": (
        "cli.py",
        "command-line interface for requested user operations",
        "python",
    ),
    "missing_worker_contract": (
        "worker.py",
        "queue worker or background worker surface for requested asynchronous work",
        "python",
    ),
    "missing_test_contract": (
        "tests/test_behavior.py",
        "behavior test suite for requested validation",
        "test",
    ),
    "missing_dependency_strategy": (
        "pyproject.toml",
        "dependency and runtime metadata for generated project",
        "config",
    ),
    "missing_runtime_service_contract": (
        "compose.yaml",
        "VM-local service runtime contract for generated project validation",
        "compose",
    ),
    "missing_stateful_service_contract": (
        "compose.yaml",
        "VM-local stateful service runtime contract with synthetic credentials",
        "compose",
    ),
}


def _complete_plan_contracts_from_issues(
    request: MaterialPlanRepairRequest,
    plan: MaterialPlan,
) -> tuple[MaterialPlan, list[str]]:
    files = list(plan.files)
    notes = list(plan.architecture_notes)
    added: list[str] = []
    existing_paths = {item.path for item in files}
    use_root_prefix = _uses_project_root_prefix(plan)
    for issue in request.coverage_issues:
        completion = _CONTRACT_COMPLETION_FILES.get(issue.issue_type)
        if completion is None:
            continue
        relative_path, purpose, kind = completion
        path = _contract_path(plan.project_root, relative_path, use_root_prefix=use_root_prefix)
        if path in existing_paths:
            continue
        files.append(
            MaterialFileSpec(
                path=path,
                purpose=purpose,
                kind=kind,
            )
        )
        existing_paths.add(path)
        added.append(issue.issue_type)
    if not added:
        return plan, []
    notes.append(
        "Contract completion added missing plan surfaces from typed coverage issues; file content remains LLM-generated."
    )
    return (
        plan.model_copy(
            update={
                "files": files,
                "architecture_notes": notes,
                "variation_reason": _variation_reason_with_completion(plan.variation_reason, added),
            }
        ),
        [f"contract completion added plan evidence for: {', '.join(added)}"],
    )


def _uses_project_root_prefix(plan: MaterialPlan) -> bool:
    root = plan.project_root.strip("/").replace("\\", "/")
    if not root:
        return False
    return any(item.path == root or item.path.startswith(f"{root}/") for item in plan.files)


def _contract_path(project_root: str, relative_path: str, *, use_root_prefix: bool) -> str:
    clean = relative_path.strip("/").replace("\\", "/")
    if not use_root_prefix:
        return clean
    root = project_root.strip("/").replace("\\", "/")
    return f"{root}/{clean}" if root else clean


def _variation_reason_with_completion(current: str | None, added: list[str]) -> str:
    suffix = f"contract completion for {', '.join(added)}"
    if current:
        return f"{current}; {suffix}"
    return suffix


__all__ = [
    "MaterialBuilderBlocked",
    "create_plan",
    "critique_repair",
    "generate_files",
    "propose_patch",
    "repair_plan",
]
