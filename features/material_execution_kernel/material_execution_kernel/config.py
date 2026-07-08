"""Runtime settings for the material execution kernel."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _read_secret_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value!r}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise ValueError(f"{name} must include at least one non-empty item")
    return values


@dataclass(frozen=True)
class SecuritySettings:
    api_key: str


@dataclass(frozen=True)
class MaterialKernelSettings:
    session_budget_seconds: int
    no_progress_watchdog_seconds: int
    security: SecuritySettings = field(default_factory=lambda: SecuritySettings(api_key=""))
    plan_coverage_repair_rounds: int = 6
    active_sandbox_owner: str = "features/workspace_execution"
    material_model_lanes: tuple[str, ...] = (
        "material_plan",
        "material_file",
        "material_patch",
        "material_repair",
        "material_critic",
    )
    prewarm_material_lanes: bool = True

    @property
    def runtime_limits(self) -> dict[str, int]:
        return {
            "session_budget_seconds": self.session_budget_seconds,
            "no_progress_watchdog_seconds": self.no_progress_watchdog_seconds,
            "plan_coverage_repair_rounds": self.plan_coverage_repair_rounds,
        }

    @property
    def model_lane_policy(self) -> dict[str, object]:
        return {
            "lanes": list(self.material_model_lanes),
            "prewarm_material_lanes": self.prewarm_material_lanes,
            "timeout_semantics": "no_progress_watchdog",
            "static_generation_shortcut_allowed": False,
        }


def get_settings() -> MaterialKernelSettings:
    api_key = _service_api_key()
    return MaterialKernelSettings(
        session_budget_seconds=_int_env("MATERIAL_EXECUTION_KERNEL_SESSION_BUDGET_SECONDS", 870),
        no_progress_watchdog_seconds=_int_env("MATERIAL_EXECUTION_KERNEL_NO_PROGRESS_WATCHDOG_SECONDS", 120),
        security=SecuritySettings(api_key=api_key),
        plan_coverage_repair_rounds=_int_env("MATERIAL_EXECUTION_KERNEL_PLAN_COVERAGE_REPAIR_ROUNDS", 6),
        material_model_lanes=_csv_env(
            "MATERIAL_EXECUTION_KERNEL_MODEL_LANES",
            (
                "material_plan",
                "material_file",
                "material_patch",
                "material_repair",
                "material_critic",
            ),
        ),
        prewarm_material_lanes=_bool_env("MATERIAL_EXECUTION_KERNEL_PREWARM_MATERIAL_LANES", True),
    )


def _service_api_key() -> str:
    for env_name in (
        "MATERIAL_EXECUTION_KERNEL_INTERNAL_API_KEY",
        "API_KEY",
        "INTERNAL_API_KEY",
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    for env_name in (
        "MATERIAL_EXECUTION_KERNEL_INTERNAL_API_KEY_FILE",
        "API_KEY_FILE",
        "INTERNAL_API_KEY_FILE",
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            secret = _read_secret_file(value)
            if secret:
                return secret
    return ""
