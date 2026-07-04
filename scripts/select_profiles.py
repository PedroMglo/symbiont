#!/usr/bin/env python3
"""Select the maximum safe Compose profiles for the current Linux machine."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

BASE_PROFILES = ["core", "storage", "agents", "features", "material", "observability", "llm"]
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return result.returncode, (result.stdout or result.stderr).strip()


def _gpu_supported() -> bool:
    resolver_gpu = _resolver_gpu_available()
    if resolver_gpu is False:
        return False
    host_rc, _ = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=10)
    if host_rc != 0:
        return False
    context = os.environ.get("AI_LOCAL_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or "default"
    info_rc, info = _run(["docker", "--context", context, "info"], timeout=10)
    if info_rc != 0:
        return False
    lower = info.lower()
    return "nvidia" in lower or "nvidia.com/gpu" in info


def _resolver_payload() -> dict[str, Any]:
    try:
        from config.resolver import DEFAULT_CONFIG_PATH, resolve_config, validate_resolved

        resolved = resolve_config(DEFAULT_CONFIG_PATH)
        errors = validate_resolved(resolved)
        if errors:
            return {"ok": False, "errors": errors, "runtime": {}}
        return {"ok": True, "errors": [], "runtime": resolved.get("runtime", {}), "warnings": resolved.get("warnings", [])}
    except Exception as exc:  # pragma: no cover - defensive fallback for partial checkouts
        return {"ok": False, "errors": [str(exc)], "runtime": {}, "warnings": []}


def _resolver_gpu_available() -> bool | None:
    payload = _resolver_payload()
    runtime = payload.get("runtime") or {}
    if "gpu_available" not in runtime:
        return None
    return bool(runtime.get("gpu_available"))


def select_profiles(*, include_observability: bool = True) -> list[str]:
    profiles = list(BASE_PROFILES)
    if not include_observability:
        profiles.remove("observability")
    if _gpu_supported():
        profiles.extend(["gpu", "heavy"])
    return profiles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-observability", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    profiles = select_profiles(include_observability=not args.no_observability)
    if args.json:
        payload = _resolver_payload()
        print(
            json.dumps(
                {
                    "profiles": profiles,
                    "profiles_csv": ",".join(profiles),
                    "runtime": payload.get("runtime", {}),
                    "warnings": payload.get("warnings", []),
                    "errors": payload.get("errors", []),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(",".join(profiles))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
