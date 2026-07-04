#!/usr/bin/env python3
"""Prepare and verify local models for ai-local."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = ROOT / ".local" / "generated" / "models.report.json"
STORAGE_ENV = ROOT / ".env.storage.generated"
LLM_ENV = ROOT / ".env.llm.generated"
OLLAMA_MODEL_KEYS = (
    "REASONING_AND_RESPONSE_MODEL",
    "MATERIAL_PLAN_MODEL",
    "MATERIAL_FILE_MODEL",
    "MATERIAL_PATCH_MODEL",
    "MATERIAL_REPAIR_MODEL",
    "MATERIAL_CRITIC_MODEL",
    "AUDIO_LLM_MODEL",
)
GGUF_MODEL_KEYS = ("LLAMA_CPP_AUX_MODEL_FILE", "LLAMA_CPP_FAST_MODEL_FILE")


def _run(cmd: list[str], *, timeout: int = 120) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return result.returncode, (result.stdout or result.stderr).strip()


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"')
    return env


def item(name: str, ok: bool, message: str, *, severity: str = "error", data: Any = None) -> dict[str, Any]:
    return {"name": name, "ok": ok, "message": message, "severity": severity, "data": data}


def _ollama_models() -> set[str] | None:
    if not shutil.which("ollama"):
        return None
    rc, out = _run(["ollama", "list"], timeout=20)
    if rc != 0:
        return set()
    models: set[str] = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts:
            models.add(parts[0])
    return models


def _expected_ollama_models(llm_env: dict[str, str]) -> list[str]:
    values = [llm_env.get(key, "").strip() for key in OLLAMA_MODEL_KEYS]
    return sorted({value for value in values if value})


def _expected_gguf_files(storage_env: dict[str, str], llm_env: dict[str, str]) -> list[Path]:
    models_dir = storage_env.get("LLM_MODELS_DIR", "")
    if not models_dir:
        return []
    root = Path(models_dir).expanduser()
    files: list[Path] = []
    for key in GGUF_MODEL_KEYS:
        raw = llm_env.get(key, "")
        if raw:
            files.append(root / Path(raw).name)
    return files


def check_models() -> dict[str, list[dict[str, Any]]]:
    storage_env = _read_env(STORAGE_ENV)
    llm_env = _read_env(LLM_ENV)
    checks: dict[str, list[dict[str, Any]]] = {"env": [], "ollama": [], "gguf": [], "vllm": []}
    checks["env"].append(item("storage-env", bool(storage_env), ".env.storage.generated present" if storage_env else "run make infra"))
    checks["env"].append(item("llm-env", bool(llm_env), ".env.llm.generated present" if llm_env else "run make infra"))

    expected_ollama = _expected_ollama_models(llm_env)
    installed = _ollama_models()
    if installed is None:
        checks["ollama"].append(item("ollama-cli", False, "ollama not installed; install Ollama or use llama.cpp/vLLM profiles", severity="warning"))
    elif not installed:
        checks["ollama"].append(item("ollama-api", False, "ollama list returned no models or failed", severity="warning"))
    for model in expected_ollama:
        if installed is None:
            checks["ollama"].append(item(f"ollama:{model}", False, "ollama unavailable", severity="warning"))
        else:
            checks["ollama"].append(item(f"ollama:{model}", model in installed, "installed" if model in installed else "missing", severity="warning"))

    for path in _expected_gguf_files(storage_env, llm_env):
        exists = path.exists() and path.stat().st_size > 100_000_000
        checks["gguf"].append(item(f"gguf:{path.name}", exists, str(path) if exists else f"missing or incomplete: {path}", severity="warning"))

    vllm_model = llm_env.get("VLLM_MODEL", "")
    if vllm_model:
        checks["vllm"].append(
            item(
                "vllm-model",
                True,
                f"{vllm_model} is downloaded by the vLLM container into HF cache on first start",
                severity="info",
            )
        )
    return checks


def pull_ollama_models(models: list[str]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not shutil.which("ollama"):
        return [item("ollama-pull", False, "ollama not installed", severity="error")]
    for model in models:
        rc, out = _run(["ollama", "pull", model], timeout=1800)
        actions.append(item(f"ollama-pull:{model}", rc == 0, out or "pulled", severity="error"))
    return actions


def download_gguf() -> list[dict[str, Any]]:
    script = ROOT / "infra" / "docker" / "scripts" / "download-llm-models.sh"
    rc, out = _run([str(script), "--all"], timeout=7200)
    return [item("download-gguf", rc == 0, out or "downloaded", severity="error")]


def build_payload(*, pull_ollama: bool = False, download_gguf_models: bool = False) -> dict[str, Any]:
    storage_env = _read_env(STORAGE_ENV)
    llm_env = _read_env(LLM_ENV)
    actions: list[dict[str, Any]] = []
    if pull_ollama:
        actions.extend(pull_ollama_models(_expected_ollama_models(llm_env)))
    if download_gguf_models:
        os.environ.update(storage_env)
        actions.extend(download_gguf())
    checks = check_models()
    all_checks = [check for group in checks.values() for check in group]
    payload = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actions": actions,
        "checks": checks,
    }
    payload["ok"] = all(check["ok"] or check["severity"] in {"warning", "info"} for check in all_checks + actions)
    return payload


def write_report(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_text(payload: dict[str, Any]) -> None:
    print("== ai-local model readiness ==")
    for group, checks in payload["checks"].items():
        print(f"\n== {group} ==")
        for check in checks:
            marker = "OK" if check["ok"] else ("WARN" if check["severity"] == "warning" else "FAIL")
            print(f"{marker:4} {check['name']}: {check['message']}")
    if payload["actions"]:
        print("\n== actions ==")
        for action in payload["actions"]:
            marker = "OK" if action["ok"] else ("WARN" if action["severity"] == "warning" else "FAIL")
            print(f"{marker:4} {action['name']}: {action['message']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-ollama", action="store_true", help="Pull expected Ollama models.")
    parser.add_argument("--download-gguf", action="store_true", help="Download expected llama.cpp GGUF models.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-report", nargs="?", const=str(DEFAULT_REPORT), metavar="PATH")
    args = parser.parse_args(argv)

    payload = build_payload(pull_ollama=args.pull_ollama, download_gguf_models=args.download_gguf)
    if args.write_report:
        write_report(payload, Path(args.write_report))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_text(payload)
        if args.write_report:
            print(f"\nGenerated: {args.write_report}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
