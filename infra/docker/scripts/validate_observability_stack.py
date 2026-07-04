#!/usr/bin/env python3
"""Validate the local observability stack wiring.

This is an infra-level check: it verifies Compose wiring, collector sinks,
Grafana dashboards and cross-owner correlation contracts without importing
service/domain code from orchestrator, RAG, storage, agents or features.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
COLLECTOR_CONFIG = ROOT / "infra" / "docker" / "otel" / "otel-collector-config.yaml"
DATASOURCE = ROOT / "infra" / "docker" / "grafana" / "provisioning" / "datasources" / "clickhouse.yaml"
DASHBOARD_PROVISIONING = ROOT / "infra" / "docker" / "grafana" / "provisioning" / "dashboards" / "dashboard.yaml"
DASHBOARD_DIR = ROOT / "infra" / "docker" / "grafana" / "dashboards"
RUNBOOK = ROOT / "infra" / "docker" / "OBSERVABILITY_RUNBOOK.md"
SEMANTIC_ATTRIBUTES = ROOT / "orchestrator" / "observability" / "semantic_attributes.py"
CAPABILITY_MANIFESTS = (
    ROOT / "agents" / "service_capabilities.toml",
    ROOT / "features" / "service_capabilities.toml",
    ROOT / "storage_guardian" / "service_capabilities.toml",
    ROOT / "obsidian-rag" / "service_capabilities.toml",
)
OBSERVABILITY_SERVICES = {
    "clickhouse",
    "grafana",
    "otel-collector",
    "langfuse-db",
    "langfuse",
}
REQUIRED_AI_LOCAL_ATTRS = {
    "ai.local.owner",
    "ai.local.component",
    "ai.local.trace_kind",
    "ai.local.request_id",
    "ai.local.session_id",
    "ai.local.task_id",
    "ai.local.run_id",
    "ai.local.capability_id",
    "ai.local.resource_lease_id",
}
DEGRADED_STATES = ("ready", "degraded", "blocked", "stale")


@dataclass(frozen=True)
class Finding:
    rule: str
    subject: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "subject": self.subject, "message": self.message}


@dataclass
class ValidationResult:
    errors: list[Finding]
    warnings: list[Finding]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, rule: str, subject: str, message: str) -> None:
        self.errors.append(Finding(rule, subject, message))

    def warning(self, rule: str, subject: str, message: str) -> None:
        self.warnings.append(Finding(rule, subject, message))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(_read(path))


def _dashboard_sql(path: Path) -> str:
    dashboard = _load_json(path)
    targets = []
    for panel in dashboard.get("panels", []):
        targets.extend(panel.get("targets", []))
    return "\n".join(str(target.get("rawSql", "")) for target in targets)


def _toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _compose_config() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from scripts import docker_policy  # pylint: disable=import-outside-toplevel

    return docker_policy.compose_config(docker_policy.load_catalog())


def _labels(service: dict[str, Any]) -> dict[str, str]:
    labels = service.get("labels") or {}
    if isinstance(labels, dict):
        return {str(key): str(value) for key, value in labels.items()}
    parsed: dict[str, str] = {}
    if isinstance(labels, list):
        for label in labels:
            key, _, value = str(label).partition("=")
            parsed[key] = value
    return parsed


def validate_compose(compose: dict[str, Any], result: ValidationResult) -> None:
    services = compose.get("services") or {}
    for service_name in sorted(OBSERVABILITY_SERVICES):
        service = services.get(service_name)
        if not isinstance(service, dict):
            result.error("compose.service_missing", service_name, "observability service is missing from Compose")
            continue
        profiles = {str(profile) for profile in service.get("profiles") or []}
        if profiles != {"observability"}:
            result.error("compose.profile", service_name, f"expected observability profile only, got {sorted(profiles)}")
        labels = _labels(service)
        if labels.get("ai.local.component") != "observability" or labels.get("ai.local.owner") != "observability":
            result.error("compose.labels", service_name, "service must be labeled as observability-owned")
        if not service.get("healthcheck"):
            result.error("compose.healthcheck", service_name, "observability service must expose a healthcheck")
        if service_name in {"clickhouse", "grafana", "langfuse", "langfuse-db"} and not service.get("secrets"):
            result.error("compose.secrets", service_name, "stateful/UI observability service must consume Docker secrets")
    otel = services.get("otel-collector") or {}
    ports = json.dumps(otel.get("ports") or [])
    for port in ("4317", "4318", "8888"):
        if port not in ports:
            result.error("compose.otel_port", "otel-collector", f"missing expected collector port {port}")


def validate_collector(result: ValidationResult) -> None:
    text = _read(COLLECTOR_CONFIG)
    required = (
        "receivers:",
        "otlp:",
        "grpc:",
        "http:",
        "exporters:",
        "clickhouse:",
        "database: ai_symbiont",
        "traces_table_name: otel_traces",
        "metrics_table_name: otel_metrics",
        "logs_table_name: otel_logs",
        "traces:",
        "metrics:",
        "logs:",
        "exporters: [clickhouse]",
    )
    for marker in required:
        if marker not in text:
            result.error("collector.marker_missing", COLLECTOR_CONFIG.name, f"missing {marker!r}")


def validate_grafana(result: ValidationResult) -> None:
    datasource = _read(DATASOURCE)
    if "uid: clickhouse" not in datasource or "type: grafana-clickhouse-datasource" not in datasource:
        result.error("grafana.datasource", DATASOURCE.name, "ClickHouse datasource must be provisioned with uid clickhouse")
    provisioning = _read(DASHBOARD_PROVISIONING)
    if "/var/lib/grafana/dashboards" not in provisioning:
        result.error("grafana.provisioning", DASHBOARD_PROVISIONING.name, "dashboard provisioning path is missing")
    dashboards = {
        "otel-dispatch-migration.json": ("ai_symbiont.otel_traces", "SpanAttributes['ai.local.owner'] = 'orchestrator'"),
        "otel-rag-migration.json": ("ai_symbiont.otel_traces", "SpanAttributes['ai.local.owner'] = 'obsidian-rag'"),
    }
    for filename, markers in dashboards.items():
        path = DASHBOARD_DIR / filename
        sql = _dashboard_sql(path)
        for marker in markers:
            if marker not in sql:
                result.error("grafana.dashboard_query", filename, f"missing query marker {marker!r}")


def validate_correlation_contracts(result: ValidationResult) -> None:
    semantic_text = _read(SEMANTIC_ATTRIBUTES)
    for attr in sorted(REQUIRED_AI_LOCAL_ATTRS):
        if attr not in semantic_text:
            result.error("correlation.semantic_attr", SEMANTIC_ATTRIBUTES.name, f"missing {attr}")
    for manifest in CAPABILITY_MANIFESTS:
        data = _toml(manifest)
        services = data.get("service_capabilities") or []
        if not services:
            result.error("correlation.manifest", manifest.as_posix(), "capability manifest has no services")
            continue
        for entry in services:
            name = entry.get("service_name", "<unknown>")
            events = entry.get("events_published") or []
            if "service.degraded" not in events:
                result.error("correlation.degraded_event", f"{_display_path(manifest)}:{name}", "service must publish service.degraded")


def validate_runbook(result: ValidationResult) -> None:
    text = _read(RUNBOOK)
    for marker in ("OTEL collector", "Grafana", "Langfuse", "ClickHouse", "correlation ids"):
        if marker not in text:
            result.error("runbook.marker_missing", RUNBOOK.name, f"missing {marker!r}")
    for state in DEGRADED_STATES:
        if re.search(rf"\b{re.escape(state)}\b", text) is None:
            result.error("runbook.state_missing", RUNBOOK.name, f"missing degraded-state term {state!r}")


def result_payload(result: ValidationResult) -> dict[str, Any]:
    return {
        "status": "pass" if result.ok else "fail",
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "errors": [finding.to_dict() for finding in result.errors],
        "warnings": [finding.to_dict() for finding in result.warnings],
    }


def print_human(result: ValidationResult) -> None:
    print(
        "Observability stack: "
        f"{'pass' if result.ok else 'fail'} "
        f"({len(result.errors)} error(s), {len(result.warnings)} warning(s))"
    )
    for finding in result.errors:
        print(f"ERROR {finding.rule} {finding.subject}: {finding.message}")
    for finding in result.warnings:
        print(f"WARN {finding.rule} {finding.subject}: {finding.message}")


def main(argv: list[str] | None = None) -> int:
    json_mode = bool(argv and "--json" in argv)
    result = ValidationResult([], [])
    try:
        compose = _compose_config()
    except SystemExit as exc:
        result.error("compose.config_failed", "docker compose config", str(exc))
        compose = {}
    validate_compose(compose, result)
    validate_collector(result)
    validate_grafana(result)
    validate_correlation_contracts(result)
    validate_runbook(result)
    if json_mode:
        print(json.dumps(result_payload(result), indent=2, sort_keys=True))
    else:
        print_human(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
