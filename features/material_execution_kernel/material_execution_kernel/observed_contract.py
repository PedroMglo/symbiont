"""Deterministic ObservedContract v0.1 extraction.

The extractor treats generated files as untrusted data. It parses text that was
already sent to the sandbox owner for materialization; it never imports,
executes, shells out, or asks the generated project to configure the runtime.
"""

from __future__ import annotations

import ast
import configparser
from collections import defaultdict
from collections.abc import Iterable
from pathlib import PurePosixPath
import re
import sys
import tomllib
from typing import Protocol

from material_execution_kernel.material_builder_client import GeneratedMaterialFile
from material_execution_kernel.types import (
    ObservedContract,
    ObservedContractIssue,
    ObservedDependency,
    ObservedEcosystem,
    ObservedEntrypoint,
    ObservedExport,
    ObservedFile,
    ObservedImport,
    ObservedTestExpectation,
)


MAX_OBSERVED_FILES = 1024
MAX_OBSERVED_FILE_BYTES = 2_000_000
PYTHON_EXTRACTOR_VERSION = "python_ast.v0.1"
_PACKAGE_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
_EXTRAS_OR_VERSION_RE = re.compile(r"[\[<>=!~;@ ]")
_ENTRYPOINT_HINTS = {
    "api": {"api", "http", "route", "server", "endpoint"},
    "cli": {"cli", "command", "console", "terminal"},
    "worker": {"worker", "queue", "job", "consumer", "background"},
    "service": {"service", "daemon", "runtime"},
}


class ObservedContractExtractor(Protocol):
    ecosystem: ObservedEcosystem
    version: str

    def supports(self, file: GeneratedMaterialFile) -> bool:
        """Return whether this extractor owns deterministic parsing for file."""

    def extract(
        self,
        *,
        file: GeneratedMaterialFile,
        project_root: str,
        local_modules: set[str],
        declared_dependencies: set[str],
    ) -> ObservedFile:
        """Return one observed file without executing generated content."""


def extract_observed_contract(
    *,
    session_id: str,
    task_id: str,
    project_root: str,
    files: Iterable[GeneratedMaterialFile],
) -> ObservedContract:
    """Build a deterministic observed contract from generated file evidence."""

    selected_files = sorted(list(files), key=lambda file: file.path)
    contract_issues: list[ObservedContractIssue] = []
    if len(selected_files) > MAX_OBSERVED_FILES:
        contract_issues.append(
            _issue(
                index=0,
                issue_type="observed_file_limit_exceeded",
                severity="blocking_contract",
                details={"file_count": len(selected_files), "max_files": MAX_OBSERVED_FILES},
            )
        )
        selected_files = selected_files[:MAX_OBSERVED_FILES]

    dependencies, dependency_issues, metadata_entrypoints = _extract_dependency_metadata(selected_files)
    declared_dependency_names = {dependency.normalized_name for dependency in dependencies}
    local_modules = _local_python_modules(selected_files, project_root)
    local_modules.update(_project_module_aliases(selected_files))
    extractor = PythonObservedContractExtractor()

    observed_files: list[ObservedFile] = []
    for file in selected_files:
        size_bytes = len(file.content.encode("utf-8"))
        if size_bytes > MAX_OBSERVED_FILE_BYTES:
            observed_files.append(
                ObservedFile(
                    path=file.path,
                    ecosystem=_ecosystem_for_path(file.path),
                    kind=file.kind,
                    parse_status="skipped",
                    issues=[
                        _issue(
                            index=len(contract_issues),
                            issue_type="observed_file_size_exceeded",
                            severity="blocking_contract",
                            path=file.path,
                            details={"size_bytes": size_bytes, "max_bytes": MAX_OBSERVED_FILE_BYTES},
                        )
                    ],
                )
            )
            continue
        if extractor.supports(file):
            observed_files.append(
                extractor.extract(
                    file=file,
                    project_root=project_root,
                    local_modules=local_modules,
                    declared_dependencies=declared_dependency_names,
                )
            )
            continue
        observed_files.append(
            ObservedFile(
                path=file.path,
                ecosystem=_ecosystem_for_path(file.path),
                kind=file.kind,
                parse_status="skipped",
            )
        )

    imports = [item for observed_file in observed_files for item in observed_file.imports]
    exports = [item for observed_file in observed_files for item in observed_file.exports]
    entrypoints = [
        item
        for observed_file in observed_files
        for item in _entrypoints_for_file(observed_file, generated_file=_file_by_path(selected_files, observed_file.path))
    ]
    entrypoints.extend(metadata_entrypoints)
    files_by_path = {file.path: file for file in selected_files}
    test_expectations = _extract_test_expectations(observed_files, files_by_path=files_by_path)
    contract_issues.extend(dependency_issues)
    contract_issues.extend(item for observed_file in observed_files for item in observed_file.issues)
    contract_issues.extend(
        _relationship_issues(
            files=observed_files,
            imports=imports,
            exports=exports,
            local_modules=local_modules,
            declared_dependencies=declared_dependency_names,
        )
    )

    ecosystems = sorted({_ecosystem_for_path(file.path) for file in selected_files})
    return ObservedContract(
        observed_contract_id=f"observed:{session_id}:v0.1",
        session_id=session_id,
        task_id=task_id,
        project_root=project_root,
        ecosystems=ecosystems,
        files=observed_files,
        imports=imports,
        exports=exports,
        dependencies=dependencies,
        test_expectations=test_expectations,
        entrypoints=_dedupe_entrypoints(entrypoints),
        issues=_dedupe_issues(contract_issues),
        extractor_versions={"python": PYTHON_EXTRACTOR_VERSION},
    )


