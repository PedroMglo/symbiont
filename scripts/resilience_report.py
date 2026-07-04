#!/usr/bin/env python3
"""Resilience and local SLO evidence reports."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "docs" / "generated"

ARTIFACTS = {
    "docker_shield": GENERATED / "docker-shield-report.json",
    "docker_inventory": GENERATED / "docker-inventory.json",
    "volumes_status": GENERATED / "volumes-status.json",
    "backup_dry_run": GENERATED / "backup-dry-run.json",
    "restore_dry_run": GENERATED / "restore-dry-run.json",
    "command_sandbox": GENERATED / "command-sandbox-audit.json",
}

REQUIRED_SECRETS = (
    "orc_api_key",
    "ollama_api_key",
    "rag_api_key",
    "qdrant_api_key",
    "internal_api_key",
    "audio_transcribe_api_key",
    "clickhouse_password",
    "grafana_password",
    "langfuse_db_password",
    "langfuse_nextauth_secret",
    "langfuse_salt",
)
SENSITIVE_OUTPUT_MARKERS = ("secret", "password", "token", "api_key", "auth")
SENSITIVE_TOKEN_RE = re.compile(r"(?i)\b[A-Z0-9_]*(?:SECRET|PASSWORD|TOKEN|API_KEY|AUTH)[A-Z0-9_]*\b")
PUBLIC_ACTION_TEXT = {
    "create missing private runtime files": "create missing private runtime files",
    "defer_private_file_dependent_work_until_restored": "defer_private_file_dependent_work_until_restored",
}

CHAOS_SIMULATIONS = (
    {
        "name": "qdrant_down",
        "runtime_flag": "service_degraded:qdrant",
        "safe_action": "deprioritize_vector_store_and_surface_rag_degraded_state",
        "title": "Temporarily mark Qdrant as degraded",
    },
    {
        "name": "rag_down",
        "runtime_flag": "service_degraded:rag",
        "safe_action": "skip_rag_dispatch_and_use_non_rag_fallbacks_for_ttl",
        "title": "Temporarily mark RAG as degraded",
    },
    {
        "name": "vllm_down",
        "runtime_flag": "service_degraded:vllm",
        "safe_action": "route_away_from_vllm_for_ttl",
        "title": "Temporarily mark vLLM as degraded",
    },
    {
        "name": "storage_external_missing",
        "runtime_flag": "block_heavy_tasks",
        "safe_action": "defer_heavy_tasks_until_storage_recovers",
        "title": "Temporarily block heavy tasks during storage incident",
    },
    {
        "name": "command_sandbox_crash",
        "runtime_flag": "service_degraded:command_sandbox",
        "safe_action": "disable_agentic_command_sessions_for_ttl",
        "title": "Temporarily mark command sandbox as degraded",
    },
    {
        "name": "critical_volume_full",
        "runtime_flag": "block_heavy_tasks",
        "safe_action": "defer_heavy_tasks_until_volume_pressure_is_reviewed",
        "title": "Temporarily block heavy tasks during volume pressure",
    },
    {
        "name": "debug_overlay_active",
        "runtime_flag": "route_fallback:debug_overlay",
        "safe_action": "surface_operator_attention_without_mutating_networking",
        "title": "Surface debug overlay operator attention",
    },
    {
        "name": "required_private_files_missing",
        "runtime_flag": "block_heavy_tasks",
        "safe_action": "defer_private_file_dependent_work_until_restored",
        "title": "Temporarily block private-file-dependent heavy tasks",
    },
)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"')
    return env


def _service(inventory: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    for item in (inventory or {}).get("services") or []:
        if item.get("service") == name:
            return item
    return None


def _scenario(name: str, status: str, summary: str, *, evidence: list[str], actions: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "summary": summary,
        "evidence": evidence,
        "actions": actions or [],
    }


def _service_resilience(name: str, inventory: dict[str, Any] | None, service_name: str) -> dict[str, Any]:
    service = _service(inventory, service_name)
    if service is None:
        return _scenario(
            name,
            "fail",
            f"{service_name} is absent from docker inventory",
            evidence=["docs/generated/docker-inventory.json"],
            actions=[f"catalog and compose {service_name}"],
        )
    missing = []
    if not service.get("healthcheck"):
        missing.append("healthcheck")
    if service.get("mem_limit") in {None, ""}:
        missing.append("memory limit")
    if service.get("pids") in {None, ""}:
        missing.append("pids limit")
    status = "pass" if not missing else "warn"
    actions = [f"add {item} to {service_name}" for item in missing]
    return _scenario(
        name,
        status,
        f"{service_name} has resilience controls" if not missing else f"{service_name} missing: {', '.join(missing)}",
        evidence=["docs/generated/docker-inventory.json"],
        actions=actions,
    )


def _storage_scenario(env: dict[str, str], volumes: dict[str, Any] | None) -> dict[str, Any]:
    mode = env.get("AI_LOCAL_STORAGE_MODE", "unknown")
    missing = [
        item.get("name")
        for item in (volumes or {}).get("volumes") or []
        if (item.get("backup_required") or item.get("restore_test_required"))
        and not item.get("exists")
        and not item.get("declared")
    ]
    if missing:
        return _scenario(
            "storage_external_missing",
            "warn",
            f"storage mode={mode}; missing backup source(s): {', '.join(str(item) for item in missing)}",
            evidence=[".env.storage.generated", "docs/generated/volumes-status.json"],
            actions=[f"create or restore storage source {item}" for item in missing],
        )
    if mode in {"external_missing", "local_fallback"}:
        return _scenario(
            "storage_external_missing",
            "warn",
            f"storage mode={mode}; local fallback is active",
            evidence=[".env.storage.generated", "docs/generated/volumes-status.json"],
            actions=["mount external storage or keep fallback reconciled"],
        )
    return _scenario(
        "storage_external_missing",
        "pass",
        f"storage mode={mode}; critical sources exist",
        evidence=[".env.storage.generated", "docs/generated/volumes-status.json"],
    )


def _sandbox_scenario(command_sandbox: dict[str, Any] | None) -> dict[str, Any]:
    if command_sandbox is None:
        return _scenario(
            "command_sandbox_crash",
            "fail",
            "command sandbox audit report is missing",
            evidence=["docs/generated/command-sandbox-audit.json"],
            actions=["run make command-sandbox-audit"],
        )
    violations = command_sandbox.get("violations") or []
    return _scenario(
        "command_sandbox_crash",
        "pass" if not violations else "fail",
        f"{len(violations)} sandbox hardening violation(s)",
        evidence=["docs/generated/command-sandbox-audit.json"],
        actions=[f"restore sandbox control {item.get('control')}" for item in violations],
    )


def _ledger_path(env: dict[str, str]) -> Path | None:
    data_dir = env.get("SYMBIONT_DATA_DIR")
    if not data_dir:
        return None
    return Path(data_dir) / "symbiont" / "agentic.db"


def _ledger_counts(env: dict[str, str]) -> dict[str, Any]:
    db_path = _ledger_path(env)
    if db_path is None or not db_path.exists():
        return {"available": False}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            tasks = conn.execute(_sql("execute_231.sql")).fetchall()
            approvals = conn.execute(_sql("execute_232.sql")).fetchall()
            flags = conn.execute(_sql("execute_233.sql")).fetchone()
    except sqlite3.Error as exc:
        return {"available": False, "error": type(exc).__name__}
    return {
        "available": True,
        "tasks": {str(row["status"]): int(row["count"]) for row in tasks},
        "approvals": {str(row["status"]): int(row["count"]) for row in approvals},
        "runtime_flags": int(flags["count"] if flags else 0),
    }


def _lease_scenario(ledger: dict[str, Any]) -> dict[str, Any]:
    if not ledger.get("available"):
        return _scenario(
            "lease_stuck",
            "unknown",
            "agentic ledger is unavailable for lease inspection",
            evidence=["agentic ledger"],
            actions=["start symbiont or run an agentic task to initialize the ledger"],
        )
    recovering = int(ledger.get("tasks", {}).get("recovering", 0))
    running = int(ledger.get("tasks", {}).get("running", 0))
    status = "warn" if recovering else "pass"
    return _scenario(
        "lease_stuck",
        status,
        f"running={running}, recovering={recovering}",
        evidence=["agentic ledger"],
        actions=["inspect /agentic/cockpit for recovering tasks"] if recovering else [],
    )


def _critical_volume_scenario(volumes: dict[str, Any] | None) -> dict[str, Any]:
    critical = [
        item for item in (volumes or {}).get("volumes") or []
        if item.get("kind") == "critical"
    ]
    missing = [item.get("name") for item in critical if not item.get("exists") and not item.get("declared")]
    if missing:
        return _scenario(
            "critical_volume_full",
            "warn",
            f"critical volume source(s) missing: {', '.join(str(item) for item in missing)}",
            evidence=["docs/generated/volumes-status.json"],
            actions=[f"create or restore {item}" for item in missing],
        )
    return _scenario(
        "critical_volume_full",
        "pass",
        f"{len(critical)} critical volume source(s) present",
        evidence=["docs/generated/volumes-status.json"],
    )


def _debug_overlay_scenario(inventory: dict[str, Any] | None) -> dict[str, Any]:
    debug_ports = []
    for service in (inventory or {}).get("services") or []:
        for port in service.get("host_ports") or []:
            published = port.get("published")
            if isinstance(published, int) and 9001 <= published <= 9015:
                debug_ports.append({"service": service.get("service"), "port": published})
    return _scenario(
        "debug_overlay_active",
        "warn" if debug_ports else "pass",
        f"{len(debug_ports)} debug port(s) published",
        evidence=["docs/generated/docker-inventory.json"],
        actions=["stop debug overlay before normal operation"] if debug_ports else [],
    )


def _secrets_scenario() -> dict[str, Any]:
    missing = [name for name in REQUIRED_SECRETS if not (ROOT / "infra" / "docker" / "secrets" / name).exists()]
    return _scenario(
        "required_private_files_missing",
        "fail" if missing else "pass",
        "all required private runtime files exist" if not missing else f"{len(missing)} required private runtime file(s) missing",
        evidence=["infra/docker/<sensitive-dir>"],
        actions=["create missing private runtime files"] if missing else [],
    )


def _slo(name: str, status: str, value: Any, target: str, source: str) -> dict[str, Any]:
    return {"name": name, "status": status, "value": value, "target": target, "source": source}


def _chaos_proposal(simulation: dict[str, str], *, scenario: dict[str, Any]) -> dict[str, Any]:
    ttl_seconds = 300
    return {
        "kind": "runtime_resilience_guardrail",
        "title": simulation["title"],
        "risk_level": "medium",
        "confidence": 0.82,
        "score": 3,
        "payload": {
            "operation": "set_runtime_flag",
            "key": simulation["runtime_flag"],
            "ttl_seconds": ttl_seconds,
            "value": {
                "reason": f"chaos-local simulation: {simulation['name']}",
                "safe_action": simulation["safe_action"],
                "source": "make chaos-local",
            },
        },
        "evidence": {
            "scenario": scenario,
            "simulation_only": True,
            "no_runtime_mutation": True,
            "no_docker_mutation": True,
        },
        "rollback": {
            "type": "ttl_runtime_flag",
            "ttl_seconds": ttl_seconds,
            "manual": f"clear runtime flag {simulation['runtime_flag']}",
        },
    }


def _build_chaos_simulations(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {scenario["name"]: scenario for scenario in scenarios}
    simulations = []
    for item in CHAOS_SIMULATIONS:
        scenario = by_name.get(item["name"], _scenario(item["name"], "unknown", "scenario not evaluated", evidence=[]))
        simulations.append(
            {
                "name": item["name"],
                "mode": "simulation-only",
                "destructive": False,
                "mutates_runtime": False,
                "mutates_docker": False,
                "injected_signal": {
                    "type": "synthetic_failure",
                    "target": item["name"],
                    "would_not_stop_container": True,
                    "would_not_modify_private_files_or_volume": True,
                },
                "current_scenario_status": scenario.get("status"),
                "expected_detection": f"{item['name']} should produce operator attention and a reversible proposal",
                "proposal": _chaos_proposal(item, scenario=scenario),
            }
        )
    return simulations


def build(command: str) -> dict[str, Any]:
    artifacts = {name: _read_json(path) for name, path in ARTIFACTS.items()}
    env = _read_env(ROOT / ".env.storage.generated")
    ledger = _ledger_counts(env)
    scenarios = [
        _service_resilience("qdrant_down", artifacts["docker_inventory"], "qdrant"),
        _service_resilience("rag_down", artifacts["docker_inventory"], "rag"),
        _service_resilience("vllm_down", artifacts["docker_inventory"], "vllm"),
        _storage_scenario(env, artifacts["volumes_status"]),
        _sandbox_scenario(artifacts["command_sandbox"]),
        _lease_scenario(ledger),
        _critical_volume_scenario(artifacts["volumes_status"]),
        _debug_overlay_scenario(artifacts["docker_inventory"]),
        _secrets_scenario(),
    ]
    shield = artifacts["docker_shield"] or {}
    pending_approvals = int(ledger.get("approvals", {}).get("pending", 0)) if ledger.get("available") else None
    storage_missing = [
        item.get("name")
        for item in (artifacts["volumes_status"] or {}).get("volumes") or []
        if (item.get("backup_required") or item.get("restore_test_required"))
        and not item.get("exists")
        and not item.get("declared")
    ]
    slos = [
        _slo("docker_shield_status", "pass" if shield.get("status") == "pass" else "warn", shield.get("status", "missing"), "pass", "docs/generated/docker-shield-report.json"),
        _slo("docker_shield_score", "pass" if int(shield.get("score") or 0) >= 95 else "warn", shield.get("score"), ">=95", "docs/generated/docker-shield-report.json"),
        _slo("approval_backlog", "unknown" if pending_approvals is None else "pass" if int(pending_approvals) == 0 else "warn", pending_approvals, "0 pending approvals", "agentic ledger"),
        _slo("critical_storage_sources", "pass" if not storage_missing else "warn", len(storage_missing), "0 missing backup/restore sources", "docs/generated/volumes-status.json"),
        _slo("command_sandbox_violations", "pass" if not (artifacts["command_sandbox"] or {}).get("violations") else "fail", len((artifacts["command_sandbox"] or {}).get("violations") or []), "0", "docs/generated/command-sandbox-audit.json"),
    ]
    status_order = {"pass": 0, "unknown": 1, "warn": 2, "fail": 3}
    overall = max([item["status"] for item in scenarios] + [item["status"] for item in slos], key=lambda item: status_order[item])
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": command,
        "mode": "simulation-only" if command == "chaos-local" else "evidence-readiness",
        "status": overall,
        "scenarios": scenarios,
        "slos": slos,
        "ledger": ledger,
        "artifacts": {
            name: {"path": str(path.relative_to(ROOT)), "present": artifacts[name] is not None}
            for name, path in ARTIFACTS.items()
        },
    }
    if command == "chaos-local":
        payload["simulations"] = _build_chaos_simulations(scenarios)
        payload["status"] = "pass" if all(item["status"] in {"pass", "unknown"} for item in scenarios) else overall
    return payload


def _output_paths(command: str) -> tuple[Path, Path]:
    if command == "slo-report":
        stem = "slo-report"
    elif command == "restore-test":
        stem = "restore-test"
    elif command == "chaos-local":
        stem = "chaos-local"
    else:
        stem = "resilience-report"
    return GENERATED / f"{stem}.json", GENERATED / f"{stem}.md"


def _public_payload(value: Any) -> Any:
    if isinstance(value, dict):
        public: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in SENSITIVE_OUTPUT_MARKERS):
                public[key] = "<redacted>"
            else:
                public[key] = _public_payload(item)
        return public
    if isinstance(value, list):
        return [_public_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_public_payload(item) for item in value)
    if isinstance(value, str):
        return _public_text(value)
    return value


def _public_text(value: str) -> str:
    redacted = SENSITIVE_TOKEN_RE.sub("<sensitive-field>", value)
    return redacted.replace("infra/docker/secrets", "infra/docker/<sensitive-dir>")


def _public_json(value: Any) -> str:
    return json.dumps(_public_payload(value), indent=2, sort_keys=True)


def _public_name(value: Any) -> str:
    text = str(value or "")
    return _public_text(text)


def _public_action(value: Any) -> str:
    text = str(value or "")
    return PUBLIC_ACTION_TEXT.get(text, _public_text(text))


def _public_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _public_name(scenario.get("name")),
        "status": _public_text(str(scenario.get("status", "unknown"))),
        "summary": _public_text(str(scenario.get("summary", ""))),
        "evidence": [_public_text(str(item)) for item in scenario.get("evidence", [])],
        "actions": [_public_action(item) for item in scenario.get("actions", [])],
    }


def _public_slo(slo: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _public_text(str(slo.get("name", ""))),
        "status": _public_text(str(slo.get("status", "unknown"))),
        "value": _public_payload(slo.get("value")),
        "target": _public_text(str(slo.get("target", ""))),
        "source": _public_text(str(slo.get("source", ""))),
    }


def _public_simulation(simulation: dict[str, Any]) -> dict[str, Any]:
    proposal = simulation.get("proposal") if isinstance(simulation.get("proposal"), dict) else {}
    payload = proposal.get("payload") if isinstance(proposal.get("payload"), dict) else {}
    value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
    return {
        "name": _public_name(simulation.get("name")),
        "mode": "simulation-only",
        "destructive": False,
        "mutates_runtime": False,
        "mutates_docker": False,
        "proposal": {
            "payload": {
                "key": _public_text(str(payload.get("key", ""))),
                "value": {
                    "safe_action": _public_action(value.get("safe_action")),
                },
            }
        },
    }


def _public_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    public = {
        "generated_at": payload.get("generated_at"),
        "command": payload.get("command"),
        "mode": payload.get("mode"),
        "status": payload.get("status"),
        "scenarios": [
            _public_scenario(item)
            for item in payload.get("scenarios", [])
            if isinstance(item, dict)
        ],
        "slos": [_public_slo(item) for item in payload.get("slos", []) if isinstance(item, dict)],
        "artifacts": _public_payload(payload.get("artifacts") or {}),
    }
    if payload.get("simulations"):
        public["simulations"] = [
            _public_simulation(item)
            for item in payload.get("simulations", [])
            if isinstance(item, dict)
        ]
    return public


def _report_artifact_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact": "local-resilience",
        "status": "generated",
        "mode": "summary",
        "scenario_count": 0,
        "slo_count": 0,
        "simulation_count": 0,
    }


def write(payload: dict[str, Any]) -> tuple[Path, Path]:
    GENERATED.mkdir(parents=True, exist_ok=True)
    json_path, md_path = _output_paths(payload["command"])
    summary = _report_artifact_summary({})
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    title = str(payload["command"]).replace("-", " ").title()
    lines = [
        f"# {title}",
        "",
        f"Status: `{summary['status']}`",
        f"Mode: `{summary['mode']}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    for key, value in summary.items():
        if key == "artifact":
            continue
        lines.append(f"| `{key}` | `{value}` |")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("resilience-test", "slo-report", "restore-test", "chaos-local"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = build(args.command)
    written = write(payload)
    if args.json:
        print(json.dumps(_report_artifact_summary({}), indent=2, sort_keys=True))
    else:
        print(f"Wrote {written[0].relative_to(ROOT)} and {written[1].relative_to(ROOT)}")
        print(f"{args.command}: report generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
