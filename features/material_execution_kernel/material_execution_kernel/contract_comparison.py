"""Contract comparison gate for material sessions.

This module compares the frozen intended contract against deterministic
observations from generated files. It does not execute generated code, call an
LLM, or depend on sandbox internals.
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re

from material_execution_kernel.types import (
    ContractComparison,
    ContractComparisonIssue,
    MaterialContract,
    MaterialContractInterface,
    ObservedContract,
    ObservedContractIssue,
    RequirementTrace,
)


_BLOCKING_OBSERVED_ISSUES = {
    "observed_file_limit_exceeded",
    "observed_file_size_exceeded",
    "python_parse_error",
}
_SYMBOL_PROVIDER_ISSUES = {"missing_named_symbol", "test_implementation_drift"}
_SYMBOL_CONSUMER_ISSUES = {"missing_import_target"}
_DEPENDENCY_ISSUES = {"missing_dependency_declaration", "dependency_manifest_parse_error"}
_LOCAL_IMPORT_GRAPH_ISSUES = {"local_import_cycle"}
_RUNTIME_INTERFACE_KINDS = {"api", "cli", "worker", "service"}


def compare_contracts(
    *,
    material_contract: MaterialContract,
    observed_contract: ObservedContract,
) -> ContractComparison:
    """Compare intended and observed contracts before runtime validation."""

    traces = _requirements_trace(material_contract=material_contract, observed_contract=observed_contract)
    issues: list[ContractComparisonIssue] = []
    issues.extend(_trace_issues(traces))
    issues.extend(_validation_requirement_issues(material_contract))
    issues.extend(_planned_file_issues(material_contract=material_contract, observed_contract=observed_contract))
    issues.extend(_observed_issue_mappings(material_contract=material_contract, observed_contract=observed_contract))
    issues.extend(_dependency_policy_issues(material_contract=material_contract, observed_contract=observed_contract))
    issues.extend(_interface_issues(material_contract=material_contract, observed_contract=observed_contract))
    issues.extend(_contract_change_issues(material_contract=material_contract, observed_contract=observed_contract))
    deduped_issues = _dedupe_issues(issues)
    blocking_issue_count = sum(1 for issue in deduped_issues if issue.severity == "blocking_completion")
    status = (
        "failed_closed"
        if blocking_issue_count
        else "amendment_required"
        if any(issue.issue_type == "contract_change_required" for issue in deduped_issues)
        else "passed"
    )
    return ContractComparison(
        comparison_id=f"comparison:{material_contract.session_id}:v0.1",
        session_id=material_contract.session_id,
        task_id=material_contract.task_id,
        material_contract_id=material_contract.contract_id,
        observed_contract_id=observed_contract.observed_contract_id,
        status=status,
        requirements_trace=traces,
        issues=deduped_issues,
        blocking_issue_count=blocking_issue_count,
        evidence_refs=_comparison_evidence_refs(traces),
    )


def _requirements_trace(
    *,
    material_contract: MaterialContract,
    observed_contract: ObservedContract,
) -> list[RequirementTrace]:
    observed_paths = {item.path for item in observed_contract.files}
    observed_entrypoint_kinds = {item.kind for item in observed_contract.entrypoints}
    traces: list[RequirementTrace] = []
    for requirement in material_contract.requirements:
        planned_files = [
            item for item in material_contract.planned_files if requirement.requirement_id in item.requirement_ids
        ]
        interfaces = [
            item
            for item in material_contract.intended_interfaces
            if requirement.requirement_id in item.requirement_ids
        ]
        validations = [
            item
            for item in material_contract.validation_profiles
            if requirement.requirement_id in item.requirement_ids
        ]
        if not validations and requirement.requirement_id == "req:validation":
            validations = list(material_contract.validation_profiles)
        artifacts = [
            item
            for item in material_contract.artifact_expectations
            if requirement.requirement_id in item.requirement_ids
        ]
        criteria = [
            item
            for item in material_contract.completion_criteria
            if requirement.requirement_id in item.requirement_ids
        ]
        if not planned_files and not interfaces and not validations and not artifacts and not criteria:
            validations = list(material_contract.validation_profiles)
            artifacts = list(material_contract.artifact_expectations)
        evidence_refs: list[str] = []
        concrete_checks: list[str] = []
        for planned_file in planned_files:
            if planned_file.path in observed_paths:
                evidence_refs.append(f"observed_file:{planned_file.path}")
                concrete_checks.append(f"file_materialized:{planned_file.path}")
        for interface in interfaces:
            if interface.kind in observed_entrypoint_kinds:
                evidence_refs.append(f"observed_entrypoint:{interface.kind}")
            concrete_checks.append(f"runtime_surface:{interface.kind}:{interface.name}")
        for validation in validations:
            evidence_refs.append(f"validation_profile:{validation.profile}")
            concrete_checks.append(f"sandbox_validation:{validation.profile}")
        for artifact in artifacts:
            evidence_refs.append(f"artifact_expectation:{artifact.artifact_id}")
            concrete_checks.append(f"artifact_root:{artifact.root}")
        acceptance = _acceptance_criterion(requirement.requirement_id, criteria)
        status = _trace_status(
            evidence_refs=evidence_refs,
            planned_files=bool(planned_files),
            validations=bool(validations),
            artifacts=bool(artifacts),
            interfaces=interfaces,
            observed_entrypoint_kinds=observed_entrypoint_kinds,
        )
        traces.append(
            RequirementTrace(
                trace_id=f"trace:{_slug(requirement.requirement_id)}",
                requirement_id=requirement.requirement_id,
                acceptance_criterion=acceptance,
                intended_interface_ids=[item.interface_id for item in interfaces],
                runtime_surfaces=[f"{item.kind}:{item.name}" for item in interfaces],
                validation_profiles=[item.profile for item in validations],
                concrete_checks=_dedupe(concrete_checks),
                evidence_refs=_dedupe(evidence_refs),
                status=status,
            )
        )
    return traces


def _acceptance_criterion(requirement_id: str, criteria: list[object]) -> str:
    descriptions = [getattr(item, "description", "") for item in criteria if getattr(item, "description", "")]
    if descriptions:
        return " | ".join(descriptions[:3])
    return f"Provide observable evidence for requirement {requirement_id}."


def _trace_status(
    *,
    evidence_refs: list[str],
    planned_files: bool,
    validations: bool,
    artifacts: bool,
    interfaces: list[MaterialContractInterface],
    observed_entrypoint_kinds: set[str],
) -> str:
    runtime_interfaces = [item for item in interfaces if item.kind in _RUNTIME_INTERFACE_KINDS]
    if runtime_interfaces and not any(item.kind in observed_entrypoint_kinds for item in runtime_interfaces):
        return "missing_evidence_path"
    if not evidence_refs:
        return "missing_evidence_path"
    if validations or artifacts:
        return "pending_runtime_evidence"
    if planned_files or interfaces:
        return "covered"
    return "missing_evidence_path"


def _trace_issues(traces: list[RequirementTrace]) -> list[ContractComparisonIssue]:
    issues: list[ContractComparisonIssue] = []
    for trace in traces:
        if trace.status != "missing_evidence_path":
            continue
        issues.append(
            _issue(
                index=len(issues),
                issue_type="requirement_uncovered",
                severity="blocking_completion",
                requirement_id=trace.requirement_id,
                details={
                    "acceptance_criterion": trace.acceptance_criterion,
                    "concrete_checks": trace.concrete_checks,
                    "runtime_surfaces": trace.runtime_surfaces,
                },
            )
        )
    return issues


def _validation_requirement_issues(material_contract: MaterialContract) -> list[ContractComparisonIssue]:
    issues: list[ContractComparisonIssue] = []
    for validation in material_contract.validation_profiles:
        if validation.requirement_ids:
            continue
        issues.append(
            _issue(
                index=len(issues),
                issue_type="validation_without_requirement",
                severity="blocking_completion",
                details={
                    "validation_id": validation.validation_id,
                    "profile": validation.profile,
                    "contract_refs": validation.contract_refs,
                },
            )
        )
    return issues


def _planned_file_issues(
    *,
    material_contract: MaterialContract,
    observed_contract: ObservedContract,
) -> list[ContractComparisonIssue]:
    observed_paths = {item.path for item in observed_contract.files}
    issues: list[ContractComparisonIssue] = []
    for planned_file in material_contract.planned_files:
        if planned_file.path in observed_paths:
            continue
        issues.append(
            _issue(
                index=len(issues),
                issue_type="observed_contract_drift",
                severity="blocking_completion",
                path=planned_file.path,
                details={
                    "drift": "planned_file_missing_from_observed_contract",
                    "file_id": planned_file.file_id,
                    "requirement_ids": planned_file.requirement_ids,
                },
            )
        )
    return issues


def _observed_issue_mappings(
    *,
    material_contract: MaterialContract,
    observed_contract: ObservedContract,
) -> list[ContractComparisonIssue]:
    issues: list[ContractComparisonIssue] = []
    validation_tool_dependencies = _validation_tool_dependencies(material_contract)
    for observed_issue in observed_contract.issues:
        if observed_issue.issue_type in _SYMBOL_PROVIDER_ISSUES:
            issue_type = "missing_symbol_provider"
        elif observed_issue.issue_type in _SYMBOL_CONSUMER_ISSUES:
            issue_type = (
                "missing_symbol_provider"
                if _missing_local_import_target(observed_issue, observed_contract)
                else "undeclared_symbol_consumer"
            )
        elif observed_issue.issue_type in _DEPENDENCY_ISSUES:
            if _is_validation_tool_dependency_issue(observed_issue, validation_tool_dependencies):
                continue
            issue_type = "dependency_strategy_mismatch"
        elif observed_issue.issue_type in _LOCAL_IMPORT_GRAPH_ISSUES:
            issue_type = "local_import_cycle"
        elif observed_issue.issue_type in _BLOCKING_OBSERVED_ISSUES:
            issue_type = "observed_contract_drift"
        else:
            continue
        issues.append(
            _issue_from_observed(
                observed_issue,
                index=len(issues),
                issue_type=issue_type,
                severity="blocking_completion",
                path=_target_path_for_observed_issue(observed_issue, observed_contract, issue_type=issue_type),
            )
        )
    return issues


def _dependency_policy_issues(
    *,
    material_contract: MaterialContract,
    observed_contract: ObservedContract,
) -> list[ContractComparisonIssue]:
    issues: list[ContractComparisonIssue] = []
    strategy = material_contract.dependency_strategy
    policy = material_contract.dependency_policy
    declared_strategy_deps = {_normalize_dependency_name(item) for item in strategy.external_dependencies}
    observed_declared_deps = {
        _normalize_dependency_name(item.normalized_name or item.name)
        for item in observed_contract.dependencies
    }
    declared_deps = declared_strategy_deps | observed_declared_deps
    observed_external_imports = [
        item
        for item in observed_contract.imports
        if not item.local and not item.standard_library and not item.relative
        and not _is_validation_tool_import(item.path, item.module, _validation_tool_dependencies(material_contract))
    ]
    undeclared_imports = sorted(
        {
            _normalize_dependency_name(item.module.split(".", 1)[0])
            for item in observed_external_imports
            if _normalize_dependency_name(item.module.split(".", 1)[0]) not in declared_deps
        }
    )
    if undeclared_imports:
        issues.append(
            _issue(
                index=len(issues),
                issue_type="missing_dependency_strategy",
                severity="blocking_completion",
                details={
                    "undeclared_external_imports": undeclared_imports,
                    "declared_dependency_files": strategy.declared_dependency_files,
                    "external_dependencies": strategy.external_dependencies,
                },
            )
        )
    if strategy.external_dependencies and policy.package_install == "disabled":
        issues.append(
            _issue(
                index=len(issues),
                issue_type="external_dependency_denied",
                severity="blocking_completion",
                details={
                    "package_install_policy": policy.package_install,
                    "external_dependencies": strategy.external_dependencies,
                },
            )
        )
    if strategy.network_required == "external" and policy.network != "external":
        issues.append(
            _issue(
                index=len(issues),
                issue_type="external_dependency_denied",
                severity="blocking_completion",
                details={
                    "network_policy": policy.network,
                    "network_required": strategy.network_required,
                },
            )
        )
    if strategy.network_required == "dependency-cache" and policy.network == "none":
        issues.append(
            _issue(
                index=len(issues),
                issue_type="install_profile_unavailable",
                severity="blocking_completion",
                details={
                    "network_policy": policy.network,
                    "network_required": strategy.network_required,
                    "dependency_cache_profile": policy.dependency_cache_profile,
                },
            )
        )
    install_profile_requires_package_install = bool(
        declared_deps or strategy.native_builds_required or strategy.network_required != "none"
    )
    if strategy.install_profiles and install_profile_requires_package_install and policy.package_install == "disabled":
        issues.append(
            _issue(
                index=len(issues),
                issue_type="install_profile_unavailable",
                severity="blocking_completion",
                details={
                    "package_install_policy": policy.package_install,
                    "install_profiles": strategy.install_profiles,
                },
            )
        )
    if policy.lockfile_required and strategy.external_dependencies and not strategy.lockfiles:
        issues.append(
            _issue(
                index=len(issues),
                issue_type="lockfile_required",
                severity="blocking_completion",
                details={
                    "external_dependencies": strategy.external_dependencies,
                    "declared_dependency_files": strategy.declared_dependency_files,
                },
            )
        )
    if strategy.native_builds_required and policy.native_builds == "deny":
        issues.append(
            _issue(
                index=len(issues),
                issue_type="native_build_policy_denied",
                severity="blocking_completion",
                details={"native_builds_policy": policy.native_builds},
            )
        )
    return issues


def _validation_tool_dependencies(material_contract: MaterialContract) -> set[str]:
    dependencies: set[str] = set()
    profiles = {item.profile for item in material_contract.validation_profiles}
    if "python-pytest" in profiles:
        dependencies.add("pytest")
    return dependencies


def _is_validation_tool_dependency_issue(
    observed_issue: ObservedContractIssue,
    validation_tool_dependencies: set[str],
) -> bool:
    if observed_issue.issue_type != "missing_dependency_declaration":
        return False
    dependency_name = str(observed_issue.details.get("dependency_name") or "")
    return _is_validation_tool_import(observed_issue.path or "", dependency_name, validation_tool_dependencies)


def _is_validation_tool_import(path: str, module: str, validation_tool_dependencies: set[str]) -> bool:
    root = _normalize_dependency_name(module.split(".", 1)[0])
    return _looks_like_test_path(path) and root in validation_tool_dependencies


def _target_path_for_observed_issue(
    observed_issue: ObservedContractIssue,
    observed_contract: ObservedContract,
    *,
    issue_type: str,
) -> str | None:
    if issue_type == "dependency_strategy_mismatch" and observed_issue.issue_type == "missing_dependency_declaration":
        return None
    if issue_type != "missing_symbol_provider":
        return observed_issue.path
    module = str(observed_issue.details.get("module") or "")
    provider_path = _provider_path_for_module(module, observed_contract)
    if (
        provider_path
        and observed_issue.path
        and _looks_like_test_path(provider_path)
        and not _looks_like_test_path(observed_issue.path)
    ):
        return observed_issue.path
    return (
        provider_path
        or _provider_path_for_missing_local_module(module, observed_issue, observed_contract)
        or observed_issue.path
    )


def _provider_path_for_module(module: str, observed_contract: ObservedContract) -> str | None:
    if not module:
        return None
    root = module.split(".", 1)[0]
    for observed_file in observed_contract.files:
        if observed_file.module == module:
            return observed_file.path
    for observed_file in observed_contract.files:
        observed_module = observed_file.module or ""
        observed_root = observed_module.split(".", 1)[0]
        if observed_root.startswith(f"{root}_"):
            return observed_file.path
    return None


def _missing_local_import_target(
    observed_issue: ObservedContractIssue,
    observed_contract: ObservedContract,
) -> bool:
    if observed_issue.issue_type != "missing_import_target":
        return False
    module = str(observed_issue.details.get("module") or "")
    return _provider_path_for_missing_local_module(module, observed_issue, observed_contract) is not None


def _provider_path_for_missing_local_module(
    module: str,
    observed_issue: ObservedContractIssue,
    observed_contract: ObservedContract,
) -> str | None:
    for candidate_module in _missing_provider_module_candidates(module, observed_issue, observed_contract):
        provider_path = _path_for_local_module(candidate_module, observed_contract)
        if provider_path is not None:
            return provider_path
    return None


def _missing_provider_module_candidates(
    module: str,
    observed_issue: ObservedContractIssue,
    observed_contract: ObservedContract,
) -> list[str]:
    candidates = [_normalize_module_name(module)]
    if "." in module:
        return [candidate for candidate in candidates if candidate]
    consumer = _observed_file_by_path(observed_issue.path, observed_contract)
    if consumer is None or not consumer.module:
        return [candidate for candidate in candidates if candidate]
    consumer_parts = [part for part in consumer.module.split(".") if part]
    if not consumer_parts:
        return [candidate for candidate in candidates if candidate]
    path_name = PurePosixPath(consumer.path.replace("\\", "/")).name
    base_parts = consumer_parts if path_name == "__init__.py" else consumer_parts[:-1]
    if base_parts:
        candidates.append(".".join([*base_parts, module]))
    return _dedupe([candidate for candidate in candidates if candidate])


def _path_for_local_module(module: str, observed_contract: ObservedContract) -> str | None:
    parts = [part for part in module.split(".") if part]
    if not parts:
        return None
    root = parts[0]
    for observed_file in observed_contract.files:
        observed_module = observed_file.module or ""
        observed_parts = [part for part in observed_module.split(".") if part]
        if not observed_parts or observed_parts[0] != root:
            continue
        path_parts = list(PurePosixPath(observed_file.path.replace("\\", "/")).parts)
        try:
            root_index = path_parts.index(root)
        except ValueError:
            continue
        prefix = path_parts[:root_index]
        if len(parts) == 1:
            candidate_parts = [*prefix, root, "__init__.py"]
        else:
            candidate_parts = [*prefix, *parts[:-1], f"{parts[-1]}.py"]
        return "/".join(candidate_parts)
    return None


def _observed_file_by_path(path: str | None, observed_contract: ObservedContract):
    if not path:
        return None
    normalized = path.replace("\\", "/")
    for observed_file in observed_contract.files:
        if observed_file.path.replace("\\", "/") == normalized:
            return observed_file
    return None


def _normalize_module_name(module: str) -> str:
    return ".".join(part for part in str(module or "").strip(".").split(".") if part)


def _interface_issues(
    *,
    material_contract: MaterialContract,
    observed_contract: ObservedContract,
) -> list[ContractComparisonIssue]:
    observed_entrypoint_kinds = {item.kind for item in observed_contract.entrypoints}
    planned_file_paths_by_id = {item.file_id: item.path for item in material_contract.planned_files}
    observed_paths = {item.path for item in observed_contract.files}
    issues: list[ContractComparisonIssue] = []
    for interface in material_contract.intended_interfaces:
        if interface.kind not in _RUNTIME_INTERFACE_KINDS:
            continue
        file_paths = [planned_file_paths_by_id[file_id] for file_id in interface.file_ids if file_id in planned_file_paths_by_id]
        has_observed_surface = interface.kind in observed_entrypoint_kinds
        has_observed_file_surface = any(path in observed_paths and _surface_hint(interface.kind, path) for path in file_paths)
        if has_observed_surface or has_observed_file_surface:
            continue
        issues.append(
            _issue(
                index=len(issues),
                issue_type="entrypoint_contract_mismatch",
                severity="blocking_completion",
                requirement_id=interface.requirement_ids[0] if interface.requirement_ids else None,
                details={
                    "interface_id": interface.interface_id,
                    "kind": interface.kind,
                    "name": interface.name,
                    "file_ids": interface.file_ids,
                    "observed_entrypoint_kinds": sorted(observed_entrypoint_kinds),
                },
            )
        )
    return issues


def _contract_change_issues(
    *,
    material_contract: MaterialContract,
    observed_contract: ObservedContract,
) -> list[ContractComparisonIssue]:
    intended_kinds = {item.kind for item in material_contract.intended_interfaces}
    issues: list[ContractComparisonIssue] = []
    for entrypoint in observed_contract.entrypoints:
        if entrypoint.kind in intended_kinds or entrypoint.kind not in _RUNTIME_INTERFACE_KINDS:
            continue
        issues.append(
            _issue(
                index=len(issues),
                issue_type="contract_change_required",
                severity="info",
                path=entrypoint.path,
                details={
                    "reason": "observed_runtime_surface_not_declared_in_material_contract",
                    "entrypoint_kind": entrypoint.kind,
                    "entrypoint_name": entrypoint.name,
                    "evidence": entrypoint.evidence,
                },
            )
        )
    return issues


def _surface_hint(kind: str, path: str) -> bool:
    text = path.casefold()
    hints = {
        "api": ("api", "http", "route", "server", "endpoint"),
        "cli": ("cli", "command", "console"),
        "worker": ("worker", "queue", "job", "consumer"),
        "service": ("service", "daemon"),
    }.get(kind, ())
    return any(hint in text for hint in hints)


def _looks_like_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    name = normalized.rsplit("/", 1)[-1]
    return "/tests/" in f"/{normalized}" or name.startswith("test_") or name.endswith("_test.py")


def _comparison_evidence_refs(traces: list[RequirementTrace]) -> list[str]:
    refs: list[str] = []
    for trace in traces:
        refs.extend(trace.evidence_refs)
    return _dedupe(refs)


def _issue_from_observed(
    observed_issue: ObservedContractIssue,
    *,
    index: int,
    issue_type: str,
    severity: str,
    path: str | None = None,
) -> ContractComparisonIssue:
    return _issue(
        index=index,
        issue_type=issue_type,
        severity=severity,
        path=path if path is not None else observed_issue.path,
        details={
            "observed_issue_id": observed_issue.issue_id,
            "observed_issue_type": observed_issue.issue_type,
            **observed_issue.details,
        },
    )


def _issue(
    *,
    index: int,
    issue_type: str,
    severity: str,
    requirement_id: str | None = None,
    path: str | None = None,
    details: dict[str, object] | None = None,
) -> ContractComparisonIssue:
    return ContractComparisonIssue(
        issue_id=f"comparison_issue:{_slug(issue_type)}:{index}",
        issue_type=issue_type,
        severity=severity,  # type: ignore[arg-type]
        requirement_id=requirement_id,
        path=path,
        details=details or {},
    )


def _dedupe_issues(issues: list[ContractComparisonIssue]) -> list[ContractComparisonIssue]:
    seen = set()
    deduped: list[ContractComparisonIssue] = []
    for index, issue in enumerate(issues):
        key = (issue.issue_type, issue.requirement_id, issue.path, repr(sorted(issue.details.items())))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            issue.model_copy(update={"issue_id": f"comparison_issue:{_slug(issue.issue_type)}:{index}"})
        )
    return deduped


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value).strip()).strip("_").lower()
    return slug or "item"


def _normalize_dependency_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().casefold())
