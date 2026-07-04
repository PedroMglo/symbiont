"""Scenario-neutral interface ledger and repair obligations.

This module projects deterministic observations into a generic ledger. Python is
currently the richest extractor, but the emitted terms intentionally avoid
Python-only policy so the repair loop can reason about any generated surface.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from material_execution_kernel.types import MaterialContract, ObservedContract, ObservedContractIssue


def build_interface_ledger(
    *,
    material_contract: MaterialContract | None,
    observed_contract: ObservedContract,
) -> dict[str, Any]:
    provided_interfaces = [
        {
            "interface_id": f"provided:{item.module}:{item.name}",
            "kind": "export",
            "ecosystem": _ecosystem_for_path(observed_contract, item.path),
            "path": item.path,
            "module": item.module,
            "name": item.name,
            "symbol_kind": item.kind,
            "line": item.line,
        }
        for item in observed_contract.exports
    ]
    provided_interfaces.extend(
        {
            "interface_id": item.entrypoint_id,
            "kind": "entrypoint",
            "ecosystem": _ecosystem_for_path(observed_contract, item.path),
            "path": item.path,
            "name": item.name,
            "entrypoint_kind": item.kind,
            "line": item.line,
            "evidence": item.evidence,
        }
        for item in observed_contract.entrypoints
    )
    required_interfaces = [
        {
            "interface_id": _required_id(item.path, item.line, item.module, item.name),
            "kind": "importable_symbol" if item.name else "importable_module",
            "ecosystem": _ecosystem_for_path(observed_contract, item.path),
            "consumer_path": item.path,
            "target_module": item.module,
            "target_name": item.name,
            "line": item.line,
            "local": item.local,
            "declared_dependency": item.declared_dependency,
        }
        for item in observed_contract.imports
    ]
    call_sites = [
        {
            "call_site_id": _required_id(item.path, item.line, item.target_module, item.target_name),
            "kind": item.expectation_kind,
            "consumer_path": item.path,
            "target_module": item.target_module,
            "target_name": item.target_name,
            "line": item.line,
        }
        for item in observed_contract.test_expectations
    ]
    validation_surfaces = []
    if material_contract is not None:
        validation_surfaces = [
            {
                "validation_id": item.validation_id,
                "profile": item.profile,
                "file_ids": item.file_ids,
                "requirement_ids": item.requirement_ids,
            }
            for item in material_contract.validation_profiles
        ]
    coherence_issues = [_coherence_issue(item) for item in observed_contract.issues]
    return {
        "schema_version": "interface_ledger.v0.1",
        "observed_contract_id": observed_contract.observed_contract_id,
        "material_contract_id": material_contract.contract_id if material_contract is not None else None,
        "provided_interfaces": provided_interfaces,
        "required_interfaces": required_interfaces,
        "call_sites": call_sites,
        "entrypoints": [item for item in provided_interfaces if item["kind"] == "entrypoint"],
        "validation_surfaces": validation_surfaces,
        "coherence_issues": coherence_issues,
    }


def build_repair_obligations(
    *,
    interface_ledger: dict[str, Any],
    observed_contract: ObservedContract,
) -> list[dict[str, Any]]:
    provider_path_by_module = {
        file.module: file.path
        for file in observed_contract.files
        if file.module and file.parse_status == "parsed"
    }
    required_by_module: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    for required in interface_ledger.get("required_interfaces") or []:
        module = str(required.get("target_module") or "")
        name = required.get("target_name")
        consumer_path = str(required.get("consumer_path") or "")
        if module and consumer_path:
            required_by_module[(module, str(name) if name else None)].append(consumer_path)

    obligations: list[dict[str, Any]] = []
    for issue in observed_contract.issues:
        obligation = _obligation_from_issue(
            issue,
            provider_path_by_module=provider_path_by_module,
            required_by_module=required_by_module,
        )
        if obligation is not None:
            obligations.append(obligation)
    return _dedupe_obligations(obligations)


def obligations_for_issue(
    obligations: list[dict[str, Any]],
    *,
    target_path: str | None,
    issue_details: dict[str, Any],
) -> list[dict[str, Any]]:
    if not obligations:
        return []
    issue_module = str(issue_details.get("module") or "")
    issue_name = str(issue_details.get("missing_name") or issue_details.get("name") or "")
    selected: list[dict[str, Any]] = []
    for obligation in obligations:
        if target_path and obligation.get("target_path") == target_path:
            selected.append(obligation)
            continue
        if issue_module and obligation.get("target_module") == issue_module:
            if not issue_name or obligation.get("symbol") == issue_name:
                selected.append(obligation)
    return _dedupe_obligations(selected)


def _obligation_from_issue(
    issue: ObservedContractIssue,
    *,
    provider_path_by_module: dict[str, str],
    required_by_module: dict[tuple[str, str | None], list[str]],
) -> dict[str, Any] | None:
    details = issue.details
    if issue.issue_type in {"missing_named_symbol", "test_implementation_drift"}:
        module = str(details.get("module") or "")
        symbol = str(details.get("missing_name") or "")
        if not module or not _is_repairable_export_symbol(symbol):
            return None
        provider_path = provider_path_by_module.get(module)
        if provider_path and issue.path and _looks_like_test_path(provider_path) and not _looks_like_test_path(issue.path):
            return {
                "obligation_id": f"obligation:invalid_test_dependency:{_safe_id(issue.path)}:{_safe_id(module)}",
                "kind": "invalid_test_module_dependency",
                "target_module": module,
                "target_path": issue.path,
                "symbol": symbol,
                "required_by": _dedupe([issue.path]),
                "source_issue_type": issue.issue_type,
                "acceptance": [
                    "runtime or reusable code no longer imports from generated test modules",
                    "required behavior is implemented in a runtime/provider module instead of a test module",
                    "tests remain consumers of runtime interfaces, not providers for runtime imports",
                ],
            }
        return {
            "obligation_id": f"obligation:importable_export:{_safe_id(module)}:{_safe_id(symbol)}",
            "kind": "importable_export",
            "target_module": module,
            "target_path": provider_path,
            "symbol": symbol,
            "required_by": _dedupe([issue.path or "", *required_by_module.get((module, symbol), [])]),
            "source_issue_type": issue.issue_type,
            "acceptance": [
                "symbol is provided by the target interface surface",
                "callers importing the symbol resolve without interface drift",
                "implementation is not placeholder-only",
            ],
        }
    if issue.issue_type == "missing_import_target":
        module = str(details.get("module") or "")
        if not module:
            return None
        return {
            "obligation_id": f"obligation:importable_module:{_safe_id(module)}",
            "kind": "importable_module",
            "target_module": module,
            "target_path": provider_path_by_module.get(module),
            "symbol": details.get("name"),
            "required_by": _dedupe([issue.path or "", *required_by_module.get((module, None), [])]),
            "source_issue_type": issue.issue_type,
            "acceptance": [
                "target module is present in the generated project interface",
                "local imports resolve without creating a new import cycle",
            ],
        }
    if issue.issue_type == "missing_dependency_declaration":
        dependency = str(details.get("dependency_name") or details.get("module") or "")
        if not dependency:
            return None
        return {
            "obligation_id": f"obligation:dependency_declaration:{_safe_id(dependency)}",
            "kind": "dependency_declaration",
            "dependency_name": dependency,
            "target_path": None,
            "required_by": _dedupe([issue.path or ""]),
            "source_issue_type": issue.issue_type,
            "acceptance": [
                "dependency policy and generated metadata agree",
                "runtime imports do not rely on undeclared external packages",
            ],
        }
    return None


def _coherence_issue(issue: ObservedContractIssue) -> dict[str, Any]:
    return {
        "issue_id": issue.issue_id,
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "path": issue.path,
        "details": issue.details,
    }


def _ecosystem_for_path(observed_contract: ObservedContract, path: str) -> str:
    for file in observed_contract.files:
        if file.path == path:
            return file.ecosystem
    return "generic"


def _required_id(path: str, line: int, module: str, name: str | None) -> str:
    return f"required:{_safe_id(path)}:{line}:{_safe_id(module)}:{_safe_id(name or 'module')}"


def _looks_like_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    name = normalized.rsplit("/", 1)[-1]
    return "/tests/" in f"/{normalized}" or name.startswith("test_") or name.endswith("_test.py")


def _safe_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_.:-" else "_" for char in str(value))
    cleaned = cleaned.strip("_")
    return (cleaned or "item")[:96]


def _is_repairable_export_symbol(value: str) -> bool:
    symbol = str(value or "").strip()
    if not symbol or symbol == "*":
        return False
    # __init__.py is a package module marker; it is not a top-level export
    # named __init__ that repair should synthesize.
    if symbol == "__init__":
        return False
    return True


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _dedupe_obligations(obligations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for obligation in obligations:
        key = str(obligation.get("obligation_id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(obligation)
    return result
