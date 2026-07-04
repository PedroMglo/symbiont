"""Task timeline projection for terminal and cockpit UX."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


def build_task_timeline(
    trace: dict[str, Any],
    *,
    command_runs: list[dict[str, Any]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build a compact timeline from the agentic ledger trace."""

    current_time = float(now if now is not None else time.time())
    task = dict(trace.get("task") or {})
    status = str(task.get("status") or "unknown")
    created_at = _float_or_none(task.get("created_at"))
    updated_at = _float_or_none(task.get("updated_at"))
    finished_at = updated_at if status in TERMINAL_TASK_STATUSES else None
    elapsed_seconds = _elapsed(created_at, finished_at or current_time)
    runs = _runs(trace.get("runs") or [], current_time=current_time)
    steps = _steps(trace.get("steps") or [], current_time=current_time)
    tool_calls = _tool_calls(trace.get("tool_calls") or [], current_time=current_time)
    events = _events(trace.get("events") or [])
    material_activity = _material_activity(trace.get("events") or [], current_time=current_time)
    command_run_items = _merge_command_runs(
        _command_runs(command_runs or [], current_time=current_time),
        _material_command_runs(material_activity, current_time=current_time),
    )
    file_activity = _file_activity(command_run_items)
    material_timing = _material_timing(material_activity)
    material_diagnostics = _material_diagnostics(material_activity)
    artifacts = _merge_artifacts(
        _artifacts(command_run_items),
        material_diagnostics.get("artifacts") if isinstance(material_diagnostics, dict) else [],
    )
    active_phase = _active_phase(status, events, runs, steps, tool_calls, command_run_items, material_activity)

    return {
        "task": {
            "id": task.get("id"),
            "trace_id": task.get("trace_id"),
            "status": status,
            "mode": task.get("mode"),
            "source": task.get("source"),
            "priority": task.get("priority"),
            "goal_preview": _preview(str(task.get("goal") or ""), 400),
            "created_at": created_at,
            "updated_at": updated_at,
            "finished_at": finished_at,
            "elapsed_seconds": elapsed_seconds,
            "terminal": status in TERMINAL_TASK_STATUSES,
            "active_phase": active_phase,
        },
        "counts": {
            "events": len(events),
            "runs": len(runs),
            "steps": len(steps),
            "tool_calls": len(tool_calls),
            "command_runs": len(command_run_items),
            "file_changes": len(file_activity),
            "material_events": len(material_activity),
            "artifacts": len(artifacts),
        },
        "runs": runs,
        "steps": steps,
        "tool_calls": tool_calls,
        "command_runs": command_run_items,
        "events": events,
        "file_activity": file_activity,
        "material_activity": material_activity,
        "material_timing": material_timing,
        "material_diagnostics": material_diagnostics,
        "artifacts": artifacts,
    }


def _events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: float(row.get("timestamp") or 0.0))
    events: list[dict[str, Any]] = []
    previous: float | None = None
    for row in ordered:
        timestamp = _float_or_none(row.get("timestamp"))
        events.append({
            "id": row.get("id"),
            "type": row.get("event_type"),
            "actor": row.get("actor"),
            "timestamp": timestamp,
            "gap_seconds": _elapsed(previous, timestamp) if previous is not None else None,
            "summary": _event_summary(row),
        })
        if timestamp is not None:
            previous = timestamp
    return events


def _runs(rows: list[dict[str, Any]], *, current_time: float) -> list[dict[str, Any]]:
    items = []
    for row in sorted(rows, key=lambda item: float(item.get("started_at") or 0.0)):
        items.append({
            "id": row.get("id"),
            "name": row.get("entrypoint"),
            "status": row.get("status"),
            "started_at": _float_or_none(row.get("started_at")),
            "finished_at": _float_or_none(row.get("finished_at")),
            "duration_seconds": _row_duration(row, current_time=current_time),
            "metadata": row.get("metadata") or {},
        })
    return items


