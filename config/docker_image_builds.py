"""Strict typed Docker image-build catalog owned by Config Center."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, Mapping, cast

from .docker_catalogs import DOCKER_CATALOGS_PATH, DockerCatalogError, load_docker_catalog

PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
IMAGE_BUILD_CATALOG_PATH: Final = DOCKER_CATALOGS_PATH

SUPPORTED_IMAGE_SMOKE_KINDS: Final = frozenset(
    {
        "caddy-version",
        "clickhouse-version",
        "command-sandbox",
        "grafana-version",
        "haproxy-version",
        "otelcol-version",
        "postgres-version",
        "nats-server-version",
        "python",
        "rag",
        "service-bus-validate",
        "symbiont",
    }
)
EXECUTION_VALIDATION_RUNTIME_CONTRACT: Final = "ai-local.execution-validation-runtime.v1"
OCI_BUILD_PROVENANCE_CONTRACT: Final = "ai-local.oci-build-provenance.v1"
WORKSPACE_SOURCE_FINGERPRINT_CONTRACT: Final = "ai-local.workspace-source-fingerprint.v1"
IMAGE_BUILD_INPUTS_CONTRACT: Final = "ai-local.image-build-inputs.v1"
IMAGE_BUILD_RECEIPT_CONTRACT: Final = "ai-local.image-build-receipt.v2"
RELEASED_IMAGE_ARTIFACT_CONTRACT: Final = "ai-local.released-image-artifact.v1"

_POLICY_FIELDS = {"build_all_mandatory", "revision_contract", "revision_build_arg", "revision_label", "source_fingerprint_contract", "build_inputs_contract", "build_receipt_contract", "build_receipt_path", "cache_cap_env", "review_after", "metrics"}
_TARGET_FIELDS = {"name", "owner", "reason", "mandatory", "image", "context", "dockerfile", "target", "build_args", "smoke"}
_TARGET_RUNTIME_FIELDS = {"runtime_profile", "immutable", "base_image", "dependency_snapshot"}
_RELEASED_ARTIFACT_FIELDS = {"contract", "name", "repository_id", "reason", "image", "publication_state", "source_ref", "proof_receipt_sha256"}
_CONTRACT_TOKEN = re.compile(r"ai-local\.[a-z0-9.-]+\.v[1-9][0-9]*")
_ENV_TOKEN = re.compile(r"[A-Z][A-Z0-9_]*")
_NAME_TOKEN = re.compile(r"[a-z0-9][a-z0-9.-]*")
_OWNER_TOKEN = re.compile(r"[a-z0-9][a-z0-9._/-]*")
_STAGE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_METRIC_TOKEN = re.compile(r"[a-z][a-z0-9_]*")
_OCI_LABEL = re.compile(r"[a-z0-9][a-z0-9./-]*[a-z0-9]")
_TAG_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_REPOSITORY_COMPONENT = re.compile(r"[a-z0-9]+(?:(?:[._]|-{1,2})[a-z0-9]+)*")
_REGISTRY_COMPONENT = re.compile(r"(?:localhost|[a-z0-9]+(?:[.-][a-z0-9]+)*)(?::[1-9][0-9]{0,4})?")
_DIGEST_PINNED_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_SHA256_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}")
_SOURCE_REVISION = re.compile(r"[0-9a-f]{40}")


class ImageBuildCatalogError(ValueError):
    """Raised when the canonical image-build catalog is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class ImageBuildPolicy:
    build_all_mandatory: bool
    revision_contract: str
    revision_build_arg: str
    revision_label: str
    source_fingerprint_contract: str
    build_inputs_contract: str
    build_receipt_contract: str
    build_receipt_path: str
    cache_cap_env: str
    review_after: str
    metrics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DirectImageBuildTarget:
    name: str
    owner: str
    reason: str
    mandatory: bool
    image: str
    context: str
    dockerfile: str
    target: str
    build_args: tuple[str, ...]
    smoke: str
    runtime_profile: str | None = None
    immutable: bool | None = None
    base_image: str | None = None
    dependency_snapshot: str | None = None

    def image_reference(self, tag: str) -> str:
        if _TAG_TOKEN.fullmatch(tag) is None:
            raise ImageBuildCatalogError(f"invalid Docker image tag: {tag!r}")
        reference = self.image.replace("{tag}", tag)
        _validate_image_reference(reference, field=f"direct target {self.name}.image")
        return reference