class PythonObservedContractExtractor:
    ecosystem: ObservedEcosystem = "python"
    version = PYTHON_EXTRACTOR_VERSION

    def supports(self, file: GeneratedMaterialFile) -> bool:
        return _is_python_file(file.path, file.kind)

    def extract(
        self,
        *,
        file: GeneratedMaterialFile,
        project_root: str,
        local_modules: set[str],
        declared_dependencies: set[str],
    ) -> ObservedFile:
        module = _module_name_for_path(file.path, project_root)
        try:
            tree = ast.parse(file.content, filename=file.path)
        except SyntaxError as exc:
            return ObservedFile(
                path=file.path,
                ecosystem="python",
                kind=file.kind,
                module=module,
                parse_status="failed",
                issues=[
                    _issue(
                        index=0,
                        issue_type="python_parse_error",
                        severity="blocking_contract",
                        path=file.path,
                        details={
                            "message": exc.msg,
                            "line": exc.lineno or 0,
                            "offset": exc.offset or 0,
                        },
                    )
                ],
            )
        imports = _python_imports(tree, path=file.path, module=module)
        exports = _python_exports(tree, path=file.path, module=module)
        annotated_imports = [
            _annotate_import(
                item,
                local_modules=local_modules,
                declared_dependencies=declared_dependencies,
            )
            for item in imports
        ]
        return ObservedFile(
            path=file.path,
            ecosystem="python",
            kind=file.kind,
            module=module,
            parse_status="parsed",
            imports=annotated_imports,
            exports=exports,
            issues=[],
        )


def _extract_dependency_metadata(
    files: list[GeneratedMaterialFile],
) -> tuple[list[ObservedDependency], list[ObservedContractIssue], list[ObservedEntrypoint]]:
    dependencies: list[ObservedDependency] = []
    issues: list[ObservedContractIssue] = []
    entrypoints: list[ObservedEntrypoint] = []
    for file in files:
        path = _normalized_path(file.path)
        if _is_requirements_file(path):
            dependencies.extend(_requirements_dependencies(file))
        elif path.name == "pyproject.toml":
            try:
                parsed = tomllib.loads(file.content)
            except tomllib.TOMLDecodeError as exc:
                issues.append(
                    _issue(
                        index=len(issues),
                        issue_type="dependency_manifest_parse_error",
                        severity="warning",
                        path=file.path,
                        details={"format": "pyproject", "message": str(exc)[:1000]},
                    )
                )
                continue
            dependencies.extend(_pyproject_dependencies(file, parsed))
            entrypoints.extend(_pyproject_entrypoints(file, parsed))
        elif path.name == "setup.cfg":
            dependencies.extend(_setup_cfg_dependencies(file))
    return _dedupe_dependencies(dependencies), issues, _dedupe_entrypoints(entrypoints)