def _steps(rows: list[dict[str, Any]], *, current_time: float) -> list[dict[str, Any]]:
    items = []
    for row in sorted(rows, key=lambda item: float(item.get("started_at") or 0.0)):
        items.append({
            "id": row.get("id"),
            "name": row.get("step_name"),
            "type": row.get("step_type"),
            "status": row.get("status"),
            "started_at": _float_or_none(row.get("started_at")),
            "finished_at": _float_or_none(row.get("finished_at")),
            "duration_seconds": _row_duration(row, current_time=current_time),
            "input_preview": row.get("input_preview"),
            "output_preview": row.get("output_preview"),
            "error": row.get("error") or {},
            "metadata": row.get("metadata") or {},
        })
    return items


def _tool_calls(rows: list[dict[str, Any]], *, current_time: float) -> list[dict[str, Any]]:
    items = []
    for row in sorted(rows, key=lambda item: float(item.get("started_at") or 0.0)):
        items.append({
            "id": row.get("id"),
            "tool": row.get("tool_name"),
            "risk_level": row.get("risk_level"),
            "status": row.get("status"),
            "started_at": _float_or_none(row.get("started_at")),
            "finished_at": _float_or_none(row.get("finished_at")),
            "duration_seconds": _row_duration(row, current_time=current_time),
            "requires_approval": bool(row.get("requires_approval")),
            "approval_id": row.get("approval_id"),
            "error": row.get("error"),
            "input_preview": row.get("input_preview"),
            "output_preview": row.get("output_preview"),
            "metadata": row.get("metadata") or {},
        })
    return items


def _command_runs(rows: list[dict[str, Any]], *, current_time: float) -> list[dict[str, Any]]:
    items = []
    for row in sorted(rows, key=lambda item: float(item.get("started_at") or 0.0)):
        metadata = dict(row.get("metadata") or {})
        items.append({
            "id": row.get("id"),
            "command": row.get("command"),
            "cwd": row.get("cwd"),
            "context_profile": row.get("context_profile"),
            "action": row.get("action"),
            "risk_level": row.get("risk_level"),
            "policy_decision": row.get("policy_decision"),
            "status": row.get("status"),
            "exit_code": row.get("exit_code"),
            "started_at": _float_or_none(row.get("started_at")),
            "finished_at": _float_or_none(row.get("finished_at")),
            "duration_seconds": _row_duration(row, current_time=current_time),
            "stdout_preview": row.get("stdout_preview"),
            "stderr_preview": row.get("stderr_preview"),
            "output": {
                "stdout_ref": metadata.get("stdout_ref"),
                "stderr_ref": metadata.get("stderr_ref"),
                "diff_ref": metadata.get("diff_ref"),
                "stdout_sha256": metadata.get("stdout_sha256"),
                "stderr_sha256": metadata.get("stderr_sha256"),
                "stdout_size_bytes": metadata.get("stdout_size_bytes"),
                "stderr_size_bytes": metadata.get("stderr_size_bytes"),
                "output_truncated": metadata.get("output_truncated"),
                "redaction_status": metadata.get("redaction_status"),
            },
            "diff_files": metadata.get("workspace_execution_diff_files") or metadata.get("diff_files") or [],
            "artifacts": metadata.get("artifacts") or metadata.get("workspace_execution_artifacts") or [],
            "metadata": metadata,
        })
    return items


