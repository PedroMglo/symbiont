"""Typed schema and validation for the ai-local root config."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Mode = Literal["dev", "prod", "local", "debug"]
HardwareProfile = Literal["auto", "cpu_only", "gpu_8gb", "low_ram"]
BackendPreference = Literal["auto", "vllm", "llama_cpp", "ollama", "cpu"]
QualityLatency = Literal["fast", "balanced", "quality"]
PackageInstallPolicy = Literal["disabled", "dependency-cache-only", "external-allowed"]
DependencyNetworkPolicy = Literal["none", "dependency-cache", "external"]
NativeBuildPolicy = Literal["deny", "allow-pure-python", "allow-with-approval"]


@dataclass(frozen=True)
class HardwareInput:
    profile: HardwareProfile = "auto"


@dataclass(frozen=True)
class StorageInput:
    external_root: Path | None = None
    expected_filesystem: str = "auto"
    require_external: bool = True
    allow_local_heavy_fallback: bool = True


@dataclass(frozen=True)
class LLMInput:
    preferred_backend: BackendPreference = "auto"
    quality_latency: QualityLatency = "balanced"
    privacy_policy: str = "local_only"


@dataclass(frozen=True)
class LimitsInput:
    max_workers: int | Literal["auto"] = "auto"
    cpu_budget_fraction: float = 0.50
    memory_budget_fraction: float = 0.70


@dataclass(frozen=True)
class PortsInput:
    bind_host: str = "127.0.0.1"
    base_port: int = 8000


@dataclass(frozen=True)
class PrivacyInput:
    record_prompts: bool = False
    record_responses: bool = False
    redact_paths: bool = True
    redact_secrets: bool = True


@dataclass(frozen=True)
class RuntimeInput:
    probe: bool = True
    docker_probe: bool = True
    force_gpu: bool | None = None


@dataclass(frozen=True)
class DockerInput:
    compose_parallel_limit: int | Literal["auto"] = "auto"
    buildkit: bool = True
    build_cache_max: str = "auto"
    up_no_build: bool = True
    up_wait: bool = True
    up_wait_timeout_seconds: int = 120
    remove_orphans: bool = False


@dataclass(frozen=True)
class CompatibilityInput:
    read_env_storage_generated: bool = True


@dataclass(frozen=True)
class InferenceInput:
    reserved_vram_fraction: float = 0.15
    reserved_vram_min_gb: float = 1.0
    vllm_gpu_memory_utilization_cap: float = 0.82
    estimated_vram_per_gpu_task_gb: float = 3.0
    estimated_ram_per_worker_gb: float = 1.5
    estimated_memory_per_batch_item_gb: float = 0.35
    min_batch_size: int = 1
    max_batch_size: int = 32


@dataclass(frozen=True)
class LifecycleInput:
    prewarm: str = "balanced"


@dataclass(frozen=True)
class DependencyPolicyInput:
    package_install: PackageInstallPolicy = "disabled"
    network: DependencyNetworkPolicy = "none"
    lockfile_required: bool = False
    native_builds: NativeBuildPolicy = "deny"
    dependency_cache_profile: str | None = None


@dataclass(frozen=True)
class MaterialInput:
    dependency_policy: DependencyPolicyInput = field(default_factory=DependencyPolicyInput)


@dataclass(frozen=True)
class AppConfig:
    version: int = 1
    mode: Mode = "dev"
    hardware: HardwareInput = field(default_factory=HardwareInput)
    storage: StorageInput = field(default_factory=StorageInput)
    llm: LLMInput = field(default_factory=LLMInput)
    limits: LimitsInput = field(default_factory=LimitsInput)
    ports: PortsInput = field(default_factory=PortsInput)
    privacy: PrivacyInput = field(default_factory=PrivacyInput)
    runtime: RuntimeInput = field(default_factory=RuntimeInput)
    docker: DockerInput = field(default_factory=DockerInput)
    compatibility: CompatibilityInput = field(default_factory=CompatibilityInput)
    inference: InferenceInput = field(default_factory=InferenceInput)
    lifecycle: LifecycleInput = field(default_factory=LifecycleInput)
    material: MaterialInput = field(default_factory=MaterialInput)


class ConfigError(ValueError):
    """Raised when user config is invalid."""


ALLOWED_TOP_LEVEL_KEYS = {
    "version",
    "mode",
    "hardware",
    "storage",
    "llm",
    "limits",
    "ports",
    "privacy",
    "runtime",
    "docker",
    "compatibility",
    "inference",
    "lifecycle",
    "material",
}


def _as_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ConfigError(f"{field_name} must be a boolean")


def _as_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be a number") from exc


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be an integer") from exc


def _validate_fraction(value: float, field_name: str) -> float:
    if not 0 < value <= 1:
        raise ConfigError(f"{field_name} must be > 0 and <= 1")
    return value


def _auto_or_int(value: Any, field_name: str, *, minimum: int = 1, maximum: int | None = None) -> int | Literal["auto"]:
    if str(value).strip().lower() == "auto":
        return "auto"
    parsed = _as_int(value, field_name)
    if parsed < minimum:
        raise ConfigError(f"{field_name} must be auto or >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{field_name} must be auto or <= {maximum}")
    return parsed


def _cache_size(value: Any, field_name: str) -> str:
    raw = str(value).strip().lower()
    if raw == "auto":
        return "auto"
    if not re.fullmatch(r"[1-9][0-9]*(b|kb|mb|gb|tb|k|m|g|t)", raw):
        raise ConfigError(f"{field_name} must be auto or a Docker size such as 30gb")
    return raw


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _external_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    raw_candidates = os.environ.get("AI_STORAGE_AUTO_CANDIDATES", "")
    for raw in raw_candidates.split(os.pathsep):
        if raw.strip():
            candidates.append(Path(raw.strip()).expanduser())

    disable_discovery = os.environ.get("AI_STORAGE_AUTO_DISABLE_DISCOVERY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if disable_discovery:
        return candidates

    user = os.environ.get("USER") or Path.home().name
    parents = [
        Path("/mnt"),
        Path("/media") / user,
        Path("/run/media") / user,
        Path("/Volumes"),
    ]
    for parent in parents:
        try:
            children = sorted(parent.glob("*"))
        except OSError:
            children = []
        for child in children:
            candidates.append(child / "ai-local")

    candidates.append(Path("/mnt/ai-extreme/ai-local"))
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _auto_external_root() -> Path | None:
    for candidate in _external_root_candidates():
        try:
            if candidate.is_dir() and os.access(candidate, os.W_OK):
                return candidate
        except OSError:
            continue
    return None


def _parse_external_root(value: Any) -> Path | None:
    if value in (None, "", "none", "local"):
        return None
    raw = str(value).strip()
    if raw.lower() in {"auto", "detect", "discover"}:
        return _auto_external_root()
    external_root = Path(raw).expanduser()
    if not external_root.is_absolute():
        raise ConfigError("storage.external_root must be an absolute path, auto, none, or local")
    return external_root


def parse_app_config(raw: dict[str, Any]) -> AppConfig:
    """Parse untrusted YAML/profile data into typed config."""

    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    unknown_keys = sorted(set(raw) - ALLOWED_TOP_LEVEL_KEYS)
    if unknown_keys:
        raise ConfigError(
            "unknown top-level config section(s): "
            + ", ".join(unknown_keys)
            + ". Move owner-specific runtime policy to its owner compatibility file or add it to the typed schema."
        )

    version = _as_int(raw.get("version", 1), "version")
    if version != 1:
        raise ConfigError("only config version 1 is supported")

    mode = str(raw.get("mode", "dev"))
    if mode not in {"dev", "prod", "local", "debug"}:
        raise ConfigError("mode must be one of dev, prod, local, debug")

    hardware_raw = _section(raw, "hardware")
    profile = str(hardware_raw.get("profile", "auto"))
    if profile not in {"auto", "cpu_only", "gpu_8gb", "low_ram"}:
        raise ConfigError("hardware.profile must be auto, cpu_only, gpu_8gb, or low_ram")

    storage_raw = _section(raw, "storage")
    external_root = _parse_external_root(storage_raw.get("external_root", "auto"))

    llm_raw = _section(raw, "llm")
    preferred_backend = str(llm_raw.get("preferred_backend", "auto"))
    if preferred_backend not in {"auto", "vllm", "llama_cpp", "ollama", "cpu"}:
        raise ConfigError("llm.preferred_backend must be auto, vllm, llama_cpp, ollama, or cpu")
    quality_latency = str(llm_raw.get("quality_latency", "balanced"))
    if quality_latency not in {"fast", "balanced", "quality"}:
        raise ConfigError("llm.quality_latency must be fast, balanced, or quality")

    limits_raw = _section(raw, "limits")
    max_workers_raw = limits_raw.get("max_workers", "auto")
    max_workers: int | Literal["auto"]
    if str(max_workers_raw).lower() == "auto":
        max_workers = "auto"
    else:
        max_workers = _as_int(max_workers_raw, "limits.max_workers")
        if max_workers < 1:
            raise ConfigError("limits.max_workers must be >= 1")

    ports_raw = _section(raw, "ports")
    base_port = _as_int(ports_raw.get("base_port", 8000), "ports.base_port")
    if not 1 <= base_port <= 65000:
        raise ConfigError("ports.base_port must be between 1 and 65000")

    privacy_raw = _section(raw, "privacy")
    runtime_raw = _section(raw, "runtime")
    docker_raw = _section(raw, "docker")
    docker_up_wait_timeout = _as_int(
        docker_raw.get("up_wait_timeout_seconds", 120),
        "docker.up_wait_timeout_seconds",
    )
    if docker_up_wait_timeout < 1:
        raise ConfigError("docker.up_wait_timeout_seconds must be >= 1")
    compatibility_raw = _section(raw, "compatibility")
    inference_raw = _section(raw, "inference")
    lifecycle_raw = _section(raw, "lifecycle")
    material_raw = _section(raw, "material")
    dependency_policy_raw = _section(material_raw, "dependency_policy")
    package_install = str(dependency_policy_raw.get("package_install", "disabled"))
    if package_install not in {"disabled", "dependency-cache-only", "external-allowed"}:
        raise ConfigError(
            "material.dependency_policy.package_install must be disabled, "
            "dependency-cache-only, or external-allowed"
        )
    dependency_network = str(dependency_policy_raw.get("network", "none"))
    if dependency_network not in {"none", "dependency-cache", "external"}:
        raise ConfigError("material.dependency_policy.network must be none, dependency-cache, or external")
    native_builds = str(dependency_policy_raw.get("native_builds", "deny"))
    if native_builds not in {"deny", "allow-pure-python", "allow-with-approval"}:
        raise ConfigError(
            "material.dependency_policy.native_builds must be deny, allow-pure-python, or allow-with-approval"
        )

    return AppConfig(
        version=version,
        mode=mode,  # type: ignore[arg-type]
        hardware=HardwareInput(profile=profile),  # type: ignore[arg-type]
        storage=StorageInput(
            external_root=external_root,
            expected_filesystem=str(storage_raw.get("expected_filesystem", "auto")),
            require_external=_as_bool(storage_raw.get("require_external", True), "storage.require_external"),
            allow_local_heavy_fallback=_as_bool(
                storage_raw.get("allow_local_heavy_fallback", True),
                "storage.allow_local_heavy_fallback",
            ),
        ),
        llm=LLMInput(
            preferred_backend=preferred_backend,  # type: ignore[arg-type]
            quality_latency=quality_latency,  # type: ignore[arg-type]
            privacy_policy=str(llm_raw.get("privacy_policy", "local_only")),
        ),
        limits=LimitsInput(
            max_workers=max_workers,
            cpu_budget_fraction=_validate_fraction(
                _as_float(limits_raw.get("cpu_budget_fraction", 0.50), "limits.cpu_budget_fraction"),
                "limits.cpu_budget_fraction",
            ),
            memory_budget_fraction=_validate_fraction(
                _as_float(limits_raw.get("memory_budget_fraction", 0.70), "limits.memory_budget_fraction"),
                "limits.memory_budget_fraction",
            ),
        ),
        ports=PortsInput(
            bind_host=str(ports_raw.get("bind_host", "127.0.0.1")),
            base_port=base_port,
        ),
        privacy=PrivacyInput(
            record_prompts=_as_bool(privacy_raw.get("record_prompts", False), "privacy.record_prompts"),
            record_responses=_as_bool(privacy_raw.get("record_responses", False), "privacy.record_responses"),
            redact_paths=_as_bool(privacy_raw.get("redact_paths", True), "privacy.redact_paths"),
            redact_secrets=_as_bool(privacy_raw.get("redact_secrets", True), "privacy.redact_secrets"),
        ),
        runtime=RuntimeInput(
            probe=_as_bool(runtime_raw.get("probe", True), "runtime.probe"),
            docker_probe=_as_bool(runtime_raw.get("docker_probe", True), "runtime.docker_probe"),
            force_gpu=(
                None
                if runtime_raw.get("force_gpu") is None
                else _as_bool(runtime_raw.get("force_gpu"), "runtime.force_gpu")
            ),
        ),
        docker=DockerInput(
            compose_parallel_limit=_auto_or_int(
                docker_raw.get("compose_parallel_limit", "auto"),
                "docker.compose_parallel_limit",
                minimum=1,
                maximum=32,
            ),
            buildkit=_as_bool(docker_raw.get("buildkit", True), "docker.buildkit"),
            build_cache_max=_cache_size(docker_raw.get("build_cache_max", "auto"), "docker.build_cache_max"),
            up_no_build=_as_bool(docker_raw.get("up_no_build", True), "docker.up_no_build"),
            up_wait=_as_bool(docker_raw.get("up_wait", True), "docker.up_wait"),
            up_wait_timeout_seconds=docker_up_wait_timeout,
            remove_orphans=_as_bool(docker_raw.get("remove_orphans", False), "docker.remove_orphans"),
        ),
        compatibility=CompatibilityInput(
            read_env_storage_generated=_as_bool(
                compatibility_raw.get("read_env_storage_generated", True),
                "compatibility.read_env_storage_generated",
            ),
        ),
        inference=InferenceInput(
            reserved_vram_fraction=_validate_fraction(
                _as_float(inference_raw.get("reserved_vram_fraction", 0.15), "inference.reserved_vram_fraction"),
                "inference.reserved_vram_fraction",
            ),
            reserved_vram_min_gb=_as_float(
                inference_raw.get("reserved_vram_min_gb", 1.0),
                "inference.reserved_vram_min_gb",
            ),
            vllm_gpu_memory_utilization_cap=_validate_fraction(
                _as_float(
                    inference_raw.get("vllm_gpu_memory_utilization_cap", 0.82),
                    "inference.vllm_gpu_memory_utilization_cap",
                ),
                "inference.vllm_gpu_memory_utilization_cap",
            ),
            estimated_vram_per_gpu_task_gb=_as_float(
                inference_raw.get("estimated_vram_per_gpu_task_gb", 3.0),
                "inference.estimated_vram_per_gpu_task_gb",
            ),
            estimated_ram_per_worker_gb=_as_float(
                inference_raw.get("estimated_ram_per_worker_gb", 1.5),
                "inference.estimated_ram_per_worker_gb",
            ),
            estimated_memory_per_batch_item_gb=_as_float(
                inference_raw.get("estimated_memory_per_batch_item_gb", 0.35),
                "inference.estimated_memory_per_batch_item_gb",
            ),
            min_batch_size=_as_int(inference_raw.get("min_batch_size", 1), "inference.min_batch_size"),
            max_batch_size=_as_int(inference_raw.get("max_batch_size", 32), "inference.max_batch_size"),
        ),
        lifecycle=LifecycleInput(prewarm=str(lifecycle_raw.get("prewarm", "balanced"))),
        material=MaterialInput(
            dependency_policy=DependencyPolicyInput(
                package_install=package_install,  # type: ignore[arg-type]
                network=dependency_network,  # type: ignore[arg-type]
                lockfile_required=_as_bool(
                    dependency_policy_raw.get("lockfile_required", False),
                    "material.dependency_policy.lockfile_required",
                ),
                native_builds=native_builds,  # type: ignore[arg-type]
                dependency_cache_profile=(
                    None
                    if dependency_policy_raw.get("dependency_cache_profile") in {None, ""}
                    else str(dependency_policy_raw.get("dependency_cache_profile"))
                ),
            )
        ),
    )


def to_plain(value: Any) -> Any:
    """Convert config dataclasses and Paths to JSON/YAML friendly values."""

    if hasattr(value, "__dataclass_fields__"):
        return {name: to_plain(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    return value