def _requirements_dependencies(file: GeneratedMaterialFile) -> list[ObservedDependency]:
    dependencies: list[ObservedDependency] = []
    for raw_line in file.content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-") or line.startswith("--"):
            continue
        line = line.split("#", 1)[0].strip()
        match = _PACKAGE_NAME_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        dependencies.append(
            ObservedDependency(
                name=name,
                normalized_name=_normalize_dependency_name(name),
                source_path=file.path,
                source_kind="requirements",
                raw=line,
            )
        )
    return dependencies


def _pyproject_dependencies(file: GeneratedMaterialFile, parsed: dict[str, object]) -> list[ObservedDependency]:
    dependencies: list[ObservedDependency] = []
    project = parsed.get("project")
    if isinstance(project, dict):
        raw_dependencies = project.get("dependencies")
        if isinstance(raw_dependencies, list):
            dependencies.extend(
                _dependency_from_raw(file.path, "pyproject", str(item)) for item in raw_dependencies if str(item)
            )
        optional_dependencies = project.get("optional-dependencies")
        if isinstance(optional_dependencies, dict):
            for values in optional_dependencies.values():
                if isinstance(values, list):
                    dependencies.extend(
                        _dependency_from_raw(file.path, "pyproject", str(item)) for item in values if str(item)
                    )
    tool = parsed.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            poetry_dependencies = poetry.get("dependencies")
            if isinstance(poetry_dependencies, dict):
                for name in poetry_dependencies:
                    if str(name).lower() != "python":
                        dependencies.append(
                            ObservedDependency(
                                name=str(name),
                                normalized_name=_normalize_dependency_name(str(name)),
                                source_path=file.path,
                                source_kind="pyproject",
                                raw=str(name),
                            )
                        )
    return dependencies


def _pyproject_entrypoints(file: GeneratedMaterialFile, parsed: dict[str, object]) -> list[ObservedEntrypoint]:
    project = parsed.get("project")
    if not isinstance(project, dict):
        return []
    scripts = project.get("scripts")
    if not isinstance(scripts, dict):
        return []
    entrypoints: list[ObservedEntrypoint] = []
    for name, target in sorted(scripts.items()):
        entrypoints.append(
            ObservedEntrypoint(
                entrypoint_id=_observed_id("entrypoint", "cli", name),
                kind="cli",
                path=file.path,
                name=str(name),
                evidence=f"project.scripts -> {target}",
            )
        )
    return entrypoints


def _setup_cfg_dependencies(file: GeneratedMaterialFile) -> list[ObservedDependency]:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(file.content)
    except configparser.Error:
        return []
    if not parser.has_option("options", "install_requires"):
        return []
    raw_value = parser.get("options", "install_requires")
    return [
        _dependency_from_raw(file.path, "setup_cfg", item.strip())
        for item in raw_value.splitlines()
        if item.strip()
    ]


def _dependency_from_raw(path: str, source_kind: str, raw: str) -> ObservedDependency:
    name = _dependency_name_from_spec(raw)
    return ObservedDependency(
        name=name,
        normalized_name=_normalize_dependency_name(name),
        source_path=path,
        source_kind=source_kind,  # type: ignore[arg-type]
        raw=raw,
    )


