#!/usr/bin/env python3
"""Bootstrap preflight for a Stack-only ai-local checkout.

The canonical installation layout contains ai-local-stack plus immutable owner
images. Runtime owner repositories are not required beside the Stack checkout.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = ROOT / ".local" / "generated" / "bootstrap.report.json"
_SYSTEM_HELPER = Path(__file__).with_name("new_user_bootstrap_system.py")


def _load_system_helper() -> Any:
    spec = importlib.util.spec_from_file_location("ai_local_stack_system_bootstrap", _SYSTEM_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load system bootstrap helper from {_SYSTEM_HELPER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(name: str, ok: bool, message: str, *, severity: str = "error") -> dict[str, Any]:
    return {"name": name, "ok": ok, "message": message, "severity": severity, "data": None}


def check_stack_checkout() -> list[dict[str, Any]]:
    required = ("compose", "config", "infra", "postgres", "scripts")
    owner_source_dirs = ("agents", "features", "obsidian-rag", "orchestrator", "storage_guardian")
    checks = [_item(".gitmodules", not (ROOT / ".gitmodules").exists(), "absent" if not (ROOT / ".gitmodules").exists() else "remove submodule metadata")]
    for name in required:
        path = ROOT / name
        checks.append(_item(f"stack:{name}", path.is_dir(), "present" if path.is_dir() else f"missing {name}/"))
    present_owner_dirs = [name for name in owner_source_dirs if (ROOT / name).exists()]
    checks.append(_item("owner-source-checkouts", not present_owner_dirs, "not required; runtimes come from immutable images" if not present_owner_dirs else "unexpected owner source directories in Stack checkout: " + ", ".join(present_owner_dirs)))
    lock = ROOT / "config" / "docker" / "released-images.lock.json"
    checks.append(_item("released-image-lock", lock.is_file(), "present" if lock.is_file() else "not generated yet; runtime commands remain fail-closed until approved owner images are published", severity="warning"))
    return checks


def build_payload(helper: Any) -> dict[str, Any]:
    distro = helper._os_release()
    checks = {"host": helper.check_host_prereqs(), "stack-checkout": check_stack_checkout(), "disk": helper.check_disk_space(), "sharedai": helper.check_sharedai()}
    all_checks = [check for group in checks.values() for check in group]
    payload = {"schema_version": 2, "layout_contract": "ai-local.stack-only-checkout.v1", "platform": helper.platform.platform(), "distro": distro, "install_hint": helper._install_hint(distro), "ollama_install_hint": helper._ollama_install_hint(), "system_install_plan": helper._system_install_plan(distro), "actions": [], "checks": checks}
    payload["ok"] = all(check["ok"] or check["severity"] in {"warning", "info"} for check in all_checks)
    return payload


def _write_report(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_text(payload: dict[str, Any]) -> None:
    print("== ai-local Stack-only bootstrap preflight ==")
    print(f"Platform: {payload['platform']}")
    for group, checks in payload["checks"].items():
        print(f"\n== {group} ==")
        for check in checks:
            marker = "OK" if check["ok"] else ("WARN" if check["severity"] == "warning" else "FAIL")
            print(f"{marker:4} {check['name']}: {check['message']}")


def main() -> int:
    helper = _load_system_helper()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print-install-command", action="store_true")
    parser.add_argument("--install-system", action="store_true")
    parser.add_argument("--write-report", nargs="?", const=str(DEFAULT_REPORT), metavar="PATH")
    args = parser.parse_args()
    payload = build_payload(helper)
    if args.print_install_command:
        print(payload["system_install_plan"])
        return 0
    if args.install_system:
        actions = helper.install_system_prereqs(payload["distro"])
        payload = build_payload(helper)
        payload["actions"] = actions
        all_checks = [check for group in payload["checks"].values() for check in group]
        payload["ok"] = all(check["ok"] or check["severity"] in {"warning", "info"} for check in all_checks + actions)
    if args.write_report:
        _write_report(payload, Path(args.write_report))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_text(payload)
        if args.write_report:
            print(f"\nGenerated: {args.write_report}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
