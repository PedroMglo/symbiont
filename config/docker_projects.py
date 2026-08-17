"""Typed Docker project policy and runtime selection owned by Config Center."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping, TypeAlias, cast

from .docker_catalogs import DOCKER_CATALOGS_PATH, DockerCatalogError, load_docker_catalog

DOCKER_PROJECTS_CATALOG_PATH = DOCKER_CATALOGS_PATH
COMPOSE_PROFILES_ENV = "AI_COMPOSE_PROFILES"
LOCAL_OWNER_IMAGES_ACTIVE_ENV = "AI_LOCAL_LOCAL_OWNER_IMAGES_ACTIVE"
LOCAL_OWNER_COMPOSE_OVERLAY_RELATIVE_PATH = Path(".local/infra/local-owner-compose.json")
PRIMARY_DOCKER_CONTEXT_ENV = "AI_LOCAL_DOCKER_CONTEXT"
NATIVE_DOCKER_CONTEXT_ENV = "DOCKER_CONTEXT"
DEFAULT_DOCKER_CONTEXT = "default"
NATIVE_COMPOSE_TOPOLOGY_ENV_VARS = (
    "COMPOSE_BAKE", "COMPOSE_CONVERT_WINDOWS_PATHS", "COMPOSE_DISABLE_ENV_FILE",
    "COMPOSE_ENV_FILES", "COMPOSE_EXPERIMENTAL", "COMPOSE_FILE", "COMPOSE_IGNORE_ORPHANS",
    "COMPOSE_PATH_SEPARATOR", "COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME", "COMPOSE_REMOVE_ORPHANS",
)


def local_owner_compose_overlay(root: Path) -> Path | None:
    if os.environ.get(LOCAL_OWNER_IMAGES_ACTIVE_ENV) != "1":
        return None
    overlay = (root / LOCAL_OWNER_COMPOSE_OVERLAY_RELATIVE_PATH).resolve()
    return overlay if overlay.is_file() else None


_PROJECT_NAME = "ai-local"
_PROFILE_TOKEN = re.compile(r"[a-z0-9][a-z0-9-]*")
_OPERATOR_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]*")
_DECISION_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_PROJECT_FIELDS = {
    "order", "role", "workdir", "files", "network_name", "use_catalog_profiles",
    "required_runtime_profiles", "default_runtime_profiles", "mandatory_build_profiles",
    "lifecycle_only_profiles", "operator_only_runtime_profiles", "operator_profile_closures",
    "runtime_recommendations", "runtime_profile_conflicts", "runtime_variant_groups",
    "runtime_requirements", "env_files", "secrets_dir",
}
RuntimeScalar: TypeAlias = bool | int | str


class DockerProjectConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeRecommendationRule:
    decision: str
    equals: RuntimeScalar
    include_profiles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeRecommendations:
    base_profiles: tuple[str, ...]
    rules: tuple[RuntimeRecommendationRule, ...]


@dataclass(frozen=True, slots=True)
class RuntimeVariantGroup:
    selection: str
    projection: str
    enabled_field: str
    profile_field: str
    variant_field: str
    variants: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RuntimeRequirement:
    projection: str
    equals: Mapping[str, RuntimeScalar]


@dataclass(frozen=True, slots=True)
class DockerProjectConfig:
    name: str
    order: int
    role: str
    workdir: str
    files: tuple[str, ...]
    network_name: str
    use_catalog_profiles: bool
    required_runtime_profiles: tuple[str, ...]
    default_runtime_profiles: tuple[str, ...]
    mandatory_build_profiles: tuple[str, ...]
    lifecycle_only_profiles: tuple[str, ...]
    operator_only_runtime_profiles: tuple[str, ...]
    operator_profile_closures: Mapping[str, tuple[str, ...]]
    runtime_recommendations: RuntimeRecommendations
    runtime_profile_conflicts: Mapping[str, tuple[str, ...]]
    runtime_variant_groups: Mapping[str, RuntimeVariantGroup]
    runtime_requirements: Mapping[str, RuntimeRequirement]
    env_files: tuple[str, ...]
    secrets_dir: str

    @property
    def all_compose_profiles(self) -> tuple[str, ...]:
        variant_profiles = tuple(profile for group in self.runtime_variant_groups.values() for profile in group.variants)
        return (*self.mandatory_build_profiles, *variant_profiles, *self.operator_only_runtime_profiles)

    @property
    def all_runtime_profiles(self) -> tuple[str, ...]:
        lifecycle = set(self.lifecycle_only_profiles)
        return tuple(profile for profile in self.all_compose_profiles if profile not in lifecycle)


def _profile_tokens(project: Mapping[str, object], field: str, *, path: Path) -> tuple[str, ...]:
    raw = project.get(field)
    if not isinstance(raw, list) or not raw:
        raise DockerProjectConfigError(f"{path}: {field} must be a non-empty array")
    if any(not isinstance(profile, str) or _PROFILE_TOKEN.fullmatch(profile) is None for profile in raw):
        raise DockerProjectConfigError(f"{path}: {field} must contain lowercase Compose profile tokens")
    profiles = tuple(cast(list[str], raw))
    if len(set(profiles)) != len(profiles):
        raise DockerProjectConfigError(f"{path}: {field} profile tokens must be unique")
    return profiles


def _relative_project_path(project: Mapping[str, object], field: str, *, path: Path, allow_dot: bool = False) -> str:
    raw = project.get(field)
    if not isinstance(raw, str) or not raw.strip() or "\\" in raw:
        raise DockerProjectConfigError(f"{path}: {field} must be a non-empty POSIX relative path")
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise DockerProjectConfigError(f"{path}: {field} must stay inside the project root")
    normalized = candidate.as_posix()
    if normalized == "." and not allow_dot:
        raise DockerProjectConfigError(f"{path}: {field} cannot be the project root")
    return normalized


def _relative_project_paths(project: Mapping[str, object], field: str, *, path: Path) -> tuple[str, ...]:
    raw = project.get(field)
    if not isinstance(raw, list) or not raw:
        raise DockerProjectConfigError(f"{path}: {field} must be a non-empty array")
    paths = [_relative_project_path({field: value}, field, path=Path(f"{path}:{field}[{index}]")) for index, value in enumerate(raw)]
    if len(set(paths)) != len(paths):
        raise DockerProjectConfigError(f"{path}: {field} paths must be unique")
    return tuple(paths)


def _operator_profile_closures(project: Mapping[str, object], *, path: Path) -> dict[str, tuple[str, ...]]:
    raw = project.get("operator_profile_closures")
    if not isinstance(raw, dict) or not raw:
        raise DockerProjectConfigError(f"{path}: operator_profile_closures must be a non-empty table")
    closures: dict[str, tuple[str, ...]] = {}
    for operator, raw_profiles in raw.items():
        if not isinstance(operator, str) or _OPERATOR_TOKEN.fullmatch(operator) is None:
            raise DockerProjectConfigError(f"{path}: operator_profile_closures keys must be lowercase operator tokens")
        closures[operator] = _profile_tokens({"profiles": raw_profiles}, "profiles", path=Path(f"{path}:operator_profile_closures.{operator}"))
    return closures


def _runtime_scalar(value: object, *, path: Path) -> RuntimeScalar:
    if type(value) not in {bool, int, str}:
        raise DockerProjectConfigError(f"{path}: policy comparison values must be booleans, integers, or strings")
    if isinstance(value, str) and not value:
        raise DockerProjectConfigError(f"{path}: policy comparison strings must be non-empty")
    return cast(RuntimeScalar, value)


def _runtime_recommendations(project: Mapping[str, object], *, path: Path) -> RuntimeRecommendations:
    raw = project.get("runtime_recommendations")
    if not isinstance(raw, dict) or set(raw) != {"base_profiles", "rules"}:
        raise DockerProjectConfigError(f"{path}: runtime_recommendations fields must be exactly ['base_profiles', 'rules']")
    base_profiles = _profile_tokens(raw, "base_profiles", path=Path(f"{path}:runtime_recommendations"))
    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise DockerProjectConfigError(f"{path}: runtime_recommendations.rules must be a non-empty array of tables")
    rules: list[RuntimeRecommendationRule] = []
    for index, raw_rule in enumerate(raw_rules):
        rule_path = Path(f"{path}:runtime_recommendations.rules[{index}]")
        if not isinstance(raw_rule, dict) or set(raw_rule) != {"decision", "equals", "include_profiles"}:
            raise DockerProjectConfigError(f"{rule_path}: fields must be exactly ['decision', 'equals', 'include_profiles']")
        decision = raw_rule.get("decision")
        if not isinstance(decision, str) or _DECISION_TOKEN.fullmatch(decision) is None:
            raise DockerProjectConfigError(f"{rule_path}: decision must be a lowercase dotted policy field")
        rules.append(RuntimeRecommendationRule(decision=decision, equals=_runtime_scalar(raw_rule.get("equals"), path=Path(f"{rule_path}:equals")), include_profiles=_profile_tokens(raw_rule, "include_profiles", path=rule_path)))
    return RuntimeRecommendations(base_profiles=base_profiles, rules=tuple(rules))


def _runtime_profile_conflicts(project: Mapping[str, object], *, path: Path) -> dict[str, tuple[str, ...]]:
    raw = project.get("runtime_profile_conflicts")
    if not isinstance(raw, dict) or not raw:
        raise DockerProjectConfigError(f"{path}: runtime_profile_conflicts must be a non-empty table")
    conflicts: dict[str, tuple[str, ...]] = {}
    for profile, raw_conflicts in raw.items():
        if not isinstance(profile, str) or _PROFILE_TOKEN.fullmatch(profile) is None:
            raise DockerProjectConfigError(f"{path}: runtime_profile_conflicts keys must be lowercase profiles")
        conflicts[profile] = _profile_tokens({"profiles": raw_conflicts}, "profiles", path=Path(f"{path}:runtime_profile_conflicts.{profile}"))
    return conflicts


def _runtime_variant_groups(project: Mapping[str, object], *, path: Path) -> dict[str, RuntimeVariantGroup]:
    raw = project.get("runtime_variant_groups")
    if not isinstance(raw, dict) or not raw:
        raise DockerProjectConfigError(f"{path}: runtime_variant_groups must be a non-empty table")
    groups: dict[str, RuntimeVariantGroup] = {}
    expected_fields = {"selection", "projection", "enabled_field", "profile_field", "variant_field", "variants"}
    for name, raw_group in raw.items():
        group_path = Path(f"{path}:runtime_variant_groups.{name}")
        if not isinstance(name, str) or _OPERATOR_TOKEN.fullmatch(name) is None:
            raise DockerProjectConfigError(f"{path}: runtime_variant_groups keys must be lowercase policy tokens")
        if not isinstance(raw_group, dict) or set(raw_group) != expected_fields:
            raise DockerProjectConfigError(f"{group_path}: fields must be exactly {sorted(expected_fields)!r}")
        selection = raw_group.get("selection")
        if selection != "at-most-one":
            raise DockerProjectConfigError(f"{group_path}: selection must be 'at-most-one'")
        string_fields: dict[str, str] = {}
        for field in ("projection", "enabled_field", "profile_field", "variant_field"):
            value = raw_group.get(field)
            if not isinstance(value, str) or _OPERATOR_TOKEN.fullmatch(value) is None:
                raise DockerProjectConfigError(f"{group_path}: {field} must be a lowercase projection token")
            string_fields[field] = value
        raw_variants = raw_group.get("variants")
        if not isinstance(raw_variants, dict) or not raw_variants:
            raise DockerProjectConfigError(f"{group_path}: variants must be a non-empty profile-to-variant table")
        variants: dict[str, str] = {}
        for profile, variant in raw_variants.items():
            if not isinstance(profile, str) or _PROFILE_TOKEN.fullmatch(profile) is None or not isinstance(variant, str) or _OPERATOR_TOKEN.fullmatch(variant) is None:
                raise DockerProjectConfigError(f"{group_path}: variants must map lowercase profiles to lowercase variant tokens")
            variants[profile] = variant
        if len(set(variants.values())) != len(variants):
            raise DockerProjectConfigError(f"{group_path}: variant values must be unique")
        groups[name] = RuntimeVariantGroup(selection=selection, projection=string_fields["projection"], enabled_field=string_fields["enabled_field"], profile_field=string_fields["profile_field"], variant_field=string_fields["variant_field"], variants=MappingProxyType(variants))
    return groups


def _runtime_requirements(project: Mapping[str, object], *, path: Path) -> dict[str, RuntimeRequirement]:
    raw = project.get("runtime_requirements")
    if not isinstance(raw, dict) or not raw:
        raise DockerProjectConfigError(f"{path}: runtime_requirements must be a non-empty table")
    requirements: dict[str, RuntimeRequirement] = {}
    for profile, raw_requirement in raw.items():
        requirement_path = Path(f"{path}:runtime_requirements.{profile}")
        if not isinstance(profile, str) or _PROFILE_TOKEN.fullmatch(profile) is None:
            raise DockerProjectConfigError(f"{path}: runtime_requirements keys must be lowercase profiles")
        if not isinstance(raw_requirement, dict) or set(raw_requirement) != {"projection", "equals"}:
            raise DockerProjectConfigError(f"{requirement_path}: fields must be exactly ['equals', 'projection']")
        projection = raw_requirement.get("projection")
        if not isinstance(projection, str) or _OPERATOR_TOKEN.fullmatch(projection) is None:
            raise DockerProjectConfigError(f"{requirement_path}: projection must be a lowercase projection token")
        raw_equals = raw_requirement.get("equals")
        if not isinstance(raw_equals, dict) or not raw_equals:
            raise DockerProjectConfigError(f"{requirement_path}: equals must be a non-empty field table")
        equals: dict[str, RuntimeScalar] = {}
        for field, value in raw_equals.items():
            if not isinstance(field, str) or _OPERATOR_TOKEN.fullmatch(field) is None:
                raise DockerProjectConfigError(f"{requirement_path}: equals keys must be lowercase field tokens")
            equals[field] = _runtime_scalar(value, path=Path(f"{requirement_path}:equals.{field}"))
        requirements[profile] = RuntimeRequirement(projection=projection, equals=MappingProxyType(equals))
    return requirements


def load_docker_project_config(path: Path | None = None) -> DockerProjectConfig:
    catalog_path = path or DOCKER_PROJECTS_CATALOG_PATH
    try:
        payload = load_docker_catalog("compose_projects", path=catalog_path)
    except DockerCatalogError as exc:
        raise DockerProjectConfigError(f"{catalog_path}: unable to load Docker project catalog: {exc}") from exc
    if set(payload) != {"projects"}:
        raise DockerProjectConfigError(f"{catalog_path}: catalog fields must be exactly ['projects']")
    projects = payload.get("projects")
    if not isinstance(projects, dict) or set(projects) != {_PROJECT_NAME}:
        raise DockerProjectConfigError(f"{catalog_path}: projects must define exactly {_PROJECT_NAME!r}")
    project = projects[_PROJECT_NAME]
    if not isinstance(project, dict) or set(project) != _PROJECT_FIELDS:
        raise DockerProjectConfigError(f"{catalog_path}: {_PROJECT_NAME} fields must be exactly {sorted(_PROJECT_FIELDS)!r}")
    order = project.get("order")
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise DockerProjectConfigError(f"{catalog_path}: {_PROJECT_NAME}.order must be a non-negative integer")
    role = project.get("role")
    if not isinstance(role, str) or _PROFILE_TOKEN.fullmatch(role) is None:
        raise DockerProjectConfigError(f"{catalog_path}: {_PROJECT_NAME}.role must be a lowercase policy token")
    use_catalog_profiles = project.get("use_catalog_profiles")
    if not isinstance(use_catalog_profiles, bool):
        raise DockerProjectConfigError(f"{catalog_path}: {_PROJECT_NAME}.use_catalog_profiles must be a boolean")
    workdir = _relative_project_path(project, "workdir", path=catalog_path, allow_dot=True)
    files = _relative_project_paths(project, "files", path=catalog_path)
    network_name = project.get("network_name")
    if not isinstance(network_name, str) or re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", network_name) is None:
        raise DockerProjectConfigError(f"{catalog_path}: {_PROJECT_NAME}.network_name must be a lowercase Docker resource name")
    env_files = _relative_project_paths(project, "env_files", path=catalog_path)
    secrets_dir = _relative_project_path(project, "secrets_dir", path=catalog_path)
    defaults = _profile_tokens(project, "default_runtime_profiles", path=catalog_path)
    required = _profile_tokens(project, "required_runtime_profiles", path=catalog_path)
    mandatory = _profile_tokens(project, "mandatory_build_profiles", path=catalog_path)
    lifecycle_only = _profile_tokens(project, "lifecycle_only_profiles", path=catalog_path)
    operator_only = _profile_tokens(project, "operator_only_runtime_profiles", path=catalog_path)
    operator_closures = _operator_profile_closures(project, path=catalog_path)
    recommendations = _runtime_recommendations(project, path=catalog_path)
    declared_conflicts = _runtime_profile_conflicts(project, path=catalog_path)
    variant_groups = _runtime_variant_groups(project, path=catalog_path)
    requirements = _runtime_requirements(project, path=catalog_path)
    variant_runtime_profiles = tuple(profile for group in variant_groups.values() for profile in group.variants)
    profile_classes = (mandatory, variant_runtime_profiles, operator_only)
    all_compose_profiles = tuple(profile for profile_class in profile_classes for profile in profile_class)
    if len(set(all_compose_profiles)) != len(all_compose_profiles):
        raise DockerProjectConfigError(f"{catalog_path}: Compose profile classes must be disjoint")
    unknown_lifecycle = sorted(set(lifecycle_only).difference(mandatory))
    if unknown_lifecycle:
        raise DockerProjectConfigError(f"{catalog_path}: lifecycle-only profiles must be a subset of mandatory build profiles: {unknown_lifecycle!r}")
    selectable_runtime_profiles = tuple(profile for profile in all_compose_profiles if profile not in set(lifecycle_only))
    unknown_required = sorted(set(required).difference(selectable_runtime_profiles))
    if unknown_required:
        raise DockerProjectConfigError(f"{catalog_path}: required runtime profiles contain unknown profiles: {unknown_required!r}")
    if not set(defaults).issubset(mandatory):
        raise DockerProjectConfigError(f"{catalog_path}: default runtime profiles must be a subset of mandatory build profiles")
    missing_required_defaults = sorted(set(required).difference(defaults))
    if missing_required_defaults:
        raise DockerProjectConfigError(f"{catalog_path}: default runtime profiles must include required runtime profiles: {missing_required_defaults!r}")
    forbidden_runtime_defaults = sorted(set((*required, *defaults)).intersection(lifecycle_only))
    if forbidden_runtime_defaults:
        raise DockerProjectConfigError(f"{catalog_path}: lifecycle-only profiles cannot be runtime defaults or requirements: {forbidden_runtime_defaults!r}")
    for operator, profiles in operator_closures.items():
        unknown = sorted(set(profiles).difference(selectable_runtime_profiles))
        if unknown:
            raise DockerProjectConfigError(f"{catalog_path}: operator profile closure {operator!r} contains unknown profiles: {unknown!r}")
    selectable_set = set(selectable_runtime_profiles)
    recommendation_profiles = {*recommendations.base_profiles, *(profile for rule in recommendations.rules for profile in rule.include_profiles)}
    unknown_recommendations = sorted(recommendation_profiles.difference(selectable_set))
    if unknown_recommendations:
        raise DockerProjectConfigError(f"{catalog_path}: runtime recommendations contain unknown profiles: {unknown_recommendations!r}")
    missing_recommended_requirements = sorted(set(required).difference(recommendations.base_profiles))
    if missing_recommended_requirements:
        raise DockerProjectConfigError(f"{catalog_path}: runtime recommendation base must include required profiles: {missing_recommended_requirements!r}")
    decisions = [rule.decision for rule in recommendations.rules]
    if len(set(decisions)) != len(decisions):
        raise DockerProjectConfigError(f"{catalog_path}: runtime recommendation decisions must be unique")
    conflict_sets: dict[str, set[str]] = {}
    for profile, declared in declared_conflicts.items():
        unknown_conflicts = sorted({profile, *declared}.difference(selectable_set))
        if unknown_conflicts:
            raise DockerProjectConfigError(f"{catalog_path}: runtime profile conflicts contain unknown profiles: {unknown_conflicts!r}")
        if profile in declared:
            raise DockerProjectConfigError(f"{catalog_path}: runtime profile {profile!r} cannot conflict with itself")
        for conflict in declared:
            conflict_sets.setdefault(profile, set()).add(conflict)
            conflict_sets.setdefault(conflict, set()).add(profile)
    conflicts = {profile: tuple(candidate for candidate in selectable_runtime_profiles if candidate in conflict_sets[profile]) for profile in selectable_runtime_profiles if profile in conflict_sets}
    variant_profiles: set[str] = set()
    variant_projections: set[str] = set()
    for name, group in variant_groups.items():
        profiles = set(group.variants)
        unknown = sorted(profiles.difference(selectable_set))
        if unknown:
            raise DockerProjectConfigError(f"{catalog_path}: runtime variant group {name!r} contains unknown profiles: {unknown!r}")
        overlap = sorted(variant_profiles.intersection(profiles))
        if overlap:
            raise DockerProjectConfigError(f"{catalog_path}: runtime variant groups overlap: {overlap!r}")
        if group.projection in variant_projections:
            raise DockerProjectConfigError(f"{catalog_path}: runtime variant group projections must be unique: {group.projection!r}")
        variant_profiles.update(profiles)
        variant_projections.add(group.projection)
    unknown_requirement_profiles = sorted(set(requirements).difference(selectable_set))
    if unknown_requirement_profiles:
        raise DockerProjectConfigError(f"{catalog_path}: runtime requirements contain unknown profiles: {unknown_requirement_profiles!r}")
    policy_selections = {"required runtime profiles": required, "default runtime profiles": defaults, "runtime recommendations": tuple(profile for profile in selectable_runtime_profiles if profile in recommendation_profiles)}
    for label, selection in policy_selections.items():
        selected = set(selection)
        pairs = sorted({tuple(sorted((profile, conflict))) for profile in selected for conflict in conflicts.get(profile, ()) if conflict in selected})
        if pairs:
            raise DockerProjectConfigError(f"{catalog_path}: {label} contain conflicting profiles: {pairs!r}")
        for name, group in variant_groups.items():
            selected_variants = tuple(profile for profile in selection if profile in group.variants)
            if group.selection == "at-most-one" and len(selected_variants) > 1:
                raise DockerProjectConfigError(f"{catalog_path}: {label} select multiple variants from runtime group {name!r}: {selected_variants!r}")
    return DockerProjectConfig(name=_PROJECT_NAME, order=order, role=role, workdir=workdir, files=files, network_name=network_name, use_catalog_profiles=use_catalog_profiles, required_runtime_profiles=required, default_runtime_profiles=defaults, mandatory_build_profiles=mandatory, lifecycle_only_profiles=lifecycle_only, operator_only_runtime_profiles=operator_only, operator_profile_closures=MappingProxyType(operator_closures), runtime_recommendations=recommendations, runtime_profile_conflicts=MappingProxyType(conflicts), runtime_variant_groups=MappingProxyType(variant_groups), runtime_requirements=MappingProxyType(requirements), env_files=env_files, secrets_dir=secrets_dir)


def _project_config(project: DockerProjectConfig | None, catalog_path: Path | None) -> DockerProjectConfig:
    return project or load_docker_project_config(catalog_path)


def _runtime_selection_conflicts(profiles: tuple[str, ...], project: DockerProjectConfig) -> tuple[tuple[str, str], ...]:
    selected = set(profiles)
    return tuple(sorted({tuple(sorted((profile, conflict))) for profile in profiles for conflict in project.runtime_profile_conflicts.get(profile, ()) if conflict in selected}))


def normalize_runtime_profiles(profiles: Iterable[str], *, project: DockerProjectConfig | None = None, catalog_path: Path | None = None) -> tuple[str, ...]:
    effective_project = _project_config(project, catalog_path)
    if isinstance(profiles, (str, bytes)):
        raise DockerProjectConfigError("runtime profiles must be an iterable of individual profile tokens")
    try:
        requested = tuple(profiles)
    except TypeError as exc:
        raise DockerProjectConfigError("runtime profiles must be an iterable of individual profile tokens") from exc
    if not requested:
        raise DockerProjectConfigError("runtime profile selection must contain at least one profile")
    invalid = sorted(repr(profile) for profile in requested if not isinstance(profile, str) or _PROFILE_TOKEN.fullmatch(profile) is None)
    if invalid:
        raise DockerProjectConfigError(f"runtime profile selection contains invalid tokens: {invalid!r}")
    lifecycle_selected = sorted(set(requested).intersection(effective_project.lifecycle_only_profiles))
    if lifecycle_selected:
        raise DockerProjectConfigError(f"runtime profile selection cannot select lifecycle-only profiles: {lifecycle_selected!r}")
    unknown = sorted(set(requested).difference(effective_project.all_runtime_profiles))
    if unknown:
        raise DockerProjectConfigError(f"runtime profile selection contains unknown profiles: {unknown!r}")
    selected = set(requested).union(effective_project.required_runtime_profiles)
    normalized = tuple(profile for profile in effective_project.all_runtime_profiles if profile in selected)
    conflicts = _runtime_selection_conflicts(normalized, effective_project)
    if conflicts:
        raise DockerProjectConfigError(f"runtime profile selection contains conflicts: {conflicts!r}")
    for name, group in effective_project.runtime_variant_groups.items():
        selected_variants = tuple(profile for profile in normalized if profile in group.variants)
        if group.selection == "at-most-one" and len(selected_variants) > 1:
            raise DockerProjectConfigError(f"runtime variant group {name!r} permits at most one profile: {selected_variants!r}")
    return normalized


def recommend_runtime_profiles(resolver_payload: Mapping[str, object], *, project: DockerProjectConfig | None = None, catalog_path: Path | None = None) -> tuple[str, ...]:
    effective_project = _project_config(project, catalog_path)
    if resolver_payload.get("ok") is not True:
        raw_errors = resolver_payload.get("errors")
        errors = raw_errors if isinstance(raw_errors, list) else []
        detail = "; ".join(str(error) for error in errors if str(error).strip())
        raise DockerProjectConfigError("cannot recommend runtime profiles from an invalid resolver snapshot" + (f": {detail}" if detail else ""))
    raw_decisions = resolver_payload.get("decisions")
    if not isinstance(raw_decisions, list):
        raise DockerProjectConfigError("resolver snapshot decisions must be a typed array")
    selected = list(effective_project.runtime_recommendations.base_profiles)
    for rule in effective_project.runtime_recommendations.rules:
        matches = [decision for decision in raw_decisions if isinstance(decision, Mapping) and decision.get("field") == rule.decision]
        if len(matches) != 1:
            raise DockerProjectConfigError(f"resolver snapshot must contain exactly one typed decision for {rule.decision!r}; found {len(matches)}")
        actual = matches[0].get("value")
        if type(actual) is not type(rule.equals):
            raise DockerProjectConfigError(f"resolver decision {rule.decision!r} has invalid value type")
        if actual == rule.equals:
            selected.extend(rule.include_profiles)
    return normalize_runtime_profiles(selected, project=effective_project)


def validate_runtime_profile_selection(profiles: Iterable[str], resolved: Mapping[str, object], *, project: DockerProjectConfig | None = None, catalog_path: Path | None = None) -> tuple[str, ...]:
    effective_project = _project_config(project, catalog_path)
    normalized = normalize_runtime_profiles(profiles, project=effective_project)
    selected = set(normalized)
    errors: list[str] = []
    for name, group in effective_project.runtime_variant_groups.items():
        selected_variants = tuple(profile for profile in normalized if profile in group.variants)
        if not selected_variants:
            continue
        selected_profile = selected_variants[0]
        projection = resolved.get(group.projection)
        if not isinstance(projection, Mapping):
            errors.append(f"variant group {name!r} requires resolved projection {group.projection!r}")
            continue
        expected = {group.enabled_field: True, group.profile_field: selected_profile, group.variant_field: group.variants[selected_profile]}
        drift = tuple(field for field, value in expected.items() if type(projection.get(field)) is not type(value) or projection.get(field) != value)
        if drift:
            errors.append(f"variant group {name!r} projection {group.projection!r} does not match {selected_profile!r}: {drift!r}")
    for profile, requirement in effective_project.runtime_requirements.items():
        if profile not in selected:
            continue
        projection = resolved.get(requirement.projection)
        if not isinstance(projection, Mapping):
            errors.append(f"profile {profile!r} requires resolved projection {requirement.projection!r}")
            continue
        drift = tuple(field for field, value in requirement.equals.items() if type(projection.get(field)) is not type(value) or projection.get(field) != value)
        if drift:
            errors.append(f"profile {profile!r} projection {requirement.projection!r} does not satisfy required fields: {drift!r}")
    if errors:
        raise DockerProjectConfigError("; ".join(errors))
    return normalized


def resolve_compose_profiles(env: Mapping[str, str] | None = None, *, project: DockerProjectConfig | None = None, catalog_path: Path | None = None) -> tuple[str, ...]:
    effective_project = _project_config(project, catalog_path)
    effective_env = os.environ if env is None else env
    if COMPOSE_PROFILES_ENV not in effective_env:
        return normalize_runtime_profiles(effective_project.default_runtime_profiles, project=effective_project)
    raw = effective_env[COMPOSE_PROFILES_ENV]
    if not isinstance(raw, str) or not raw.strip():
        raise DockerProjectConfigError(f"{COMPOSE_PROFILES_ENV} must select at least one Compose profile")
    profiles = tuple(token for token in re.split(r"[,\s]+", raw.strip()) if token)
    if not profiles:
        raise DockerProjectConfigError(f"{COMPOSE_PROFILES_ENV} must select at least one Compose profile")
    invalid = sorted({profile for profile in profiles if _PROFILE_TOKEN.fullmatch(profile) is None})
    if invalid:
        raise DockerProjectConfigError(f"{COMPOSE_PROFILES_ENV} contains invalid profile tokens: {invalid!r}")
    try:
        return normalize_runtime_profiles(profiles, project=effective_project)
    except DockerProjectConfigError as exc:
        raise DockerProjectConfigError(f"{COMPOSE_PROFILES_ENV}: {exc}") from exc


def sanitize_compose_operator_env(env: Mapping[str, str]) -> dict[str, str]:
    overrides = tuple(key for key in NATIVE_COMPOSE_TOPOLOGY_ENV_VARS if key in env)
    if overrides:
        raise DockerProjectConfigError(f"native Docker Compose topology controls are forbidden for ai-local operators: {overrides!r}; use {COMPOSE_PROFILES_ENV} and Config Center")
    return dict(env)


def resolve_docker_context(env: Mapping[str, str] | None = None) -> str:
    effective_env = os.environ if env is None else env
    for key in (PRIMARY_DOCKER_CONTEXT_ENV, NATIVE_DOCKER_CONTEXT_ENV):
        raw = effective_env.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise DockerProjectConfigError(f"{key} must be a string")
        context = raw.strip()
        if context:
            return context
    return DEFAULT_DOCKER_CONTEXT
