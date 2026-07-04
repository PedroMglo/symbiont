"""Diagnostic bundle projection for material execution sessions.

Diagnostics are derived evidence. They must not become an independent runtime
state store or a policy source.
"""

from __future__ import annotations

from typing import Any

from material_execution_kernel.types import MaterialEvent, MaterialManifest


def build_diagnostics_bundle(
    *,
    session_id: str,
    raw: dict[str, Any],
    events: list[MaterialEvent],
) -> dict[str, Any]:
    manifest: MaterialManifest = raw["manifest"]
    event_dicts = [event.model_dump(mode="json") for event in events]
    manifest_dict = manifest.model_dump(mode="json")
    return {
        "schema_version": "material_diagnostics.v3.2",
        "session": _session(raw, manifest),
        "events": event_dicts,
        "manifest": manifest_dict,
        "interface_ledger": manifest_dict.get("interface_ledger") or {},
        "repair_obligations": manifest_dict.get("repair_obligations") or [],
        "repair_arbiter": manifest_dict.get("repair_arbiter") or {},
        "requirements_trace": manifest_dict.get("requirements_trace") or [],
        "contract_comparison": manifest_dict.get("contract_comparison") or {},
        "issue_target_decisions": _issue_target_decisions(manifest_dict),
        "patch_rejection_history": _patch_rejection_history(manifest_dict, event_dicts),
        "dependency_policy": _dependency_policy(manifest_dict),
        "validation_applicability_decisions": _validation_applicability_decisions(manifest_dict, event_dicts),
        "model_lane_metrics": _model_lane_metrics(event_dicts),
        "command_runs": [item.model_dump(mode="json") for item in raw.get("command_runs", [])],
        "background_services": _background_services(event_dicts),
        "policy_decisions": _events_by_prefix(event_dicts, "material.policy."),
        "security_decisions": _security_decisions(manifest_dict, event_dicts),
        "artifact": manifest_dict.get("artifact") or {},
        "final_summary": _final_summary(raw, manifest, event_dicts),
    }


def _session(raw: dict[str, Any], manifest: MaterialManifest) -> dict[str, Any]:
    return {
        "session_id": manifest.session_id,
        "task_id": manifest.task_id,
        "trace_id": manifest.trace_id,
        "status": raw.get("status"),
        "diagnostics_ref": raw.get("diagnostics_ref"),
        "runtime_limits": raw.get("runtime_limits") or {},
    }


def _issue_target_decisions(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for issue in manifest.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        decisions.append(
            {
                "issue_id": issue.get("issue_id"),
                "issue_type": issue.get("issue_type"),
                "target_kind": issue.get("target_kind"),
                "target_path": issue.get("target_path"),
                "target_resolution": issue.get("target_resolution"),
                "requirement_refs": issue.get("requirement_refs") or [],
                "contract_refs": issue.get("contract_refs") or [],
            }
        )
    return decisions


def _patch_rejection_history(manifest: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for issue in manifest.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        history.extend(item for item in issue.get("patch_rejections") or [] if isinstance(item, dict))
    for event in events:
        if str(event.get("event_type") or "") in {
            "material.patch_set.rejected",
            "material.repair.rejected",
        }:
            history.append(
                {
                    "event_id": event.get("event_id"),
                    "event_type": event.get("event_type"),
                    "payload": event.get("payload") or {},
                }
            )
    return history


def _dependency_policy(manifest: dict[str, Any]) -> dict[str, Any]:
    contract = manifest.get("material_contract") if isinstance(manifest.get("material_contract"), dict) else {}
    return {
        "dependency_policy": contract.get("dependency_policy") or {},
        "dependency_strategy": contract.get("dependency_strategy") or {},
    }


def _validation_applicability_decisions(
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for validation in manifest.get("validations") or []:
        if not isinstance(validation, dict):
            continue
        if validation.get("status") in {"skipped", "not_applicable", "unavailable_optional"}:
            decisions.append(validation)
    for event in events:
        if str(event.get("event_type") or "") in {
            "material.validation.skipped",
            "material.validation.not_applicable",
        }:
            decisions.append({"event_id": event.get("event_id"), **(event.get("payload") or {})})
    return decisions


def _model_lane_metrics(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        model_route = payload.get("model_route") if isinstance(payload.get("model_route"), dict) else {}
        lane_metrics = model_route.get("lane_metrics") if isinstance(model_route.get("lane_metrics"), dict) else {}
        if lane_metrics:
            metrics.append(
                {
                    "event_id": event.get("event_id"),
                    "event_type": event.get("event_type"),
                    "phase": event.get("phase"),
                    **lane_metrics,
                }
            )
        if str(event.get("event_type") or "") == "material.model_lanes.prewarm.requested":
            metrics.append(
                {
                    "event_id": event.get("event_id"),
                    "event_type": event.get("event_type"),
                    "prewarm_request": payload,
                }
            )
    return metrics


def _background_services(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        runtime = payload.get("runtime_metadata") if isinstance(payload.get("runtime_metadata"), dict) else {}
        if runtime.get("services") or runtime.get("service_container_ids") or runtime.get("health_checks"):
            services.append(
                {
                    "event_id": event.get("event_id"),
                    "event_type": event.get("event_type"),
                    "services": runtime.get("services") or [],
                    "service_container_ids": runtime.get("service_container_ids") or [],
                    "health_checks": runtime.get("health_checks") or [],
                    "cleanup": runtime.get("cleanup") or {},
                }
            )
    return services


def _events_by_prefix(events: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    return [event for event in events if str(event.get("event_type") or "").startswith(prefix)]


def _security_decisions(manifest: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    sandbox = manifest.get("sandbox") if isinstance(manifest.get("sandbox"), dict) else {}
    return {
        "sandbox": sandbox,
        "host_execution_used": bool(sandbox.get("host_execution_used")),
        "docker_socket_available_to_generated_project": bool(
            sandbox.get("docker_socket_available_to_generated_project")
        ),
        "vm_events": _events_by_prefix(events, "material.vm."),
    }


def _final_summary(raw: dict[str, Any], manifest: MaterialManifest, events: list[dict[str, Any]]) -> dict[str, Any]:
    latency_by_source: dict[str, int] = {}
    for event in events:
        source = str(event.get("latency_source") or "unknown")
        duration_ms = event.get("duration_ms")
        if isinstance(duration_ms, int):
            latency_by_source[source] = latency_by_source.get(source, 0) + duration_ms
    return {
        "status": raw.get("status"),
        "event_count": len(events),
        "file_count": len(manifest.files),
        "validation_count": len(manifest.validations),
        "issue_count": len(manifest.issues),
        "artifact_status": manifest.artifact.status,
        "artifact_sha256": manifest.artifact.sha256,
        "latency_by_source_ms": latency_by_source,
    }
