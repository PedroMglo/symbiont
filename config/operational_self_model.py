"""Operational self model derived from central configuration."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIR = ROOT / ".local" / "generated"
OPERATIONAL_SELF_MODEL_PATH = GENERATED_DIR / "operational-self-model.json"
CALIBRATION_REPORT_PATH = GENERATED_DIR / "calibration.report.json"
CALIBRATION_TRENDS_PATH = GENERATED_DIR / "calibration.trends.json"
AUTOTUNING_EFFECTIVE_PATH = GENERATED_DIR / "autotuning.effective.json"
OPERATIONAL_SELF_MODEL_CONTRACT = "ai-local.operational-self-model.v1"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(payload: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _env_map(items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        return {}
    result: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        env = item.get("env")
        if env:
            result[str(env)] = item.get("value")
    return result


def _decision_value(resolved: dict[str, Any], field: str, default: Any = None) -> Any:
    for decision in resolved.get("decisions", []):
        if isinstance(decision, dict) and decision.get("field") == field:
            return decision.get("value", default)
    return default


def _service_index(resolved: dict[str, Any]) -> dict[str, dict[str, Any]]:
    endpoints = resolved.get("service_endpoints") if isinstance(resolved.get("service_endpoints"), list) else []
    return {
        str(item.get("service")): item
        for item in endpoints
        if isinstance(item, dict) and item.get("service")
    }


def _service_owner(endpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "service": endpoint.get("service"),
        "boundary": "https_api",
        "url": endpoint.get("url"),
        "workers": endpoint.get("workers"),
        "healthcheck_path": endpoint.get("healthcheck_path"),
        "healthcheck_timeout_seconds": endpoint.get("healthcheck_timeout_seconds"),
    }


def _background_storage_capacity(storage_mode: str) -> str:
    if storage_mode == "external_missing":
        return "blocked"
    if storage_mode == "local_fallback":
        return "degraded"
    if storage_mode in {"external", "local"}:
        return "available"
    return "unknown"


def _foreground_capacity(config_status: str) -> str:
    if config_status in {"blocked", "external_missing"}:
        return "blocked"
    if config_status in {"degraded", "local_fallback", "stale"}:
        return "degraded"
    return "available"


def _prewarming_feed(
    *,
    resolved: dict[str, Any],
    policy: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    lifecycle = resolved.get("config", {}).get("lifecycle", {})
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    limits = policy.get("limits") if isinstance(policy.get("limits"), dict) else {}
    heavy_gpu_concurrency = int(limits.get("heavy_gpu_concurrency") or 0)
    gpu_available = bool(runtime.get("gpu_available"))
    mode = str(lifecycle.get("prewarm") or "balanced")
    max_gpu_prewarm = max(0, min(heavy_gpu_concurrency, 1)) if gpu_available and mode != "off" else 0
    return {
        "source": "config.lifecycle.prewarm + resource_governor_policy.limits",
        "mode": mode,
        "gpu_available": gpu_available,
        "max_gpu_prewarm_lanes": max_gpu_prewarm,
        "background_only": True,
        "policy": "advisory_capacity; prewarming owner decides execution",
    }


def _degradations(
    *,
    health: dict[str, Any],
    calibration_report: dict[str, Any],
    calibration_trends: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for error in health.get("errors") or []:
        items.append({"source": "config_health", "severity": "blocker", "message": error})
    for warning in health.get("warnings") or []:
        items.append({"source": "config_health", "severity": "warning", "message": warning})
    for output in health.get("stale_outputs") or []:
        items.append({"source": "config_health", "severity": "warning", "message": f"stale generated output: {output}"})
    for recommendation in calibration_report.get("recommendations") or []:
        if isinstance(recommendation, dict):
            items.append(
                {
                    "source": "calibration_report",
                    "severity": recommendation.get("severity") or "info",
                    "id": recommendation.get("id"),
                    "message": recommendation.get("message"),
                    "action": recommendation.get("action"),
                }
            )
    for hint in calibration_trends.get("hints") or []:
        if isinstance(hint, dict):
            items.append(
                {
                    "source": "calibration_trends",
                    "severity": hint.get("severity") or "info",
                    "id": hint.get("id"),
                    "message": hint.get("message"),
                    "action": hint.get("action"),
                }
            )
    return items


def build_operational_self_model(
    resolved: dict[str, Any],
    *,
    calibration_report: dict[str, Any] | None = None,
    calibration_trends: dict[str, Any] | None = None,
    autotuning_effective: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    calibration_report = calibration_report or {}
    calibration_trends = calibration_trends or {}
    autotuning_effective = autotuning_effective or {}
    runtime = resolved.get("runtime") if isinstance(resolved.get("runtime"), dict) else {}
    storage_paths = resolved.get("storage_paths") if isinstance(resolved.get("storage_paths"), dict) else {}
    health = resolved.get("config_health") if isinstance(resolved.get("config_health"), dict) else {}
    policy = resolved.get("resource_governor_policy") if isinstance(resolved.get("resource_governor_policy"), dict) else {}
    limits = policy.get("limits") if isinstance(policy.get("limits"), dict) else {}
    storage_policy = policy.get("storage_policy") if isinstance(policy.get("storage_policy"), dict) else {}
    docker_resources = _env_map(resolved.get("docker_resources"))
    symbiont_runtime = _env_map(resolved.get("symbiont_runtime"))
    routing_values = {
        key: value
        for key, value in symbiont_runtime.items()
        if key.startswith("ORC_DYNAMIC_ROUTING_") or key.startswith("ORC_DISPATCH_")
    }
    services = _service_index(resolved)
    storage_mode = str(storage_paths.get("AI_LOCAL_STORAGE_MODE") or "unknown")
    config_status = str(health.get("status") or "unknown")
    heavy_gpu_concurrency = int(limits.get("heavy_gpu_concurrency") or 0)

    return {
        "schema_version": 1,
        "contract": OPERATIONAL_SELF_MODEL_CONTRACT,
        "generated_at": generated_at or _utc_now(),
        "status": config_status,
        "resources": {
            "host": {
                "cpu_threads": runtime.get("cpu_threads"),
                "ram_total_gb": runtime.get("ram_total_gb"),
                "ram_available_gb": runtime.get("ram_available_gb"),
                "gpu_available": runtime.get("gpu_available"),
                "gpu_name": runtime.get("gpu_name"),
                "vram_total_gb": runtime.get("vram_total_gb"),
                "vram_free_gb": runtime.get("vram_free_gb"),
            },
            "docker": {
                "available": runtime.get("docker_available"),
                "context": runtime.get("docker_context"),
                "resource_env": docker_resources,
            },
            "storage": {
                "mode": storage_mode,
                "root": storage_paths.get("AI_LOCAL_STORAGE_ROOT"),
                "external_root": storage_paths.get("AI_STORAGE_EXTERNAL_ROOT"),
                "host_bind_root": storage_paths.get("AI_STORAGE_HOST_BIND_ROOT"),
                "container_bind_root": storage_paths.get("AI_STORAGE_CONTAINER_BIND_ROOT"),
                "require_external": storage_paths.get("AI_STORAGE_REQUIRE_EXTERNAL"),
                "allow_local_heavy_fallback": storage_paths.get("AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK"),
            },
        },
        "limits": {
            "resolved_workers": _decision_value(resolved, "runtime.workers.final"),
            "resolved_batch_size": _decision_value(resolved, "runtime.batch_size"),
            "resource_governor": limits,
            "docker_resources": docker_resources,
        },
        "active_owners": {
            "orchestrator": {
                "owner": "orchestrator",
                "boundary": "runtime control flow, dispatch, policy gates and ledger",
            },
            "storage_guardian": {
                "owner": "storage_guardian",
                "boundary": "managed storage writes and lifecycle API",
                "storage_mode": storage_mode,
                "endpoint": services.get("storage_guardian", {}).get("url"),
            },
            "rag": {
                "owner": "obsidian-rag",
                "boundary": "RAG API",
                "endpoint": services.get("rag", {}).get("url"),
            },
            "services": [_service_owner(endpoint) for _, endpoint in sorted(services.items())],
        },
        "degradations": _degradations(
            health=health,
            calibration_report=calibration_report,
            calibration_trends=calibration_trends,
        ),
        "storage": {
            "mode": storage_mode,
            "policy": storage_policy,
            "capacity": _background_storage_capacity(storage_mode),
        },
        "execution_capacity": {
            "foreground_interaction": _foreground_capacity(config_status),
            "background_storage": _background_storage_capacity(storage_mode),
            "heavy_gpu": "available" if bool(runtime.get("gpu_available")) and heavy_gpu_concurrency > 0 else "unavailable",
            "docker_lifecycle": "available" if runtime.get("docker_available") else "unavailable",
            "routing": "available" if routing_values else "unknown",
        },
        "feeds": {
            "resource_governor": {
                "contract": policy.get("contract_version") or policy.get("contract"),
                "mode": policy.get("mode"),
                "machine_profile": policy.get("machine_profile"),
                "limits": limits,
                "storage_policy": storage_policy,
                "autotuning": policy.get("autotuning") or {},
            },
            "routing": {
                "source": "resolved.symbiont_runtime",
                "values": routing_values,
                "policy": "advisory_budget_surface; orchestrator owns routing behavior",
            },
            "prewarming": _prewarming_feed(resolved=resolved, policy=policy, runtime=runtime),
        },
        "evidence": {
            "config_health": health,
            "calibration": {
                "report_status": calibration_report.get("status"),
                "trends_status": calibration_trends.get("status"),
                "sample_count": calibration_trends.get("sample_count"),
            },
            "autotuning_effective": {
                "status": autotuning_effective.get("status"),
                "generated_at": autotuning_effective.get("generated_at"),
                "override_count": len(autotuning_effective.get("overrides") or []),
            },
        },
    }


def write_operational_self_model(payload: dict[str, Any], output_path: Path = OPERATIONAL_SELF_MODEL_PATH) -> None:
    _write_json(payload, output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m config.operational_self_model")
    parser.add_argument("--config", default=str(ROOT / "config" / "main.yaml"))
    parser.add_argument("--calibration-report", default=str(CALIBRATION_REPORT_PATH), metavar="PATH")
    parser.add_argument("--calibration-trends", default=str(CALIBRATION_TRENDS_PATH), metavar="PATH")
    parser.add_argument("--autotuning-effective", default=str(AUTOTUNING_EFFECTIVE_PATH), metavar="PATH")
    parser.add_argument("--write", nargs="?", const=str(OPERATIONAL_SELF_MODEL_PATH), metavar="PATH")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from .resolver import resolve_config

    resolved = resolve_config(Path(args.config))
    payload = build_operational_self_model(
        resolved,
        calibration_report=_load_json(Path(args.calibration_report)),
        calibration_trends=_load_json(Path(args.calibration_trends)),
        autotuning_effective=_load_json(Path(args.autotuning_effective)),
    )
    if args.write:
        write_operational_self_model(payload, Path(args.write))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"operational_self_model: {payload['status']}")
        print(f"foreground_interaction: {payload['execution_capacity']['foreground_interaction']}")
        print(f"background_storage: {payload['execution_capacity']['background_storage']}")
        print(f"degradations: {len(payload['degradations'])}")
        if args.write:
            print(f"Generated: {args.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
