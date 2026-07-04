"""Safe local calibration report for autonomous configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .resolver import ROOT, resolve_config

GENERATED_DIR = ROOT / ".local" / "generated"
STATE_DIR = ROOT / ".local" / "state"
CALIBRATION_REPORT_PATH = GENERATED_DIR / "calibration.report.json"
CALIBRATION_TRENDS_PATH = GENERATED_DIR / "calibration.trends.json"
CALIBRATION_HISTORY_PATH = STATE_DIR / "calibration-history.json"
MAX_HISTORY_ENTRIES = 50

RunCommand = Callable[[list[str], float], tuple[int, str, str]]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _decision_value(resolved: dict[str, Any], field: str, default: Any = None) -> Any:
    for decision in resolved.get("decisions", []):
        if decision.get("field") == field:
            return decision.get("value", default)
    return default


def _bench_cpu_hash(*, size_bytes: int = 1024 * 1024, rounds: int = 8) -> dict[str, Any]:
    data = b"ai-local-calibration" * max(1, size_bytes // len(b"ai-local-calibration"))
    data = data[:size_bytes]
    started = time.perf_counter()
    digest = ""
    for _ in range(rounds):
        digest = hashlib.sha256(data).hexdigest()
    elapsed = max(time.perf_counter() - started, 0.000001)
    mib = (len(data) * rounds) / 1024 / 1024
    return {
        "status": "ok",
        "elapsed_ms": round(elapsed * 1000, 2),
        "throughput_mib_s": round(mib / elapsed, 2),
        "digest_prefix": digest[:12],
    }


def _bench_storage_write(storage_root: Path | None, *, size_bytes: int = 1024 * 1024) -> dict[str, Any]:
    if storage_root is None:
        return {"status": "skipped", "reason": "storage root not resolved"}
    try:
        target_dir = storage_root.expanduser() / ".calibration"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "write-test.bin"
        data = b"0" * size_bytes
        started = time.perf_counter()
        with target.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        elapsed = max(time.perf_counter() - started, 0.000001)
        try:
            target.unlink()
            target_dir.rmdir()
        except OSError:
            pass
    except OSError as exc:
        return {"status": "error", "reason": str(exc)}
    mib = size_bytes / 1024 / 1024
    return {
        "status": "ok",
        "elapsed_ms": round(elapsed * 1000, 2),
        "throughput_mib_s": round(mib / elapsed, 2),
        "bytes": size_bytes,
    }


def _bench_docker_latency(context: str | None, run: RunCommand = _run) -> dict[str, Any]:
    if not context:
        return {"status": "skipped", "reason": "docker context not resolved"}
    started = time.perf_counter()
    rc, out, err = run(["docker", "--context", context, "ps", "--format", "{{.Names}}"], 8.0)
    elapsed = max(time.perf_counter() - started, 0.000001)
    if rc != 0:
        return {"status": "error", "elapsed_ms": round(elapsed * 1000, 2), "reason": err or out}
    names = [line for line in out.splitlines() if line.strip()]
    return {"status": "ok", "elapsed_ms": round(elapsed * 1000, 2), "container_count": len(names)}


def _status_from_recommendations(recommendations: list[dict[str, Any]]) -> str:
    if any(item.get("severity") == "blocker" for item in recommendations):
        return "blocked"
    if any(item.get("severity") == "warning" for item in recommendations):
        return "degraded"
    return "ready"


def _metric(report: dict[str, Any], path: tuple[str, ...]) -> float | None:
    current: Any = report
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    if isinstance(current, bool) or current is None:
        return None
    try:
        return float(current)
    except (TypeError, ValueError):
        return None


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _slope(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return round(values[-1] - values[0], 2)


def _recommendations(resolved: dict[str, Any], benchmarks: dict[str, Any]) -> list[dict[str, Any]]:
    runtime = resolved.get("runtime") or {}
    storage_paths = resolved.get("storage_paths") or {}
    policy = resolved.get("resource_governor_policy") or {}
    storage_policy = policy.get("storage_policy") or {}
    recommendations: list[dict[str, Any]] = []

    if storage_policy.get("fallback_is_operational"):
        recommendations.append(
            {
                "id": "storage-local-fallback-operational",
                "severity": "info",
                "message": "External storage is absent but local_fallback is operational.",
                "action": "Do not block runtime; reconcile stale external binds when containers are recreated.",
            }
        )
    elif storage_policy.get("missing_external_is_blocker"):
        recommendations.append(
            {
                "id": "storage-external-missing-blocker",
                "severity": "blocker",
                "message": "External storage is required and local fallback is disabled.",
                "action": "Mount the external storage or enable AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK.",
            }
        )

    storage_bench = benchmarks.get("storage_write") or {}
    if storage_bench.get("status") == "ok" and float(storage_bench.get("throughput_mib_s") or 0) < 10:
        recommendations.append(
            {
                "id": "storage-write-throughput-low",
                "severity": "warning",
                "message": "Storage write throughput is low for background archive or indexing work.",
                "action": "Keep storage lane at one worker and prefer checkpointed background jobs.",
            }
        )

    if not runtime.get("docker_available"):
        recommendations.append(
            {
                "id": "docker-unavailable",
                "severity": "warning",
                "message": "Docker is not available to the resolver.",
                "action": "Keep apply/reconcile disabled until Docker is reachable.",
            }
        )

    if _decision_value(resolved, "llm.backend.effective") == "vllm" and not runtime.get("gpu_available"):
        recommendations.append(
            {
                "id": "vllm-without-gpu",
                "severity": "blocker",
                "message": "vLLM was selected but no GPU is available in probes.",
                "action": "Force CPU backend or fix GPU visibility before enabling vLLM.",
            }
        )

    if storage_paths.get("AI_LOCAL_STORAGE_MODE") == "local_fallback":
        recommendations.append(
            {
                "id": "storage-reconcile-when-external-returns",
                "severity": "info",
                "message": "Local fallback should be reconciled when the external SSD returns.",
                "action": "Run make infra or start the stack normally after mounting the SSD.",
            }
        )

    return recommendations


def build_calibration_report(
    *,
    resolved: dict[str, Any] | None = None,
    run_benchmarks: bool = True,
    storage_bytes: int = 1024 * 1024,
    run: RunCommand = _run,
) -> dict[str, Any]:
    resolver_started = time.perf_counter()
    resolved = resolved if resolved is not None else resolve_config()
    resolver_elapsed_ms = round((time.perf_counter() - resolver_started) * 1000, 2)

    storage_paths = resolved.get("storage_paths") or {}
    runtime = resolved.get("runtime") or {}
    policy = resolved.get("resource_governor_policy") or {}
    storage_root_raw = storage_paths.get("AI_LOCAL_STORAGE_ROOT")
    storage_root = Path(storage_root_raw) if storage_root_raw else None
    docker_context = runtime.get("docker_context")

    benchmarks: dict[str, Any]
    if run_benchmarks:
        benchmarks = {
            "cpu_hash": _bench_cpu_hash(),
            "storage_write": _bench_storage_write(storage_root, size_bytes=storage_bytes),
            "docker_ps": _bench_docker_latency(str(docker_context) if docker_context else None, run=run),
        }
    else:
        benchmarks = {
            "cpu_hash": {"status": "skipped", "reason": "benchmarks disabled"},
            "storage_write": {"status": "skipped", "reason": "benchmarks disabled"},
            "docker_ps": {"status": "skipped", "reason": "benchmarks disabled"},
        }

    recommendations = _recommendations(resolved, benchmarks)
    return {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "status": _status_from_recommendations(recommendations),
        "resolver_elapsed_ms": resolver_elapsed_ms,
        "profile": policy.get("machine_profile"),
        "storage_mode": storage_paths.get("AI_LOCAL_STORAGE_MODE"),
        "llm_backend": _decision_value(resolved, "llm.backend.effective"),
        "runtime": {
            "cpu_threads": runtime.get("cpu_threads"),
            "ram_total_gb": runtime.get("ram_total_gb"),
            "gpu_available": runtime.get("gpu_available"),
            "docker_available": runtime.get("docker_available"),
            "docker_context": docker_context,
            "battery_percent": runtime.get("battery_percent"),
            "battery_power_plugged": runtime.get("battery_power_plugged"),
            "thermal_max_celsius": runtime.get("thermal_max_celsius"),
            "thermal_throttle": runtime.get("thermal_throttle"),
        },
        "resource_governor": {
            "mode": policy.get("mode"),
            "machine_profile": policy.get("machine_profile"),
            "limits": policy.get("limits") or {},
            "storage_policy": policy.get("storage_policy") or {},
            "operational_authority": policy.get("operational_authority") or {},
        },
        "benchmarks": benchmarks,
        "recommendations": recommendations,
    }


def write_calibration_report(report: dict[str, Any], output_path: Path = CALIBRATION_REPORT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _history_entry(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "profile": report.get("profile"),
        "storage_mode": report.get("storage_mode"),
        "llm_backend": report.get("llm_backend"),
        "runtime": report.get("runtime") or {},
        "benchmarks": report.get("benchmarks") or {},
        "recommendation_ids": [item.get("id") for item in report.get("recommendations", []) if item.get("id")],
    }


def load_calibration_history(path: Path = CALIBRATION_HISTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        return [item for item in payload["entries"] if isinstance(item, dict)]
    return []


def append_calibration_history(
    report: dict[str, Any],
    *,
    path: Path = CALIBRATION_HISTORY_PATH,
    max_entries: int = MAX_HISTORY_ENTRIES,
) -> list[dict[str, Any]]:
    history = load_calibration_history(path)
    history.append(_history_entry(report))
    history = history[-max_entries:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": _utc_now(),
                "max_entries": max_entries,
                "entries": history,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return history


def build_calibration_trends(history: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [entry for entry in history if isinstance(entry, dict)]
    recent = entries[-10:]
    storage_values = [
        value
        for entry in recent
        if (value := _metric(entry, ("benchmarks", "storage_write", "throughput_mib_s"))) is not None
    ]
    cpu_values = [
        value
        for entry in recent
        if (value := _metric(entry, ("benchmarks", "cpu_hash", "throughput_mib_s"))) is not None
    ]
    docker_values = [
        value
        for entry in recent
        if (value := _metric(entry, ("benchmarks", "docker_ps", "elapsed_ms"))) is not None
    ]
    thermal_values = [
        value
        for entry in recent
        if (value := _metric(entry, ("runtime", "thermal_max_celsius"))) is not None
    ]
    blocker_count = sum(1 for entry in recent if entry.get("status") == "blocked")
    degraded_count = sum(1 for entry in recent if entry.get("status") == "degraded")

    hints: list[dict[str, Any]] = []
    avg_storage = _average(storage_values)
    avg_docker = _average(docker_values)
    max_thermal = max(thermal_values) if thermal_values else None
    if avg_storage is not None and avg_storage < 10:
        hints.append(
            {
                "id": "trend-storage-slow",
                "severity": "warning",
                "message": "Recent storage write throughput is low.",
                "action": "Keep storage workers at 1 and defer archive/indexing work during interaction.",
            }
        )
    if avg_docker is not None and avg_docker > 1000:
        hints.append(
            {
                "id": "trend-docker-latency-high",
                "severity": "warning",
                "message": "Docker control plane latency is high.",
                "action": "Avoid lifecycle churn; batch reconcile actions behind approval.",
            }
        )
    if max_thermal is not None and max_thermal >= 85:
        hints.append(
            {
                "id": "trend-thermal-high",
                "severity": "warning",
                "message": "Recent thermal readings are high.",
                "action": "Prefer foreground work and defer heavy background/GPU jobs.",
            }
        )
    if blocker_count:
        hints.append(
            {
                "id": "trend-blockers-present",
                "severity": "blocker",
                "message": "At least one recent calibration was blocked.",
                "action": "Inspect calibration history before enabling supervised apply.",
            }
        )
    elif degraded_count:
        hints.append(
            {
                "id": "trend-degraded-present",
                "severity": "info",
                "message": "Recent calibrations include degraded states.",
                "action": "Keep recommendations advisory until the next ready calibration.",
            }
        )

    status = _status_from_recommendations(hints)
    return {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "status": status,
        "sample_count": len(entries),
        "window_count": len(recent),
        "status_counts": {
            "ready": sum(1 for entry in recent if entry.get("status") == "ready"),
            "degraded": degraded_count,
            "blocked": blocker_count,
        },
        "averages": {
            "storage_write_mib_s": avg_storage,
            "cpu_hash_mib_s": _average(cpu_values),
            "docker_ps_elapsed_ms": avg_docker,
            "thermal_max_celsius": _average(thermal_values),
        },
        "deltas": {
            "storage_write_mib_s": _slope(storage_values),
            "cpu_hash_mib_s": _slope(cpu_values),
            "docker_ps_elapsed_ms": _slope(docker_values),
            "thermal_max_celsius": _slope(thermal_values),
        },
        "hints": hints,
    }


def write_calibration_trends(trends: dict[str, Any], output_path: Path = CALIBRATION_TRENDS_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(trends, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m config.calibration")
    parser.add_argument("--write", nargs="?", const=str(CALIBRATION_REPORT_PATH), metavar="PATH")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-benchmarks", action="store_true")
    parser.add_argument("--storage-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--record-history", action="store_true")
    parser.add_argument("--write-trends", nargs="?", const=str(CALIBRATION_TRENDS_PATH), metavar="PATH")
    args = parser.parse_args(argv)

    report = build_calibration_report(run_benchmarks=not args.no_benchmarks, storage_bytes=args.storage_bytes)
    if args.write:
        output_path = Path(args.write)
        write_calibration_report(report, output_path)
    trends = None
    if args.record_history or args.write_trends:
        history = append_calibration_history(report) if args.record_history else load_calibration_history()
        trends = build_calibration_trends(history)
    if args.write_trends and trends is not None:
        write_calibration_trends(trends, Path(args.write_trends))
    if args.json:
        payload = {"report": report, "trends": trends} if trends is not None else report
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"calibration: {report['status']}")
        print(f"profile: {report.get('profile')}")
        print(f"storage_mode: {report.get('storage_mode')}")
        print(f"recommendations: {len(report.get('recommendations') or [])}")
        if trends is not None:
            print(f"history_samples: {trends['sample_count']}")
            print(f"trend_hints: {len(trends.get('hints') or [])}")
        if args.write:
            print(f"Generated: {args.write}")
        if args.write_trends:
            print(f"Generated: {args.write_trends}")
    return 0 if report["status"] in {"ready", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