def _material_command_runs(activity: list[dict[str, Any]], *, current_time: float) -> list[dict[str, Any]]:
    items_by_id: dict[str, dict[str, Any]] = {}
    for item in activity:
        command_run_id = str(item.get("command_run_id") or "")
        if not command_run_id:
            continue
        duration_ms = _int_or_none(item.get("duration_ms"))
        finished_at = _float_or_none(item.get("timestamp"))
        started_at = finished_at - (duration_ms / 1000.0) if finished_at is not None and duration_ms is not None else None
        profile = str(item.get("validation_profile") or "")
        event_type = str(item.get("event_type") or "")
        status = str(item.get("status") or "")
        if event_type.endswith(".passed"):
            status = "completed"
        elif event_type.endswith(".failed"):
            status = "failed"
        existing = items_by_id.get(command_run_id)
        if existing and existing.get("status") == "failed":
            continue
        output = {
            "stdout_ref": item.get("stdout_ref"),
            "stderr_ref": item.get("stderr_ref"),
            "diff_ref": None,
            "stdout_sha256": None,
            "stderr_sha256": None,
            "stdout_size_bytes": None,
            "stderr_size_bytes": None,
            "output_truncated": None,
            "redaction_status": None,
        }
        metadata = {
            "source": "material_activity",
            "event_id": item.get("event_id"),
            "validation_profile": profile or None,
            "vm_session_id": item.get("vm_session_id"),
            "material_session_id": item.get("session_id"),
            "runtime_metadata": item.get("runtime_metadata") or {},
        }
        items_by_id[command_run_id] = {
            "id": command_run_id,
            "command": f"material validation: {profile}" if profile else "material validation",
            "cwd": None,
            "context_profile": "material_execution",
            "action": "material.validation",
            "risk_level": "sandboxed",
            "policy_decision": "allow",
            "status": status,
            "exit_code": item.get("exit_code") if item.get("exit_code") is not None else (0 if status == "completed" else None),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": round(duration_ms / 1000.0, 3) if duration_ms is not None else _elapsed(started_at, current_time),
            "stdout_preview": item.get("stdout_preview"),
            "stderr_preview": item.get("stderr_preview"),
            "output": output,
            "diff_files": [],
            "artifacts": [],
            "metadata": metadata,
        }
    return sorted(items_by_id.values(), key=lambda run: float(run.get("started_at") or run.get("finished_at") or 0.0))