@dataclass(frozen=True, slots=True)
class ReleasedImageArtifact:
    contract: str
    name: str
    repository_id: str
    reason: str
    image: str
    publication_state: str
    source_ref: str
    proof_receipt_sha256: str

    def image_reference(self, tag: str) -> str:
        if _TAG_TOKEN.fullmatch(tag) is None:
            raise ImageBuildCatalogError(f"invalid Docker image tag: {tag!r}")
        return self.image.replace("{tag}", tag)


@dataclass(frozen=True, slots=True)
class ExecutionValidationProfile:
    name: str
    image: str
    immutable: bool
    catalog_target: str
    base_image: str
    dependency_snapshot: str
    capabilities: tuple[str, ...]
    versions: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ExecutionValidationCatalog:
    contract: str
    default_profile: str
    profiles: Mapping[str, ExecutionValidationProfile]


@dataclass(frozen=True, slots=True)
class ImageBuildCatalog:
    policy: ImageBuildPolicy
    base_images: Mapping[str, str]
    execution_validation: ExecutionValidationCatalog
    direct_targets: tuple[DirectImageBuildTarget, ...]
    released_artifacts: tuple[ReleasedImageArtifact, ...]


def _required_string(payload: Mapping[str, object], field: str, *, path: str) -> str:
    raw = payload.get(field)
    if not isinstance(raw, str) or not raw.strip() or raw != raw.strip():
        raise ImageBuildCatalogError(f"{path}.{field} must be a non-empty string")
    return raw


def _contract(payload: Mapping[str, object], field: str, *, path: str) -> str:
    value = _required_string(payload, field, path=path)
    if _CONTRACT_TOKEN.fullmatch(value) is None:
        raise ImageBuildCatalogError(f"{path}.{field} is not a versioned contract id")
    return value


def _supported_contract(payload: Mapping[str, object], field: str, *, path: str, expected: str) -> str:
    value = _contract(payload, field, path=path)
    if value != expected:
        raise ImageBuildCatalogError(f"{path}.{field} is unsupported; expected {expected!r}")
    return value