def _python_imports(tree: ast.AST, *, path: str, module: str) -> list[ObservedImport]:
    imports: list[ObservedImport] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    ObservedImport(
                        path=path,
                        module=alias.name,
                        alias=alias.asname,
                        line=getattr(node, "lineno", 0),
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import_from_module(
                current_module=module,
                imported_module=node.module,
                level=node.level,
                package_init=_normalized_path(path).name == "__init__.py",
            )
            for alias in node.names:
                imports.append(
                    ObservedImport(
                        path=path,
                        module=resolved,
                        name=alias.name,
                        alias=alias.asname,
                        line=getattr(node, "lineno", 0),
                        relative=node.level > 0,
                    )
                )
    return imports


def _python_exports(tree: ast.Module, *, path: str, module: str) -> list[ObservedExport]:
    exports: list[ObservedExport] = []
    package_init = _normalized_path(path).name == "__init__.py"
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exports.append(
                ObservedExport(path=path, module=module, name=node.name, kind="function", line=node.lineno)
            )
        elif isinstance(node, ast.ClassDef):
            exports.append(ObservedExport(path=path, module=module, name=node.name, kind="class", line=node.lineno))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            exports.append(
                ObservedExport(path=path, module=module, name=node.target.id, kind="variable", line=node.lineno)
            )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    exports.append(
                        ObservedExport(path=path, module=module, name=target.id, kind="variable", line=node.lineno)
                    )
        elif package_init and isinstance(node, ast.ImportFrom):
            exports.extend(_python_package_reexports(node, path=path, module=module))
    return exports


def _python_package_reexports(node: ast.ImportFrom, *, path: str, module: str) -> list[ObservedExport]:
    if node.module == "__future__":
        return []
    exports: list[ObservedExport] = []
    for alias in node.names:
        if alias.name == "*":
            continue
        exported_name = alias.asname or alias.name
        exports.append(
            ObservedExport(
                path=path,
                module=module,
                name=exported_name,
                kind="module" if node.module is None else "unknown",
                line=getattr(node, "lineno", 0),
            )
        )
    return exports


def _extract_test_expectations(
    observed_files: list[ObservedFile],
    *,
    files_by_path: dict[str, GeneratedMaterialFile],
) -> list[ObservedTestExpectation]:
    expectations: list[ObservedTestExpectation] = []
    for observed_file in observed_files:
        if not _is_test_path(observed_file.path, observed_file.kind):
            continue
        for observed_import in observed_file.imports:
            expectation_kind = "imported_symbol" if observed_import.name else "imported_module"
            expectations.append(
                ObservedTestExpectation(
                    path=observed_file.path,
                    target_module=observed_import.module,
                    target_name=observed_import.name,
                    expectation_kind=expectation_kind,
                    line=observed_import.line,
                )
            )
        expectations.extend(_call_expectations_for_test(observed_file, files_by_path=files_by_path))
    return _dedupe_expectations(expectations)


def _call_expectations_for_test(
    observed_file: ObservedFile,
    *,
    files_by_path: dict[str, GeneratedMaterialFile],
) -> list[ObservedTestExpectation]:
    if observed_file.parse_status != "parsed":
        return []
    imported_aliases: dict[str, tuple[str, str | None]] = {}
    for observed_import in observed_file.imports:
        if observed_import.name:
            imported_aliases[observed_import.alias or observed_import.name] = (
                observed_import.module,
                observed_import.name,
            )
        else:
            imported_aliases[observed_import.alias or _import_root(observed_import.module)] = (
                observed_import.module,
                None,
            )
    source_file = files_by_path.get(observed_file.path)
    if source_file is None:
        return []
    try:
        tree = ast.parse(source_file.content, filename=source_file.path)
    except SyntaxError:
        return []
    expectations: list[ObservedTestExpectation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _call_target(node.func, imported_aliases)
        if target is None:
            continue
        target_module, target_name = target
        expectations.append(
            ObservedTestExpectation(
                path=observed_file.path,
                target_module=target_module,
                target_name=target_name,
                expectation_kind="entrypoint_call",
                line=getattr(node, "lineno", 0),
            )
        )
    return expectations

def _relationship_issues(
    *,
    files: list[ObservedFile],
    imports: list[ObservedImport],
    exports: list[ObservedExport],
    local_modules: set[str],
    declared_dependencies: set[str],
) -> list[ObservedContractIssue]:
    issues: list[ObservedContractIssue] = []
    exports_by_module: dict[str, set[str]] = defaultdict(set)
    for item in exports:
        for module in _module_export_names(item.module):
            exports_by_module[module].add(item.name)
    local_roots = {_import_root(module) for module in local_modules}
    for observed_import in imports:
        root = _import_root(observed_import.module)
        local_root = root in local_roots
        if local_root and observed_import.module not in local_modules:
            if observed_import.name and f"{observed_import.module}.{observed_import.name}" in local_modules:
                continue
            issues.append(
                _issue(
                    index=len(issues),
                    issue_type="missing_import_target",
                    severity="warning",
                    path=observed_import.path,
                    details={
                        "module": observed_import.module,
                        "name": observed_import.name,
                        "line": observed_import.line,
                    },
                )
            )
            continue
        if observed_import.name and observed_import.name != "*" and observed_import.module in local_modules:
            if f"{observed_import.module}.{observed_import.name}" in local_modules:
                continue
            if observed_import.name not in exports_by_module.get(observed_import.module, set()):
                issue_type = (
                    "test_implementation_drift"
                    if _is_test_path(observed_import.path, "other")
                    else "missing_named_symbol"
                )
                issues.append(
                    _issue(
                        index=len(issues),
                        issue_type=issue_type,
                        severity="warning",
                        path=observed_import.path,
                        details={
                            "module": observed_import.module,
                            "missing_name": observed_import.name,
                            "line": observed_import.line,
                        },
                    )
                )
            continue
        if observed_import.local or observed_import.standard_library or observed_import.declared_dependency:
            continue
        if _normalize_dependency_name(root) not in declared_dependencies:
            issues.append(
                _issue(
                    index=len(issues),
                    issue_type="missing_dependency_declaration",
                    severity="warning",
                    path=observed_import.path,
                    details={
                        "module": observed_import.module,
                        "dependency_name": root,
                        "line": observed_import.line,
                    },
                )
            )
    issues.extend(
        _local_import_cycle_issues(
            files=files,
            imports=imports,
            local_modules=local_modules,
            start_index=len(issues),
        )
    )
    return issues


def _module_export_names(module: str) -> set[str]:
    names = {module}
    names.update(_src_layout_aliases(module))
    return {name for name in names if name}


def _local_import_cycle_issues(
    *,
    files: list[ObservedFile],
    imports: list[ObservedImport],
    local_modules: set[str],
    start_index: int,
) -> list[ObservedContractIssue]:
    module_to_path = {
        str(file.module): file.path
        for file in files
        if file.module and file.parse_status == "parsed"
    }
    if len(module_to_path) < 2:
        return []
    graph: dict[str, set[str]] = defaultdict(set)
    edge_lines: dict[tuple[str, str], int] = {}
    path_to_module = {path: module for module, path in module_to_path.items()}
    for observed_import in imports:
        source_module = path_to_module.get(observed_import.path)
        if not source_module:
            continue
        target_module = _local_import_target_module(observed_import, module_to_path, local_modules)
        if not target_module or target_module == source_module:
            continue
        graph[source_module].add(target_module)
        edge_lines[(source_module, target_module)] = observed_import.line

    cycles = _module_cycles(graph)
    issues: list[ObservedContractIssue] = []
    for cycle in cycles:
        cycle_paths = [module_to_path[module] for module in cycle if module in module_to_path]
        if len(cycle_paths) < 2:
            continue
        for module in cycle:
            path = module_to_path.get(module)
            if not path:
                continue
            related_paths = [candidate for candidate in cycle_paths if candidate != path]
            outgoing = sorted(graph.get(module, set()) & set(cycle))
            first_line = min(
                [edge_lines[(module, target)] for target in outgoing if (module, target) in edge_lines] or [0]
            )
            issues.append(
                _issue(
                    index=start_index + len(issues),
                    issue_type="local_import_cycle",
                    severity="warning",
                    path=path,
                    details={
                        "message": "local Python import cycle detected between generated modules",
                        "module": module,
                        "cycle_modules": cycle,
                        "cycle_paths": cycle_paths,
                        "related_targets": related_paths,
                        "line": first_line,
                        "target_resolution": {
                            "primary_target": path,
                            "related_targets": related_paths,
                            "candidate_targets": cycle_paths,
                            "confidence": 0.94,
                            "rationale": "static import graph detected a cycle between generated local modules",
                        },
                    },
                )
            )
    return issues


def _local_import_target_module(
    observed_import: ObservedImport,
    module_to_path: dict[str, str],
    local_modules: set[str],
) -> str | None:
    module = _normalize_module_name(observed_import.module)
    if observed_import.name:
        named_module = _normalize_module_name(f"{module}.{observed_import.name}")
        if named_module in module_to_path:
            return named_module
    if module in module_to_path:
        return module
    candidates = [
        candidate
        for candidate in local_modules
        if candidate in module_to_path and (candidate == module or candidate.startswith(f"{module}."))
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _module_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for start in sorted(graph):
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            module, path = stack.pop()
            for target in sorted(graph.get(module, set())):
                if target == start and len(path) > 1:
                    canonical = _canonical_cycle(path)
                    key = tuple(canonical)
                    if key not in seen:
                        seen.add(key)
                        cycles.append(canonical)
                    continue
                if target in path:
                    continue
                stack.append((target, [*path, target]))
    return cycles


def _canonical_cycle(cycle: list[str]) -> list[str]:
    if not cycle:
        return cycle
    rotations = [cycle[index:] + cycle[:index] for index in range(len(cycle))]
    return min(rotations)


def _entrypoints_for_file(
    observed_file: ObservedFile,
    *,
    generated_file: GeneratedMaterialFile | None,
) -> list[ObservedEntrypoint]:
    if generated_file is None:
        return []
    text = " ".join(
        [
            observed_file.path,
            observed_file.kind,
            getattr(generated_file, "kind", ""),
        ]
    ).casefold()
    entrypoints: list[ObservedEntrypoint] = []
    for kind, terms in _ENTRYPOINT_HINTS.items():
        if any(term in text for term in terms):
            entrypoints.append(
                ObservedEntrypoint(
                    entrypoint_id=_observed_id("entrypoint", kind, observed_file.path),
                    kind=kind,  # type: ignore[arg-type]
                    path=observed_file.path,
                    name=_normalized_path(observed_file.path).stem or observed_file.path,
                    evidence="path or file kind indicates runtime surface",
                )
            )
    if observed_file.parse_status == "parsed" and generated_file.content:
        try:
            tree = ast.parse(generated_file.content, filename=generated_file.path)
        except SyntaxError:
            return entrypoints
        if _has_main_guard(tree):
            entrypoints.append(
                ObservedEntrypoint(
                    entrypoint_id=_observed_id("entrypoint", "cli", observed_file.path, "__main__"),
                    kind="cli",
                    path=observed_file.path,
                    name="__main__",
                    evidence="Python __main__ guard",
                )
            )
    return entrypoints


def _annotate_import(
    item: ObservedImport,
    *,
    local_modules: set[str],
    declared_dependencies: set[str],
) -> ObservedImport:
    root = _import_root(item.module)
    local_roots = {_import_root(module) for module in local_modules}
    standard_library = root in getattr(sys, "stdlib_module_names", set()) or root == "__future__"
    local = (
        item.module in local_modules
        or root in local_roots
        or _looks_like_local_module_alias(root, local_roots)
        or item.relative
    )
    declared_dependency = _normalize_dependency_name(root) in declared_dependencies
    return item.model_copy(
        update={
            "local": local,
            "standard_library": standard_library,
            "declared_dependency": declared_dependency,
        }
    )


def _looks_like_local_module_alias(root: str, local_roots: set[str]) -> bool:
    normalized = _safe_module_name(root)
    if len(normalized) < 3:
        return False
    return any(local_root.startswith(f"{normalized}_") for local_root in local_roots)


def _local_python_modules(files: list[GeneratedMaterialFile], project_root: str) -> set[str]:
    modules = set()
    for file in files:
        if _is_python_file(file.path, file.kind):
            module = _module_name_for_path(file.path, project_root)
            if module:
                modules.add(module)
                modules.update(_src_layout_aliases(module))
                parts = module.split(".")
                for index in range(1, len(parts)):
                    modules.add(".".join(parts[:index]))
    return modules


def _src_layout_aliases(module: str) -> set[str]:
    if not module.startswith("src."):
        return set()
    alias = module.removeprefix("src.")
    if not alias:
        return set()
    aliases = {alias}
    parts = alias.split(".")
    for index in range(1, len(parts)):
        aliases.add(".".join(parts[:index]))
    return aliases


def _project_module_aliases(files: list[GeneratedMaterialFile]) -> set[str]:
    aliases: set[str] = set()
    for file in files:
        if _normalized_path(file.path).name != "pyproject.toml":
            continue
        try:
            parsed = tomllib.loads(file.content)
        except tomllib.TOMLDecodeError:
            continue
        for name in _pyproject_project_names(parsed):
            alias = _safe_module_name(name)
            if alias:
                aliases.add(alias)
    return aliases


def _pyproject_project_names(parsed: dict[str, object]) -> list[str]:
    names: list[str] = []
    project = parsed.get("project")
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        names.append(project["name"])
    tool = parsed.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict) and isinstance(poetry.get("name"), str):
            names.append(poetry["name"])
    return names


def _module_name_for_path(path: str, project_root: str) -> str:
    rel_path = _relative_project_path(path, project_root)
    if rel_path.suffix != ".py":
        return rel_path.stem
    parts = list(rel_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return _safe_module_name(project_root)
    return ".".join(_safe_module_name(part) for part in parts if part and part != ".")


def _safe_module_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in str(value or "project")).strip("_")
    if not cleaned:
        return "project"
    if cleaned[0].isdigit():
        return f"project_{cleaned}"
    return cleaned


def _relative_project_path(path: str, project_root: str) -> PurePosixPath:
    normalized = _normalized_path(path)
    parts = list(normalized.parts)
    if parts and parts[0] == project_root:
        parts = parts[1:]
    return PurePosixPath(*parts) if parts else PurePosixPath(normalized.name)


def _normalized_path(path: str) -> PurePosixPath:
    return PurePosixPath(path.replace("\\", "/"))


def _is_python_file(path: str, kind: str) -> bool:
    return _normalized_path(path).suffix == ".py" or kind in {"python", "test"}


def _is_test_path(path: str, kind: str) -> bool:
    normalized = str(_normalized_path(path)).casefold()
    name = _normalized_path(path).name.casefold()
    return kind == "test" or "/tests/" in f"/{normalized}" or name.startswith("test_") or name.endswith("_test.py")


def _is_requirements_file(path: PurePosixPath) -> bool:
    return path.name == "requirements.txt" or (
        path.suffix == ".txt" and any(part == "requirements" for part in path.parts)
    )


def _ecosystem_for_path(path: str) -> ObservedEcosystem:
    normalized = _normalized_path(path)
    if normalized.suffix == ".py" or normalized.name in {"pyproject.toml", "requirements.txt", "setup.cfg"}:
        return "python"
    if normalized.name == "package.json":
        return "node"
    return "generic"


def _resolve_import_from_module(
    current_module: str,
    imported_module: str | None,
    level: int,
    *,
    package_init: bool = False,
) -> str:
    if level <= 0:
        return imported_module or "."
    current_parts = current_module.split(".") if current_module else []
    package_offset = 1 if package_init else 0
    base_parts = current_parts[: max(len(current_parts) - level + package_offset, 0)]
    if imported_module:
        base_parts.extend(imported_module.split("."))
    return ".".join(part for part in base_parts if part) or "."


def _import_root(module: str) -> str:
    stripped = module.lstrip(".")
    return stripped.split(".", 1)[0] if stripped else module


def _normalize_module_name(module: str) -> str:
    return ".".join(part for part in str(module or "").strip(".").split(".") if part)


def _dependency_name_from_spec(raw: str) -> str:
    clean = raw.strip()
    if " @ " in clean:
        clean = clean.split(" @ ", 1)[0].strip()
    match = _PACKAGE_NAME_RE.match(clean)
    if match:
        return _EXTRAS_OR_VERSION_RE.split(match.group(1), 1)[0]
    return clean


def _normalize_dependency_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _has_main_guard(tree: ast.Module) -> bool:
    return any(isinstance(node, ast.If) and _is_main_guard_test(node.test) for node in tree.body)


def _is_main_guard_test(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
        return False
    left = node.left
    right = node.comparators[0]
    if not isinstance(left, ast.Name) or left.id != "__name__":
        return False
    if not isinstance(right, ast.Constant) or right.value != "__main__":
        return False
    return isinstance(node.ops[0], ast.Eq)


def _call_target(
    func: ast.AST,
    imported_aliases: dict[str, tuple[str, str | None]],
) -> tuple[str, str | None] | None:
    if isinstance(func, ast.Name):
        return imported_aliases.get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        target = imported_aliases.get(func.value.id)
        if target is None:
            return None
        target_module, _ = target
        return target_module, func.attr
    return None


def _dedupe_dependencies(dependencies: list[ObservedDependency]) -> list[ObservedDependency]:
    seen = set()
    deduped: list[ObservedDependency] = []
    for dependency in dependencies:
        key = (dependency.normalized_name, dependency.source_path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dependency)
    return deduped


def _dedupe_entrypoints(entrypoints: list[ObservedEntrypoint]) -> list[ObservedEntrypoint]:
    seen = set()
    deduped: list[ObservedEntrypoint] = []
    for entrypoint in entrypoints:
        key = (entrypoint.kind, entrypoint.path, entrypoint.name)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entrypoint)
    return sorted(deduped, key=lambda item: (item.kind, item.path, item.name))


def _dedupe_expectations(expectations: list[ObservedTestExpectation]) -> list[ObservedTestExpectation]:
    seen = set()
    deduped: list[ObservedTestExpectation] = []
    for expectation in expectations:
        key = (
            expectation.path,
            expectation.target_module,
            expectation.target_name,
            expectation.expectation_kind,
            expectation.line,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(expectation)
    return deduped


def _dedupe_issues(issues: list[ObservedContractIssue]) -> list[ObservedContractIssue]:
    seen = set()
    deduped: list[ObservedContractIssue] = []
    for index, issue in enumerate(issues):
        key = (issue.issue_type, issue.path, repr(sorted(issue.details.items())))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue.model_copy(update={"issue_id": _observed_id("issue", issue.issue_type, str(index))}))
    return deduped


def _file_by_path(files: list[GeneratedMaterialFile], path: str) -> GeneratedMaterialFile | None:
    for file in files:
        if file.path == path:
            return file
    return None


def _issue(
    *,
    index: int,
    issue_type: str,
    severity: str = "warning",
    path: str | None = None,
    details: dict[str, object] | None = None,
) -> ObservedContractIssue:
    return ObservedContractIssue(
        issue_id=_observed_id("issue", issue_type, str(index)),
        issue_type=issue_type,
        severity=severity,  # type: ignore[arg-type]
        path=path,
        details=details or {},
    )


def _observed_id(*parts: str) -> str:
    raw = ":".join(parts)
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw).strip("_")
    if len(cleaned) <= 128:
        return cleaned or "observed:item"
    return cleaned[:128].rstrip("_.:-") or "observed:item"