def _merge_command_runs(
    stored_runs: list[dict[str, Any]],
    material_runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {str(run.get("id") or ""): run for run in stored_runs if run.get("id")}
    for run in material_runs:
        run_id = str(run.get("id") or "")
        if not run_id:
            continue
        merged.setdefault(run_id, run)
    return sorted(merged.values(), key=lambda run: float(run.get("started_at") or run.get("finished_at") or 0.0))


def _file_activity(command_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for run in command_runs:
        run_id = str(run.get("id") or "")
        for item in run.get("diff_files") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            status = str(item.get("status") or "changed")
            key = (run_id, path, status)
            if not path or key in seen:
                continue
            seen.add(key)
            files.append({
                "run_id": run_id,
                "path": path,
                "status": status,
                "additions": item.get("additions"),
                "deletions": item.get("deletions"),
                "patch": item.get("patch"),
                "patch_ref": item.get("patch_ref"),
                "binary": bool(item.get("binary")),
            })
        for artifact in run.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            path = str(artifact.get("path") or "")
            key = (run_id, path, "artifact")
            if not path or key in seen:
                continue
            seen.add(key)
            files.append({
                "run_id": run_id,
                "path": path,
                "status": "artifact",
                "artifact_id": artifact.get("artifact_id"),
                "sha256": artifact.get("sha256"),
                "size_bytes": artifact.get("size_bytes"),
            })
    return files


def _artifacts(command_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run in command_runs:
        for artifact in run.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            artifact_id = str(artifact.get("artifact_id") or artifact.get("path") or "")
            if not artifact_id or artifact_id in seen:
                continue
            seen.add(artifact_id)
            artifacts.append({**artifact, "run_id": run.get("id")})
    return artifacts


def _material_activity(rows: list[dict[str, Any]], *, current_time: float) -> list[dict[str, Any]]:
    activity: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: float(item.get("timestamp") or 0.0)):
        raw_type = str(row.get("event_type") or "")
        if not raw_type.startswith("ai_local.material."):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        material_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        inner_payload = material_payload.get("payload") if isinstance(material_payload.get("payload"), dict) else {}
        issue = material_payload.get("issue") if isinstance(material_payload.get("issue"), dict) else {}
        artifact = material_payload.get("artifact") if isinstance(material_payload.get("artifact"), dict) else {}
        artifacts = material_payload.get("artifacts") if isinstance(material_payload.get("artifacts"), list) else []
        first_artifact = next((item for item in artifacts if isinstance(item, dict)), {})
        model_route = inner_payload.get("model_route") if isinstance(inner_payload.get("model_route"), dict) else {}
        if not model_route:
            model_route = material_payload.get("model_route") if isinstance(material_payload.get("model_route"), dict) else {}
        lane_metrics = model_route.get("lane_metrics") if isinstance(model_route.get("lane_metrics"), dict) else {}
        runtime_metadata = (
            inner_payload.get("runtime_metadata")
            if isinstance(inner_payload.get("runtime_metadata"), dict)
            else material_payload.get("runtime_metadata")
            if isinstance(material_payload.get("runtime_metadata"), dict)
            else {}
        )
        phase = material_payload.get("phase") or _material_phase(raw_type)
        status = material_payload.get("status") or inner_payload.get("status") or _material_status(raw_type)
        started_at = _time_or_none(material_payload.get("started_at") or inner_payload.get("started_at"))
        finished_at = _time_or_none(material_payload.get("finished_at") or inner_payload.get("finished_at"))
        last_progress_at = _time_or_none(material_payload.get("last_progress_at") or inner_payload.get("last_progress_at"))
        duration_ms = _int_or_none(material_payload.get("duration_ms") or inner_payload.get("duration_ms"))
        latency_source = material_payload.get("latency_source") or inner_payload.get("latency_source") or _material_latency_source(raw_type)
        activity.append({
            "event_id": payload.get("event_id") or material_payload.get("event_id"),
            "event_type": raw_type.removeprefix("ai_local."),
            "timestamp": _float_or_none(row.get("timestamp")),
            "producer": payload.get("producer"),
            "severity": payload.get("severity"),
            "status": status,
            "phase": phase,
            "session_id": material_payload.get("session_id") or inner_payload.get("session_id"),
            "vm_session_id": material_payload.get("vm_session_id") or inner_payload.get("vm_session_id"),
            "started_at": started_at,
            "finished_at": finished_at,
            "last_progress_at": last_progress_at,
            "progress_age_seconds": _elapsed(last_progress_at, current_time) if last_progress_at is not None else None,
            "duration_ms": duration_ms,
            "duration_seconds": round(duration_ms / 1000.0, 3) if duration_ms is not None else None,
            "latency_source": latency_source,
            "action_id": material_payload.get("action_id"),
            "validation_profile": material_payload.get("validation_profile") or inner_payload.get("profile"),
            "command_run_id": material_payload.get("command_run_id") or inner_payload.get("command_run_id"),
            "stdout_ref": material_payload.get("stdout_ref") or inner_payload.get("stdout_ref"),
            "stderr_ref": material_payload.get("stderr_ref") or inner_payload.get("stderr_ref"),
            "stdout_preview": material_payload.get("stdout_preview") or inner_payload.get("stdout_preview"),
            "stderr_preview": material_payload.get("stderr_preview") or inner_payload.get("stderr_preview"),
            "exit_code": material_payload.get("exit_code") if material_payload.get("exit_code") is not None else inner_payload.get("exit_code"),
            "issue_code": issue.get("code") or inner_payload.get("issue_type") or inner_payload.get("reason"),
            "issue_gate": issue.get("gate"),
            "issue_id": inner_payload.get("issue_id"),
            "issue_bundle_id": inner_payload.get("bundle_id"),
            "repair_focus_profile": inner_payload.get("repair_focus_profile")
            or material_payload.get("repair_focus_profile"),
            "repair_focus_target_path": inner_payload.get("repair_focus_target_path")
            or material_payload.get("repair_focus_target_path"),
            "repair_focus_reason": inner_payload.get("repair_focus_reason")
            or material_payload.get("repair_focus_reason"),
            "target_path": inner_payload.get("target_path"),
            "target_paths": inner_payload.get("target_paths") or [],
            "target_resolution": inner_payload.get("target_resolution"),
            "patch_set_id": inner_payload.get("patch_set_id"),
            "patch_count": inner_payload.get("patch_count"),
            "patch_attempt": inner_payload.get("patch_attempt"),
            "proposal_rejection_count": inner_payload.get("proposal_rejection_count"),
            "retryable": inner_payload.get("retryable"),
            "rejection_reason": inner_payload.get("reason"),
            "repair_mode": inner_payload.get("repair_mode"),
            "focused_revalidation_profiles": inner_payload.get("focused_revalidation_profiles") or [],
            "full_validation_required": inner_payload.get("full_validation_required"),
            "before_sha256": inner_payload.get("before_sha256"),
            "after_sha256": inner_payload.get("after_sha256"),
            "patches": inner_payload.get("patches") or [],
            "contract_id": inner_payload.get("contract_id"),
            "observed_contract_id": inner_payload.get("observed_contract_id"),
            "comparison_id": inner_payload.get("comparison_id"),
            "model_route": model_route,
            "lane_metrics": lane_metrics,
            "runtime_metadata": runtime_metadata,
            "cleanup": inner_payload.get("cleanup") or runtime_metadata.get("cleanup") or {},
            "artifact_path": artifact.get("path") or first_artifact.get("path") or inner_payload.get("artifact_path"),
            "artifact_sha256": artifact.get("sha256") or first_artifact.get("sha256") or inner_payload.get("sha256"),
            "storage_object_ref": artifact.get("storage_object_ref")
            or first_artifact.get("storage_object_ref")
            or inner_payload.get("storage_object_ref"),
            "chain_of_custody_ref": artifact.get("chain_of_custody_ref")
            or first_artifact.get("chain_of_custody_ref")
            or inner_payload.get("chain_of_custody_ref"),
            "materialized_path": artifact.get("materialized_path")
            or first_artifact.get("materialized_path")
            or inner_payload.get("materialized_path"),
            "materialized_sha256": artifact.get("materialized_sha256")
            or first_artifact.get("materialized_sha256")
            or inner_payload.get("materialized_sha256"),
            "extracted_path": artifact.get("extracted_path")
            or first_artifact.get("extracted_path")
            or inner_payload.get("extracted_path"),
            "extracted_files_count": artifact.get("extracted_files_count")
            or first_artifact.get("extracted_files_count")
            or inner_payload.get("extracted_files_count"),
            "extracted_top_level_paths": artifact.get("extracted_top_level_paths")
            or first_artifact.get("extracted_top_level_paths")
            or inner_payload.get("extracted_top_level_paths")
            or [],
        })
    return activity


def _material_timing(activity: list[dict[str, Any]]) -> dict[str, Any]:
    latency_by_source: dict[str, int] = {}
    latest_progress: float | None = None
    latest_item: dict[str, Any] | None = None
    for item in activity:
        source = str(item.get("latency_source") or "unknown")
        duration_ms = _int_or_none(item.get("duration_ms"))
        if duration_ms is not None:
            latency_by_source[source] = latency_by_source.get(source, 0) + duration_ms
        progress = _float_or_none(item.get("last_progress_at")) or _float_or_none(item.get("timestamp"))
        if progress is not None and (latest_progress is None or progress >= latest_progress):
            latest_progress = progress
            latest_item = item
    return {
        "latency_by_source_ms": latency_by_source,
        "last_progress_at": latest_progress,
        "last_phase": latest_item.get("phase") if latest_item else None,
        "last_status": latest_item.get("status") if latest_item else None,
        "last_latency_source": latest_item.get("latency_source") if latest_item else None,
    }


def _material_diagnostics(activity: list[dict[str, Any]]) -> dict[str, Any]:
    issue_types: dict[str, int] = {}
    repair_rejection_reasons: dict[str, int] = {}
    validation_profiles: dict[str, int] = {}
    lane_metrics: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    cleanup_events: list[dict[str, Any]] = []
    issue_bundles: list[dict[str, Any]] = []
    repair_rejections: list[dict[str, Any]] = []
    repair_focuses: list[dict[str, Any]] = []
    patch_sets: list[dict[str, Any]] = []
    for item in activity:
        issue_code = str(item.get("issue_code") or "")
        if issue_code:
            issue_types[issue_code] = issue_types.get(issue_code, 0) + 1
        rejection_reason = str(item.get("rejection_reason") or "")
        if rejection_reason:
            repair_rejection_reasons[rejection_reason] = repair_rejection_reasons.get(rejection_reason, 0) + 1
        profile = str(item.get("validation_profile") or "")
        if profile:
            validation_profiles[profile] = validation_profiles.get(profile, 0) + 1
        metrics = item.get("lane_metrics")
        if isinstance(metrics, dict) and metrics:
            lane_metrics.append({"event_id": item.get("event_id"), **metrics})
        if item.get("artifact_path") or item.get("artifact_sha256"):
            artifacts.append(
                {
                    "event_id": item.get("event_id"),
                    "path": item.get("artifact_path"),
                    "sha256": item.get("artifact_sha256"),
                    "storage_object_ref": item.get("storage_object_ref"),
                    "chain_of_custody_ref": item.get("chain_of_custody_ref"),
                    "materialized_path": item.get("materialized_path"),
                    "materialized_sha256": item.get("materialized_sha256"),
                    "extracted_path": item.get("extracted_path"),
                    "extracted_files_count": item.get("extracted_files_count"),
                    "extracted_top_level_paths": item.get("extracted_top_level_paths") or [],
                }
            )
        cleanup = item.get("cleanup")
        if isinstance(cleanup, dict) and cleanup:
            cleanup_events.append({"event_id": item.get("event_id"), **cleanup})
        if item.get("issue_bundle_id"):
            issue_bundles.append(
                {
                    "event_id": item.get("event_id"),
                    "bundle_id": item.get("issue_bundle_id"),
                    "repair_focus_profile": item.get("repair_focus_profile"),
                    "repair_focus_target_path": item.get("repair_focus_target_path"),
                    "repair_focus_reason": item.get("repair_focus_reason"),
                    "target_paths": item.get("target_paths") or [],
                }
            )
        if item.get("repair_focus_target_path"):
            repair_focuses.append(
                {
                    "event_id": item.get("event_id"),
                    "profile": item.get("repair_focus_profile"),
                    "target_path": item.get("repair_focus_target_path"),
                    "reason": item.get("repair_focus_reason"),
                }
            )
        if item.get("rejection_reason"):
            repair_rejections.append(
                {
                    "event_id": item.get("event_id"),
                    "issue_id": item.get("issue_id"),
                    "reason": item.get("rejection_reason"),
                    "patch_attempt": item.get("patch_attempt"),
                    "proposal_rejection_count": item.get("proposal_rejection_count"),
                    "retryable": item.get("retryable"),
                    "target_path": item.get("target_path"),
                }
            )
        if item.get("patch_set_id") or item.get("patch_count") or item.get("patches"):
            patch_sets.append(
                {
                    "event_id": item.get("event_id"),
                    "patch_set_id": item.get("patch_set_id"),
                    "patch_count": item.get("patch_count"),
                    "target_paths": item.get("target_paths") or [],
                    "patches": item.get("patches") or [],
                    "status": item.get("status"),
                }
            )
    return {
        "issue_types": issue_types,
        "repair_rejection_reasons": repair_rejection_reasons,
        "validation_profiles": validation_profiles,
        "model_lane_metrics": lane_metrics,
        "artifacts": artifacts,
        "cleanup_events": cleanup_events,
        "issue_bundles": issue_bundles,
        "repair_rejections": repair_rejections,
        "repair_focuses": repair_focuses,
        "patch_sets": patch_sets,
    }


def _merge_artifacts(command_artifacts: list[dict[str, Any]], material_artifacts: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        artifact_id = str(item.get("artifact_id") or item.get("path") or item.get("sha256") or "")
        if not artifact_id or artifact_id in seen:
            return
        seen.add(artifact_id)
        artifacts.append(item)

    for item in command_artifacts:
        if isinstance(item, dict):
            add(item)
    for item in material_artifacts if isinstance(material_artifacts, list) else []:
        if not isinstance(item, dict):
            continue
        add(
            {
                "artifact_id": item.get("artifact_id") or item.get("path") or item.get("sha256"),
                "path": item.get("path"),
                "sha256": item.get("sha256"),
                "storage_object_ref": item.get("storage_object_ref"),
                "chain_of_custody_ref": item.get("chain_of_custody_ref"),
                "materialized_path": item.get("materialized_path"),
                "materialized_sha256": item.get("materialized_sha256"),
                "extracted_path": item.get("extracted_path"),
                "extracted_files_count": item.get("extracted_files_count"),
                "extracted_top_level_paths": item.get("extracted_top_level_paths") or [],
                "event_id": item.get("event_id"),
                "source": "material_activity",
            }
        )
    return artifacts


def _material_status(raw_type: str) -> str:
    if raw_type.endswith(".failed"):
        return "failed"
    if raw_type.endswith(".created") or raw_type.endswith(".completed") or raw_type.endswith(".passed") or raw_type.endswith(".ready"):
        return "completed"
    if raw_type.endswith(".started"):
        return "running"
    if raw_type.endswith(".heartbeat"):
        return "heartbeat"
    return "info"


def _material_phase(raw_type: str) -> str:
    event_name = raw_type.removeprefix("ai_local.material.")
    if event_name.startswith("vm."):
        return "vm_" + event_name.removeprefix("vm.").replace(".", "_")
    return event_name.replace(".", "_")


def _material_latency_source(raw_type: str) -> str:
    event_name = raw_type.removeprefix("ai_local.material.")
    if event_name.startswith("vm."):
        return "vm"
    if event_name.startswith("plan.") or event_name.startswith("file.") or event_name.startswith("patch."):
        return "llm"
    if event_name.startswith("validation."):
        return "validation"
    if event_name.startswith("workspace.") or event_name.startswith("command."):
        return "sandbox"
    if event_name.startswith("package."):
        return "storage"
    if event_name.startswith("policy."):
        return "policy"
    return "unknown"


def _active_phase(
    status: str,
    events: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    command_runs: list[dict[str, Any]],
    material_activity: list[dict[str, Any]],
) -> str:
    if status in TERMINAL_TASK_STATUSES:
        return status
    for item in reversed(material_activity):
        material_status = str(item.get("status") or "")
        if material_status in {"running", "progress", "heartbeat"}:
            return "material:" + str(item.get("phase") or material_status)
    active_statuses = {"running", "planning", "queued", "waiting_approval", "recovering"}
    for collection, label_key in (
        (command_runs, "command"),
        (tool_calls, "tool"),
        (steps, "name"),
        (runs, "name"),
    ):
        for item in reversed(collection):
            if str(item.get("status") or "") in active_statuses or item.get("finished_at") is None:
                return str(item.get(label_key) or item.get("status") or "running")
    if events:
        return str(events[-1].get("type") or status)
    return status


def _event_summary(row: dict[str, Any]) -> str:
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        return _preview(str(payload), 160)
    for key in ("reason", "status", "decision", "action", "run_id", "worker_id", "error"):
        value = payload.get(key)
        if value:
            return f"{key}={_preview(str(value), 120)}"
    return _preview(" ".join(f"{key}={value}" for key, value in sorted(payload.items())[:3]), 160)


def _row_duration(row: dict[str, Any], *, current_time: float) -> float | None:
    duration_ms = row.get("duration_ms")
    if duration_ms is not None:
        try:
            return round(float(duration_ms) / 1000.0, 3)
        except (TypeError, ValueError):
            pass
    started = _float_or_none(row.get("started_at"))
    finished = _float_or_none(row.get("finished_at")) or current_time
    return _elapsed(started, finished)


def _elapsed(started: float | None, finished: float | None) -> float | None:
    if started is None or finished is None:
        return None
    return round(max(0.0, float(finished) - float(started)), 3)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _time_or_none(value: Any) -> float | None:
    if value is None:
        return None
    numeric = _float_or_none(value)
    if numeric is not None:
        return numeric
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _preview(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