def _relative_path(value: object, *, field: str, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        raise ImageBuildCatalogError(f"{field} must be a normalized non-empty POSIX relative path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != value or (candidate.as_posix() == "." and not allow_dot):
        raise ImageBuildCatalogError(f"{field} must stay inside the project root")
    return candidate.as_posix()


def _resolved_project_path(project_root: Path, relative: str, *, field: str) -> Path:
    root = project_root.resolve(strict=True)
    resolved = (root / relative).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ImageBuildCatalogError(f"{field} resolves outside the project root") from exc
    return resolved


def _validate_image_reference(reference: str, *, field: str) -> None:
    if not reference or reference != reference.strip() or any(character.isspace() for character in reference) or "://" in reference or "@" in reference or "{" in reference or "}" in reference:
        raise ImageBuildCatalogError(f"{field} is not a safe tagged image reference")
    last_slash = reference.rfind("/")
    last_colon = reference.rfind(":")
    if last_colon <= last_slash:
        raise ImageBuildCatalogError(f"{field} must include an explicit tag")
    repository = reference[:last_colon]
    tag = reference[last_colon + 1 :]
    if _TAG_TOKEN.fullmatch(tag) is None:
        raise ImageBuildCatalogError(f"{field} has an invalid tag")
    components = repository.split("/")
    if not components or any(not component for component in components):
        raise ImageBuildCatalogError(f"{field} has an invalid repository")
    first_is_registry = len(components) > 1 and ("." in components[0] or ":" in components[0] or components[0] == "localhost")
    for index, component in enumerate(components):
        pattern = _REGISTRY_COMPONENT if index == 0 and first_is_registry else _REPOSITORY_COMPONENT
        if pattern.fullmatch(component) is None:
            raise ImageBuildCatalogError(f"{field} has an invalid repository component")


def _image_template(payload: Mapping[str, object], *, path: str) -> str:
    image = _required_string(payload, "image", path=path)
    placeholder_count = image.count("{tag}")
    if placeholder_count not in {0, 1}:
        raise ImageBuildCatalogError(f"{path}.image may contain the {{tag}} placeholder at most once")
    rendered = image.replace("{tag}", "catalog-validation")
    _validate_image_reference(rendered, field=f"{path}.image")
    return image


def _policy(payload: Mapping[str, object], *, catalog_path: Path) -> ImageBuildPolicy:
    if set(payload) != _POLICY_FIELDS:
        raise ImageBuildCatalogError(f"{catalog_path}: policy fields must be exactly {sorted(_POLICY_FIELDS)!r}")
    if payload.get("build_all_mandatory") is not True:
        raise ImageBuildCatalogError(f"{catalog_path}: policy.build_all_mandatory must be true")
    revision_build_arg = _required_string(payload, "revision_build_arg", path="policy")
    if _ENV_TOKEN.fullmatch(revision_build_arg) is None:
        raise ImageBuildCatalogError("policy.revision_build_arg is not an env token")
    revision_label = _required_string(payload, "revision_label", path="policy")
    if _OCI_LABEL.fullmatch(revision_label) is None:
        raise ImageBuildCatalogError("policy.revision_label is not an OCI label key")
    receipt_path = _relative_path(payload.get("build_receipt_path"), field="policy.build_receipt_path")
    receipt_parts = PurePosixPath(receipt_path).parts
    if receipt_parts[:2] != (".local", "infra") or len(receipt_parts) < 3 or PurePosixPath(receipt_path).suffix != ".json":
        raise ImageBuildCatalogError("policy.build_receipt_path must be a JSON path under .local/infra")
    cache_cap_env = _required_string(payload, "cache_cap_env", path="policy")
    if _ENV_TOKEN.fullmatch(cache_cap_env) is None:
        raise ImageBuildCatalogError("policy.cache_cap_env is not an env token")
    review_after = _required_string(payload, "review_after", path="policy")
    try:
        parsed_review_after = date.fromisoformat(review_after)
    except ValueError as exc:
        raise ImageBuildCatalogError("policy.review_after must be an ISO date") from exc
    if parsed_review_after.isoformat() != review_after:
        raise ImageBuildCatalogError("policy.review_after must be a canonical ISO date")
    raw_metrics = payload.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics or any(not isinstance(metric, str) or _METRIC_TOKEN.fullmatch(metric) is None for metric in raw_metrics):
        raise ImageBuildCatalogError("policy.metrics must be non-empty metric tokens")
    metrics = tuple(cast(list[str], raw_metrics))
    if len(set(metrics)) != len(metrics):
        raise ImageBuildCatalogError("policy.metrics must be unique")
    return ImageBuildPolicy(
        build_all_mandatory=True,
        revision_contract=_supported_contract(payload, "revision_contract", path="policy", expected=OCI_BUILD_PROVENANCE_CONTRACT),
        revision_build_arg=revision_build_arg,
        revision_label=revision_label,
        source_fingerprint_contract=_supported_contract(payload, "source_fingerprint_contract", path="policy", expected=WORKSPACE_SOURCE_FINGERPRINT_CONTRACT),
        build_inputs_contract=_supported_contract(payload, "build_inputs_contract", path="policy", expected=IMAGE_BUILD_INPUTS_CONTRACT),
        build_receipt_contract=_supported_contract(payload, "build_receipt_contract", path="policy", expected=IMAGE_BUILD_RECEIPT_CONTRACT),
        build_receipt_path=receipt_path,
        cache_cap_env=cache_cap_env,
        review_after=review_after,
        metrics=metrics,
    )


def _base_images(payload: Mapping[str, object], *, catalog_path: Path) -> Mapping[str, str]:
    if not payload:
        raise ImageBuildCatalogError(f"{catalog_path}: base_images must not be empty")
    values: dict[str, str] = {}
    for build_arg, image in payload.items():
        if not isinstance(build_arg, str) or _ENV_TOKEN.fullmatch(build_arg) is None:
            raise ImageBuildCatalogError(f"{catalog_path}: base_images keys must be build-argument env tokens")
        if not isinstance(image, str) or _DIGEST_PINNED_IMAGE.fullmatch(image) is None:
            raise ImageBuildCatalogError(f"{catalog_path}: base_images.{build_arg} must be digest pinned")
        values[build_arg] = image
    if len(set(values.values())) != len(values):
        raise ImageBuildCatalogError(f"{catalog_path}: base_images values must have one canonical build argument")
    return MappingProxyType(values)


def _validate_base_image_consumers(base_images: Mapping[str, str], targets: tuple[DirectImageBuildTarget, ...], *, project_root: Path) -> None:
    consumers: dict[str, list[str]] = {name: [] for name in base_images}
    for target in targets:
        target_base_args = tuple(name for name in target.build_args if name in base_images)
        if len(target_base_args) > 1:
            raise ImageBuildCatalogError(f"direct target {target.name} must consume at most one catalog base image")
        if target.runtime_profile is not None:
            if len(target_base_args) != 1:
                raise ImageBuildCatalogError(f"direct target {target.name} immutable metadata requires one catalog base image")
            if base_images[target_base_args[0]] != target.base_image:
                raise ImageBuildCatalogError(f"direct target {target.name} base_image metadata must equal its catalog base image")
        if not target_base_args:
            continue
        build_arg = target_base_args[0]
        consumers[build_arg].append(target.name)
        dockerfile = (project_root / target.dockerfile).read_text(encoding="utf-8")
        if re.search(rf"(?m)^ARG {re.escape(build_arg)}=", dockerfile):
            raise ImageBuildCatalogError(f"direct target {target.name} Dockerfile must not default {build_arg}")
        if re.search(rf"(?m)^ARG {re.escape(build_arg)}\s*$", dockerfile) is None:
            raise ImageBuildCatalogError(f"direct target {target.name} Dockerfile must declare {build_arg}")
        if re.search(rf"(?m)^FROM \$\{{{re.escape(build_arg)}\}}(?:\s+AS\s+\S+)?\s*$", dockerfile, re.IGNORECASE) is None:
            raise ImageBuildCatalogError(f"direct target {target.name} Dockerfile must use {build_arg} in FROM")
    unused = sorted(name for name, names in consumers.items() if not names)
    if unused:
        raise ImageBuildCatalogError(f"catalog base image arguments are unused: {', '.join(unused)}")


def _target(payload: Mapping[str, object], *, index: int, policy: ImageBuildPolicy, project_root: Path) -> DirectImageBuildTarget:
    path = f"direct_targets[{index}]"
    fields = set(payload)
    if fields != _TARGET_FIELDS and fields != _TARGET_FIELDS | _TARGET_RUNTIME_FIELDS:
        raise ImageBuildCatalogError(f"{path} fields must be exactly {sorted(_TARGET_FIELDS)!r} or {sorted(_TARGET_FIELDS | _TARGET_RUNTIME_FIELDS)!r}")
    name = _required_string(payload, "name", path=path)
    if _NAME_TOKEN.fullmatch(name) is None:
        raise ImageBuildCatalogError(f"{path}.name is not a lowercase image token")
    owner = _required_string(payload, "owner", path=path)
    if _OWNER_TOKEN.fullmatch(owner) is None or owner.startswith("/") or ".." in PurePosixPath(owner).parts or "//" in owner:
        raise ImageBuildCatalogError(f"{path}.owner is not a safe ownership path")
    reason = _required_string(payload, "reason", path=path)
    if any(ord(character) < 32 for character in reason):
        raise ImageBuildCatalogError(f"{path}.reason contains control characters")
    if payload.get("mandatory") is not True:
        raise ImageBuildCatalogError(f"{path}.mandatory must be true")
    context = _relative_path(payload.get("context"), field=f"{path}.context", allow_dot=True)
    dockerfile = _relative_path(payload.get("dockerfile"), field=f"{path}.dockerfile")
    resolved_context = _resolved_project_path(project_root, context, field=f"{path}.context")
    resolved_dockerfile = _resolved_project_path(project_root, dockerfile, field=f"{path}.dockerfile")
    if not resolved_context.is_dir():
        raise ImageBuildCatalogError(f"{path}.context is not a project directory")
    if not resolved_dockerfile.is_file():
        raise ImageBuildCatalogError(f"{path}.dockerfile is not a project file")
    try:
        resolved_dockerfile.relative_to(resolved_context)
    except ValueError as exc:
        raise ImageBuildCatalogError(f"{path}.dockerfile must be contained by its build context") from exc
    raw_stage = payload.get("target")
    if not isinstance(raw_stage, str) or (raw_stage and _STAGE_TOKEN.fullmatch(raw_stage) is None):
        raise ImageBuildCatalogError(f"{path}.target is not a Docker stage token")
    raw_build_args = payload.get("build_args")
    if not isinstance(raw_build_args, list) or not raw_build_args or any(not isinstance(build_arg, str) or _ENV_TOKEN.fullmatch(build_arg) is None for build_arg in raw_build_args):
        raise ImageBuildCatalogError(f"{path}.build_args must be non-empty env tokens")
    build_args = tuple(cast(list[str], raw_build_args))
    if len(set(build_args)) != len(build_args):
        raise ImageBuildCatalogError(f"{path}.build_args must be unique")
    if policy.revision_build_arg not in build_args:
        raise ImageBuildCatalogError(f"{path}.build_args must include policy.revision_build_arg")
    smoke = _required_string(payload, "smoke", path=path)
    if smoke not in SUPPORTED_IMAGE_SMOKE_KINDS:
        raise ImageBuildCatalogError(f"{path}.smoke is unsupported: {smoke!r}")
    runtime_profile: str | None = None
    immutable: bool | None = None
    base_image: str | None = None
    dependency_snapshot: str | None = None
    if _TARGET_RUNTIME_FIELDS <= fields:
        runtime_profile = _required_string(payload, "runtime_profile", path=path)
        if _NAME_TOKEN.fullmatch(runtime_profile) is None:
            raise ImageBuildCatalogError(f"{path}.runtime_profile is invalid")
        if payload.get("immutable") is not True:
            raise ImageBuildCatalogError(f"{path}.immutable must be true")
        immutable = True
        base_image = _required_string(payload, "base_image", path=path)
        if _DIGEST_PINNED_IMAGE.fullmatch(base_image) is None:
            raise ImageBuildCatalogError(f"{path}.base_image must be digest pinned")
        dependency_snapshot = _required_string(payload, "dependency_snapshot", path=path)
        if any(character.isspace() for character in dependency_snapshot):
            raise ImageBuildCatalogError(f"{path}.dependency_snapshot is invalid")
    return DirectImageBuildTarget(name=name, owner=owner, reason=reason, mandatory=True, image=_image_template(payload, path=path), context=context, dockerfile=dockerfile, target=raw_stage, build_args=build_args, smoke=smoke, runtime_profile=runtime_profile, immutable=immutable, base_image=base_image, dependency_snapshot=dependency_snapshot)


def _released_artifact(payload: Mapping[str, object], *, index: int) -> ReleasedImageArtifact:
    path = f"released_artifacts[{index}]"
    if set(payload) != _RELEASED_ARTIFACT_FIELDS:
        raise ImageBuildCatalogError(f"{path} fields must be exactly {sorted(_RELEASED_ARTIFACT_FIELDS)!r}")
    contract = _supported_contract(payload, "contract", path=path, expected=RELEASED_IMAGE_ARTIFACT_CONTRACT)
    name = _required_string(payload, "name", path=path)
    if _NAME_TOKEN.fullmatch(name) is None:
        raise ImageBuildCatalogError(f"{path}.name is not a lowercase image token")
    repository_id = _required_string(payload, "repository_id", path=path)
    if _NAME_TOKEN.fullmatch(repository_id) is None:
        raise ImageBuildCatalogError(f"{path}.repository_id is invalid")
    reason = _required_string(payload, "reason", path=path)
    if any(ord(character) < 32 for character in reason):
        raise ImageBuildCatalogError(f"{path}.reason contains control characters")
    publication_state = _required_string(payload, "publication_state", path=path)
    if publication_state not in {"local-proof-only", "released"}:
        raise ImageBuildCatalogError(f"{path}.publication_state must be local-proof-only or released")
    image = _required_string(payload, "image", path=path)
    if publication_state == "released":
        if _DIGEST_PINNED_IMAGE.fullmatch(image) is None:
            raise ImageBuildCatalogError(f"{path}.image must be digest pinned after publication")
    else:
        image = _image_template(payload, path=path)
    source_ref = _required_string(payload, "source_ref", path=path)
    if _SOURCE_REVISION.fullmatch(source_ref) is None:
        raise ImageBuildCatalogError(f"{path}.source_ref must be a full Git revision")
    proof_receipt_sha256 = _required_string(payload, "proof_receipt_sha256", path=path)
    if _SHA256_IDENTITY.fullmatch(proof_receipt_sha256) is None:
        raise ImageBuildCatalogError(f"{path}.proof_receipt_sha256 must be a sha256 identity")
    return ReleasedImageArtifact(contract=contract, name=name, repository_id=repository_id, reason=reason, image=image, publication_state=publication_state, source_ref=source_ref, proof_receipt_sha256=proof_receipt_sha256)


def _execution_validation_profile(name: str, payload: Mapping[str, object], *, target: DirectImageBuildTarget | ReleasedImageArtifact) -> ExecutionValidationProfile:
    path = f"execution_validation.profiles.{name}"
    expected_fields = {"image", "immutable", "catalog_target", "base_image", "dependency_snapshot", "capabilities", "versions"}
    if set(payload) != expected_fields:
        raise ImageBuildCatalogError(f"{path} fields must be exactly {sorted(expected_fields)!r}")
    image = _required_string(payload, "image", path=path)
    _validate_image_reference(image, field=f"{path}.image")
    immutable = payload.get("immutable")
    if not isinstance(immutable, bool):
        raise ImageBuildCatalogError(f"{path}.immutable must be a boolean")
    catalog_target = _required_string(payload, "catalog_target", path=path)
    if catalog_target != target.name:
        raise ImageBuildCatalogError(f"{path}.catalog_target does not resolve to its direct target")
    if image != target.image:
        raise ImageBuildCatalogError(f"{path}.image does not match direct target {target.name!r}")
    base_image = payload.get("base_image")
    dependency_snapshot = payload.get("dependency_snapshot")
    if not isinstance(base_image, str) or not isinstance(dependency_snapshot, str):
        raise ImageBuildCatalogError(f"{path}.base_image and dependency_snapshot must be strings")
    if immutable:
        if isinstance(target, DirectImageBuildTarget) and (target.immutable is not True or target.runtime_profile != name or target.base_image != base_image or target.dependency_snapshot != dependency_snapshot):
            raise ImageBuildCatalogError(f"{path} immutable metadata does not match its direct target")
        if _DIGEST_PINNED_IMAGE.fullmatch(base_image) is None:
            raise ImageBuildCatalogError(f"{path}.base_image must be digest pinned")
        if not dependency_snapshot or any(character.isspace() for character in dependency_snapshot):
            raise ImageBuildCatalogError(f"{path}.dependency_snapshot must be a non-empty snapshot token")
    elif base_image or dependency_snapshot or (isinstance(target, DirectImageBuildTarget) and (target.runtime_profile is not None or target.immutable is not None or target.base_image is not None or target.dependency_snapshot is not None)):
        raise ImageBuildCatalogError(f"{path} mutable profile cannot declare immutable target metadata")
    raw_capabilities = payload.get("capabilities")
    if not isinstance(raw_capabilities, list) or not raw_capabilities or any(not isinstance(capability, str) or _NAME_TOKEN.fullmatch(capability) is None for capability in raw_capabilities):
        raise ImageBuildCatalogError(f"{path}.capabilities must be non-empty lowercase tokens")
    capabilities = tuple(cast(list[str], raw_capabilities))
    if len(set(capabilities)) != len(capabilities):
        raise ImageBuildCatalogError(f"{path}.capabilities must be unique")
    raw_versions = payload.get("versions")
    if not isinstance(raw_versions, dict) or not raw_versions:
        raise ImageBuildCatalogError(f"{path}.versions must be a non-empty table")
    versions: dict[str, str] = {}
    for runtime, version in raw_versions.items():
        if not isinstance(runtime, str) or _NAME_TOKEN.fullmatch(runtime) is None or not isinstance(version, str) or not version.strip() or version != version.strip():
            raise ImageBuildCatalogError(f"{path}.versions must map runtime tokens to version strings")
        versions[runtime] = version
    return ExecutionValidationProfile(name=name, image=image, immutable=immutable, catalog_target=catalog_target, base_image=base_image, dependency_snapshot=dependency_snapshot, capabilities=capabilities, versions=MappingProxyType(versions))


def _execution_validation(payload: Mapping[str, object], *, targets: tuple[DirectImageBuildTarget, ...], released_artifacts: tuple[ReleasedImageArtifact, ...]) -> ExecutionValidationCatalog:
    if set(payload) != {"contract", "default_profile", "profiles"}:
        raise ImageBuildCatalogError("execution_validation fields must be exactly ['contract', 'default_profile', 'profiles']")
    contract = _contract(payload, "contract", path="execution_validation")
    if contract != EXECUTION_VALIDATION_RUNTIME_CONTRACT:
        raise ImageBuildCatalogError("execution_validation.contract is unsupported")
    default_profile = _required_string(payload, "default_profile", path="execution_validation")
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, dict) or set(raw_profiles) != {"standard", "polyglot"}:
        raise ImageBuildCatalogError("execution_validation.profiles must define exactly standard and polyglot")
    if default_profile not in raw_profiles:
        raise ImageBuildCatalogError("execution_validation.default_profile is not declared")
    targets_by_name: dict[str, DirectImageBuildTarget | ReleasedImageArtifact] = {target.name: target for target in (*targets, *released_artifacts)}
    profiles: dict[str, ExecutionValidationProfile] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            raise ImageBuildCatalogError(f"execution_validation.profiles.{name} must be a table")
        catalog_target = raw_profile.get("catalog_target")
        if not isinstance(catalog_target, str) or catalog_target not in targets_by_name:
            raise ImageBuildCatalogError(f"execution_validation.profiles.{name}.catalog_target is not declared")
        profiles[name] = _execution_validation_profile(name, raw_profile, target=targets_by_name[catalog_target])
    if len({profile.catalog_target for profile in profiles.values()}) != len(profiles):
        raise ImageBuildCatalogError("execution validation profiles must use distinct direct targets")
    return ExecutionValidationCatalog(contract=contract, default_profile=default_profile, profiles=MappingProxyType(profiles))


def load_image_build_catalog(path: Path | None = None, *, project_root: Path | None = None) -> ImageBuildCatalog:
    catalog_path = path or IMAGE_BUILD_CATALOG_PATH
    root = project_root or PROJECT_ROOT
    try:
        payload = load_docker_catalog("image_build_catalog", path=catalog_path)
    except DockerCatalogError as exc:
        raise ImageBuildCatalogError(f"{catalog_path}: unable to load image-build catalog: {exc}") from exc
    root_fields = set(payload)
    required_root_fields = {"policy", "base_images", "execution_validation", "direct_targets"}
    if frozenset(root_fields) not in {frozenset(required_root_fields), frozenset(required_root_fields | {"released_artifacts"})}:
        raise ImageBuildCatalogError(f"{catalog_path}: root fields must be exactly ['base_images', 'direct_targets', 'execution_validation', 'policy'] with optional 'released_artifacts'")
    raw_policy = payload.get("policy")
    if not isinstance(raw_policy, dict):
        raise ImageBuildCatalogError(f"{catalog_path}: policy must be a table")
    policy = _policy(raw_policy, catalog_path=catalog_path)
    raw_base_images = payload.get("base_images")
    if not isinstance(raw_base_images, dict):
        raise ImageBuildCatalogError(f"{catalog_path}: base_images must be a table")
    base_images = _base_images(raw_base_images, catalog_path=catalog_path)
    raw_execution_validation = payload.get("execution_validation")
    if not isinstance(raw_execution_validation, dict):
        raise ImageBuildCatalogError(f"{catalog_path}: execution_validation must be a table")
    raw_targets = payload.get("direct_targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ImageBuildCatalogError(f"{catalog_path}: direct_targets must be a non-empty array of tables")
    targets: list[DirectImageBuildTarget] = []
    for index, raw_target in enumerate(raw_targets):
        if not isinstance(raw_target, dict):
            raise ImageBuildCatalogError(f"{catalog_path}: direct_targets[{index}] must be a table")
        targets.append(_target(raw_target, index=index, policy=policy, project_root=root))
    raw_artifacts = payload.get("released_artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise ImageBuildCatalogError(f"{catalog_path}: released_artifacts must be an array of tables")
    artifacts: list[ReleasedImageArtifact] = []
    for index, raw_artifact in enumerate(raw_artifacts):
        if not isinstance(raw_artifact, dict):
            raise ImageBuildCatalogError(f"{catalog_path}: released_artifacts[{index}] must be a table")
        artifacts.append(_released_artifact(raw_artifact, index=index))
    names = [target.name for target in targets]
    if len(set(names)) != len(names):
        raise ImageBuildCatalogError(f"{catalog_path}: direct target names must be unique")
    images = [target.image for target in targets]
    if len(set(images)) != len(images):
        raise ImageBuildCatalogError(f"{catalog_path}: direct target image references must be unique")
    image_repositories = [target.image.replace("{tag}", "catalog-validation").rsplit(":", 1)[0] for target in targets]
    if len(set(image_repositories)) != len(image_repositories):
        raise ImageBuildCatalogError(f"{catalog_path}: direct target image repositories must be unique")
    build_sources = [(target.context, target.dockerfile, target.target) for target in targets]
    if len(set(build_sources)) != len(build_sources):
        raise ImageBuildCatalogError(f"{catalog_path}: direct target context/dockerfile/stage contracts must be unique")
    typed_artifacts = tuple(artifacts)
    inventory_names = [*(target.name for target in targets), *(artifact.name for artifact in typed_artifacts)]
    if len(set(inventory_names)) != len(inventory_names):
        raise ImageBuildCatalogError(f"{catalog_path}: direct target and released artifact names must be unique")
    inventory_images = [*(target.image for target in targets), *(artifact.image for artifact in typed_artifacts)]
    if len(set(inventory_images)) != len(inventory_images):
        raise ImageBuildCatalogError(f"{catalog_path}: direct target and released artifact images must be unique")
    typed_targets = tuple(targets)
    execution_validation = _execution_validation(raw_execution_validation, targets=typed_targets, released_artifacts=typed_artifacts)
    _validate_base_image_consumers(base_images, typed_targets, project_root=root)
    return ImageBuildCatalog(policy=policy, base_images=base_images, execution_validation=execution_validation, direct_targets=typed_targets, released_artifacts=typed_artifacts)
