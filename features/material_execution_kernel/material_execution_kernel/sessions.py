"""In-memory material session store.

The store owns material session state, manifest projection and phase
coordination. Side effects still cross typed owner boundaries: material builder
proposals through ``MaterialBuilderClient`` and sandbox operations through
``WorkspaceClient``.
"""

from __future__ import annotations

import ast
from collections import Counter
from datetime import UTC, datetime
import json
import re
import shlex
import sys
from typing import Any
from uuid import uuid4

from material_execution_kernel.contract_comparison import compare_contracts
from material_execution_kernel.config import MaterialKernelSettings, get_settings
from material_execution_kernel.diagnostics import build_diagnostics_bundle
from material_execution_kernel.events import new_material_event
from material_execution_kernel.material_builder_client import (
    GeneratedMaterialFile,
    MaterialBuilderClient,
    MaterialBuilderUnavailable,
    MaterialPatchProposal,
    MaterialPatchSetProposal,
    MaterialPlanProposal,
    MaterialRegenerateFromContractProposal,
    MaterialRepairProposal,
    MaterialReplacementProposal,
    MaterialValidationCommandProposal,
    PlanCoverageIssueProposal,
    PlannedMaterialFile,
    UnavailableMaterialBuilderClient,
)
from material_execution_kernel.material_contract import (
    MaterialContractValidationError,
    freeze_material_contract,
)
from material_execution_kernel.interface_ledger import (
    build_interface_ledger,
    build_repair_obligations,
    obligations_for_issue,
)
from material_execution_kernel.observed_contract import extract_observed_contract
from material_execution_kernel.plan_coverage import plan_coverage_issues
from material_execution_kernel.repair_arbiter import (
    apply_attempt_decision_to_issue,
    arbitrate_repair_attempt,
    arbitrate_repair_rejection,
)
from material_execution_kernel.builder_repair_request import compile_builder_repair_request
from material_execution_kernel.repair_case import (
    RepairCase,
    compile_repair_case,
    repair_case_allows_llm,
)
from material_execution_kernel.types import (
    ArtifactEvidence,
    CommandRunEvidence,
    ContractComparisonIssue,
    MaterialManifest,
    MaterialManifestArtifact,
    MaterialManifestFile,
    MaterialManifestValidation,
    MaterialEvent,
    MaterialExecutionConstraints,
    MaterialIssue,
    MaterialContract,
    MaterialPhaseTiming,
    MaterialSessionRequest,
    MaterialSessionResponse,
    MaterialSessionStatus,
    IssueBundle,
    IssueBundleFailure,
    IssueBundleSkippedProfile,
    ObservedContract,
    PatchRejectionEvidence,
    RepairTargetResolution,
    SandboxEvidence,
    ValidationSummary,
)
from material_execution_kernel.validation_plan import (
    effective_required_capabilities,
    harden_required_validation_profiles,
)
from material_execution_kernel.workspace_client import (
    CommandValidationResult,
    UnavailableWorkspaceClient,
    WorkspaceClient,
    WorkspaceIssue,
)


class MaterialSessionNotFound(KeyError):
    """Raised when a material session id is unknown."""


_TERMINAL_SESSION_STATUSES = {
    "completed",
    "cancelled",
    "failed_closed",
    "blocked_by_policy",
    "blocked_by_vm_isolation",
    "blocked_by_sandbox_profile",
    "blocked_by_contract",
    "blocked_by_missing_tool",
    "stalled",
}
_FAILURE_SNAPSHOT_STATUSES = {
    "failed_closed",
    "blocked_by_contract",
    "blocked_by_sandbox_profile",
    "blocked_by_missing_tool",
}


class MaterialSessionStore:
    def __init__(
        self,
        *,
        material_builder: MaterialBuilderClient | None = None,
        workspace_client: WorkspaceClient | None = None,
        settings: MaterialKernelSettings | None = None,
    ) -> None:
        self._material_builder = material_builder or UnavailableMaterialBuilderClient()
        self._workspace_client = workspace_client or UnavailableWorkspaceClient()
        self._settings = settings or get_settings()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, str] = {}
        self._events: dict[str, list[MaterialEvent]] = {}

    def create_or_resume(self, request: MaterialSessionRequest) -> MaterialSessionResponse:
        existing = self._idempotency.get(request.idempotency_key)
        if existing:
            return self.get(existing)

        session_id = f"mat_{uuid4().hex}"
        sandbox = SandboxEvidence(
            owner="features/workspace_execution",
            vm_isolation="required",
            network_policy=request.constraints.network_policy,
        )
        manifest = MaterialManifest(
            session_id=session_id,
            task_id=request.task_id,
            trace_id=request.trace_id,
            status="created",
            language={
                "original_query_language": request.language_context.original_language,
                "source_variant": request.language_context.source_variant,
                "working_language": request.language_context.working_language,
                "target_language": request.language_context.target_language,
                "translation_available": request.language_context.translation_available,
                "translation_safe": request.language_context.translation_safe,
                "internal_contract_language": request.language_context.internal_contract_language,
                "final_response_language": request.language_context.final_response_language,
                "contract_version": request.language_context.contract_version,
                "quality": request.language_context.quality,
                "safety_error": request.language_context.safety_error,
            },
            sandbox={
                "owner": sandbox.owner,
                "vm_required": True,
                "host_execution_used": False,
                "network_policy": request.constraints.network_policy,
                "generated_project_trust": request.constraints.generated_project_trust,
            },
        )
        effective_capabilities = effective_required_capabilities(request)
        self._sessions[session_id] = {
            "request": request,
            "effective_required_capabilities": effective_capabilities,
            "status": "created",
            "sandbox": sandbox,
            "manifest": manifest,
            "plan": None,
            "material_contract": None,
            "observed_contract": None,
            "interface_ledger": None,
            "repair_obligations": [],
            "repair_cases": [],
            "repair_arbiter": {"schema_version": "repair_arbiter.v0.1", "attempts": [], "rejections": []},
            "contract_comparison": None,
            "generated_files": [],
            "artifact": None,
            "validation_summary": ValidationSummary(),
            "command_runs": [],
            "issues": [],
            "issue_bundles": [],
            "repair_rounds": 0,
            "focused_revalidation_profiles": [],
            "full_validation_required": False,
            "diagnostics_ref": f"diagnostics:{session_id}",
            "runtime_limits": self._settings.runtime_limits,
        }
        self._idempotency[request.idempotency_key] = session_id
        self._events[session_id] = []
        self.append_event(
            new_material_event(
                event_type="material.session.created",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="created",
                status="completed",
                latency_source="kernel",
                payload={
                    "trace_id": request.trace_id,
                    "constraints": request.constraints.model_dump(mode="json"),
                    "required_capabilities": effective_capabilities,
                    "runtime_limits": self._settings.runtime_limits,
                },
            )
        )
        if self._settings.prewarm_material_lanes:
            self.append_event(
                new_material_event(
                    event_type="material.model_lanes.prewarm.requested",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase="created",
                    status="progress",
                    latency_source="llm",
                    payload={
                        "lanes": list(self._settings.material_model_lanes),
                        "policy": self._settings.model_lane_policy,
                        "owner": "lifecycle/prewarming",
                        "kernel_side_effects": False,
                    },
                )
            )
        return self.get(session_id)

    def get(self, session_id: str) -> MaterialSessionResponse:
        raw = self._sessions.get(session_id)
        if raw is None:
            raise MaterialSessionNotFound(session_id)
        request: MaterialSessionRequest = raw["request"]
        events = self._events.get(session_id, [])
        return MaterialSessionResponse(
            session_id=session_id,
            task_id=request.task_id,
            status=raw["status"],
            sandbox=raw["sandbox"],
            artifact=raw["artifact"],
            validation_summary=raw["validation_summary"],
            command_runs=raw["command_runs"],
            issues=raw["issues"],
            phase_timings=phase_timings(events),
            last_progress_at=last_progress_at(events),
            latency_summary=latency_summary(events),
            manifest_ref=f"manifest:{session_id}:{raw['status']}",
            diagnostics_ref=raw.get("diagnostics_ref"),
        )

    def step(self, session_id: str) -> MaterialSessionResponse:
        raw = self._sessions.get(session_id)
        if raw is None:
            raise MaterialSessionNotFound(session_id)
        status = str(raw.get("status") or "created")
        if status in _TERMINAL_SESSION_STATUSES:
            self._append_heartbeat(raw, session_id)
            return self.get(session_id)
        if status == "created":
            self._policy_preflight(raw, session_id)
        elif status == "policy_preflight":
            self._allocate_vm(raw, session_id)
        elif status == "vm_ready":
            if _remote_source_requires_acquisition(raw):
                self._acquire_remote_source(raw, session_id)
            else:
                self._create_plan(raw, session_id)
        elif status == "planning":
            self._generate_files(raw, session_id)
        elif status == "generating_files":
            self._materialize_workspace(raw, session_id)
        elif status == "workspace_materializing":
            if _workspace_materialized(raw):
                self._run_validations(raw, session_id)
            else:
                self._materialize_workspace(raw, session_id)
        elif status == "validating":
            if raw.get("full_validation_required"):
                self._run_validations(raw, session_id)
            else:
                self._package_artifact(raw, session_id)
        elif status == "repairing":
            self._repair_latest_issue(raw, session_id)
        elif status == "revalidating":
            self._run_validations(raw, session_id, revalidation=True)
        elif status == "packaging":
            self._complete(raw, session_id)
        else:
            self._block_with_issue(
                raw,
                session_id,
                issue_type="unsupported_material_phase",
                message="material session is in an unsupported phase",
                details={"status": status},
                target_status="blocked_by_contract",
            )
        return self.get(session_id)

    def cancel(self, session_id: str) -> MaterialSessionResponse:
        raw = self._sessions.get(session_id)
        if raw is None:
            raise MaterialSessionNotFound(session_id)
        request: MaterialSessionRequest = raw["request"]
        raw["status"] = "cancelled"
        self.append_event(
            new_material_event(
                event_type="material.cancelled",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="cancelled",
                status="cancelled",
                latency_source="kernel",
                payload={"trace_id": request.trace_id},
            )
        )
        return self.get(session_id)

    def events(self, session_id: str) -> list[MaterialEvent]:
        if session_id not in self._sessions:
            raise MaterialSessionNotFound(session_id)
        return list(self._events.get(session_id, []))

    def manifest(self, session_id: str) -> MaterialManifest:
        raw = self._sessions.get(session_id)
        if raw is None:
            raise MaterialSessionNotFound(session_id)
        return raw["manifest"]

    def diagnostics(self, session_id: str) -> dict[str, Any]:
        raw = self._sessions.get(session_id)
        if raw is None:
            raise MaterialSessionNotFound(session_id)
        return build_diagnostics_bundle(
            session_id=session_id,
            raw=raw,
            events=self._events.get(session_id, []),
        )

    def append_event(self, event: MaterialEvent) -> MaterialEvent:
        self._events.setdefault(event.session_id, []).append(event)
        raw = self._sessions.get(event.session_id)
        if raw is not None and event.phase and event.phase in _MATERIAL_STATUSES:
            raw["status"] = event.phase
            raw["manifest"].status = event.phase
        return event

    def _policy_preflight(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        self.append_event(
            new_material_event(
                event_type="material.policy.preflight.started",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="policy_preflight",
                status="started",
                latency_source="policy",
                payload={
                    "policy_context": request.policy_context.model_dump(mode="json"),
                    "required_capabilities": raw.get("effective_required_capabilities", []),
                },
            )
        )
        self.append_event(
            new_material_event(
                event_type="material.policy.preflight.passed",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="policy_preflight",
                status="completed",
                latency_source="policy",
                payload={
                    "must_use_vm_backed_sandbox": request.constraints.must_use_vm_backed_sandbox,
                    "must_not_execute_on_host": request.constraints.must_not_execute_on_host,
                },
            )
        )

    def _allocate_vm(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        self.append_event(
            new_material_event(
                event_type="material.vm.requested",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="vm_allocating",
                status="started",
                latency_source="vm",
                payload={"sandbox_owner": "features/workspace_execution"},
            )
        )
        result = self._workspace_client.request_vm_session(
            session_id=session_id,
            task_id=request.task_id,
            trace_id=request.trace_id,
            idempotency_key=f"{request.idempotency_key}:vm",
            network_policy=request.constraints.network_policy,
            material_source=_material_remote_source(request),
        )
        if result.status != "ready" or not result.vm_backed or not result.vm_session_id:
            self._record_workspace_issue(
                raw,
                session_id,
                WorkspaceIssue(
                    code=result.failure_code or "vm_runtime_unavailable",
                    message=result.failure_reason or "VM-backed sandbox is not ready",
                    details={"status": result.status},
                ),
                target_status="blocked_by_vm_isolation",
                target_kind="vm_session",
            )
            self.append_event(
                new_material_event(
                    event_type="material.vm.failed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="sandbox_owner",
                    phase="blocked_by_vm_isolation",
                    status="blocked",
                    latency_source="vm",
                    payload={"reason": result.failure_code or "vm_runtime_unavailable"},
                )
            )
            return
        raw["sandbox"] = SandboxEvidence(
            owner=result.owner,
            vm_session_id=result.vm_session_id,
            vm_isolation="vm",
            host_execution_used=False,
            docker_socket_available_to_generated_project=False,
            network_policy=request.constraints.network_policy,
            cleanup_recorded=False,
        )
        raw["manifest"].sandbox.update(
            {
                "owner": result.owner,
                "vm_session_id": result.vm_session_id,
                "vm_isolation": result.isolation_mode,
                "host_execution_used": False,
                "docker_socket_available_to_generated_project": False,
            }
        )
        self.append_event(
            new_material_event(
                event_type="material.vm.ready",
                session_id=session_id,
                task_id=request.task_id,
                source="sandbox_owner",
                phase="vm_ready",
                status="completed",
                latency_source="vm",
                payload={"vm_session_id": result.vm_session_id, "isolation_mode": result.isolation_mode},
            )
        )

    def _acquire_remote_source(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        source = _material_remote_source(request)
        if not source:
            raw["source_evidence_context"] = {}
            return
        destination = _remote_source_destination(source)
        self.append_event(
            new_material_event(
                event_type="material.source_acquisition.started",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="vm_ready",
                status="started",
                latency_source="sandbox",
                payload={
                    "source_kind": source.get("kind"),
                    "destination": destination,
                    "sandbox_owner": "features/workspace_execution",
                },
            )
        )
        result = self._workspace_client.acquire_remote_source(
            session_id=session_id,
            material_session_id=session_id,
            vm_session_id=str(sandbox.vm_session_id),
            source=source,
            destination=destination,
            idempotency_key=f"{request.idempotency_key}:remote-source",
        )
        if result.status != "completed":
            self._record_workspace_issue(
                raw,
                session_id,
                result.issue or WorkspaceIssue(
                    code="remote_source_acquisition_failed",
                    message="remote source could not be acquired by the sandbox owner",
                    details={"source_kind": source.get("kind"), "destination": destination},
                ),
                target_status="failed_closed",
                target_kind="remote_source",
            )
            return
        raw["source_evidence_context"] = dict(result.evidence_context)
        raw["request"] = _request_with_source_evidence(raw["request"], raw["source_evidence_context"])
        self.append_event(
            new_material_event(
                event_type="material.source_acquisition.completed",
                session_id=session_id,
                task_id=request.task_id,
                source="sandbox_owner",
                phase="vm_ready",
                status="completed",
                latency_source="sandbox",
                payload={
                    "source_kind": source.get("kind"),
                    "destination": result.destination,
                    "effective_url": result.effective_url,
                    "clone_attempt_count": len(result.clone_attempts),
                    "evidence_context_available": bool(result.evidence_context),
                },
            )
        )

    def _create_plan(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        self.append_event(
            new_material_event(
                event_type="material.plan.requested",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="planning",
                status="started",
                latency_source="material_builder",
                payload={"required_capabilities": raw.get("effective_required_capabilities", [])},
            )
        )
        try:
            builder_constraints, language_context_payload, original_query, original_language = (
                _builder_request_context(request)
            )
            plan = self._material_builder.create_plan(
                session_id=session_id,
                task_id=request.task_id,
                working_query=request.goal,
                original_query=original_query,
                original_language=original_language,
                language_context=language_context_payload,
                required_capabilities=list(raw.get("effective_required_capabilities", [])),
                constraints=builder_constraints,
                variation_nonce=session_id,
            )
        except MaterialBuilderUnavailable as exc:
            self._block_with_issue(
                raw,
                session_id,
                issue_type="material_builder_unavailable",
                message=str(exc),
                details={},
                target_status="blocked_by_contract",
            )
            return
        plan = harden_required_validation_profiles(request, plan)
        coverage_issues = plan_coverage_issues(request, plan)
        repair_round = 0
        while coverage_issues and repair_round < self._settings.plan_coverage_repair_rounds:
            self.append_event(
                new_material_event(
                    event_type="material.plan.coverage.failed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase="planning",
                    status="progress",
                    latency_source="kernel",
                    payload={
                        "repair_round": repair_round + 1,
                        "max_repair_rounds": self._settings.plan_coverage_repair_rounds,
                        "issues": [_coverage_issue_payload(issue) for issue in coverage_issues],
                    },
                )
            )
            try:
                repaired_plan = self._material_builder.repair_plan(
                    session_id=session_id,
                    task_id=request.task_id,
                    working_query=request.goal,
                    original_query=original_query,
                    original_language=original_language,
                    language_context=language_context_payload,
                    required_capabilities=list(raw.get("effective_required_capabilities", [])),
                    constraints=builder_constraints,
                    plan=plan,
                    coverage_issues=coverage_issues,
                )
            except MaterialBuilderUnavailable as exc:
                self._block_with_issue(
                    raw,
                    session_id,
                    issue_type="material_plan_coverage_unresolved",
                    message=str(exc),
                    details={
                        "repair_round": repair_round + 1,
                        "coverage_issues": [_coverage_issue_payload(issue) for issue in coverage_issues],
                    },
                    target_status="blocked_by_contract",
                )
                return
            repaired_plan = harden_required_validation_profiles(request, repaired_plan)
            plan = repaired_plan
            repair_round += 1
            coverage_issues = plan_coverage_issues(request, plan)
            self.append_event(
                new_material_event(
                    event_type="material.plan.repaired",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="material_builder",
                    phase="planning",
                    status="completed",
                    latency_source="material_builder",
                    payload={
                        "repair_round": repair_round,
                        "model_route": plan.model_route,
                        "file_count": len(plan.files),
                        "remaining_issue_count": len(coverage_issues),
                    },
                )
            )
        if coverage_issues:
            self._block_with_issue(
                raw,
                session_id,
                issue_type="material_plan_coverage_unresolved",
                message="material plan repair did not satisfy required capability coverage",
                details={
                    "repair_rounds": repair_round,
                    "max_repair_rounds": self._settings.plan_coverage_repair_rounds,
                    "coverage_issues": [_coverage_issue_payload(issue) for issue in coverage_issues],
                },
                target_status="blocked_by_contract",
            )
            return
        try:
            material_contract = freeze_material_contract(
                session_id=session_id,
                request=request,
                plan=plan,
            )
        except MaterialContractValidationError as exc:
            self.append_event(
                new_material_event(
                    event_type="material.contract.failed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase="planning",
                    status="blocked",
                    latency_source="kernel",
                    payload={"error": str(exc)[:2000]},
                )
            )
            self._block_with_issue(
                raw,
                session_id,
                issue_type="material_contract_invalid",
                message="material contract could not be frozen from the material plan",
                details={"error": str(exc)[:2000]},
                target_status="blocked_by_contract",
            )
            return
        raw["material_contract"] = material_contract
        raw["manifest"].material_contract = material_contract.model_dump(mode="json")
        self.append_event(
            new_material_event(
                event_type="material.contract.created",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="planning",
                status="progress",
                latency_source="kernel",
                payload={
                    "contract_id": material_contract.contract_id,
                    "requirement_count": len(material_contract.requirements),
                    "planned_file_count": len(material_contract.planned_files),
                    "validation_count": len(material_contract.validation_profiles),
                },
            )
        )
        self.append_event(
            new_material_event(
                event_type="material.contract.frozen",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="planning",
                status="completed",
                latency_source="kernel",
                payload={
                    "contract_id": material_contract.contract_id,
                    "schema_version": material_contract.schema_version,
                    "frozen": material_contract.frozen,
                },
            )
        )
        self._accept_plan(raw, plan)
        self.append_event(
            new_material_event(
                event_type="material.plan.created",
                session_id=session_id,
                task_id=request.task_id,
                source="material_builder",
                phase="planning",
                status="completed",
                latency_source="material_builder",
                payload={
                    "project_root": plan.project_root,
                    "file_count": len(plan.files),
                    "required_validation_profiles": plan.required_validation_profiles,
                    "model_route": plan.model_route,
                },
            )
        )

    def _generate_files(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        if raw.get("material_contract") is None:
            self._block_with_issue(
                raw,
                session_id,
                issue_type="material_contract_missing",
                message="file generation requires a frozen material contract",
                details={"session_id": session_id},
                target_status="blocked_by_contract",
            )
            return
        plan: MaterialPlanProposal = raw["plan"]
        files: list[GeneratedMaterialFile] = []
        raw["generated_files"] = []
        model_route = _builder_model_route(
            self._material_builder,
            session_id=session_id,
            task_id=request.task_id,
            phase="files",
        )
        manifest_files = {item.path: item for item in raw["manifest"].files}
        total_files = len(plan.files)
        for index, planned_file in enumerate(plan.files, start=1):
            self.append_event(
                new_material_event(
                    event_type="material.file.requested",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase="generating_files",
                    status="started" if index == 1 else "progress",
                    latency_source="material_builder",
                    payload={"path": planned_file.path, "file_index": index, "file_count": total_files},
                )
            )
            try:
                generated = self._material_builder.generate_files(
                    session_id=session_id,
                    task_id=request.task_id,
                    plan=plan,
                    target_file_paths=[planned_file.path],
                )
            except MaterialBuilderUnavailable as exc:
                self._block_with_issue(
                    raw,
                    session_id,
                    issue_type="material_builder_unavailable",
                    message=str(exc),
                    details={"path": planned_file.path, "file_index": index, "file_count": total_files},
                    target_status="blocked_by_contract",
                    target_path=planned_file.path,
                )
                return
            if len(generated) != 1 or generated[0].path != planned_file.path:
                self._block_with_issue(
                    raw,
                    session_id,
                    issue_type="material_file_contract_mismatch",
                    message="material builder returned files that do not match the requested planned file",
                    details={
                        "requested_path": planned_file.path,
                        "returned_paths": [file.path for file in generated],
                        "file_index": index,
                        "file_count": total_files,
                    },
                    target_status="blocked_by_contract",
                    target_path=planned_file.path,
                )
                return
            file = generated[0]
            files.append(file)
            raw["generated_files"] = list(files)
            model_route = _builder_model_route(
                self._material_builder,
                session_id=session_id,
                task_id=request.task_id,
                phase="files",
            )
            manifest_file = manifest_files.get(file.path)
            if manifest_file is not None:
                manifest_file.state = "generated"
                manifest_file.content_hash = file.sha256
                manifest_file.kind = file.kind
            self.append_event(
                new_material_event(
                    event_type="material.file.completed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="material_builder",
                    phase="generating_files",
                    status="progress",
                    latency_source="material_builder",
                    payload={
                        "path": file.path,
                        "sha256": file.sha256,
                        "file_index": index,
                        "file_count": total_files,
                        "model_route": model_route,
                    },
                )
            )
        raw["generated_files"] = files
        self.append_event(
            new_material_event(
                event_type="material.files.generated",
                session_id=session_id,
                task_id=request.task_id,
                source="material_builder",
                phase="generating_files",
                status="completed",
                latency_source="material_builder",
                payload={"file_count": len(files), "model_route": model_route},
            )
        )

    def _materialize_workspace(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        plan: MaterialPlanProposal = raw["plan"]
        files: list[GeneratedMaterialFile] = raw["generated_files"]
        self.append_event(
            new_material_event(
                event_type="material.workspace.write.started",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="workspace_materializing",
                status="started",
                latency_source="sandbox",
                payload={"file_count": len(files), "project_root": plan.project_root},
            )
        )
        try:
            result = self._workspace_client.write_files_batch(
                session_id=session_id,
                material_session_id=session_id,
                vm_session_id=str(sandbox.vm_session_id),
                project_root=plan.project_root,
                files=files,
                idempotency_key=f"{request.idempotency_key}:files:batch",
            )
        except Exception as exc:
            self._record_workspace_issue(
                raw,
                session_id,
                WorkspaceIssue(
                    code="workspace_materialization_failed",
                    message="workspace batch write failed before completion evidence was recorded",
                    details={"error": str(exc)[:1000]},
                ),
                target_status="blocked_by_sandbox_profile",
                target_kind="workspace_write",
            )
            return
        if result.status != "completed":
            self._record_workspace_issue(
                raw,
                session_id,
                result.issue
                or WorkspaceIssue(code="workspace_materialization_failed", message="workspace batch write failed"),
                target_status="blocked_by_sandbox_profile",
                target_kind="workspace_write",
            )
            return
        expected_paths = {file.path for file in files}
        written_paths = set(result.written_paths)
        missing_paths = sorted(expected_paths - written_paths)
        if missing_paths:
            self._record_workspace_issue(
                raw,
                session_id,
                WorkspaceIssue(
                    code="workspace_materialization_incomplete",
                    message="workspace batch write completed without proving every generated file was written",
                    details={"missing_paths": missing_paths, "written_paths": sorted(written_paths)},
                ),
                target_status="blocked_by_sandbox_profile",
                target_kind="workspace_write",
            )
            return
        for item in raw["manifest"].files:
            if item.path in result.written_paths:
                item.state = "workspace_written"
        self.append_event(
            new_material_event(
                event_type="material.workspace.write.completed",
                session_id=session_id,
                task_id=request.task_id,
                source="sandbox_owner",
                phase="workspace_materializing",
                status="completed",
                latency_source="sandbox",
                payload={"file_count": len(result.written_paths), "state_hash": result.state_hash},
            )
        )
        self._extract_observed_contract(raw, session_id)
        self._compare_contracts(raw, session_id)

    def _run_validations(self, raw: dict[str, Any], session_id: str, *, revalidation: bool = False) -> None:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        plan: MaterialPlanProposal = raw["plan"]
        phase = "revalidating" if revalidation else "validating"
        self._extract_observed_contract(raw, session_id, phase=phase)
        self._compare_contracts(raw, session_id, phase=phase)
        if raw.get("status") in {"repairing", "failed_closed", "blocked_by_contract"}:
            return
        repair_round = int(raw.get("repair_rounds") or 0)
        full_profiles = _ordered_validation_batch(plan.required_validation_profiles)
        profiles, validation_scope = _profiles_for_validation_phase(
            raw,
            full_profiles=full_profiles,
            revalidation=revalidation,
        )
        self.append_event(
            new_material_event(
                event_type="material.validation.started",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase=phase,
                status="started",
                latency_source="validation",
                payload={
                    "profiles": profiles,
                    "full_required_profiles": full_profiles,
                    "repair_round": repair_round,
                    "validation_scope": validation_scope,
                    "full_validation_required": bool(raw.get("full_validation_required")),
                },
            )
        )
        passed: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        failures: list[tuple[str, WorkspaceIssue, str, CommandValidationResult]] = []
        skipped_profiles: list[IssueBundleSkippedProfile] = []
        for profile in profiles:
            if profile == "artifact":
                skipped.append(profile)
                raw["manifest"].validations.append(
                    MaterialManifestValidation(
                        profile=profile,
                        status="deferred_to_packaging",
                        vm_session_id=str(sandbox.vm_session_id),
                        details={
                            "reason": "artifact evidence is produced by the packaging phase",
                            "repair_round": repair_round,
                        },
                    )
                )
                self.append_event(
                    new_material_event(
                        event_type="material.validation.deferred",
                        session_id=session_id,
                        task_id=request.task_id,
                        source="kernel",
                        phase=phase,
                        status="progress",
                        latency_source="validation",
                        payload={
                            "profile": profile,
                            "reason": "artifact evidence is produced by the packaging phase",
                            "repair_round": repair_round,
                        },
                    )
                )
                continue
            skip = _validation_skip_for_profile(profile, failed_profiles=set(failed))
            if skip is not None:
                skipped.append(profile)
                skipped_profiles.append(skip)
                raw["manifest"].validations.append(
                    MaterialManifestValidation(
                        profile=profile,
                        status="skipped",
                        vm_session_id=str(sandbox.vm_session_id),
                    )
                )
                self.append_event(
                    new_material_event(
                        event_type="material.validation.skipped",
                        session_id=session_id,
                        task_id=request.task_id,
                        source="kernel",
                        phase=phase,
                        status="blocked",
                        latency_source="validation",
                        payload={
                            "profile": profile,
                            "reason": skip.reason,
                            "blocked_by": skip.blocked_by,
                            "repair_round": repair_round,
                        },
                    )
                )
                continue
            result = self._workspace_client.run_validation(
                session_id=session_id,
                material_session_id=session_id,
                vm_session_id=str(sandbox.vm_session_id),
                project_root=plan.project_root,
                profile=profile,
                command=_validation_command_for_profile(raw, profile),
                idempotency_key=f"{request.idempotency_key}:validation:{phase}:{profile}:repair:{repair_round}",
            )
            raw["manifest"].validations.append(_manifest_validation_from_result(result))
            raw["command_runs"].append(
                CommandRunEvidence(
                    command_run_id=result.command_run_id,
                    profile=profile,
                    vm_session_id=str(sandbox.vm_session_id),
                    host_execution_used=False,
                    duration_ms=result.duration_ms,
                )
            )
            if result.status == "completed":
                passed.append(profile)
                self.append_event(
                    new_material_event(
                        event_type="material.validation.passed",
                        session_id=session_id,
                        task_id=request.task_id,
                        source="sandbox_owner",
                        phase=phase,
                        status="progress",
                        latency_source="validation",
                        duration_ms=result.duration_ms,
                        payload={
                            "profile": profile,
                            "command_run_id": result.command_run_id,
                            "repair_round": repair_round,
                            "validation_command_provided": profile in plan.validation_commands,
                            "stdout_ref": result.stdout_ref,
                            "stderr_ref": result.stderr_ref,
                        },
                    )
                )
                continue
            failed.append(profile)
            issue = result.issue or WorkspaceIssue(code="validation_failed", message="validation failed")
            failures.append((profile, issue, result.command_run_id, result))
            self.append_event(
                new_material_event(
                    event_type="material.validation.failed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="sandbox_owner",
                    phase=phase,
                    status="failed",
                    latency_source="validation",
                    duration_ms=result.duration_ms,
                    payload={
                        "profile": profile,
                        "command_run_id": result.command_run_id,
                        "issue": {
                            "code": issue.code,
                            "message": issue.message,
                            "details": issue.details,
                        },
                        "repair_round": repair_round,
                        "validation_command_provided": profile in plan.validation_commands,
                        "exit_code": result.exit_code,
                        "stdout_ref": result.stdout_ref,
                        "stderr_ref": result.stderr_ref,
                        "stdout_preview": result.stdout_preview,
                        "stderr_preview": result.stderr_preview,
                    },
                )
            )
        optional_not_applicable = (
            []
            if validation_scope == "focused"
            else self._record_optional_validation_applicability(
                raw,
                session_id,
                phase=phase,
                repair_round=repair_round,
                required_profiles=profiles,
            )
        )
        raw["validation_summary"] = ValidationSummary(passed=passed, failed=failed, skipped=skipped)
        raw["manifest"].required_validation_profiles = full_profiles
        if failures:
            bundle = _issue_bundle_from_failures(raw, failures=failures, skipped=skipped_profiles)
            raw["issue_bundles"].append(bundle)
            raw["manifest"].issue_bundles = list(raw["issue_bundles"])
            self.append_event(
                new_material_event(
                    event_type="material.issue_bundle.created",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase=phase,
                    status="progress",
                    latency_source="validation",
                    payload=bundle.model_dump(mode="json"),
                )
            )
            focus_profile, focus_issue, focus_command_run_id, _focus_result = _first_repair_focus(raw, failures)
            focus_issue.details["issue_bundle"] = bundle.model_dump(mode="json")
            self._handle_validation_failure(
                raw,
                session_id,
                focus_issue,
                profile=focus_profile,
                command_run_id=focus_command_run_id,
            )
            return
        self.append_event(
            new_material_event(
                event_type="material.validation.completed",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase=phase,
                status="completed",
                latency_source="validation",
                payload={
                    "passed": passed,
                    "skipped": skipped,
                    "optional_not_applicable": optional_not_applicable,
                    "repair_round": repair_round,
                    "validation_scope": validation_scope,
                    "full_validation_required": bool(raw.get("full_validation_required")),
                },
            )
        )
        if raw.get("contract_comparison_deferred_until_runtime"):
            self._compare_contracts(raw, session_id, phase=phase)
            if raw.get("status") in {"repairing", "failed_closed", "blocked_by_contract"}:
                return
        if validation_scope == "focused":
            raw["focused_revalidation_profiles"] = []
            raw["full_validation_required"] = True
            raw["status"] = "validating"
            raw["manifest"].status = "validating"
        elif revalidation:
            raw["focused_revalidation_profiles"] = []
            raw["full_validation_required"] = False
            raw["status"] = "validating"
            raw["manifest"].status = "validating"
        else:
            raw["full_validation_required"] = False
            raw["status"] = "validating"
            raw["manifest"].status = "validating"

    def _record_optional_validation_applicability(
        self,
        raw: dict[str, Any],
        session_id: str,
        *,
        phase: str,
        repair_round: int,
        required_profiles: list[str],
    ) -> list[str]:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        plan: MaterialPlanProposal = raw["plan"]
        optional_profiles = [
            profile for profile in _ordered_validation_batch(plan.optional_validation_profiles) if profile not in required_profiles
        ]
        recorded: list[str] = []
        for profile in optional_profiles:
            raw["manifest"].validations.append(
                MaterialManifestValidation(
                    profile=profile,
                    status="not_applicable",
                    vm_session_id=str(sandbox.vm_session_id),
                    details={
                        "optional": True,
                        "reason": "optional validation profile was not required by the material contract",
                        "repair_round": repair_round,
                    },
                )
            )
            recorded.append(profile)
            self.append_event(
                new_material_event(
                    event_type="material.validation.not_applicable",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase=phase,
                    status="progress",
                    latency_source="validation",
                    payload={
                        "profile": profile,
                        "optional": True,
                        "reason": "optional validation profile was not required by the material contract",
                        "repair_round": repair_round,
                    },
                )
            )
        raw["manifest"].optional_validation_profiles = list(plan.optional_validation_profiles)
        return recorded

    def _handle_validation_failure(
        self,
        raw: dict[str, Any],
        session_id: str,
        issue: WorkspaceIssue,
        *,
        profile: str,
        command_run_id: str,
    ) -> None:
        request: MaterialSessionRequest = raw["request"]
        target_resolution = _target_resolution_for_issue(raw, issue=issue, profile=profile)
        target_path = target_resolution.primary_target
        issue_code = _classified_validation_issue_code(issue, profile=profile)
        target_kind = _target_kind_for_path(raw, target_path) if target_path else "validation"
        repairable = bool(target_path) and _is_repairable_validation_issue(issue, profile=profile)
        repair_rounds = int(raw.get("repair_rounds") or 0)
        can_repair = repairable and repair_rounds < request.max_repair_rounds
        details = {
            "message": issue.message,
            "profile": profile,
            "command_run_id": command_run_id,
            "repair_round": repair_rounds,
            "target_resolution": target_resolution.model_dump(mode="json"),
            **issue.details,
        }
        if not can_repair:
            details["repair_skipped_reason"] = (
                "max_repair_rounds_exhausted"
                if repairable and repair_rounds >= request.max_repair_rounds
                else "issue_is_not_repairable_by_patch"
            )
        self._record_workspace_issue(
            raw,
            session_id,
            WorkspaceIssue(code=issue_code, message=issue.message, details=details),
            target_status="repairing" if can_repair else "failed_closed",
            target_kind=target_kind,
            target_path=target_path,
            severity="repairable" if can_repair else "blocking_completion",
        )

    def _repair_latest_issue(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        plan: MaterialPlanProposal = raw["plan"]
        issue = _latest_repairable_issue(raw)
        if issue is None or not issue.target_path:
            self._block_with_issue(
                raw,
                session_id,
                issue_type="repair_issue_missing",
                message="no repairable issue with a target path is available",
                details={},
                target_status="failed_closed",
            )
            return
        generated_file = _generated_file(raw, issue.target_path)
        if generated_file is None:
            generated_file = _placeholder_generated_file_for_missing_target(raw, issue)
            if generated_file is None:
                self._record_workspace_issue(
                    raw,
                    session_id,
                    WorkspaceIssue(
                        code="repair_target_missing",
                        message="repair target is not present in generated file evidence",
                        details={"target_path": issue.target_path, "issue_id": issue.issue_id},
                    ),
                    target_status="failed_closed",
                    target_kind=issue.target_kind,
                    target_path=issue.target_path,
                )
                return
            issue.details["target_file_missing"] = True
            issue.details["repair_mode_hint"] = "create_dependency_manifest"
            _replace_generated_file(raw, generated_file)
            _ensure_manifest_file(raw, generated_file.path, kind=generated_file.kind)
        current_sha256 = _manifest_hash(raw, issue.target_path) or generated_file.sha256
        repair_round = int(raw.get("repair_rounds") or 0)
        _attach_related_symbol_repair_context(raw, issue)
        current_context = _current_content_context(generated_file.content, issue=issue)
        symbol_provider_candidates = _expected_symbol_provider_candidates(raw, issue)
        if symbol_provider_candidates:
            current_context["expected_symbol_provider_candidates"] = symbol_provider_candidates
        repair_case = self._compile_and_attach_repair_case(
            raw,
            session_id,
            issue,
            target_sha256=current_sha256,
        )
        current_context["repair_case"] = repair_case.model_dump(mode="json")
        current_context["repair_obligations"] = [item.model_dump(mode="json") for item in repair_case.obligations]
        current_context["success_criteria"] = [item.model_dump(mode="json") for item in repair_case.success_criteria]
        if not repair_case_allows_llm(repair_case):
            self.append_event(
                new_material_event(
                    event_type="material.repair.llm.blocked",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase="repairing",
                    status="blocked",
                    latency_source="kernel",
                    payload={
                        "issue_id": issue.issue_id,
                        "case_id": repair_case.case_id,
                        "repair_case_status": repair_case.status,
                        "root_cause_kind": repair_case.root_cause_kind,
                        "stop_conditions": repair_case.stop_conditions,
                        "allowed_actions": repair_case.allowed_actions,
                    },
                )
            )
            self._record_workspace_issue(
                raw,
                session_id,
                WorkspaceIssue(
                    code="repair_case_not_ready_for_llm",
                    message="repair LLM call blocked because the repair case is not ready for an LLM proposal",
                    details={
                        "issue_id": issue.issue_id,
                        "repair_case": repair_case.model_dump(mode="json"),
                    },
                ),
                target_status="failed_closed",
                target_kind=issue.target_kind,
                target_path=issue.target_path,
            )
            return
        target_bundle = _repair_target_bundle(raw, issue, primary_file=generated_file, primary_sha256=current_sha256)
        if len(target_bundle) > 1:
            current_context["target_bundle"] = target_bundle
        arbiter_decision = arbitrate_repair_attempt(
            issue,
            target_sha256=current_sha256,
            related_target_count=len(target_bundle),
            max_repair_rounds=request.max_repair_rounds,
        )
        apply_attempt_decision_to_issue(issue, arbiter_decision)
        current_context["repair_arbiter"] = arbiter_decision.model_dump()
        _record_repair_arbiter_attempt(raw, arbiter_decision.model_dump())
        self.append_event(
            new_material_event(
                event_type="material.repair.arbiter.decided",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="repairing",
                status="progress" if arbiter_decision.strategy != "failed_closed" else "failed",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "repair_round": repair_round,
                    **arbiter_decision.model_dump(),
                },
            )
        )
        if arbiter_decision.strategy == "failed_closed":
            self._record_workspace_issue(
                raw,
                session_id,
                WorkspaceIssue(
                    code="repair_arbiter_budget_exhausted",
                    message="repair arbiter exhausted the configured strategy ladder",
                    details={
                        "issue_id": issue.issue_id,
                        "target_path": issue.target_path,
                        "repair_arbiter": arbiter_decision.model_dump(),
                    },
                ),
                target_status="failed_closed",
                target_kind=issue.target_kind,
                target_path=issue.target_path,
            )
            return
        import_cycle_context = _local_import_cycle_context(raw, issue)
        if import_cycle_context:
            current_context["local_import_cycle"] = import_cycle_context
        prior_patch_rejections = [
            rejection.model_dump(mode="json") for rejection in issue.patch_rejections
        ]
        issue_contract = _issue_contract(issue)
        validation_profile = str(issue.details.get("profile") or "") or None
        target_resolution_payload = issue.target_resolution.model_dump(mode="json") if issue.target_resolution else None
        builder_request = compile_builder_repair_request(
            issue=issue,
            repair_case=repair_case,
            target_path=issue.target_path,
            expected_current_sha256=current_sha256,
            current_context=current_context,
            validation_profile=validation_profile,
            issue_contract=issue_contract,
            prior_patch_rejections=prior_patch_rejections,
            target_resolution=target_resolution_payload,
            repair_arbiter=arbiter_decision.model_dump(),
        )
        self.append_event(
            new_material_event(
                event_type="material.repair.builder_request.compiled",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="repairing",
                status="progress",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "case_id": repair_case.case_id,
                    "schema_version": builder_request.schema_version,
                    "target_path": builder_request.target_path,
                    "validation_profile": builder_request.validation_profile,
                    "evidence_keys": sorted(builder_request.command_evidence.keys()),
                    "allowed_actions": builder_request.allowed_actions,
                    "forbidden_actions": builder_request.forbidden_actions,
                },
            )
        )
        if arbiter_decision.request_critic_advisory:
            critic_repair = getattr(self._material_builder, "critique_repair", None)
            try:
                if critic_repair is None:
                    raise MaterialBuilderUnavailable(
                        "material_builder_critic_unavailable",
                        "material_builder client does not expose a material repair critic advisory lane",
                    )
                advisory = critic_repair(
                    session_id=session_id,
                    task_id=request.task_id,
                    plan=plan,
                    issue_id=builder_request.issue_id,
                    issue_contract=builder_request.issue_contract,
                    target_path=builder_request.target_path,
                    current_content=generated_file.content,
                    current_context=builder_request.current_context,
                    command_evidence=builder_request.command_evidence,
                    prior_patch_rejections=builder_request.prior_patch_rejections,
                    repair_arbiter=builder_request.command_evidence["repair_arbiter"],
                )
            except MaterialBuilderUnavailable as exc:
                self.append_event(
                    new_material_event(
                        event_type="material.repair.critic.unavailable",
                        session_id=session_id,
                        task_id=request.task_id,
                        source="material_builder",
                        phase="repairing",
                        status="progress",
                        latency_source="material_builder",
                        payload={
                            "issue_id": issue.issue_id,
                            "repair_round": repair_round,
                            "reason": str(exc)[:1000],
                            "advisory_only": True,
                        },
                    )
                )
            else:
                advisory_payload = {
                    "advisory_only": advisory.advisory_only,
                    "findings": advisory.findings,
                    "likely_root_cause": advisory.likely_root_cause,
                    "recommended_strategy": advisory.recommended_strategy,
                    "confidence": advisory.confidence,
                    "model_route": advisory.model_route,
                    "lane_metrics": advisory.lane_metrics,
                }
                current_context["critic_advisory"] = advisory_payload
                issue.details["critic_advisory"] = advisory_payload
                builder_request = compile_builder_repair_request(
                    issue=issue,
                    repair_case=repair_case,
                    target_path=issue.target_path,
                    expected_current_sha256=current_sha256,
                    current_context=current_context,
                    validation_profile=validation_profile,
                    issue_contract=issue_contract,
                    prior_patch_rejections=prior_patch_rejections,
                    target_resolution=target_resolution_payload,
                    repair_arbiter=arbiter_decision.model_dump(),
                )
                self.append_event(
                    new_material_event(
                        event_type="material.repair.critic.advisory",
                        session_id=session_id,
                        task_id=request.task_id,
                        source="material_builder",
                        phase="repairing",
                        status="completed",
                        latency_source="material_builder",
                        payload={
                            "issue_id": issue.issue_id,
                            "repair_round": repair_round,
                            **advisory_payload,
                        },
                    )
                )
        self.append_event(
            new_material_event(
                event_type="material.repair.started",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="repairing",
                status="started",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "issue_type": issue.issue_type,
                    "target_path": issue.target_path,
                    "repair_round": repair_round,
                },
            )
        )
        try:
            proposal = self._material_builder.propose_patch(
                session_id=session_id,
                task_id=request.task_id,
                plan=plan,
                issue_id=builder_request.issue_id,
                issue_contract=builder_request.issue_contract,
                target_path=builder_request.target_path,
                expected_current_sha256=builder_request.expected_current_sha256,
                current_content=generated_file.content,
                current_context=builder_request.current_context,
                validation_profile=builder_request.validation_profile,
                command_evidence=builder_request.command_evidence,
                prior_patch_rejections=builder_request.prior_patch_rejections,
                patch_blueprints=_patch_blueprints(request),
                target_resolution=builder_request.target_resolution,
                patch_set_blueprints=_patch_set_blueprints(request),
                replacement_blueprints=_replacement_blueprints(request),
                regeneration_blueprints=_regeneration_blueprints(request),
            )
        except MaterialBuilderUnavailable as exc:
            message = str(exc)
            if _looks_like_material_builder_schema_failure(message):
                self._reject_repair(
                    raw,
                    session_id,
                    issue,
                    reason="material_builder_patch_schema_invalid",
                    details={"message": message, "issue_id": issue.issue_id, "target_path": issue.target_path},
                )
                return
            if _looks_like_material_builder_contract_violation(message):
                self._reject_repair(
                    raw,
                    session_id,
                    issue,
                    reason="material_builder_patch_contract_invalid",
                    details={"message": message, "issue_id": issue.issue_id, "target_path": issue.target_path},
                )
                return
            self._record_workspace_issue(
                raw,
                session_id,
                WorkspaceIssue(
                    code="material_builder_patch_unavailable",
                    message=message,
                    details={"issue_id": issue.issue_id, "target_path": issue.target_path},
                ),
                target_status="failed_closed",
                target_kind=issue.target_kind,
                target_path=issue.target_path,
            )
            return
        repair = _normalize_repair_proposal(proposal)
        if repair.replacement is not None:
            self._apply_replacement_repair(
                raw,
                session_id,
                issue,
                generated_file,
                repair.replacement,
                current_sha256=current_sha256,
                repair_round=repair_round,
            )
            return
        if repair.regeneration is not None:
            self._reject_governed_repair_mode(
                raw,
                session_id,
                issue,
                event_type="material.regeneration.proposed",
                reason="regeneration_runner_unavailable",
                proposal={
                    "target_paths": repair.regeneration.target_paths,
                    "requirement_refs": repair.regeneration.requirement_refs,
                    "contract_refs": repair.regeneration.contract_refs,
                },
            )
            return
        if repair.patch_set is not None:
            self._apply_patch_set_repair(raw, session_id, issue, repair.patch_set, repair_round=repair_round)
            return
        patch = repair.patch
        if patch is None:
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason="patch_contract_mismatch",
                details={"message": "material builder returned no executable repair proposal"},
            )
            return
        if patch.target_path != issue.target_path or patch.expected_current_sha256 != current_sha256:
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason="patch_contract_mismatch",
                details={
                    "target_path": issue.target_path,
                    "patch_target_path": patch.target_path,
                    "expected_current_sha256": current_sha256,
                    "patch_expected_current_sha256": patch.expected_current_sha256,
                },
            )
            return
        diff_target_mismatch = _patch_diff_target_mismatch(patch.unified_diff, patch.target_path)
        if diff_target_mismatch:
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason="patch_contract_mismatch",
                details=diff_target_mismatch,
            )
            return
        if not patch.requirement_refs or not patch.contract_refs:
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason="patch_contract_mismatch",
                details={
                    "target_path": issue.target_path,
                    "missing_requirement_refs": not bool(patch.requirement_refs),
                    "missing_contract_refs": not bool(patch.contract_refs),
                },
            )
            return
        model_route = _builder_model_route(
            self._material_builder,
            session_id=session_id,
            task_id=request.task_id,
            phase="patch",
        )
        self.append_event(
            new_material_event(
                event_type="material.patch.proposed",
                session_id=session_id,
                task_id=request.task_id,
                source="material_builder",
                phase="repairing",
                status="progress",
                latency_source="material_builder",
                payload={
                    "issue_id": issue.issue_id,
                    "target_path": patch.target_path,
                    "expected_current_sha256": patch.expected_current_sha256,
                    "requirement_refs": patch.requirement_refs,
                    "contract_refs": patch.contract_refs,
                    "repair_round": repair_round,
                    "patch_attempt": repair_round + 1,
                    "model_route": model_route,
                },
            )
        )
        self.append_event(
            new_material_event(
                event_type="material.patch.critic.completed",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="repairing",
                status="completed",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "target_path": patch.target_path,
                    "verification_mode": "deterministic_patch_contract",
                    "requirement_refs_present": bool(patch.requirement_refs),
                    "contract_refs_present": bool(patch.contract_refs),
                    "expected_current_sha256_matched": patch.expected_current_sha256 == current_sha256,
                    "accepted_for_sandbox_apply": True,
                },
            )
        )
        result = self._workspace_client.apply_patch(
            session_id=session_id,
            material_session_id=session_id,
            vm_session_id=str(sandbox.vm_session_id),
            patch=patch,
            idempotency_key=f"{request.idempotency_key}:patch:{issue.issue_id}:round:{repair_round}",
        )
        if result.status != "completed" or not result.after_sha256:
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason=result.issue.code if result.issue else "patch_apply_failed",
                details={
                    "message": result.issue.message if result.issue else "patch apply failed",
                    "patch_set_id": result.patch_set_id,
                    "target_path": result.target_path,
                },
            )
            return
        raw["repair_rounds"] = repair_round + 1
        _mark_file_repaired(raw, issue.target_path, result.after_sha256)
        _clear_accepted_repair_issues(raw, issue)
        _invalidate_observed_contract_after_repair(raw)
        focused_profiles = _mark_repair_requires_revalidation(raw, issue)
        self.append_event(
            new_material_event(
                event_type="material.patch.applied",
                session_id=session_id,
                task_id=request.task_id,
                source="sandbox_owner",
                phase="repairing",
                status="progress",
                latency_source="sandbox",
                payload={
                    "issue_id": issue.issue_id,
                    "target_path": result.target_path,
                    "patch_set_id": result.patch_set_id,
                    "before_sha256": result.before_sha256,
                    "after_sha256": result.after_sha256,
                    "repair_round": repair_round,
                },
            )
        )
        self.append_event(
            new_material_event(
                event_type="material.repair.accepted",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="revalidating",
                status="completed",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "target_path": issue.target_path,
                    "remaining_issues": len(raw["issues"]),
                    "repair_round": repair_round + 1,
                    "focused_revalidation_profiles": focused_profiles,
                    "full_validation_required": bool(raw.get("full_validation_required")),
                },
            )
        )

    def _apply_replacement_repair(
        self,
        raw: dict[str, Any],
        session_id: str,
        issue: MaterialIssue,
        generated_file: GeneratedMaterialFile,
        replacement: MaterialReplacementProposal,
        *,
        current_sha256: str,
        repair_round: int,
    ) -> None:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        plan: MaterialPlanProposal = raw["plan"]
        replacement_file = GeneratedMaterialFile.from_text(
            path=replacement.target_path,
            content=replacement.replacement_content,
            kind=generated_file.kind,
        )
        mismatch: dict[str, object] = {}
        if replacement.target_path != issue.target_path:
            mismatch["target_path"] = issue.target_path
            mismatch["replacement_target_path"] = replacement.target_path
        if replacement.expected_current_sha256 != current_sha256:
            mismatch["expected_current_sha256"] = current_sha256
            mismatch["replacement_expected_current_sha256"] = replacement.expected_current_sha256
        if replacement.replacement_sha256 != replacement_file.sha256:
            mismatch["replacement_sha256"] = replacement_file.sha256
            mismatch["proposal_replacement_sha256"] = replacement.replacement_sha256
        if not replacement.requirement_refs or not replacement.contract_refs:
            mismatch["missing_requirement_refs"] = not bool(replacement.requirement_refs)
            mismatch["missing_contract_refs"] = not bool(replacement.contract_refs)
        if mismatch:
            self._reject_repair(raw, session_id, issue, reason="replacement_contract_mismatch", details=mismatch)
            return
        forbidden_imports = _replacement_forbidden_python_imports(raw, replacement_file)
        if forbidden_imports:
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason="replacement_contract_mismatch",
                details={
                    "message": "replacement content introduced imports outside the allowed dependency contract",
                    "target_path": replacement.target_path,
                    "forbidden_imports": forbidden_imports,
                    "expected_exports": _expected_python_exports_for_issue(issue),
                    "dependency_contract_guidance": (
                        "Do not satisfy missing local symbols by importing a package root from a child module "
                        "when that package root imports or re-exports the child module; define the required "
                        "symbols in the target module or in another planned local provider."
                    ),
                },
            )
            return
        missing_exports = _missing_expected_python_exports(issue, replacement_file.content)
        if missing_exports:
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason="replacement_contract_mismatch",
                details={
                    "message": "replacement content does not provide expected Python exports",
                    "target_path": replacement.target_path,
                    "missing_exports": missing_exports,
                    "expected_exports": _expected_python_exports_for_issue(issue),
                },
            )
            return
        if replacement_file.sha256 == current_sha256:
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason="replacement_noop",
                details={
                    "message": "replacement content did not change the repair target",
                    "target_path": replacement.target_path,
                    "replacement_sha256": replacement_file.sha256,
                },
            )
            return
        model_route = _builder_model_route(
            self._material_builder,
            session_id=session_id,
            task_id=request.task_id,
            phase="patch",
        )
        self.append_event(
            new_material_event(
                event_type="material.replacement.proposed",
                session_id=session_id,
                task_id=request.task_id,
                source="material_builder",
                phase="repairing",
                status="progress",
                latency_source="material_builder",
                payload={
                    "issue_id": issue.issue_id,
                    "target_path": replacement.target_path,
                    "expected_current_sha256": replacement.expected_current_sha256,
                    "replacement_sha256": replacement.replacement_sha256,
                    "requirement_refs": replacement.requirement_refs,
                    "contract_refs": replacement.contract_refs,
                    "repair_round": repair_round,
                    "replacement_attempt": repair_round + 1,
                    "model_route": model_route,
                },
            )
        )
        result = self._workspace_client.write_files_batch(
            session_id=session_id,
            material_session_id=session_id,
            vm_session_id=str(sandbox.vm_session_id),
            project_root=plan.project_root,
            files=[replacement_file],
            idempotency_key=f"{request.idempotency_key}:replacement:{issue.issue_id}:round:{repair_round}",
        )
        if result.status != "completed":
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason=result.issue.code if result.issue else "replacement_apply_failed",
                details={
                    "message": result.issue.message if result.issue else "replacement apply failed",
                    "target_path": replacement.target_path,
                    "state_hash": result.state_hash,
                },
            )
            return
        raw["repair_rounds"] = repair_round + 1
        _replace_generated_file(raw, replacement_file)
        _mark_file_repaired(raw, issue.target_path, replacement_file.sha256)
        _clear_accepted_repair_issues(raw, issue)
        _invalidate_observed_contract_after_repair(raw)
        focused_profiles = _mark_repair_requires_revalidation(raw, issue)
        self.append_event(
            new_material_event(
                event_type="material.replacement.applied",
                session_id=session_id,
                task_id=request.task_id,
                source="sandbox_owner",
                phase="repairing",
                status="progress",
                latency_source="sandbox",
                payload={
                    "issue_id": issue.issue_id,
                    "target_path": replacement.target_path,
                    "before_sha256": current_sha256,
                    "after_sha256": replacement_file.sha256,
                    "state_hash": result.state_hash,
                    "repair_round": repair_round,
                },
            )
        )
        self.append_event(
            new_material_event(
                event_type="material.repair.accepted",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="revalidating",
                status="completed",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "target_path": issue.target_path,
                    "remaining_issues": len(raw["issues"]),
                    "repair_round": repair_round + 1,
                    "repair_mode": "replacement",
                    "focused_revalidation_profiles": focused_profiles,
                    "full_validation_required": bool(raw.get("full_validation_required")),
                },
            )
        )

    def _apply_patch_set_repair(
        self,
        raw: dict[str, Any],
        session_id: str,
        issue: MaterialIssue,
        patch_set: MaterialPatchSetProposal,
        *,
        repair_round: int,
    ) -> None:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        mismatch = _patch_set_contract_mismatch(raw, issue, patch_set)
        if mismatch:
            self.append_event(
                new_material_event(
                    event_type="material.patch_set.rejected",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase="repairing",
                    status="failed",
                    latency_source="kernel",
                    payload={"issue_id": issue.issue_id, "reason": "patch_contract_mismatch", **mismatch},
                )
            )
            self._reject_repair(raw, session_id, issue, reason="patch_contract_mismatch", details=mismatch)
            return
        model_route = _builder_model_route(
            self._material_builder,
            session_id=session_id,
            task_id=request.task_id,
            phase="patch",
        )
        self.append_event(
            new_material_event(
                event_type="material.patch_set.proposed",
                session_id=session_id,
                task_id=request.task_id,
                source="material_builder",
                phase="repairing",
                status="progress",
                latency_source="material_builder",
                payload={
                    "issue_id": issue.issue_id,
                    "patch_count": len(patch_set.patches),
                    "target_paths": [patch.target_path for patch in patch_set.patches],
                    "requirement_refs": patch_set.requirement_refs,
                    "contract_refs": patch_set.contract_refs,
                    "repair_round": repair_round,
                    "patch_attempt": repair_round + 1,
                    "model_route": model_route,
                },
            )
        )
        self.append_event(
            new_material_event(
                event_type="material.patch.critic.completed",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="repairing",
                status="completed",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "verification_mode": "deterministic_patch_set_contract",
                    "patch_count": len(patch_set.patches),
                    "target_paths": [patch.target_path for patch in patch_set.patches],
                    "requirement_refs_present": bool(patch_set.requirement_refs),
                    "contract_refs_present": bool(patch_set.contract_refs),
                    "accepted_for_sandbox_apply": True,
                },
            )
        )
        result = self._workspace_client.apply_patch_set(
            session_id=session_id,
            material_session_id=session_id,
            vm_session_id=str(sandbox.vm_session_id),
            patch_set=patch_set,
            idempotency_key=f"{request.idempotency_key}:patch-set:{issue.issue_id}:round:{repair_round}",
        )
        if result.status != "completed":
            details = {
                "message": result.issue.message if result.issue else "patch set apply failed",
                "patch_set_id": result.patch_set_id,
                "target_paths": [patch.target_path for patch in patch_set.patches],
                "applied_patches": [
                    {
                        "target_path": patch.target_path,
                        "after_sha256": patch.after_sha256,
                        "status": patch.status,
                    }
                    for patch in result.patches
                ],
            }
            self.append_event(
                new_material_event(
                    event_type="material.patch_set.rejected",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="sandbox_owner",
                    phase="repairing",
                    status="failed",
                    latency_source="sandbox",
                    payload={"issue_id": issue.issue_id, "reason": result.issue.code if result.issue else "patch_set_apply_failed", **details},
                )
            )
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason=result.issue.code if result.issue else "patch_set_apply_failed",
                details=details,
            )
            return
        missing_hashes = [
            patch.target_path
            for patch in result.patches
            if not patch.before_sha256 or not patch.after_sha256
        ]
        if missing_hashes:
            details = {
                "message": "sandbox did not prove before/after hashes for every patch in the set",
                "patch_set_id": result.patch_set_id,
                "target_paths": missing_hashes,
            }
            self.append_event(
                new_material_event(
                    event_type="material.patch_set.rejected",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase="repairing",
                    status="failed",
                    latency_source="kernel",
                    payload={"issue_id": issue.issue_id, "reason": "patch_set_missing_hash_evidence", **details},
                )
            )
            self._reject_repair(
                raw,
                session_id,
                issue,
                reason="patch_set_missing_hash_evidence",
                details=details,
            )
            return
        raw["repair_rounds"] = repair_round + 1
        for patch in result.patches:
            if patch.after_sha256:
                _mark_file_repaired(raw, patch.target_path, patch.after_sha256)
        _clear_accepted_repair_issues(raw, issue)
        _invalidate_observed_contract_after_repair(raw)
        focused_profiles = _mark_repair_requires_revalidation(raw, issue)
        self.append_event(
            new_material_event(
                event_type="material.patch_set.applied",
                session_id=session_id,
                task_id=request.task_id,
                source="sandbox_owner",
                phase="repairing",
                status="progress",
                latency_source="sandbox",
                payload={
                    "issue_id": issue.issue_id,
                    "patch_set_id": result.patch_set_id,
                    "patches": [
                        {
                            "target_path": patch.target_path,
                            "before_sha256": patch.before_sha256,
                            "after_sha256": patch.after_sha256,
                        }
                        for patch in result.patches
                    ],
                    "repair_round": repair_round,
                },
            )
        )
        self.append_event(
            new_material_event(
                event_type="material.repair.accepted",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="revalidating",
                status="completed",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "target_path": issue.target_path,
                    "remaining_issues": len(raw["issues"]),
                    "repair_round": repair_round + 1,
                    "repair_mode": "patch_set",
                    "focused_revalidation_profiles": focused_profiles,
                    "full_validation_required": bool(raw.get("full_validation_required")),
                },
            )
        )

    def _reject_governed_repair_mode(
        self,
        raw: dict[str, Any],
        session_id: str,
        issue: MaterialIssue,
        *,
        event_type: str,
        reason: str,
        proposal: dict[str, Any],
    ) -> None:
        request: MaterialSessionRequest = raw["request"]
        self.append_event(
            new_material_event(
                event_type=event_type,
                session_id=session_id,
                task_id=request.task_id,
                source="material_builder",
                phase="repairing",
                status="progress",
                latency_source="material_builder",
                payload={"issue_id": issue.issue_id, **proposal},
            )
        )
        self._reject_repair(
            raw,
            session_id,
            issue,
            reason=reason,
            details={"message": "repair mode is governed but no active sandbox runner supports it", **proposal},
        )

    def _reject_repair(
        self,
        raw: dict[str, Any],
        session_id: str,
        issue: MaterialIssue,
        *,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        request: MaterialSessionRequest = raw["request"]
        repair_round = int(raw.get("repair_rounds") or 0)
        rejection_attempt = len(issue.patch_rejections) + 1
        arbiter_rejection = arbitrate_repair_rejection(
            issue,
            reason=reason,
            details=details,
            rejection_attempt=rejection_attempt,
            max_repair_rounds=request.max_repair_rounds,
        )
        details = {**details, "repair_arbiter": arbiter_rejection.model_dump()}
        _record_repair_arbiter_rejection(
            raw,
            {
                "issue_id": issue.issue_id,
                "repair_round": repair_round,
                "attempt": rejection_attempt,
                "reason": reason,
                **arbiter_rejection.model_dump(),
            },
        )
        can_retry = reason in _PATCH_REPAIR_RETRY_REASONS and arbiter_rejection.retryable
        can_defer = (not can_retry) and _should_defer_exhausted_repair_rejection(
            raw,
            issue,
            reason=reason,
            details=details,
        )
        rejection = PatchRejectionEvidence(
            rejection_id=f"patch_rejection:{issue.issue_id}:{rejection_attempt}",
            issue_id=issue.issue_id,
            attempt=rejection_attempt,
            reason=reason,
            retryable=can_retry or can_defer,
            target_path=issue.target_path,
            patch_set_id=str(details.get("patch_set_id")) if details.get("patch_set_id") is not None else None,
            message=_bounded_diagnostic_text(details.get("message")),
            diagnostics={str(key): value for key, value in details.items()},
        )
        self.append_event(
            new_material_event(
                event_type="material.repair.rejected",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="repairing" if can_retry or can_defer else "failed_closed",
                status="progress" if can_retry or can_defer else "failed",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "reason": reason,
                    "repair_round": repair_round,
                    "patch_attempt": rejection.attempt,
                    "proposal_rejection_count": rejection_attempt,
                    "retryable": can_retry or can_defer,
                    "deferred": can_defer,
                    "repair_arbiter": arbiter_rejection.model_dump(),
                    "rejection": rejection.model_dump(mode="json"),
                    **details,
                },
            )
        )
        if can_retry:
            if reason not in _PROPOSAL_ONLY_REJECTION_REASONS:
                raw["repair_rounds"] = repair_round + 1
            issue.patch_rejections.append(rejection)
            issue.details["last_patch_rejection"] = rejection.model_dump(mode="json")
            issue.details["proposal_rejection_count"] = len(issue.patch_rejections)
            issue.details["repair_round"] = int(raw.get("repair_rounds") or 0)
            raw["status"] = "repairing"
            raw["manifest"].status = "repairing"
            raw["manifest"].issues = list(raw["issues"])
            return
        if can_defer:
            issue.patch_rejections.append(rejection)
            issue.details["last_patch_rejection"] = rejection.model_dump(mode="json")
            issue.details["proposal_rejection_count"] = len(issue.patch_rejections)
            issue.details["deferred_repair_reason"] = reason
            issue.details["deferred_until_alternative_symbol_repairs"] = True
            _move_issue_to_end(raw, issue)
            raw["status"] = "repairing"
            raw["manifest"].status = "repairing"
            raw["manifest"].issues = list(raw["issues"])
            return
        failed_details = {
            **details,
            "patch_rejections": [
                item.model_dump(mode="json") for item in [*issue.patch_rejections, rejection]
            ],
        }
        self._record_workspace_issue(
            raw,
            session_id,
            WorkspaceIssue(
                code=reason,
                message=_bounded_diagnostic_text(details.get("message") or reason) or reason,
                details=failed_details,
            ),
            target_status="failed_closed",
            target_kind=issue.target_kind,
            target_path=issue.target_path,
        )

    def _package_artifact(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        plan: MaterialPlanProposal = raw["plan"]
        if not self._write_validation_evidence_file(raw, session_id):
            return
        result = self._workspace_client.package_artifact(
            session_id=session_id,
            material_session_id=session_id,
            vm_session_id=str(sandbox.vm_session_id),
            project_root=plan.project_root,
            idempotency_key=f"{request.idempotency_key}:artifact",
        )
        if result.status != "completed":
            self._record_workspace_issue(
                raw,
                session_id,
                result.issue or WorkspaceIssue(code="artifact_packaging_failed", message="artifact packaging failed"),
                target_status="failed_closed",
                target_kind="artifact",
            )
            return
        artifact = ArtifactEvidence(path=result.path, sha256=result.sha256, size_bytes=result.size_bytes)
        if request.constraints.durable_publish:
            if not result.artifact_id:
                self._record_workspace_issue(
                    raw,
                    session_id,
                    WorkspaceIssue(
                        code="artifact_publish_failed",
                        message="sandbox package response did not include an artifact id for durable publication",
                        details={"artifact_path": result.path},
                    ),
                    target_status="failed_closed",
                    target_kind="artifact",
                )
                return
            publish_target = _artifact_publish_target(request, session_id=session_id, artifact_path=result.path)
            publish = self._workspace_client.publish_artifact(
                session_id=session_id,
                material_session_id=session_id,
                artifact_id=result.artifact_id,
                target=publish_target,
                idempotency_key=f"{request.idempotency_key}:artifact:publish",
            )
            if publish.status not in {"published", "already_published"}:
                self._record_workspace_issue(
                    raw,
                    session_id,
                    publish.issue or WorkspaceIssue(code="artifact_publish_failed", message="artifact publication failed"),
                    target_status="failed_closed",
                    target_kind="artifact",
                )
                return
            artifact = artifact.model_copy(
                update={
                    "storage_object_ref": publish.storage_object_ref,
                    "chain_of_custody_ref": publish.chain_of_custody_ref,
                    "materialized_path": publish.materialized_path,
                    "materialized_sha256": publish.materialized_sha256,
                    "extracted_path": publish.extracted_path,
                    "extracted_files_count": publish.extracted_files_count,
                    "extracted_top_level_paths": publish.extracted_top_level_paths,
                }
            )
            missing_materialization = [
                field
                for field, value in {
                    "storage_object_ref": artifact.storage_object_ref,
                    "materialized_path": artifact.materialized_path,
                    "materialized_sha256": artifact.materialized_sha256,
                    "extracted_path": artifact.extracted_path,
                }.items()
                if not value
            ]
            if missing_materialization:
                self._record_workspace_issue(
                    raw,
                    session_id,
                    WorkspaceIssue(
                        code="artifact_materialization_incomplete",
                        message="storage_guardian publication did not return complete materialization evidence",
                        details={
                            "artifact_id": result.artifact_id,
                            "missing_fields": missing_materialization,
                            "storage_object_ref": artifact.storage_object_ref,
                            "materialized_path": artifact.materialized_path,
                            "extracted_path": artifact.extracted_path,
                        },
                    ),
                    target_status="failed_closed",
                    target_kind="artifact",
                )
                return
            self.append_event(
                new_material_event(
                    event_type="material.artifact.published",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="sandbox_owner",
                    phase="packaging",
                    status="completed",
                    latency_source="storage",
                    payload={
                        "artifact_id": result.artifact_id,
                        "artifact_path": result.path,
                        "sha256": result.sha256,
                        "storage_object_ref": publish.storage_object_ref,
                        "chain_of_custody_ref": publish.chain_of_custody_ref,
                        "materialized_path": publish.materialized_path,
                        "materialized_sha256": publish.materialized_sha256,
                        "extracted_path": publish.extracted_path,
                        "extracted_files_count": publish.extracted_files_count,
                        "extracted_top_level_paths": publish.extracted_top_level_paths,
                    },
                )
            )
        raw["artifact"] = artifact
        raw["manifest"].artifact = MaterialManifestArtifact(
            status="created",
            path=result.path,
            sha256=result.sha256,
            size_bytes=result.size_bytes,
            storage_object_ref=artifact.storage_object_ref,
            chain_of_custody_ref=artifact.chain_of_custody_ref,
            materialized_path=artifact.materialized_path,
            materialized_sha256=artifact.materialized_sha256,
            extracted_path=artifact.extracted_path,
            extracted_files_count=artifact.extracted_files_count,
            extracted_top_level_paths=artifact.extracted_top_level_paths,
        )
        self.append_event(
            new_material_event(
                event_type="material.package.created",
                session_id=session_id,
                task_id=request.task_id,
                source="sandbox_owner",
                phase="packaging",
                status="completed",
                latency_source="sandbox",
                payload={
                    "artifact_path": result.path,
                    "sha256": result.sha256,
                    "storage_object_ref": artifact.storage_object_ref,
                    "chain_of_custody_ref": artifact.chain_of_custody_ref,
                    "materialized_path": artifact.materialized_path,
                    "materialized_sha256": artifact.materialized_sha256,
                    "extracted_path": artifact.extracted_path,
                    "extracted_files_count": artifact.extracted_files_count,
                    "extracted_top_level_paths": artifact.extracted_top_level_paths,
                },
            )
        )

    def _write_validation_evidence_file(self, raw: dict[str, Any], session_id: str) -> bool:
        target_path = _validation_evidence_target_path(raw)
        if target_path is None:
            return True
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        plan: MaterialPlanProposal = raw["plan"]
        evidence = GeneratedMaterialFile.from_text(
            path=target_path,
            kind=_manifest_kind_for_path(target_path),
            content=_render_validation_evidence(raw, session_id=session_id),
        )
        result = self._workspace_client.write_files_batch(
            session_id=session_id,
            material_session_id=session_id,
            vm_session_id=str(sandbox.vm_session_id),
            project_root=plan.project_root,
            files=[evidence],
            idempotency_key=f"{request.idempotency_key}:validation-evidence:{len(raw.get('command_runs') or [])}",
        )
        if result.status != "completed":
            self._record_workspace_issue(
                raw,
                session_id,
                result.issue
                or WorkspaceIssue(
                    code="validation_evidence_write_failed",
                    message="validation evidence file could not be written before artifact packaging",
                    details={"target_path": target_path},
                ),
                target_status="failed_closed",
                target_kind="validation_evidence",
                target_path=target_path,
            )
            return False
        _replace_generated_file(raw, evidence)
        _mark_manifest_file_written(raw, target_path, evidence.sha256, producer="material_execution_kernel")
        self.append_event(
            new_material_event(
                event_type="material.validation_evidence.written",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="packaging",
                status="completed",
                latency_source="sandbox",
                payload={
                    "path": target_path,
                    "sha256": evidence.sha256,
                    "command_runs": [item.command_run_id for item in raw.get("command_runs", [])],
                },
            )
        )
        return True

    def _complete(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        cleanup = self._workspace_client.cleanup_vm(
            session_id=session_id,
            vm_session_id=str(sandbox.vm_session_id),
            idempotency_key=f"{request.idempotency_key}:vm:cleanup",
        )
        if not cleanup.cleanup_recorded:
            self._record_workspace_issue(
                raw,
                session_id,
                cleanup.issue or WorkspaceIssue(code="vm_cleanup_missing", message="VM cleanup evidence is missing"),
                target_status="failed_closed",
                target_kind="vm_cleanup",
            )
            return
        raw["sandbox"] = sandbox.model_copy(update={"cleanup_recorded": True})
        raw["manifest"].sandbox["cleanup_recorded"] = True
        self.append_event(
            new_material_event(
                event_type="material.vm.cleanup.completed",
                session_id=session_id,
                task_id=request.task_id,
                source="sandbox_owner",
                phase="packaging",
                status="progress",
                latency_source="vm",
                payload={"vm_session_id": sandbox.vm_session_id},
            )
        )
        raw["status"] = "completed"
        raw["manifest"].status = "completed"
        self.append_event(
            new_material_event(
                event_type="material.completed",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase="completed",
                status="completed",
                latency_source="kernel",
                payload={
                    "artifact": raw["artifact"].model_dump(mode="json") if raw["artifact"] else None,
                    "validation_summary": raw["validation_summary"].model_dump(mode="json"),
                },
            )
        )

    def _accept_plan(self, raw: dict[str, Any], plan: MaterialPlanProposal) -> None:
        raw["plan"] = plan
        manifest: MaterialManifest = raw["manifest"]
        manifest.project_root = plan.project_root
        manifest.files = [
            MaterialManifestFile(path=file.path, purpose=file.purpose, kind=file.kind, state="planned")
            for file in plan.files
        ]
        manifest.required_validation_profiles = list(plan.required_validation_profiles)
        manifest.optional_validation_profiles = list(plan.optional_validation_profiles)
        material_contract = raw.get("material_contract")
        if material_contract is not None:
            manifest.material_contract = material_contract.model_dump(mode="json")
        observed_contract = raw.get("observed_contract")
        if observed_contract is not None:
            manifest.observed_contract = observed_contract.model_dump(mode="json")
        interface_ledger = raw.get("interface_ledger")
        if interface_ledger is not None:
            manifest.interface_ledger = interface_ledger
            manifest.repair_obligations = list(raw.get("repair_obligations") or [])
        repair_arbiter = raw.get("repair_arbiter")
        if repair_arbiter is not None:
            manifest.repair_arbiter = repair_arbiter
        manifest.repair_cases = list(raw.get("repair_cases") or [])
        contract_comparison = raw.get("contract_comparison")
        if contract_comparison is not None:
            manifest.requirements_trace = list(contract_comparison.requirements_trace)
            manifest.contract_comparison = contract_comparison.model_dump(mode="json")

    def _extract_observed_contract(
        self,
        raw: dict[str, Any],
        session_id: str,
        *,
        phase: str = "workspace_materializing",
    ) -> None:
        if raw.get("observed_contract") is not None:
            return
        request: MaterialSessionRequest = raw["request"]
        plan: MaterialPlanProposal = raw["plan"]
        files: list[GeneratedMaterialFile] = raw["generated_files"]
        observed_contract = extract_observed_contract(
            session_id=session_id,
            task_id=request.task_id,
            project_root=plan.project_root,
            files=files,
        )
        raw["observed_contract"] = observed_contract
        raw["manifest"].observed_contract = observed_contract.model_dump(mode="json")
        material_contract: MaterialContract | None = raw.get("material_contract")
        interface_ledger = build_interface_ledger(
            material_contract=material_contract,
            observed_contract=observed_contract,
        )
        repair_obligations = build_repair_obligations(
            interface_ledger=interface_ledger,
            observed_contract=observed_contract,
        )
        raw["interface_ledger"] = interface_ledger
        raw["repair_obligations"] = repair_obligations
        raw["manifest"].interface_ledger = interface_ledger
        raw["manifest"].repair_obligations = repair_obligations
        self.append_event(
            new_material_event(
                event_type="material.observed_contract.extracted",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase=phase,
                status="progress",
                latency_source="kernel",
                payload={
                    "observed_contract_id": observed_contract.observed_contract_id,
                    "schema_version": observed_contract.schema_version,
                    "ecosystems": observed_contract.ecosystems,
                    "file_count": len(observed_contract.files),
                    "import_count": len(observed_contract.imports),
                    "export_count": len(observed_contract.exports),
                    "dependency_count": len(observed_contract.dependencies),
                    "test_expectation_count": len(observed_contract.test_expectations),
                    "entrypoint_count": len(observed_contract.entrypoints),
                    "issue_count": len(observed_contract.issues),
                    "interface_ledger_id": interface_ledger["observed_contract_id"],
                    "repair_obligation_count": len(repair_obligations),
                },
            )
        )

    def _compare_contracts(
        self,
        raw: dict[str, Any],
        session_id: str,
        *,
        phase: str = "workspace_materializing",
    ) -> None:
        request: MaterialSessionRequest = raw["request"]
        material_contract: MaterialContract | None = raw.get("material_contract")
        observed_contract: ObservedContract | None = raw.get("observed_contract")
        if material_contract is None or observed_contract is None:
            self._block_with_issue(
                raw,
                session_id,
                issue_type="contract_comparison_missing_input",
                message="contract comparison requires both material and observed contracts",
                details={
                    "has_material_contract": material_contract is not None,
                    "has_observed_contract": observed_contract is not None,
                },
                target_status="failed_closed",
                target_kind="contract_comparison",
            )
            return
        if raw.get("contract_comparison") is not None:
            if not (raw.get("contract_comparison_deferred_until_runtime") and raw.get("command_runs")):
                return
            comparison = raw["contract_comparison"]
            raw["contract_comparison_deferred_until_runtime"] = False
        else:
            comparison = compare_contracts(
                material_contract=material_contract,
                observed_contract=observed_contract,
            )
            raw["contract_comparison"] = comparison
            raw["manifest"].requirements_trace = list(comparison.requirements_trace)
            raw["manifest"].contract_comparison = comparison.model_dump(mode="json")
        if any(issue.issue_type == "contract_change_required" for issue in comparison.issues):
            self.append_event(
                new_material_event(
                    event_type="material.contract.change.required",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase=phase,
                    status="progress",
                    latency_source="kernel",
                    payload={
                        "comparison_id": comparison.comparison_id,
                        "issues": [
                            issue.model_dump(mode="json")
                            for issue in comparison.issues
                            if issue.issue_type == "contract_change_required"
                        ],
                    },
                )
            )
        if comparison.blocking_issue_count:
            blocking_issues = [issue for issue in comparison.issues if issue.severity == "blocking_completion"]
            repairable_issue_count = sum(1 for issue in blocking_issues if _is_repairable_contract_comparison_issue(issue))
            has_non_repairable_issue = repairable_issue_count < len(blocking_issues)
            if not raw.get("command_runs") and has_non_repairable_issue:
                raw["contract_comparison_deferred_until_runtime"] = True
                self.append_event(
                    new_material_event(
                        event_type="material.contract_comparison.deferred",
                        session_id=session_id,
                        task_id=request.task_id,
                        source="kernel",
                        phase=phase,
                        status="progress",
                        latency_source="kernel",
                        payload={
                            "comparison_id": comparison.comparison_id,
                            "blocking_issue_count": comparison.blocking_issue_count,
                            "repairable_issue_count": repairable_issue_count,
                            "has_non_repairable_issue": has_non_repairable_issue,
                            "reason": "runtime validation evidence is required before static contract issues can block execution",
                            "issue_types": [issue.issue_type for issue in comparison.issues],
                        },
                    )
                )
                return
            self.append_event(
                new_material_event(
                    event_type="material.contract_comparison.failed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase="failed_closed" if has_non_repairable_issue else "repairing",
                    status="failed" if has_non_repairable_issue else "progress",
                    latency_source="kernel",
                    payload={
                        "comparison_id": comparison.comparison_id,
                        "blocking_issue_count": comparison.blocking_issue_count,
                        "repairable_issue_count": repairable_issue_count,
                        "issue_types": [issue.issue_type for issue in comparison.issues],
                    },
                )
            )
            for issue in comparison.issues:
                if issue.severity != "blocking_completion":
                    continue
                target_path = _target_path_for_contract_comparison_issue(raw, issue)
                repairable = bool(target_path) and _is_repairable_contract_comparison_issue(issue)
                self._record_workspace_issue(
                    raw,
                    session_id,
                    WorkspaceIssue(
                        code=issue.issue_type,
                        message=f"contract comparison failed: {issue.issue_type}",
                        details={
                            "comparison_id": comparison.comparison_id,
                            "requirement_id": issue.requirement_id,
                            **issue.details,
                        },
                    ),
                    target_status="repairing" if repairable else "failed_closed",
                    target_kind="contract_comparison",
                    target_path=target_path,
                    severity="repairable" if repairable else "blocking_completion",
                )
            return
        raw["contract_comparison_deferred_until_runtime"] = False
        self.append_event(
            new_material_event(
                    event_type="material.contract_comparison.completed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase=phase,
                    status="completed",
                latency_source="kernel",
                payload={
                    "comparison_id": comparison.comparison_id,
                    "status": comparison.status,
                    "trace_count": len(comparison.requirements_trace),
                    "issue_count": len(comparison.issues),
                    "blocking_issue_count": comparison.blocking_issue_count,
                },
            )
        )

    def _block_with_issue(
        self,
        raw: dict[str, Any],
        session_id: str,
        *,
        issue_type: str,
        message: str,
        details: dict[str, Any],
        target_status: MaterialSessionStatus,
        target_kind: str = "kernel_contract",
        target_path: str | None = None,
    ) -> None:
        self._record_workspace_issue(
            raw,
            session_id,
            WorkspaceIssue(code=issue_type, message=message, details=details),
            target_status=target_status,
            target_kind=target_kind,
            target_path=target_path,
        )

    def _record_workspace_issue(
        self,
        raw: dict[str, Any],
        session_id: str,
        issue: WorkspaceIssue,
        *,
        target_status: MaterialSessionStatus,
        target_kind: str,
        target_path: str | None = None,
        severity: str = "blocking_completion",
    ) -> None:
        request: MaterialSessionRequest = raw["request"]
        target_resolution = _target_resolution_from_details(issue.details, target_path=target_path)
        material_issue = MaterialIssue(
            issue_id=f"issue_{uuid4().hex}",
            issue_type=issue.code,
            severity=severity,
            target_kind=target_kind,
            target_path=target_path,
            target_resolution=target_resolution,
            requirement_refs=_requirement_refs_for_issue(raw, target_path=target_path, profile=issue.details.get("profile")),
            contract_refs=_contract_refs_for_issue(raw),
            details={"message": issue.message, **issue.details},
        )
        issue_obligations = obligations_for_issue(
            list(raw.get("repair_obligations") or []),
            target_path=target_path,
            issue_details=material_issue.details,
        )
        if issue_obligations:
            material_issue.details["repair_obligations"] = issue_obligations
        if severity == "repairable":
            repair_case = self._compile_and_attach_repair_case(
                raw,
                session_id,
                material_issue,
                target_sha256=_manifest_hash(raw, material_issue.target_path) if material_issue.target_path else None,
            )
            if repair_case.status in {"under_specified", "blocked", "failed_closed"}:
                material_issue.severity = "blocking_completion"
                material_issue.details["repair_skipped_reason"] = repair_case.status
                target_status = "failed_closed"
                severity = "blocking_completion"
            primary = repair_case.primary_repair_target
            if primary is not None and primary.path and primary.path != material_issue.target_path:
                previous_target = material_issue.target_path
                material_issue.details["previous_target_path"] = previous_target
                material_issue.details["previous_target_resolution"] = (
                    material_issue.target_resolution.model_dump(mode="json")
                    if material_issue.target_resolution is not None
                    else None
                )
                material_issue.target_path = primary.path
                material_issue.target_kind = _target_kind_for_path(raw, primary.path)
                material_issue.requirement_refs = _requirement_refs_for_issue(
                    raw,
                    target_path=primary.path,
                    profile=material_issue.details.get("profile"),
                )
                material_issue.target_resolution = RepairTargetResolution(
                    primary_target=primary.path,
                    related_targets=[target.path for target in repair_case.related_targets],
                    candidate_targets=_dedupe_strings(
                        [
                            primary.path,
                            *[target.path for target in repair_case.symptom_targets],
                            *[target.path for target in repair_case.related_targets],
                        ]
                    ),
                    confidence=max(primary.confidence, repair_case.confidence),
                    rationale=(
                        "repair case selected provider-owned target; validation surface paths are "
                        "symptom or related targets"
                    ),
                )
                material_issue.details["target_resolution"] = material_issue.target_resolution.model_dump(mode="json")
                self.append_event(
                    new_material_event(
                        event_type="material.repair.target.disagreement",
                        session_id=session_id,
                        task_id=request.task_id,
                        source="kernel",
                        phase=target_status,
                        status="progress",
                        latency_source="kernel",
                        payload={
                            "issue_id": material_issue.issue_id,
                            "previous_target": previous_target,
                            "repair_case_primary_target": primary.path,
                            "symptom_targets": [target.path for target in repair_case.symptom_targets],
                            "provider_resolution": repair_case.provider_resolution.model_dump(mode="json")
                            if repair_case.provider_resolution is not None
                            else None,
                            "confidence": repair_case.confidence,
                            "root_cause_kind": repair_case.root_cause_kind,
                        },
                    )
                )
        raw["issues"].append(material_issue)
        raw["manifest"].issues = list(raw["issues"])
        if target_status in _FAILURE_SNAPSHOT_STATUSES:
            self._materialize_failure_snapshot(raw, session_id, material_issue, phase=target_status)
        raw["status"] = target_status
        raw["manifest"].status = target_status
        self.append_event(
            new_material_event(
                event_type=f"material.{target_status}",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase=target_status,
                status=_event_status_for_issue_state(target_status),
                latency_source="kernel",
                payload={"issue": material_issue.model_dump(mode="json")},
            )
        )

    def _materialize_failure_snapshot(
        self,
        raw: dict[str, Any],
        session_id: str,
        issue: MaterialIssue,
        *,
        phase: str = "failed_closed",
    ) -> None:
        if raw.get("artifact") is not None:
            return
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        plan: MaterialPlanProposal | None = raw.get("plan")
        if sandbox.vm_session_id is None:
            return
        project_root = _failure_snapshot_project_root(request, plan=plan, session_id=session_id)
        if raw["manifest"].project_root is None:
            raw["manifest"].project_root = project_root
        self._materialize_failure_snapshot_inputs(raw, session_id, issue, phase=phase)
        result = self._workspace_client.package_artifact(
            session_id=session_id,
            material_session_id=session_id,
            vm_session_id=str(sandbox.vm_session_id),
            project_root=project_root,
            idempotency_key=f"{request.idempotency_key}:failure-snapshot",
        )
        if result.status != "completed":
            self.append_event(
                new_material_event(
                    event_type="material.failure_snapshot.failed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="sandbox_owner",
                    phase=phase,
                    status="failed",
                    latency_source="sandbox",
                    payload={
                        "issue_id": issue.issue_id,
                        "reason": "artifact_packaging_failed",
                        "workspace_issue": _workspace_issue_payload(result.issue),
                    },
                )
            )
            return
        artifact = ArtifactEvidence(path=result.path, sha256=result.sha256, size_bytes=result.size_bytes)
        if request.constraints.durable_publish and result.artifact_id:
            publish_target = _artifact_publish_target(request, session_id=session_id, artifact_path=result.path)
            publish = self._workspace_client.publish_artifact(
                session_id=session_id,
                material_session_id=session_id,
                artifact_id=result.artifact_id,
                target=publish_target,
                idempotency_key=f"{request.idempotency_key}:failure-snapshot:publish",
            )
            if publish.status in {"published", "already_published"}:
                artifact = artifact.model_copy(
                    update={
                        "storage_object_ref": publish.storage_object_ref,
                        "chain_of_custody_ref": publish.chain_of_custody_ref,
                        "materialized_path": publish.materialized_path,
                        "materialized_sha256": publish.materialized_sha256,
                        "extracted_path": publish.extracted_path,
                        "extracted_files_count": publish.extracted_files_count,
                        "extracted_top_level_paths": publish.extracted_top_level_paths,
                    }
                )
            else:
                self.append_event(
                    new_material_event(
                        event_type="material.failure_snapshot.publish_failed",
                        session_id=session_id,
                        task_id=request.task_id,
                        source="sandbox_owner",
                        phase=phase,
                        status="failed",
                        latency_source="storage",
                        payload={
                            "issue_id": issue.issue_id,
                            "artifact_id": result.artifact_id,
                            "workspace_issue": _workspace_issue_payload(publish.issue),
                        },
                    )
                )
        raw["artifact"] = artifact
        raw["manifest"].artifact = MaterialManifestArtifact(
            status="failed_snapshot",
            path=result.path,
            sha256=result.sha256,
            size_bytes=result.size_bytes,
            storage_object_ref=artifact.storage_object_ref,
            chain_of_custody_ref=artifact.chain_of_custody_ref,
            materialized_path=artifact.materialized_path,
            materialized_sha256=artifact.materialized_sha256,
            extracted_path=artifact.extracted_path,
            extracted_files_count=artifact.extracted_files_count,
            extracted_top_level_paths=artifact.extracted_top_level_paths,
        )
        self.append_event(
            new_material_event(
                event_type="material.failure_snapshot.materialized",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase=phase,
                status="completed",
                latency_source="storage" if artifact.materialized_path else "sandbox",
                payload={
                    "issue_id": issue.issue_id,
                    "artifact_path": result.path,
                    "sha256": result.sha256,
                    "storage_object_ref": artifact.storage_object_ref,
                    "chain_of_custody_ref": artifact.chain_of_custody_ref,
                    "materialized_path": artifact.materialized_path,
                    "materialized_sha256": artifact.materialized_sha256,
                    "extracted_path": artifact.extracted_path,
                    "extracted_files_count": artifact.extracted_files_count,
                    "extracted_top_level_paths": artifact.extracted_top_level_paths,
                    "validated": False,
                },
            )
        )

    def _materialize_failure_snapshot_inputs(
        self,
        raw: dict[str, Any],
        session_id: str,
        issue: MaterialIssue,
        *,
        phase: str,
    ) -> None:
        request: MaterialSessionRequest = raw["request"]
        sandbox: SandboxEvidence = raw["sandbox"]
        plan: MaterialPlanProposal | None = raw.get("plan")
        project_root = _failure_snapshot_project_root(request, plan=plan, session_id=session_id)
        generated_files = [] if _workspace_materialized(raw) else list(raw.get("generated_files") or [])
        snapshot_files = _failure_snapshot_diagnostic_files(
            raw,
            session_id=session_id,
            issue=issue,
            phase=phase,
            project_root=project_root,
        )
        files = [*generated_files, *snapshot_files]
        if sandbox.vm_session_id is None or not files:
            return
        _ensure_failure_snapshot_manifest_files(raw, snapshot_files)
        try:
            result = self._workspace_client.write_files_batch(
                session_id=session_id,
                material_session_id=session_id,
                vm_session_id=str(sandbox.vm_session_id),
                project_root=project_root,
                files=files,
                idempotency_key=f"{request.idempotency_key}:failure-snapshot:files",
            )
        except Exception as exc:
            self.append_event(
                new_material_event(
                    event_type="material.failure_snapshot.workspace_write_failed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="sandbox_owner",
                    phase=phase,
                    status="failed",
                    latency_source="sandbox",
                    payload={
                        "issue_id": issue.issue_id,
                        "reason": "workspace_batch_write_exception",
                        "error": str(exc)[:1000],
                        "file_count": len(files),
                        "generated_file_count": len(generated_files),
                        "diagnostic_file_count": len(snapshot_files),
                    },
                )
            )
            return
        if result.status != "completed":
            self.append_event(
                new_material_event(
                    event_type="material.failure_snapshot.workspace_write_failed",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="sandbox_owner",
                    phase=phase,
                    status="failed",
                    latency_source="sandbox",
                    payload={
                        "issue_id": issue.issue_id,
                        "reason": "workspace_batch_write_failed",
                        "workspace_issue": _workspace_issue_payload(result.issue),
                        "file_count": len(files),
                        "generated_file_count": len(generated_files),
                        "diagnostic_file_count": len(snapshot_files),
                    },
                )
            )
            return
        written_paths = set(result.written_paths)
        written_manifest_paths = {
            _failure_snapshot_manifest_path(project_root, path)
            for path in written_paths
        }
        for item in raw["manifest"].files:
            if item.path in written_paths or item.path in written_manifest_paths:
                item.state = "workspace_written"
        self.append_event(
            new_material_event(
                event_type="material.failure_snapshot.workspace_write.completed",
                session_id=session_id,
                task_id=request.task_id,
                source="sandbox_owner",
                phase=phase,
                status="completed",
                latency_source="sandbox",
                payload={
                    "issue_id": issue.issue_id,
                    "file_count": len(files),
                    "generated_file_count": len(generated_files),
                    "diagnostic_file_count": len(snapshot_files),
                    "written_paths": sorted(written_paths),
                    "state_hash": result.state_hash,
                    "partial": True,
                },
            )
        )

    def _compile_and_attach_repair_case(
        self,
        raw: dict[str, Any],
        session_id: str,
        issue: MaterialIssue,
        *,
        target_sha256: str | None,
    ) -> RepairCase:
        request: MaterialSessionRequest = raw["request"]
        repair_case = compile_repair_case(
            raw,
            issue,
            target_sha256=target_sha256,
            max_repair_rounds=request.max_repair_rounds,
        )
        repair_case_payload = repair_case.model_dump(mode="json")
        issue.details["repair_case"] = repair_case_payload
        issue.details["repair_case_id"] = repair_case.case_id
        issue.details["repair_case_status"] = repair_case.status
        issue.details["root_cause_kind"] = repair_case.root_cause_kind
        raw.setdefault("repair_cases", []).append(repair_case_payload)
        raw["manifest"].repair_cases = list(raw.get("repair_cases") or [])
        self.append_event(
            new_material_event(
                event_type="material.repair.case.compiled",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase=str(raw.get("status") or "repairing"),
                status="progress" if repair_case.status not in {"under_specified", "blocked", "failed_closed"} else "blocked",
                latency_source="kernel",
                payload={
                    "issue_id": issue.issue_id,
                    "case_id": repair_case.case_id,
                    "status": repair_case.status,
                    "root_cause_kind": repair_case.root_cause_kind,
                    "primary_repair_target": repair_case.primary_repair_target.model_dump(mode="json")
                    if repair_case.primary_repair_target is not None
                    else None,
                    "symptom_targets": [target.model_dump(mode="json") for target in repair_case.symptom_targets],
                    "obligation_count": len(repair_case.obligations),
                    "success_criteria_count": len(repair_case.success_criteria),
                    "progress": repair_case.progress_state.model_dump(mode="json"),
                    "allowed_actions": repair_case.allowed_actions,
                    "forbidden_actions": repair_case.forbidden_actions,
                    "stop_conditions": repair_case.stop_conditions,
                },
            )
        )
        if repair_case.status in {"under_specified", "blocked", "failed_closed"}:
            self.append_event(
                new_material_event(
                    event_type="material.repair.case.under_specified",
                    session_id=session_id,
                    task_id=request.task_id,
                    source="kernel",
                    phase=str(raw.get("status") or "repairing"),
                    status="blocked",
                    latency_source="kernel",
                    payload={
                        "issue_id": issue.issue_id,
                        "case_id": repair_case.case_id,
                        "status": repair_case.status,
                        "root_cause_kind": repair_case.root_cause_kind,
                        "stop_conditions": repair_case.stop_conditions,
                    },
                )
            )
        return repair_case

    def _append_heartbeat(self, raw: dict[str, Any], session_id: str) -> None:
        request: MaterialSessionRequest = raw["request"]
        phase = str(raw.get("status") or "created")
        self.append_event(
            new_material_event(
                event_type="material.no_progress.heartbeat",
                session_id=session_id,
                task_id=request.task_id,
                source="kernel",
                phase=phase,
                status="heartbeat",
                latency_source="kernel",
                payload={"trace_id": request.trace_id, "message": "terminal or blocked phase heartbeat"},
            )
        )


_MATERIAL_STATUSES = set(MaterialSessionStatus.__args__)  # type: ignore[attr-defined]
_NON_REPAIRABLE_VALIDATION_CODES = {
    "docker_runtime_unavailable",
    "host_execution_attempted",
    "network_policy_violation",
    "sandbox_client_unavailable",
    "secret_exposure_attempt",
    "symlink_escape_attempt",
    "validation_command_unavailable",
    "validation_profile_unsupported",
    "validation_tool_unavailable",
    "vm_runtime_unavailable",
    "workspace_cwd_missing",
    "workspace_root_missing",
    "workspace_transport_failed",
}
_PATCH_REPAIR_RETRY_REASONS = {
    "material_builder_patch_contract_invalid",
    "material_builder_patch_schema_invalid",
    "microvm_patch_apply_failed",
    "patch_apply_failed",
    "patch_contract_mismatch",
    "replacement_contract_mismatch",
    "replacement_noop",
}
_PROPOSAL_ONLY_REJECTION_REASONS = {
    "material_builder_patch_contract_invalid",
    "material_builder_patch_schema_invalid",
    "patch_contract_mismatch",
    "replacement_contract_mismatch",
    "replacement_noop",
}


def _patch_rejection_retry_limit(reason: str, max_repair_rounds: int) -> int:
    if reason in {"material_builder_patch_contract_invalid", "material_builder_patch_schema_invalid"}:
        return min(max_repair_rounds, 2)
    if reason in _PROPOSAL_ONLY_REJECTION_REASONS:
        return min(max_repair_rounds, 3)
    return max_repair_rounds


_PATCH_REJECTION_REPLACEMENT_MARKERS = {
    "llm_schema_invalid",
    "llm_contract_violation",
    "schema_invalid_after_repair",
    "did not contain a json object",
    "does not match the repair target",
    "path that does not match",
    "context_mismatch",
    "removal_mismatch",
    "checksum_mismatch",
    "expected_current_sha256",
    "empty_diff_line",
    "invalid_diff",
    "invalid_diff_line_prefix",
    "malformed_diff",
    "patch_apply_failed",
    "microvm_patch_apply_failed",
    "replacement_noop",
}
_VALIDATION_PROFILE_ORDER = [
    "python-basic",
    "node-basic",
    "docker-compose-static",
    "python-pytest",
    "python-api",
    "docker-compose-runtime",
    "stateful-postgres",
    "stateful-redis",
    "worker-queue",
    "cli",
    "artifact",
]
_ISSUE_EVIDENCE_DETAIL_KEYS = {
    "error",
    "message",
    "stderr",
    "stderr_excerpt",
    "stderr_preview",
    "stdout",
    "stdout_excerpt",
    "stdout_preview",
    "traceback",
}
_VALIDATION_PROFILE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "python-pytest": ("python-basic",),
    "python-api": ("python-basic",),
    "docker-compose-runtime": ("docker-compose-static",),
    "stateful-postgres": ("docker-compose-runtime",),
    "stateful-redis": ("docker-compose-runtime",),
    "worker-queue": ("python-basic", "docker-compose-runtime"),
    "cli": ("python-basic",),
}


def phase_timings(events: list[MaterialEvent]) -> list[MaterialPhaseTiming]:
    timings: list[MaterialPhaseTiming] = []
    for event in events:
        if not event.phase:
            continue
        timings.append(
            MaterialPhaseTiming(
                phase=event.phase,
                status=event.status,
                started_at=event.started_at,
                finished_at=event.finished_at,
                last_progress_at=event.last_progress_at,
                duration_ms=event.duration_ms,
                latency_source=event.latency_source,
                event_id=event.event_id,
            )
        )
    return timings


def last_progress_at(events: list[MaterialEvent]) -> datetime | None:
    progress = [event.last_progress_at or event.created_at for event in events]
    return max(progress) if progress else None


def _workspace_materialized(raw: dict[str, Any]) -> bool:
    files = list(raw["manifest"].files)
    return bool(files) and all(item.state in {"workspace_written", "repaired"} for item in files)


def _workspace_issue_payload(issue: WorkspaceIssue | None) -> dict[str, object] | None:
    if issue is None:
        return None
    return {"code": issue.code, "message": issue.message, "details": dict(issue.details)}


def _failure_snapshot_project_root(
    request: MaterialSessionRequest,
    *,
    plan: MaterialPlanProposal | None,
    session_id: str,
) -> str:
    blueprint = request.material_builder_context.get("plan_blueprint")
    blueprint_root = blueprint.get("project_root") if isinstance(blueprint, dict) else None
    candidates = [
        getattr(plan, "project_root", None),
        request.material_builder_context.get("expected_artifact_root"),
        request.material_builder_context.get("requested_project"),
        blueprint_root,
    ]
    for candidate in candidates:
        root = _safe_relative_project_root(candidate)
        if root:
            return root
    return f"failure-snapshot-{_safe_path_segment(session_id)}"


def _safe_relative_project_root(value: object) -> str | None:
    text = str(value or "").strip().strip("/")
    if not text or text.startswith(("~", "\\")):
        return None
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return "/".join(_safe_path_segment(part) for part in parts)


def _failure_snapshot_diagnostic_files(
    raw: dict[str, Any],
    *,
    session_id: str,
    issue: MaterialIssue,
    phase: str,
    project_root: str,
) -> list[GeneratedMaterialFile]:
    request: MaterialSessionRequest = raw["request"]
    manifest: MaterialManifest = raw["manifest"]
    generated_files = list(raw.get("generated_files") or [])
    report = {
        "schema_version": "material_failure_snapshot.v1",
        "session_id": session_id,
        "task_id": request.task_id,
        "trace_id": request.trace_id,
        "phase": phase,
        "status": raw.get("status") or phase,
        "project_root": project_root,
        "generated_file_count": len(generated_files),
        "manifest_status": manifest.status,
        "blocking_issue": issue.model_dump(mode="json"),
        "issues": [item.model_dump(mode="json") for item in raw.get("issues", [])],
        "language": manifest.language,
        "artifact": manifest.artifact.model_dump(mode="json"),
        "material_builder_context": request.material_builder_context,
    }
    report_json = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    original_query = str(
        request.material_builder_context.get("original_query")
        or request.material_builder_context.get("normalized_query")
        or request.goal
    )
    readme = (
        "# Material failure snapshot\n\n"
        "This artifact was materialized because the material workflow stopped before successful completion.\n\n"
        f"- task_id: `{request.task_id}`\n"
        f"- session_id: `{session_id}`\n"
        f"- phase: `{phase}`\n"
        f"- project_root: `{project_root}`\n"
        f"- generated_file_count: `{len(generated_files)}`\n"
        f"- primary_issue_type: `{issue.issue_type}`\n\n"
        "See `failure-report.json` for structured details and `original-request.txt` for the user request.\n"
    )
    evidence = (
        "Validation status: not executed successfully.\n"
        f"Reason: material workflow stopped in phase {phase} before artifact-ready completion.\n"
        f"Primary issue: {issue.issue_type}\n"
    )
    return [
        GeneratedMaterialFile.from_text(
            path="__failure_snapshot__/README.md",
            content=readme,
            kind="markdown",
        ),
        GeneratedMaterialFile.from_text(
            path="__failure_snapshot__/failure-report.json",
            content=report_json,
            kind="json",
        ),
        GeneratedMaterialFile.from_text(
            path="__failure_snapshot__/original-request.txt",
            content=original_query.rstrip() + "\n",
            kind="text",
        ),
        GeneratedMaterialFile.from_text(
            path="__failure_snapshot__/validation-evidence.txt",
            content=evidence,
            kind="text",
        ),
    ]


def _ensure_failure_snapshot_manifest_files(
    raw: dict[str, Any],
    snapshot_files: list[GeneratedMaterialFile],
) -> None:
    known_paths = {item.path for item in raw["manifest"].files}
    for file in snapshot_files:
        if file.path in known_paths:
            continue
        raw["manifest"].files.append(
            MaterialManifestFile(
                path=file.path,
                purpose="failure snapshot diagnostic evidence",
                state="planned",
                kind=file.kind,
                content_hash=file.sha256,
                producer="material_execution_kernel",
            )
        )
        known_paths.add(file.path)


def _failure_snapshot_manifest_path(project_root: str, workspace_path: str) -> str:
    root = str(project_root or "").strip("/").replace("\\", "/")
    path = str(workspace_path or "").strip("/").replace("\\", "/")
    if root and path.startswith(f"{root}/"):
        return path[len(root) + 1 :]
    if root and path == root:
        return "."
    return path


def latency_summary(events: list[MaterialEvent]) -> dict[str, int]:
    totals: Counter[str] = Counter()
    now = datetime.now(UTC)
    for event in events:
        duration_ms = event.duration_ms
        if duration_ms is None and event.started_at is not None:
            finished = event.finished_at or now
            duration_ms = int(max(0.0, (finished - event.started_at).total_seconds() * 1000))
        if duration_ms is not None:
            totals[event.latency_source] += int(duration_ms)
    return dict(totals)


def _manifest_validation_from_result(result: CommandValidationResult) -> MaterialManifestValidation:
    return MaterialManifestValidation(
        profile=result.profile,
        status="passed" if result.status == "completed" else "failed",
        command_run_id=result.command_run_id,
        vm_session_id=result.vm_session_id,
        duration_ms=result.duration_ms,
    )


def _validation_evidence_target_path(raw: dict[str, Any]) -> str | None:
    candidates: list[str] = []
    candidates.extend(item.path for item in raw.get("generated_files", []) if isinstance(item, GeneratedMaterialFile))
    candidates.extend(item.path for item in raw["manifest"].files)
    for path in candidates:
        normalized = str(path or "").replace("\\", "/").strip("/")
        if not normalized or normalized.startswith("__failure_snapshot__/"):
            continue
        if normalized.rsplit("/", 1)[-1] == "validation-evidence.txt":
            return normalized
    return None


def _render_validation_evidence(raw: dict[str, Any], *, session_id: str) -> str:
    request: MaterialSessionRequest = raw["request"]
    plan: MaterialPlanProposal = raw["plan"]
    validations = list(raw["manifest"].validations)
    portuguese = _validation_evidence_is_portuguese(request)
    evidence_context = _validation_evidence_context(raw)
    evidence_commands = [
        str(command).strip()
        for command in evidence_context.get("commands", [])
        if str(command).strip()
    ] if isinstance(evidence_context.get("commands"), list) else []
    lines = [
        "Evidência de validação" if portuguese else "Validation evidence",
        "======================" if portuguese else "===================",
        "",
        f"Task: {request.task_id}",
        f"Material session: {session_id}",
        f"Project root: {plan.project_root}",
        "",
        "Comandos de validação executados:" if portuguese else "Commands executed:",
    ]
    executable_validations = [
        item
        for item in validations
        if item.status not in {"deferred_to_packaging", "skipped"} and item.command_run_id
    ]
    if executable_validations:
        for validation in executable_validations:
            lines.extend(
                [
                    f"- profile: {validation.profile}",
                    f"  command: {_validation_command_display(raw, validation.profile)}",
                    f"  observed_result: {validation.status}",
                    f"  command_run_id: {validation.command_run_id}",
                    f"  vm_session_id: {validation.vm_session_id or 'unknown'}",
                    f"  duration_ms: {validation.duration_ms}",
                ]
            )
    else:
        if evidence_commands:
            lines.append(
                "- nenhum comando de validação executável; este artefacto usa comandos read-only de aquisição de evidência listados abaixo."
                if portuguese
                else "- no executable validation commands; read-only evidence acquisition commands are listed below"
            )
        else:
            lines.append("- nenhum registado" if portuguese else "- none recorded")
    deferred = [item for item in validations if item.status in {"deferred_to_packaging", "skipped"}]
    if deferred:
        lines.extend(["", "Perfis adiados ou ignorados:" if portuguese else "Deferred or skipped profiles:"])
        for validation in deferred:
            reason = validation.details.get("reason") if isinstance(validation.details, dict) else None
            suffix = f" ({reason})" if reason else ""
            lines.append(f"- {validation.profile}: {validation.status}{suffix}")
    summary = raw.get("validation_summary")
    if summary is not None:
        lines.extend(
            [
                "",
                "Resumo:" if portuguese else "Summary:",
                f"- passed: {_comma_or_none(summary.passed)}",
                f"- failed: {_comma_or_none(summary.failed)}",
                f"- skipped: {_comma_or_none(summary.skipped)}",
            ]
        )
    if evidence_context:
        workspace = str(evidence_context.get("workspace") or "").strip()
        summary_text = str(evidence_context.get("evidence_summary") or "").strip()
        lines.extend(["", "Aquisição de evidência read-only:" if portuguese else "Read-only evidence acquisition:"])
        if workspace:
            workspace_label = "Pasta analisada" if portuguese else "Inspected folder"
            lines.append(f"- {workspace_label}: {_human_evidence_text(workspace, portuguese=portuguese)}")
        if summary_text:
            label = "Resumo" if portuguese else "Summary"
            lines.append(f"- {label}: {_human_evidence_text(summary_text, portuguese=portuguese)}")
        if evidence_commands:
            lines.append("- Comandos:" if portuguese else "- Commands:")
            lines.extend(f"  - {_human_evidence_text(command, portuguese=portuguese)}" for command in evidence_commands)
    lines.extend(
        [
            "",
            "Notas:" if portuguese else "Notes:",
            "- Os comandos de validação executáveis, quando existem, correm num workspace controlado e isolado."
            if portuguese
            else "- Commands, when present, run inside a controlled isolated workspace.",
            "- Este ficheiro foi gerado antes do empacotamento do artefacto."
            if portuguese
            else "- This file was generated before artifact packaging.",
        ]
    )
    return "\n".join(lines) + "\n"


def _human_evidence_text(value: str, *, portuguese: bool) -> str:
    text = str(value or "")
    inspected_folder = "pasta indicada no pedido do utilizador" if portuguese else "folder indicated in the user request"
    text = re.sub(r"/host_home(?:/[^\s;,\)]*)?", inspected_folder, text)
    replacements = {
        "workspace_execution": "workspace controlado" if portuguese else "controlled workspace",
        "material_execution_kernel": "fluxo de materialização" if portuguese else "materialization flow",
        "Specialist evidence results attached": (
            "Leituras especializadas anexadas" if portuguese else "Specialized readings attached"
        ),
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _validation_evidence_is_portuguese(request: MaterialSessionRequest) -> bool:
    candidates = [
        request.language_context.final_response_language,
        request.language_context.target_language,
        request.language_context.source_variant,
        request.language_context.original_language,
    ]
    builder_language = request.material_builder_context.get("language_context")
    if isinstance(builder_language, dict):
        candidates.extend(
            [
                builder_language.get("response_language"),
                builder_language.get("final_response_language"),
                builder_language.get("original_language"),
            ]
        )
    for value in candidates:
        text = str(value or "").strip().casefold()
        if text.startswith("pt") or "portugu" in text:
            return True
    return False


def _validation_evidence_context(raw: dict[str, Any]) -> dict[str, Any]:
    request: MaterialSessionRequest = raw["request"]
    context = request.material_builder_context.get("evidence_context")
    return context if isinstance(context, dict) else {}


def _validation_command_display(raw: dict[str, Any], profile: str) -> str:
    command = _validation_command_for_profile(raw, profile) or _default_validation_command(raw, profile)
    if command is None:
        return "profile-managed command"
    argv = " ".join(shlex.quote(str(part)) for part in command.argv)
    cwd = command.cwd or raw["plan"].project_root
    return f"cd {shlex.quote(str(cwd))} && {argv}"


def _default_validation_command(raw: dict[str, Any], profile: str) -> MaterialValidationCommandProposal | None:
    plan: MaterialPlanProposal = raw["plan"]
    deterministic_commands = {
        "python-basic": ["python", "-m", "compileall", "."],
        "python-pytest": ["python", "-m", "pytest"],
        "docker-compose-static": ["docker", "compose", "config"],
        "docker-compose-runtime": ["docker-compose", "up", "-d"],
    }
    argv = deterministic_commands.get(profile)
    if argv is None:
        return None
    return MaterialValidationCommandProposal(profile=profile, argv=argv, cwd=plan.project_root)


def _comma_or_none(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _ordered_validation_batch(profiles: list[str]) -> list[str]:
    seen = set()
    ordered: list[str] = []
    requested = list(profiles)
    for profile in [*_VALIDATION_PROFILE_ORDER, *requested]:
        if profile not in requested or profile in seen:
            continue
        seen.add(profile)
        ordered.append(profile)
    return ordered


def _profiles_for_validation_phase(
    raw: dict[str, Any],
    *,
    full_profiles: list[str],
    revalidation: bool,
) -> tuple[list[str], str]:
    if not revalidation:
        return full_profiles, "full"
    focused = _ordered_validation_batch(
        [
            profile
            for profile in list(raw.get("focused_revalidation_profiles") or [])
            if profile in set(full_profiles)
        ]
    )
    if not focused or focused == full_profiles:
        return full_profiles, "full"
    return focused, "focused"


def _mark_repair_requires_revalidation(raw: dict[str, Any], issue: MaterialIssue) -> list[str]:
    plan: MaterialPlanProposal = raw["plan"]
    full_profiles = _ordered_validation_batch(plan.required_validation_profiles or ["python-basic"])
    focus_profile = str(issue.details.get("profile") or "").strip()
    focused_profiles = _ordered_validation_batch([focus_profile]) if focus_profile else []
    if not focused_profiles or any(profile not in full_profiles for profile in focused_profiles):
        focused_profiles = list(full_profiles)
    raw["focused_revalidation_profiles"] = focused_profiles
    raw["full_validation_required"] = focused_profiles != full_profiles
    return focused_profiles


def _validation_command_for_profile(raw: dict[str, Any], profile: str) -> MaterialValidationCommandProposal | None:
    plan: MaterialPlanProposal = raw["plan"]
    if profile == "cli":
        derived = _derive_cli_validation_command(raw)
        if derived is not None:
            return derived
    command = plan.validation_commands.get(profile)
    if command is not None:
        return command
    return None


def _derive_cli_validation_command(raw: dict[str, Any]) -> MaterialValidationCommandProposal | None:
    observed_contract: ObservedContract | None = raw.get("observed_contract")
    if observed_contract is None:
        return None
    plan: MaterialPlanProposal = raw["plan"]
    entrypoints = [item for item in observed_contract.entrypoints if item.kind == "cli"]
    modules_by_path = {
        item.path: item.module
        for item in observed_contract.files
        if item.ecosystem == "python" and item.module and item.parse_status == "parsed"
    }
    for entrypoint in entrypoints:
        target = _pyproject_script_target(entrypoint.evidence)
        if target:
            code = (
                "import importlib, sys\n"
                f"target = {json.dumps(target)}\n"
                "module, _, attr = target.partition(':')\n"
                "if not module or not attr:\n"
                "    raise SystemExit('invalid console script target')\n"
                "sys.argv = [module, '--help']\n"
                "fn = getattr(importlib.import_module(module), attr)\n"
                "raise SystemExit(fn())\n"
            )
            return MaterialValidationCommandProposal(
                profile="cli",
                argv=["python", "-c", code],
                cwd=plan.project_root,
                timeout_seconds=60,
                purpose="validate observed Python console-script entrypoint with --help",
            )
    for entrypoint in entrypoints:
        module = modules_by_path.get(entrypoint.path)
        if entrypoint.name == "__main__" and module and _is_importable_python_module_path(
            plan.project_root,
            entrypoint.path,
            module,
        ):
            return MaterialValidationCommandProposal(
                profile="cli",
                argv=["python", "-m", module, "--help"],
                cwd=plan.project_root,
                timeout_seconds=60,
                purpose="validate observed Python module CLI entrypoint with --help",
            )
        if entrypoint.name == "__main__" and entrypoint.path.endswith(".py"):
            return MaterialValidationCommandProposal(
                profile="cli",
                argv=["python", _path_relative_to_project_root(plan.project_root, entrypoint.path), "--help"],
                cwd=plan.project_root,
                timeout_seconds=60,
                purpose="validate observed Python __main__ CLI entrypoint with --help",
            )
    for entrypoint in entrypoints:
        module = modules_by_path.get(entrypoint.path)
        if not module:
            continue
        code = (
            "import importlib, sys\n"
            f"module = {json.dumps(module)}\n"
            "sys.argv = [module, '--help']\n"
            "fn = getattr(importlib.import_module(module), 'main', None)\n"
            "if not callable(fn):\n"
            "    raise SystemExit('observed CLI module does not expose callable main()')\n"
            "raise SystemExit(fn())\n"
        )
        return MaterialValidationCommandProposal(
            profile="cli",
            argv=["python", "-c", code],
            cwd=plan.project_root,
            timeout_seconds=60,
            purpose="validate observed Python CLI module with --help",
        )
    return None


def _path_relative_to_project_root(project_root: str, path: str) -> str:
    normalized_root = str(project_root or "").strip("/").replace("\\", "/")
    normalized_path = str(path or "").strip("/").replace("\\", "/").lstrip("./")
    if normalized_root and normalized_path.startswith(f"{normalized_root}/"):
        return normalized_path[len(normalized_root) + 1 :]
    return normalized_path


def _is_importable_python_module_path(project_root: str, path: str, module: str) -> bool:
    normalized_root = str(project_root or "").strip("/").replace("\\", "/")
    normalized_path = str(path or "").strip("/").replace("\\", "/")
    if normalized_root and normalized_path.startswith(f"{normalized_root}/"):
        normalized_path = normalized_path[len(normalized_root) + 1 :]
    if not normalized_path.endswith(".py"):
        return False
    parts = normalized_path[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or any(not part.isidentifier() for part in parts):
        return False
    return ".".join(parts) == module


def _pyproject_script_target(evidence: str) -> str | None:
    if "project.scripts" not in evidence or "->" not in evidence:
        return None
    target = evidence.split("->", 1)[1].strip()
    return target or None


def _validation_skip_for_profile(
    profile: str,
    *,
    failed_profiles: set[str],
) -> IssueBundleSkippedProfile | None:
    blocked_by = sorted(set(_VALIDATION_PROFILE_DEPENDENCIES.get(profile, ())) & failed_profiles)
    if not blocked_by:
        return None
    return IssueBundleSkippedProfile(
        profile=profile,
        reason="validation profile depends on failed prerequisite profiles",
        blocked_by=blocked_by,
    )


def _issue_bundle_from_failures(
    raw: dict[str, Any],
    *,
    failures: list[tuple[str, WorkspaceIssue, str, CommandValidationResult]],
    skipped: list[IssueBundleSkippedProfile],
) -> IssueBundle:
    bundle_failures: list[IssueBundleFailure] = []
    repairable = False
    for profile, issue, command_run_id, _result in failures:
        resolution = _target_resolution_for_issue(raw, issue=issue, profile=profile)
        issue_repairable = bool(resolution.primary_target) and _is_repairable_validation_issue(issue, profile=profile)
        repairable = repairable or issue_repairable
        bundle_failures.append(
            IssueBundleFailure(
                profile=profile,
                command_run_id=command_run_id,
                issue_code=_classified_validation_issue_code(issue, profile=profile),
                message=issue.message,
                target_path=resolution.primary_target,
                target_resolution=resolution,
                details=dict(issue.details),
            )
        )
    focus = _select_repair_focus(raw, failures, skipped=skipped)
    repair_focus_profile: str | None = None
    repair_focus_target_path: str | None = None
    repair_focus_reason: str | None = None
    if focus is not None:
        profile, _issue, _command_run_id, _result, resolution, reason = focus
        repair_focus_profile = profile
        repair_focus_target_path = resolution.primary_target
        repair_focus_reason = reason
    manifest_validations = raw["manifest"].validations
    attempted = [
        validation.profile
        for validation in manifest_validations
        if validation.status != "skipped"
    ]
    return IssueBundle(
        bundle_id=f"issue_bundle_{uuid4().hex}",
        profiles_attempted=_dedupe_strings(attempted),
        profiles_failed=[profile for profile, _issue, _command_run_id, _result in failures],
        profiles_skipped=[item.profile for item in skipped],
        failures=bundle_failures,
        skipped=skipped,
        repair_focus_profile=repair_focus_profile,
        repair_focus_target_path=repair_focus_target_path,
        repair_focus_reason=repair_focus_reason,
        repairable=repairable,
    )


def _first_repair_focus(
    raw: dict[str, Any],
    failures: list[tuple[str, WorkspaceIssue, str, CommandValidationResult]],
) -> tuple[str, WorkspaceIssue, str, CommandValidationResult]:
    focus = _select_repair_focus(raw, failures, skipped=[])
    if focus is not None:
        profile, issue, command_run_id, result, _resolution, _reason = focus
        return profile, issue, command_run_id, result
    return failures[0]


def _select_repair_focus(
    raw: dict[str, Any],
    failures: list[tuple[str, WorkspaceIssue, str, CommandValidationResult]],
    *,
    skipped: list[IssueBundleSkippedProfile],
) -> tuple[str, WorkspaceIssue, str, CommandValidationResult, RepairTargetResolution, str] | None:
    candidates: list[
        tuple[int, int, str, WorkspaceIssue, str, CommandValidationResult, RepairTargetResolution, str]
    ] = []
    failed_profiles = [profile for profile, _issue, _command_run_id, _result in failures]
    skipped_profiles = [item.profile for item in skipped]
    repairable_targets: list[tuple[str, str]] = []
    for profile, issue, _command_run_id, _result in failures:
        resolution = _target_resolution_for_issue(raw, issue=issue, profile=profile)
        if resolution.primary_target and _is_repairable_validation_issue(issue, profile=profile):
            repairable_targets.append((profile, resolution.primary_target))
    has_non_test_target = any(_target_kind_for_path(raw, target) != "test_file" for _profile, target in repairable_targets)
    for index, (profile, issue, command_run_id, result) in enumerate(failures):
        resolution = _target_resolution_for_issue(raw, issue=issue, profile=profile)
        target = resolution.primary_target
        if not target or not _is_repairable_validation_issue(issue, profile=profile):
            continue
        score, reason = _repair_focus_score(
            raw,
            issue,
            profile=profile,
            target_path=target,
            resolution=resolution,
            failed_profiles=failed_profiles,
            skipped_profiles=skipped_profiles,
            has_non_test_target=has_non_test_target,
        )
        candidates.append((score, -index, profile, issue, command_run_id, result, resolution, reason))
    if not candidates:
        return None
    _score, _order, profile, issue, command_run_id, result, resolution, reason = max(
        candidates,
        key=lambda item: (item[0], item[1]),
    )
    return profile, issue, command_run_id, result, resolution, reason


def _repair_focus_score(
    raw: dict[str, Any],
    issue: WorkspaceIssue,
    *,
    profile: str,
    target_path: str,
    resolution: RepairTargetResolution,
    failed_profiles: list[str],
    skipped_profiles: list[str],
    has_non_test_target: bool,
) -> tuple[int, str]:
    target_kind = _target_kind_for_path(raw, target_path)
    score = int(float(resolution.confidence or 0.0) * 100)
    reasons: list[str] = []
    if target_kind in {"python_file", "config_file", "compose_file"}:
        score += 80
        reasons.append("source or configuration target can unblock dependent validation")
        if profile == "python-pytest" and not target_path.replace("\\", "/").endswith("/__init__.py"):
            score += 40
            reasons.append("pytest evidence identifies a concrete implementation module")
    elif target_kind == "test_file":
        score += 10
        if has_non_test_target and not _looks_like_missing_test_contract(issue, profile=profile):
            score -= 180
            reasons.append("test target appears correlated with a non-test repair target")
    else:
        score += 30
    dependents = [
        dependent
        for dependent, prerequisites in _VALIDATION_PROFILE_DEPENDENCIES.items()
        if profile in prerequisites and dependent in {*failed_profiles, *skipped_profiles}
    ]
    if dependents:
        score += 90 + (20 * len(dependents))
        reasons.append(f"{profile} is a prerequisite for {', '.join(sorted(dependents))}")
    profile_priority = {
        "python-basic": 90,
        "docker-compose-static": 85,
        "cli": 70,
        "python-api": 70,
        "worker-queue": 65,
        "docker-compose-runtime": 60,
        "stateful-postgres": 55,
        "stateful-redis": 55,
        "python-pytest": 45,
    }
    score += profile_priority.get(profile, 50)
    if resolution.related_targets:
        score += 15
        reasons.append("repair evidence includes related generated targets")
    if not reasons:
        reasons.append("selected highest-confidence repairable validation target")
    return score, "; ".join(reasons)


def _event_status_for_issue_state(target_status: MaterialSessionStatus) -> str:
    if str(target_status).startswith("blocked"):
        return "blocked"
    if target_status in {"repairing", "revalidating"}:
        return "progress"
    return "failed"


def _is_repairable_validation_issue(issue: WorkspaceIssue, *, profile: str) -> bool:
    return issue.code not in _NON_REPAIRABLE_VALIDATION_CODES


def _is_repairable_contract_comparison_issue(issue: ContractComparisonIssue) -> bool:
    if issue.issue_type == "local_import_cycle":
        return bool(issue.path)
    if issue.details.get("drift") == "planned_file_missing_from_observed_contract":
        return False
    if not issue.path and not _is_missing_dependency_strategy_without_target(issue):
        return False
    return issue.issue_type in {
        "dependency_strategy_mismatch",
        "missing_dependency_strategy",
        "missing_symbol_provider",
        "observed_contract_drift",
        "undeclared_symbol_consumer",
    }


def _is_missing_dependency_strategy_without_target(issue: ContractComparisonIssue) -> bool:
    return issue.issue_type == "missing_dependency_strategy" and bool(issue.details.get("undeclared_external_imports"))


def _contract_issue_targets_dependency_manifest(issue: ContractComparisonIssue) -> bool:
    if _is_missing_dependency_strategy_without_target(issue):
        return True
    if issue.issue_type != "dependency_strategy_mismatch":
        return False
    observed_issue_type = str(issue.details.get("observed_issue_type") or "")
    return observed_issue_type == "missing_dependency_declaration" and bool(
        issue.details.get("dependency_name") or issue.details.get("module")
    )


def _target_path_for_contract_comparison_issue(
    raw: dict[str, Any],
    issue: ContractComparisonIssue,
) -> str | None:
    if _contract_issue_targets_dependency_manifest(issue):
        return _contract_dependency_manifest_target(raw)
    if issue.path:
        return issue.path
    return None


def _patch_rejection_requires_replacement(rejection: PatchRejectionEvidence) -> bool:
    if rejection.reason in {"microvm_patch_apply_failed", "patch_apply_failed"}:
        return True
    evidence = " ".join(
        str(value)
        for value in (
            rejection.reason,
            rejection.message,
            json.dumps(rejection.diagnostics, ensure_ascii=False, sort_keys=True),
        )
        if value
    ).casefold()
    return any(marker in evidence for marker in _PATCH_REJECTION_REPLACEMENT_MARKERS)


def _looks_like_material_builder_schema_failure(message: str) -> bool:
    evidence = message.casefold()
    return (
        "llm_schema_invalid" in evidence
        or "schema_invalid_after_repair" in evidence
        or "did not contain a json object" in evidence
    )


def _looks_like_material_builder_contract_violation(message: str) -> bool:
    evidence = message.casefold()
    return "llm_contract_violation" in evidence or "does not match the repair target" in evidence


def _missing_expected_python_exports(issue: MaterialIssue, content: str) -> list[str]:
    expected = _expected_python_exports_for_issue(issue)
    if not expected or not (issue.target_path or "").endswith(".py"):
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    exported = _top_level_python_exports(tree)
    return sorted(symbol for symbol in expected if symbol not in exported)


def _replacement_forbidden_python_imports(raw: dict[str, Any], replacement_file: GeneratedMaterialFile) -> list[str]:
    if not replacement_file.path.endswith(".py"):
        return []
    try:
        tree = ast.parse(replacement_file.content)
    except SyntaxError:
        return []
    replacement_module = _python_module_name_for_path(
        str(getattr(raw.get("manifest"), "project_root", "") or ""),
        replacement_file.path,
    )
    allowed_local_roots = _local_python_package_roots(raw)
    allowed_dependencies = _declared_dependency_roots(raw)
    allowed_validation_tools = _validation_tool_import_roots_for_file(replacement_file)
    forbidden: list[str] = _replacement_package_root_cycle_imports(
        raw,
        tree,
        replacement_module,
        replacement_path=replacement_file.path,
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _python_import_is_forbidden_self_reference(
                    alias.name,
                    replacement_module,
                    replacement_path=replacement_file.path,
                ):
                    forbidden.append(alias.name)
                    continue
                root = alias.name.split(".", 1)[0]
                if not _python_import_root_allowed(
                    root,
                    allowed_local_roots,
                    allowed_dependencies | allowed_validation_tools,
                ):
                    forbidden.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0 or not node.module:
                continue
            if _python_import_is_forbidden_self_reference(
                node.module,
                replacement_module,
                replacement_path=replacement_file.path,
            ):
                forbidden.append(node.module)
                continue
            root = node.module.split(".", 1)[0]
            if not _python_import_root_allowed(
                root,
                allowed_local_roots,
                allowed_dependencies | allowed_validation_tools,
            ):
                forbidden.append(node.module)
    return _dedupe_strings(forbidden)


def _replacement_package_root_cycle_imports(
    raw: dict[str, Any],
    tree: ast.Module,
    replacement_module: str | None,
    *,
    replacement_path: str,
) -> list[str]:
    if not replacement_module:
        return []
    if _replacement_target_is_package_init(replacement_path):
        return _package_init_child_cycle_imports(raw, tree, replacement_module)
    if "." not in replacement_module:
        return []
    package_root = replacement_module.split(".", 1)[0]
    forbidden: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.strip(".")
                if module == package_root:
                    forbidden.append(module)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = node.module.strip(".")
            if module == package_root:
                forbidden.append(module)
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            module = _resolved_relative_import_module(replacement_module, node)
            if module == package_root:
                forbidden.append(module)
    return _dedupe_strings(forbidden)


def _package_init_child_cycle_imports(raw: dict[str, Any], tree: ast.Module, package_root: str) -> list[str]:
    forbidden: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.strip(".")
                if module.startswith(f"{package_root}.") and _module_imports_package_root(
                    raw,
                    module,
                    package_root,
                ):
                    forbidden.append(module)
        elif isinstance(node, ast.ImportFrom):
            for module in _package_init_import_from_targets(package_root, node):
                if module.startswith(f"{package_root}.") and _module_imports_package_root(
                    raw,
                    module,
                    package_root,
                ):
                    forbidden.append(module)
    return _dedupe_strings(forbidden)


def _module_imports_package_root(raw: dict[str, Any], module_name: str, package_root: str) -> bool:
    path = _target_path_for_python_module(raw, module_name)
    if not path:
        return False
    generated = _generated_file(raw, path)
    if generated is None:
        return False
    try:
        tree = ast.parse(generated.content)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.strip(".")
                if module == package_root:
                    return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.strip(".") == package_root:
                return True
    return False


def _resolved_relative_import_module(current_module: str, node: ast.ImportFrom) -> str:
    parts = [part for part in current_module.split(".") if part]
    if not parts:
        return str(node.module or "").strip(".")
    base_count = max(0, len(parts) - node.level)
    base_parts = parts[:base_count]
    module_tail = [part for part in str(node.module or "").strip(".").split(".") if part]
    return ".".join([*base_parts, *module_tail])


def _package_init_imports_module(raw: dict[str, Any], package_root: str, module_name: str) -> bool:
    project_root = str(getattr(raw.get("manifest"), "project_root", "") or "").strip("/").replace("\\", "/")
    init_path = f"{package_root}/__init__.py"
    if project_root:
        init_path = f"{project_root}/{init_path}"
    generated = _generated_file(raw, init_path)
    if generated is None:
        return False
    try:
        tree = ast.parse(generated.content)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported = alias.name.strip(".")
                if module_name == imported or module_name.startswith(f"{imported}."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            for imported in _package_init_import_from_targets(package_root, node):
                if module_name == imported or module_name.startswith(f"{imported}."):
                    return True
    return False


def _package_init_import_from_targets(package_root: str, node: ast.ImportFrom) -> list[str]:
    module = str(node.module or "").strip(".")
    if node.level > 0:
        if module:
            return [f"{package_root}.{module}"]
        return [
            f"{package_root}.{alias.name}"
            for alias in node.names
            if alias.name and alias.name != "*"
        ]
    if module:
        return [module]
    return []


def _python_import_is_forbidden_self_reference(
    imported_module: str,
    replacement_module: str | None,
    *,
    replacement_path: str,
) -> bool:
    if not replacement_module:
        return False
    module = str(imported_module or "").strip(".")
    target = str(replacement_module or "").strip(".")
    if not module or not target:
        return False
    if _replacement_target_is_package_init(replacement_path) and module.startswith(f"{target}."):
        return False
    return module == target or module.startswith(f"{target}.")


def _replacement_target_is_package_init(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.endswith("/__init__.py") or normalized == "__init__.py"


def _validation_tool_import_roots_for_file(replacement_file: GeneratedMaterialFile) -> set[str]:
    if replacement_file.kind == "test" or _looks_like_pytest_file_path(replacement_file.path):
        return {"pytest"}
    return set()


def _python_module_name_for_path(project_root: str, path: str) -> str | None:
    normalized_root = str(project_root or "").strip("/").replace("\\", "/")
    normalized_path = str(path or "").strip("/").replace("\\", "/")
    if normalized_root and normalized_path.startswith(f"{normalized_root}/"):
        normalized_path = normalized_path[len(normalized_root) + 1 :]
    if not normalized_path.endswith(".py"):
        return None
    parts = normalized_path[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or any(not part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _python_import_root_allowed(root: str, local_roots: set[str], dependency_roots: set[str]) -> bool:
    if not root:
        return True
    if root == "__future__" or root in getattr(sys, "stdlib_module_names", set()):
        return True
    if root in local_roots:
        return True
    return _normalize_dependency_root(root) in dependency_roots


def _declared_dependency_roots(raw: dict[str, Any]) -> set[str]:
    roots: set[str] = set()
    material_contract: MaterialContract | None = raw.get("material_contract")
    if material_contract is not None:
        roots.update(_normalize_dependency_root(item) for item in material_contract.dependency_strategy.external_dependencies)
    plan = raw.get("plan")
    dependency_strategy = getattr(plan, "dependency_strategy", None)
    external_dependencies = getattr(dependency_strategy, "external_dependencies", [])
    if isinstance(external_dependencies, list):
        roots.update(_normalize_dependency_root(str(item)) for item in external_dependencies)
    return {root for root in roots if root}


def _normalize_dependency_root(value: str) -> str:
    raw_name = str(value).strip()
    if not raw_name:
        return ""
    raw_name = raw_name.split(";", 1)[0].strip()
    raw_name = raw_name.split("[", 1)[0].strip()
    raw_name = re.split(r"\s+|[<>=!~@]", raw_name, maxsplit=1)[0].strip()
    return re.sub(r"[-_.]+", "-", raw_name).casefold()


def _expected_python_exports_for_issue(issue: MaterialIssue) -> list[str]:
    symbols: list[str] = []
    evidence = _target_scoped_material_issue_evidence_text(issue)
    profile = str(issue.details.get("profile") or issue.details.get("validation_profile") or "")
    if (
        profile == "cli"
        and (issue.target_path or "").endswith(".py")
        and (issue.issue_type == "cli_smoke_failed" or "observed cli module" in evidence.casefold())
    ):
        symbols.append("main")
    for key in ("missing_name", "name"):
        missing_name = issue.details.get(key)
        if isinstance(missing_name, str) and missing_name and missing_name != "*":
            symbols.append(missing_name)
    for key in ("expected_symbols", "missing_expected_symbols", "expected_exports", "missing_exports"):
        raw = issue.details.get(key)
        if isinstance(raw, str):
            symbols.append(raw)
        elif isinstance(raw, list):
            symbols.extend(str(item).strip() for item in raw)
    repair_obligations = issue.details.get("repair_obligations")
    if isinstance(repair_obligations, list):
        for obligation in repair_obligations:
            if not isinstance(obligation, dict):
                continue
            if obligation.get("kind") != "importable_export":
                continue
            symbol = obligation.get("symbol")
            if isinstance(symbol, str) and symbol and symbol != "*":
                symbols.append(symbol)
    for match in re.finditer(r"does not expose callable\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(\)", evidence):
        name = match.group(1).strip()
        if name and name != "*":
            symbols.append(name)
    for match in re.finditer(r"cannot import name ['\"]([^'\"]+)['\"] from", evidence):
        name = match.group(1).strip()
        if name and name != "*":
            symbols.append(name)
    return _dedupe_strings([symbol for symbol in symbols if _is_repairable_python_export_symbol(symbol)])


def _is_repairable_python_export_symbol(value: str) -> bool:
    symbol = str(value or "").strip()
    if not symbol or symbol == "*":
        return False
    return symbol != "__init__"


def _top_level_python_exports(tree: ast.Module) -> set[str]:
    exports: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            exports.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                exports.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    exports.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    exports.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            exports.add(node.target.id)
    return exports


def _expected_symbol_provider_candidates(raw: dict[str, Any], issue: MaterialIssue) -> list[dict[str, object]]:
    if not issue.target_path or not issue.target_path.endswith(".py"):
        return []
    expected_symbols = _expected_python_exports_for_issue(issue)
    if not expected_symbols:
        return []
    project_root = str(raw["manifest"].project_root or "")
    candidates: list[dict[str, object]] = []
    for generated in raw.get("generated_files", []):
        path = str(getattr(generated, "path", "") or "")
        if path == issue.target_path or not path.endswith(".py"):
            continue
        module = _python_module_name_for_path(project_root, path)
        if not module:
            continue
        try:
            tree = ast.parse(generated.content)
        except SyntaxError:
            continue
        exports = _top_level_python_exports(tree)
        module_leaf = module.rsplit(".", 1)[-1]
        for symbol in expected_symbols:
            if not symbol.isidentifier():
                continue
            if symbol == module_leaf:
                candidates.append(
                    {
                        "symbol": symbol,
                        "provider_path": path,
                        "provider_module": module,
                        "provider_kind": "module",
                        "suggested_imports": _suggested_symbol_imports(
                            target_path=issue.target_path,
                            provider_module=module,
                            symbol=symbol,
                            provider_kind="module",
                            project_root=project_root,
                        ),
                    }
                )
            if symbol in exports:
                candidates.append(
                    {
                        "symbol": symbol,
                        "provider_path": path,
                        "provider_module": module,
                        "provider_kind": "export",
                        "suggested_imports": _suggested_symbol_imports(
                            target_path=issue.target_path,
                            provider_module=module,
                            symbol=symbol,
                            provider_kind="export",
                            project_root=project_root,
                        ),
                    }
                )
    return _dedupe_symbol_provider_candidates(candidates)


def _suggested_symbol_imports(
    *,
    target_path: str,
    provider_module: str,
    symbol: str,
    provider_kind: str,
    project_root: str,
) -> list[str]:
    target_package = _current_python_package_for_path(target_path, project_root)
    imports: list[str] = []
    if target_package and provider_module.startswith(f"{target_package}."):
        relative_module = provider_module[len(target_package) + 1 :]
        if provider_kind == "module":
            if "." in relative_module:
                parent, leaf = relative_module.rsplit(".", 1)
                imports.append(f"from .{parent} import {leaf}")
            else:
                imports.append(f"from . import {relative_module}")
        else:
            imports.append(f"from .{relative_module} import {symbol}")
    if provider_kind == "module":
        if "." in provider_module:
            parent, leaf = provider_module.rsplit(".", 1)
            imports.append(f"from {parent} import {leaf}")
        else:
            imports.append(f"import {provider_module}")
    else:
        imports.append(f"from {provider_module} import {symbol}")
    return _dedupe_strings(imports)


def _current_python_package_for_path(path: str, project_root: str) -> str:
    module = _python_module_name_for_path(project_root, path)
    if not module:
        return ""
    normalized = path.replace("\\", "/")
    if normalized.endswith("/__init__.py") or normalized == "__init__.py":
        return module
    if "." in module:
        return module.rsplit(".", 1)[0]
    return ""


def _dedupe_symbol_provider_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, object]] = []
    for candidate in candidates:
        key = (
            str(candidate.get("symbol") or ""),
            str(candidate.get("provider_path") or ""),
            str(candidate.get("provider_kind") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped[:32]


def _material_issue_evidence_text(issue: MaterialIssue) -> str:
    return "\n".join(
        _dedupe_strings(
            [
                issue.issue_type,
                str(issue.details.get("message") or ""),
                *_nested_issue_evidence_strings(issue.details),
            ]
        )
    )


def _target_scoped_material_issue_evidence_text(issue: MaterialIssue) -> str:
    """Return evidence that may define contracts for this issue's target.

    Issue bundles intentionally carry correlated failures so the repair loop can
    reason about root cause. Contract extraction is narrower: a failure for a
    different primary target must not become an export/signature obligation for
    the current target.
    """

    direct_details = {
        key: value for key, value in issue.details.items() if key != "issue_bundle"
    }
    evidence = [
        issue.issue_type,
        str(issue.details.get("message") or ""),
        *_nested_issue_evidence_strings(direct_details),
    ]
    target_path = str(issue.target_path or "")
    bundle = issue.details.get("issue_bundle")
    if isinstance(bundle, dict) and target_path:
        for failure in bundle.get("failures") or []:
            if not isinstance(failure, dict):
                continue
            if _bundle_failure_targets_path(failure, target_path):
                evidence.extend(_nested_issue_evidence_strings(failure))
    elif isinstance(bundle, dict):
        evidence.extend(_nested_issue_evidence_strings(bundle))
    return "\n".join(_dedupe_strings([item for item in evidence if item]))


def _bundle_failure_targets_path(failure: dict[str, Any], target_path: str) -> bool:
    if not target_path:
        return False
    has_target_hint = False
    targets = {
        str(failure.get("target_path") or ""),
    }
    has_target_hint = has_target_hint or bool(failure.get("target_path"))
    resolution = failure.get("target_resolution")
    if isinstance(resolution, dict):
        for key in ("primary_target", "target_path"):
            value = resolution.get(key)
            if isinstance(value, str):
                targets.add(value)
                has_target_hint = True
        for key in ("related_targets", "candidate_targets"):
            values = resolution.get(key)
            if isinstance(values, list):
                hinted = [str(value) for value in values if value]
                targets.update(hinted)
                has_target_hint = has_target_hint or bool(hinted)
    details = failure.get("details")
    if isinstance(details, dict):
        for key in ("target_path", "path", "file_path", "filename"):
            value = details.get(key)
            if isinstance(value, str):
                targets.add(value)
                has_target_hint = True
    if not has_target_hint:
        return True
    return target_path in targets


def _nested_issue_evidence_strings(value: object, *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, dict):
        evidence: list[str] = []
        for key, raw in value.items():
            if key in _ISSUE_EVIDENCE_DETAIL_KEYS and isinstance(raw, str) and raw:
                evidence.append(raw)
            elif isinstance(raw, (dict, list)):
                evidence.extend(_nested_issue_evidence_strings(raw, depth=depth + 1))
        return evidence
    if isinstance(value, list):
        evidence = []
        for item in value:
            evidence.extend(_nested_issue_evidence_strings(item, depth=depth + 1))
        return evidence
    return []


def _contract_dependency_manifest_target(raw: dict[str, Any]) -> str:
    paths = [item.path for item in raw["manifest"].files]
    existing = _first_existing_path(
        paths,
        suffixes=(
            "/pyproject.toml",
            "/requirements.txt",
            "/setup.cfg",
            "/setup.py",
            "pyproject.toml",
            "requirements.txt",
            "setup.cfg",
            "setup.py",
        ),
    )
    if existing:
        return existing
    root = str(raw["manifest"].project_root or "").strip().strip("/")
    if root and root != ".":
        return f"{root}/pyproject.toml"
    return "pyproject.toml"


def _classified_validation_issue_code(issue: WorkspaceIssue, *, profile: str) -> str:
    if _looks_like_missing_test_contract(issue, profile=profile):
        return "missing_test_contract"
    return issue.code


def _target_resolution_for_issue(
    raw: dict[str, Any],
    *,
    issue: WorkspaceIssue,
    profile: str,
) -> RepairTargetResolution:
    planned = {item.path for item in raw["manifest"].files}
    generated = {item.path for item in raw.get("generated_files", [])}
    known_paths = planned | generated
    explicit_candidates = _explicit_target_candidates(issue)
    candidates: list[str] = [candidate for candidate in explicit_candidates if candidate in known_paths]
    related = _related_target_candidates(issue, known_paths=known_paths)

    test_contract_target = _pytest_contract_target_for_issue(raw, issue=issue, profile=profile)
    if test_contract_target:
        return RepairTargetResolution(
            primary_target=test_contract_target,
            related_targets=[path for path in related if path != test_contract_target],
            candidate_targets=_dedupe_strings([test_contract_target, *candidates, *related]),
            confidence=0.9,
            rationale="pytest validation requires a collectible test module target",
        )

    imported_callable_target = _target_path_from_pytest_imported_callable(raw, issue=issue, profile=profile)
    if imported_callable_target:
        return RepairTargetResolution(
            primary_target=imported_callable_target,
            related_targets=[path for path in related if path != imported_callable_target],
            candidate_targets=_dedupe_strings([imported_callable_target, *candidates, *related]),
            confidence=0.9,
            rationale="pytest failure called a symbol imported from a generated implementation module",
        )

    missing_local_module_target = _target_path_from_missing_local_python_module(raw, issue)
    if missing_local_module_target:
        return RepairTargetResolution(
            primary_target=missing_local_module_target,
            related_targets=[path for path in related if path != missing_local_module_target],
            candidate_targets=_dedupe_strings([missing_local_module_target, *candidates, *related]),
            confidence=0.9,
            rationale="python validation evidence identified a missing local module provider",
        )

    missing_symbol_target = _target_path_from_missing_python_symbol(raw, issue)
    if missing_symbol_target:
        return RepairTargetResolution(
            primary_target=missing_symbol_target,
            related_targets=[path for path in related if path != missing_symbol_target],
            candidate_targets=_dedupe_strings([missing_symbol_target, *candidates, *related]),
            confidence=0.9,
            rationale="python validation evidence identified a missing local symbol provider",
        )

    if candidates:
        primary = candidates[0]
        return RepairTargetResolution(
            primary_target=primary,
            related_targets=[path for path in related if path != primary],
            candidate_targets=_dedupe_strings([*candidates, *related]),
            confidence=0.95,
            rationale="explicit target path was provided by sandbox validation evidence",
        )

    surface_target = _target_path_from_validation_surface(raw, issue=issue, profile=profile)
    if surface_target:
        return RepairTargetResolution(
            primary_target=surface_target,
            related_targets=[path for path in related if path != surface_target],
            candidate_targets=_dedupe_strings([surface_target, *related]),
            confidence=0.82,
            rationale="validation profile maps to an observed generated runtime surface",
        )

    focused_bundle_resolution = _target_resolution_from_focused_issue_bundle_failure(raw, issue, profile=profile)
    if focused_bundle_resolution is not None and focused_bundle_resolution.primary_target:
        return focused_bundle_resolution

    tool_error_target = _target_path_from_tool_error_path(raw, issue)
    if tool_error_target:
        return RepairTargetResolution(
            primary_target=tool_error_target,
            related_targets=[path for path in related if path != tool_error_target],
            candidate_targets=_dedupe_strings([tool_error_target, *related]),
            confidence=0.9,
            rationale="validation tool error identified the generated file that must be repaired",
        )

    traceback_leaf_target = _target_path_from_traceback_leaf(raw, issue)
    if traceback_leaf_target:
        return RepairTargetResolution(
            primary_target=traceback_leaf_target,
            related_targets=[path for path in related if path != traceback_leaf_target],
            candidate_targets=_dedupe_strings([traceback_leaf_target, *related]),
            confidence=0.85,
            rationale="validation traceback identified the failing generated source file",
        )

    pytest_location_target = _target_path_from_pytest_failure_location(raw, issue)
    if pytest_location_target:
        return RepairTargetResolution(
            primary_target=pytest_location_target,
            related_targets=[path for path in related if path != pytest_location_target],
            candidate_targets=_dedupe_strings([pytest_location_target, *related]),
            confidence=0.88,
            rationale="pytest failure evidence identified the failing generated test or source file",
        )

    evidence_target = _target_path_from_validation_evidence(raw, issue)
    if evidence_target:
        return RepairTargetResolution(
            primary_target=evidence_target,
            related_targets=[path for path in related if path != evidence_target],
            candidate_targets=_dedupe_strings([evidence_target, *related]),
            confidence=0.85,
            rationale="target path was parsed from validation evidence",
        )

    dependency_target = _dependency_manifest_target_for_issue(raw, issue=issue, profile=profile)
    if dependency_target:
        return RepairTargetResolution(
            primary_target=dependency_target,
            related_targets=[path for path in related if path != dependency_target],
            candidate_targets=_dedupe_strings([dependency_target, *related]),
            confidence=0.8,
            rationale="dependency failure maps to the declared dependency manifest target",
        )

    collection_target = _target_path_from_pytest_collection(raw, issue)
    if collection_target:
        return RepairTargetResolution(
            primary_target=collection_target,
            related_targets=[path for path in related if path != collection_target],
            candidate_targets=_dedupe_strings([collection_target, *related]),
            confidence=0.8,
            rationale="test collection evidence identified the failing collection target",
        )

    fallback_candidates = [
        item.path for item in raw["manifest"].files if profile.startswith("python") and item.kind in {"python", "test"}
    ]
    if fallback_candidates:
        primary = fallback_candidates[0]
        return RepairTargetResolution(
            primary_target=primary,
            related_targets=[path for path in related if path != primary],
            candidate_targets=_dedupe_strings([*fallback_candidates, *related]),
            confidence=0.45,
            rationale="profile-compatible generated file selected as a low-confidence repair target",
        )
    if raw["manifest"].files:
        primary = raw["manifest"].files[0].path
        return RepairTargetResolution(
            primary_target=primary,
            related_targets=[path for path in related if path != primary],
            candidate_targets=_dedupe_strings([primary, *related]),
            confidence=0.25,
            rationale="first generated file selected because no stronger target evidence was available",
        )
    return RepairTargetResolution(confidence=0.0, rationale="no generated target file is available")


def _target_path_for_issue(raw: dict[str, Any], *, issue: WorkspaceIssue, profile: str) -> str | None:
    return _target_resolution_for_issue(raw, issue=issue, profile=profile).primary_target


def _target_resolution_from_details(details: dict[str, Any], *, target_path: str | None) -> RepairTargetResolution:
    raw = details.get("target_resolution")
    if isinstance(raw, dict):
        try:
            return RepairTargetResolution.model_validate(raw)
        except ValueError:
            pass
    return RepairTargetResolution(
        primary_target=target_path,
        candidate_targets=[target_path] if target_path else [],
        confidence=0.0 if target_path is None else 0.5,
        rationale="target resolution was reconstructed from issue target_path",
    )


def _explicit_target_candidates(issue: WorkspaceIssue) -> list[str]:
    candidates: list[str] = []
    for key in ("target_path", "path", "file_path", "filename"):
        value = issue.details.get(key)
        if isinstance(value, str):
            candidates.append(value.strip())
    return _dedupe_strings([candidate for candidate in candidates if candidate])


def _related_target_candidates(issue: WorkspaceIssue, *, known_paths: set[str]) -> list[str]:
    candidates: list[str] = []
    values = issue.details.get("related_targets")
    if isinstance(values, list):
        candidates.extend(str(item).strip() for item in values)
    candidates.extend(_known_targets_from_evidence(_issue_evidence_text(issue), known_paths=known_paths))
    bundle = issue.details.get("issue_bundle")
    if isinstance(bundle, dict):
        failures = bundle.get("failures")
        if isinstance(failures, list):
            for failure in failures:
                if not isinstance(failure, dict):
                    continue
                target = failure.get("target_path")
                if isinstance(target, str):
                    candidates.append(target.strip())
                resolution = failure.get("target_resolution")
                if isinstance(resolution, dict):
                    for key in ("primary_target", "related_targets", "candidate_targets"):
                        raw_value = resolution.get(key)
                        if isinstance(raw_value, str):
                            candidates.append(raw_value.strip())
                        elif isinstance(raw_value, list):
                            candidates.extend(str(item).strip() for item in raw_value)
                details = failure.get("details")
                if isinstance(details, dict):
                    candidates.extend(
                        _known_targets_from_evidence(
                            _workspace_issue_details_text(details),
                            known_paths=known_paths,
                        )
                    )
    return _dedupe_strings([candidate for candidate in candidates if candidate in known_paths])


def _known_targets_from_evidence(evidence: str, *, known_paths: set[str]) -> list[str]:
    if not evidence or not known_paths:
        return []
    candidates: list[str] = []
    suffixes = r"(?:py|toml|cfg|ini|yaml|yml|json|md|txt)"
    patterns = (
        r'File "([^"]+)"',
        rf"\(([^()\n]+\.{suffixes})\)",
        rf"(?im)(?:^|\s)([/\w.\-]+\.{suffixes})(?::\d+)?(?::|\s|$)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, evidence):
            resolved = _known_target_from_evidence_candidate(match.group(1), known_paths=known_paths)
            if resolved:
                candidates.append(resolved)
    return _dedupe_strings(candidates)


def _known_target_from_evidence_candidate(candidate: str, *, known_paths: set[str]) -> str | None:
    normalized = str(candidate or "").strip().replace("\\", "/").lstrip("./")
    if not normalized:
        return None
    normalized = normalized.split(":", 1)[0]
    if normalized in known_paths:
        return normalized
    for known in sorted(known_paths, key=len, reverse=True):
        if normalized.endswith(f"/{known}") or normalized == known:
            return known
    return None


def _workspace_issue_details_text(details: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in details.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            parts.append(_workspace_issue_details_text(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(_workspace_issue_details_text(item))
    return "\n".join(part for part in parts if part)


def _target_resolution_from_focused_issue_bundle_failure(
    raw: dict[str, Any],
    issue: WorkspaceIssue,
    *,
    profile: str,
) -> RepairTargetResolution | None:
    bundle = issue.details.get("issue_bundle")
    if not isinstance(bundle, dict):
        return None
    failures = bundle.get("failures")
    if not isinstance(failures, list):
        return None
    focus_profile = str(bundle.get("repair_focus_profile") or issue.details.get("profile") or profile)
    planned = {item.path for item in raw["manifest"].files}
    generated = {item.path for item in raw.get("generated_files", [])}
    known_paths = planned | generated
    related = _related_target_candidates(issue, known_paths=known_paths)
    for failure in failures:
        if not isinstance(failure, dict) or str(failure.get("profile") or "") != focus_profile:
            continue
        failure_issue = _workspace_issue_from_bundle_failure(failure)
        implementation_target = _target_path_from_pytest_imported_callable(
            raw,
            issue=failure_issue,
            profile=focus_profile,
        )
        if implementation_target and implementation_target in known_paths:
            return RepairTargetResolution(
                primary_target=implementation_target,
                related_targets=[path for path in related if path != implementation_target],
                candidate_targets=_dedupe_strings([implementation_target, *related]),
                confidence=0.93,
                rationale=(
                    "issue bundle repair focus retargeted pytest evidence "
                    "from generated test to imported implementation module"
                ),
            )
        low_confidence_resolution = False
        resolution = failure.get("target_resolution")
        if isinstance(resolution, dict):
            try:
                parsed = RepairTargetResolution.model_validate(resolution)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.confidence < 0.5:
                low_confidence_resolution = True
            if parsed is not None and parsed.confidence >= 0.5 and parsed.primary_target in known_paths:
                primary = parsed.primary_target
                return RepairTargetResolution(
                    primary_target=primary,
                    related_targets=_dedupe_strings(
                        [
                            *(parsed.related_targets or []),
                            *[path for path in related if path != primary],
                        ]
                    ),
                    candidate_targets=_dedupe_strings(
                        [
                            primary,
                            *(parsed.candidate_targets or []),
                            *related,
                        ]
                    ),
                    confidence=max(parsed.confidence, 0.9),
                    rationale="issue bundle repair focus selected the matching validation profile target",
                )
        target = failure.get("target_path")
        if isinstance(target, str) and target in known_paths and not low_confidence_resolution:
            return RepairTargetResolution(
                primary_target=target,
                related_targets=[path for path in related if path != target],
                candidate_targets=_dedupe_strings([target, *related]),
                confidence=0.9,
                rationale="issue bundle repair focus selected the matching validation profile target",
            )
    return None


def _workspace_issue_from_bundle_failure(failure: dict[str, Any]) -> WorkspaceIssue:
    details = failure.get("details")
    merged_details = dict(details) if isinstance(details, dict) else {}
    target = failure.get("target_path")
    if isinstance(target, str) and target:
        merged_details.setdefault("target_path", target)
    resolution = failure.get("target_resolution")
    if isinstance(resolution, dict):
        primary = resolution.get("primary_target")
        if isinstance(primary, str) and primary:
            merged_details.setdefault("target_path", primary)
        candidates = resolution.get("candidate_targets")
        if isinstance(candidates, list):
            merged_details.setdefault("related_targets", [str(item) for item in candidates])
    return WorkspaceIssue(
        code=str(failure.get("issue_code") or "validation_failed"),
        message=str(failure.get("message") or ""),
        details=merged_details,
    )


def _target_path_from_validation_surface(raw: dict[str, Any], *, issue: WorkspaceIssue, profile: str) -> str | None:
    profile_entrypoint_kinds = {
        "cli": {"cli"},
        "python-api": {"api", "service"},
        "worker-queue": {"worker"},
    }
    wanted_kinds = profile_entrypoint_kinds.get(profile)
    if not wanted_kinds:
        return None
    observed_contract: ObservedContract | None = raw.get("observed_contract")
    if observed_contract is None:
        return None
    planned = {item.path for item in raw["manifest"].files}
    generated = {item.path for item in raw.get("generated_files", [])}
    known_paths = planned | generated
    if not known_paths:
        return None
    issue_text = _issue_evidence_text(issue).casefold()
    candidates = [
        item.path
        for item in observed_contract.entrypoints
        if item.kind in wanted_kinds and item.path in known_paths
    ]
    if not candidates:
        return None
    code_candidates = [
        path
        for path in candidates
        if path.endswith(".py") and _path_kind(raw, path) in {"python", "test", "other"}
    ]
    if profile == "cli" and "does not expose callable main" in issue_text:
        main_candidates = [path for path in code_candidates if _observed_file_exports(raw, path, "main")]
        if main_candidates:
            return main_candidates[0]
        if code_candidates:
            return code_candidates[0]
    return (code_candidates or candidates)[0]


def _target_path_from_pytest_imported_callable(
    raw: dict[str, Any],
    *,
    issue: WorkspaceIssue,
    profile: str,
) -> str | None:
    if profile != "python-pytest":
        return None
    evidence = _issue_evidence_text(issue)
    called_names = _pytest_called_names_from_evidence(evidence)
    called_attributes = _pytest_called_attribute_chains_from_evidence(evidence)
    if not called_names:
        called_names = {chain[-1] for chain in called_attributes if chain}
    force_imported_symbol_target = _pytest_failure_expects_imported_symbol_contract(evidence)
    if not called_names and not called_attributes:
        if not force_imported_symbol_target:
            return None
    known_paths = {item.path for item in raw["manifest"].files} | {item.path for item in raw.get("generated_files", [])}
    test_targets = [candidate for candidate in _explicit_target_candidates(issue) if candidate in known_paths]
    if not test_targets:
        location_target = _target_path_from_pytest_failure_location(raw, issue)
        if location_target:
            test_targets.append(location_target)
    if not test_targets:
        test_targets.extend(sorted(path for path in known_paths if _looks_like_pytest_file_path(path)))
    for test_target in test_targets:
        if not _looks_like_pytest_file_path(test_target):
            continue
        generated = _generated_file(raw, test_target)
        if generated is None:
            continue
        try:
            tree = ast.parse(generated.content, filename=test_target)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 0 or not node.module:
                continue
            for alias in node.names:
                local_name = alias.asname or alias.name
                if force_imported_symbol_target:
                    target = _target_path_for_python_module(raw, node.module)
                    if target and target != test_target:
                        return target
                if local_name not in called_names:
                    for chain in called_attributes:
                        if len(chain) >= 2 and chain[0] == local_name and chain[-1] in called_names:
                            target = _target_path_for_python_module(raw, f"{node.module}.{alias.name}")
                            if target and target != test_target:
                                return target
                    continue
                target = _target_path_for_python_module(raw, node.module)
                if target and target != test_target:
                    return target
    return None


def _pytest_failure_expects_imported_symbol_contract(evidence: str) -> bool:
    normalized = evidence.casefold()
    return (
        "failed: did not raise" in normalized
        or "did not raise systemexit" in normalized
        or "pytest.raises" in normalized and "did not raise" in normalized
    )


def _pytest_called_names_from_evidence(evidence: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"(?m)^\s*>\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", evidence):
        names.add(match.group(1))
    for line_match in re.finditer(r"(?m)^\s*>\s*(.+)$", evidence):
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", line_match.group(1)):
            name = match.group(1)
            if name not in {"assert", "print", "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple"}:
                names.add(name)
    for match in re.finditer(r"(?m)^\s*E\s*\+\s+where\s+.+?=\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", evidence):
        names.add(match.group(1))
    for chain in _pytest_called_attribute_chains_from_evidence(evidence):
        if chain:
            names.add(chain[-1])
    for match in re.finditer(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\(\)\s+takes\s+\d+\s+positional arguments?",
        evidence,
    ):
        names.add(match.group(1))
    return names


def _pytest_called_attribute_chains_from_evidence(evidence: str) -> list[list[str]]:
    chains: list[list[str]] = []
    for match in re.finditer(
        r"(?m)^\s*>\s*((?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*)\s*\(",
        evidence,
    ):
        chains.append([part for part in match.group(1).split(".") if part])
    for line_match in re.finditer(r"(?m)^\s*>\s*(.+)$", evidence):
        line = line_match.group(1)
        for match in re.finditer(
            r"\b((?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*)\s*\(",
            line,
        ):
            chains.append([part for part in match.group(1).split(".") if part])
    return chains


def _target_path_for_python_module(raw: dict[str, Any], module_name: str) -> str | None:
    project_root = str(raw["manifest"].project_root or "").strip("/").replace("\\", "/")
    candidates: list[str] = []
    module_path = module_name.replace(".", "/")
    if project_root:
        candidates.extend(
            [
                f"{project_root}/{module_path}.py",
                f"{project_root}/{module_path}/__init__.py",
            ]
        )
    candidates.extend([f"{module_path}.py", f"{module_path}/__init__.py"])
    known_paths = {item.path for item in raw["manifest"].files} | {item.path for item in raw.get("generated_files", [])}
    for candidate in candidates:
        if candidate in known_paths:
            return candidate
    return None


def _path_kind(raw: dict[str, Any], path: str) -> str:
    for item in [*raw["manifest"].files, *raw.get("generated_files", [])]:
        if item.path == path:
            return str(getattr(item, "kind", "") or "other")
    return "other"


def _observed_file_exports(raw: dict[str, Any], path: str, name: str) -> bool:
    observed_contract: ObservedContract | None = raw.get("observed_contract")
    if observed_contract is None:
        return False
    return any(item.path == path and item.name == name for item in observed_contract.exports)


def _target_path_from_validation_evidence(raw: dict[str, Any], issue: WorkspaceIssue) -> str | None:
    evidence = _issue_evidence_text(issue)
    if not evidence:
        return None
    root = str(raw["manifest"].project_root or "").strip("/").replace("\\", "/")
    planned = {item.path for item in raw["manifest"].files}
    generated = {item.path for item in raw.get("generated_files", [])}
    for match in re.finditer(r'File "([^"]+)"', evidence):
        candidate = match.group(1).strip().replace("\\", "/").lstrip("./")
        if root and candidate and not candidate.startswith(f"{root}/"):
            candidate = f"{root}/{candidate}"
        if candidate in planned or candidate in generated:
            return candidate
    return None


def _target_path_from_tool_error_path(raw: dict[str, Any], issue: WorkspaceIssue) -> str | None:
    evidence = _issue_evidence_text(issue)
    if not evidence:
        return None
    suffixes = r"(?:py|toml|cfg|ini|yaml|yml|json)"
    patterns = (
        rf"(?im)(?:^|\n)\s*(?:ERROR|Error|error):\s+([^\s:]+\.{suffixes})(?::|\s)",
        rf"(?im)\b([/\w.\-]+\.{suffixes}):\s+(?:Expected|Invalid|Error|error|line\b)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, evidence):
            target = _normalize_evidence_path(raw, match.group(1))
            if target:
                return target
    return None


def _target_path_from_missing_local_python_module(raw: dict[str, Any], issue: WorkspaceIssue) -> str | None:
    evidence = _issue_evidence_text(issue)
    if not evidence:
        return None
    match = re.search(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", evidence)
    if not match:
        return None
    module_name = match.group(1).strip()
    if not module_name or module_name.startswith("."):
        return None
    parts = [part for part in module_name.split(".") if part]
    if not parts:
        return None
    local_roots = _local_python_package_roots(raw)
    if parts[0] not in local_roots:
        return None
    root = str(raw["manifest"].project_root or "").strip("/").replace("\\", "/")
    relative = "/".join(parts) + ".py"
    candidate = f"{root}/{relative}" if root and root != "." else relative
    known_paths = {item.path for item in raw["manifest"].files} | {item.path for item in raw.get("generated_files", [])}
    parent_init = f"{root}/{'/'.join(parts[:-1])}/__init__.py" if root and len(parts) > 1 else f"{'/'.join(parts[:-1])}/__init__.py"
    if candidate in known_paths or parent_init in known_paths or parts[0] in local_roots:
        return candidate
    return None


def _target_path_from_missing_python_symbol(raw: dict[str, Any], issue: WorkspaceIssue) -> str | None:
    evidence = _issue_evidence_text(issue)
    if not evidence:
        return None
    match = re.search(
        r"cannot import name ['\"][^'\"]+['\"] from ['\"][^'\"]+['\"] \(([^)]+\.py)\)",
        evidence,
    )
    if not match:
        return None
    return _normalize_evidence_path(raw, match.group(1))


def _target_path_from_traceback_leaf(raw: dict[str, Any], issue: WorkspaceIssue) -> str | None:
    evidence = _issue_evidence_text(issue)
    if not evidence:
        return None
    targets: list[str] = []
    for match in re.finditer(r'File "([^"]+\.py)"', evidence):
        target = _normalize_evidence_path(raw, match.group(1))
        if target:
            targets.append(target)
    non_test_targets = [target for target in targets if not _looks_like_pytest_file_path(target)]
    if non_test_targets:
        return non_test_targets[-1]
    return targets[-1] if targets else None


def _target_path_from_pytest_failure_location(raw: dict[str, Any], issue: WorkspaceIssue) -> str | None:
    evidence = _issue_evidence_text(issue)
    if not evidence:
        return None
    targets: list[str] = []
    patterns = (
        r"(?:^|\n)\s*(?!FAILED\b)([^\s:\n]+\.py):\d+:\s*(?:in\s+\S+|[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))",
        r"(?:^|\n)FAILED\s+([^\s:]+\.py)::",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, evidence):
            target = _normalize_evidence_path(raw, match.group(1))
            if target:
                targets.append(target)
    non_test_targets = [target for target in targets if not _looks_like_pytest_file_path(target)]
    collection_import_error = (
        "error collecting" in evidence.casefold()
        or "importerror while importing test module" in evidence.casefold()
    )
    if collection_import_error and non_test_targets:
        return non_test_targets[-1]
    test_targets = [target for target in targets if _looks_like_pytest_file_path(target)]
    if test_targets:
        return test_targets[-1]
    return non_test_targets[-1] if non_test_targets else None


def _local_python_package_roots(raw: dict[str, Any]) -> set[str]:
    roots: set[str] = set()
    root = str(raw["manifest"].project_root or "").strip("/").replace("\\", "/")
    prefix = f"{root}/" if root and root != "." else ""
    for path in [item.path for item in raw["manifest"].files] + [item.path for item in raw.get("generated_files", [])]:
        normalized = path.replace("\\", "/")
        if prefix and normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
        parts = normalized.split("/")
        if not normalized.endswith(".py"):
            continue
        if len(parts) == 1:
            stem = parts[0][:-3]
            if stem and stem != "__init__" and stem.isidentifier():
                roots.add(stem)
            continue
        if parts[-1] == "__init__.py" and parts[0].isidentifier():
            roots.add(parts[0])
        elif parts[0].isidentifier():
            roots.add(parts[0])
    return roots


def _target_path_from_pytest_collection(raw: dict[str, Any], issue: WorkspaceIssue) -> str | None:
    evidence = _issue_evidence_text(issue)
    if not evidence:
        return None
    for match in re.finditer(r"ERROR collecting\s+([^\n\r]+)", evidence):
        target = _normalize_evidence_path(raw, match.group(1))
        if target:
            return target
    return None


def _normalize_evidence_path(raw: dict[str, Any], candidate: str) -> str | None:
    normalized = candidate.strip().strip(":").replace("\\", "/").lstrip("./")
    if not normalized:
        return None
    root = str(raw["manifest"].project_root or "").strip("/").replace("\\", "/")
    planned = {item.path for item in raw["manifest"].files}
    generated = {item.path for item in raw.get("generated_files", [])}
    known_paths = planned | generated
    candidates = [normalized]
    if root and not normalized.startswith(f"{root}/"):
        candidates.append(f"{root}/{normalized}")
    for item in candidates:
        if item in known_paths:
            return item
    absolute_normalized = normalized.lstrip("/")
    for known_path in known_paths:
        known_normalized = known_path.replace("\\", "/").lstrip("./")
        if absolute_normalized == known_normalized or absolute_normalized.endswith(f"/{known_normalized}"):
            return known_path
    return None


def _looks_like_missing_test_contract(issue: WorkspaceIssue, *, profile: str) -> bool:
    if profile != "python-pytest":
        return False
    evidence = _issue_evidence_text(issue).casefold()
    if _looks_like_python_dependency_issue(evidence) or "error collecting" in evidence:
        return False
    return (
        re.search(r"collected\s+0\s+items\s*(?:\n|$)", evidence) is not None
        or "no tests ran" in evidence
        or "no tests collected" in evidence
    )


def _dependency_manifest_target_for_issue(raw: dict[str, Any], *, issue: WorkspaceIssue, profile: str) -> str | None:
    evidence = _issue_evidence_text(issue)
    if not evidence:
        return None
    paths = [item.path for item in raw["manifest"].files]
    if profile.startswith("python") and _looks_like_python_dependency_issue(evidence):
        return _first_existing_path(
            paths,
            suffixes=(
                "/requirements.txt",
                "/pyproject.toml",
                "/setup.cfg",
                "/setup.py",
                "requirements.txt",
                "pyproject.toml",
                "setup.cfg",
                "setup.py",
            ),
        )
    if profile.startswith("node") and _looks_like_node_dependency_issue(evidence):
        return _first_existing_path(paths, suffixes=("/package.json", "package.json"))
    return None


def _pytest_contract_target_for_issue(raw: dict[str, Any], *, issue: WorkspaceIssue, profile: str) -> str | None:
    if not _looks_like_missing_test_contract(issue, profile=profile):
        return None
    manifest_files = list(raw["manifest"].files)
    for item in manifest_files:
        if item.kind == "test" and _looks_like_pytest_file_path(item.path):
            return item.path
    for item in manifest_files:
        if item.kind == "test":
            return _derived_pytest_path(raw, seed_path=item.path)
    for item in manifest_files:
        if item.kind == "python" or item.path.endswith(".py"):
            return _derived_pytest_path(raw, seed_path=item.path)
    return _derived_pytest_path(raw, seed_path="behavior.py")


def _looks_like_pytest_file_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return normalized.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{normalized}")


def _derived_pytest_path(raw: dict[str, Any], *, seed_path: str) -> str:
    root = str(raw["manifest"].project_root or "").strip().strip("/")
    name = seed_path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if stem in {"", "__init__", "test", "tests"}:
        stem = "behavior"
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_").lower() or "behavior"
    filename = stem if stem.startswith("test_") else f"test_{stem}"
    prefix = "" if not root or root == "." else f"{root}/"
    return f"{prefix}tests/{filename}.py"


def _issue_evidence_text(issue: WorkspaceIssue) -> str:
    return "\n".join(_dedupe_strings([issue.code, issue.message, *_nested_issue_evidence_strings(issue.details)]))


def _looks_like_python_dependency_issue(evidence: str) -> bool:
    patterns = (
        r"ModuleNotFoundError:\s+No module named",
        r"ImportError:\s+No module named",
        r"pkg_resources\.DistributionNotFound",
    )
    return any(re.search(pattern, evidence) for pattern in patterns)


def _looks_like_node_dependency_issue(evidence: str) -> bool:
    patterns = (
        r"Cannot find module",
        r"ERR_MODULE_NOT_FOUND",
        r"npm ERR! missing",
    )
    return any(re.search(pattern, evidence) for pattern in patterns)


def _first_existing_path(paths: list[str], *, suffixes: tuple[str, ...]) -> str | None:
    for suffix in suffixes:
        for path in paths:
            if path == suffix or path.endswith(suffix):
                return path
    return None


def _target_kind_for_path(raw: dict[str, Any], target_path: str | None) -> str:
    if not target_path:
        return "validation"
    for item in raw["manifest"].files:
        if item.path == target_path:
            return f"{item.kind}_file" if item.kind else "file"
    return "file"


def _contract_refs_for_issue(raw: dict[str, Any]) -> list[str]:
    material_contract: MaterialContract | None = raw.get("material_contract")
    if material_contract is None:
        return []
    return [material_contract.contract_id]


def _requirement_refs_for_issue(
    raw: dict[str, Any],
    *,
    target_path: str | None,
    profile: object,
) -> list[str]:
    material_contract: MaterialContract | None = raw.get("material_contract")
    if material_contract is None:
        return []
    refs: list[str] = []
    if isinstance(profile, str) and profile:
        for validation in material_contract.validation_profiles:
            if validation.profile == profile:
                refs.extend(validation.requirement_ids)
    if target_path:
        for planned_file in material_contract.planned_files:
            if planned_file.path == target_path:
                refs.extend(planned_file.requirement_ids)
    if not refs and material_contract.requirements:
        refs.append(material_contract.requirements[0].requirement_id)
    return _dedupe_strings(refs)


def _latest_repairable_issue(raw: dict[str, Any]) -> MaterialIssue | None:
    issues = list(raw.get("issues", []))
    non_deferred = [
        issue
        for issue in issues
        if issue.severity == "repairable" and not bool(issue.details.get("deferred_until_alternative_symbol_repairs"))
    ]
    for issue in non_deferred or issues:
        if issue.severity == "repairable":
            return issue
    return None


def _should_defer_exhausted_repair_rejection(
    raw: dict[str, Any],
    issue: MaterialIssue,
    *,
    reason: str,
    details: dict[str, Any],
) -> bool:
    if issue.issue_type != "missing_symbol_provider":
        return False
    if reason == "replacement_contract_mismatch" and not details.get("forbidden_imports"):
        return False
    if reason not in {"replacement_noop", "replacement_contract_mismatch"}:
        return False
    issue_symbols = set(_expected_python_exports_for_issue(issue))
    for candidate in raw.get("issues", []):
        if candidate.issue_id == issue.issue_id or candidate.severity != "repairable":
            continue
        if candidate.issue_type != "missing_symbol_provider":
            continue
        if not bool(candidate.target_path):
            continue
        candidate_symbols = set(_expected_python_exports_for_issue(candidate))
        if issue_symbols and candidate_symbols and not (issue_symbols & candidate_symbols):
            continue
        if candidate.target_path != issue.target_path:
            return True
        if candidate_symbols and candidate_symbols != issue_symbols:
            return True
    return False


def _move_issue_to_end(raw: dict[str, Any], issue: MaterialIssue) -> None:
    issues = [item for item in raw.get("issues", []) if item.issue_id != issue.issue_id]
    issues.append(issue)
    raw["issues"] = issues


def _record_repair_arbiter_attempt(raw: dict[str, Any], attempt: dict[str, Any]) -> None:
    state = raw.get("repair_arbiter")
    if not isinstance(state, dict):
        state = {"schema_version": "repair_arbiter.v0.1", "attempts": [], "rejections": []}
        raw["repair_arbiter"] = state
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
        state["attempts"] = attempts
    attempts.append(attempt)
    raw["manifest"].repair_arbiter = state


def _record_repair_arbiter_rejection(raw: dict[str, Any], rejection: dict[str, Any]) -> None:
    state = raw.get("repair_arbiter")
    if not isinstance(state, dict):
        state = {"schema_version": "repair_arbiter.v0.1", "attempts": [], "rejections": []}
        raw["repair_arbiter"] = state
    rejections = state.get("rejections")
    if not isinstance(rejections, list):
        rejections = []
        state["rejections"] = rejections
    rejections.append(rejection)
    raw["manifest"].repair_arbiter = state


def _clear_accepted_repair_issues(raw: dict[str, Any], accepted_issue: MaterialIssue) -> None:
    raw["issues"] = [
        issue
        for issue in raw.get("issues", [])
        if not _issue_cleared_by_accepted_repair(issue, accepted_issue)
    ]
    raw["manifest"].issues = list(raw["issues"])


def _issue_cleared_by_accepted_repair(issue: MaterialIssue, accepted_issue: MaterialIssue) -> bool:
    if issue.issue_id == accepted_issue.issue_id:
        return True
    if issue.target_path != accepted_issue.target_path:
        return False
    dependency_issue_types = {"dependency_strategy_mismatch", "missing_dependency_strategy"}
    if issue.issue_type in dependency_issue_types and accepted_issue.issue_type in dependency_issue_types:
        return True
    symbol_issue_types = {"missing_symbol_provider"}
    if issue.issue_type in symbol_issue_types and accepted_issue.issue_type in symbol_issue_types:
        return True
    return False


def _attach_related_symbol_repair_context(raw: dict[str, Any], issue: MaterialIssue) -> None:
    if issue.issue_type != "missing_symbol_provider" or not issue.target_path:
        return
    related = [
        item
        for item in raw.get("issues", [])
        if item.issue_type == "missing_symbol_provider" and item.target_path == issue.target_path
    ]
    expected_symbols = _dedupe_strings(
        [
            symbol
            for item in related
            for symbol in _expected_python_exports_for_issue(item)
            if symbol
        ]
    )
    if not expected_symbols:
        return
    issue.details["expected_symbols"] = expected_symbols
    repair_obligations = [
        dict(item)
        for item in issue.details.get("repair_obligations") or []
        if isinstance(item, dict)
    ]
    obligated_symbols = {
        str(item.get("symbol") or item.get("missing_name") or "").strip()
        for item in repair_obligations
        if str(item.get("symbol") or item.get("missing_name") or "").strip()
    }
    target_module = _symbol_provider_target_module(issue)
    for symbol in expected_symbols:
        if symbol in obligated_symbols:
            continue
        repair_obligations.append(
            {
                "obligation_id": f"obligation:importable_export:{target_module or issue.target_path}:{symbol}",
                "kind": "importable_export",
                "target_module": target_module or None,
                "target_path": issue.target_path,
                "symbol": symbol,
                "required_by": [item.target_path for item in related if item.target_path],
                "source_issue_type": issue.issue_type,
                "acceptance": [
                    "symbol is provided by the target interface surface",
                    "callers importing the symbol resolve without interface drift",
                    "implementation is not placeholder-only",
                ],
            }
        )
    if repair_obligations:
        issue.details["repair_obligations"] = repair_obligations
    issue.details["related_open_symbol_issues"] = [
        {
            "issue_id": item.issue_id,
            "observed_issue_type": item.details.get("observed_issue_type"),
            "missing_name": item.details.get("missing_name") or item.details.get("name"),
            "line": item.details.get("line"),
        }
        for item in related
    ]


def _symbol_provider_target_module(issue: MaterialIssue) -> str:
    module = str(issue.details.get("module") or "").strip()
    if module:
        return module
    for item in issue.details.get("repair_obligations") or []:
        if not isinstance(item, dict):
            continue
        module = str(item.get("target_module") or "").strip()
        if module:
            return module
    return ""


def _invalidate_observed_contract_after_repair(raw: dict[str, Any]) -> None:
    raw["observed_contract"] = None
    raw["contract_comparison"] = None
    raw["manifest"].observed_contract = None
    raw["manifest"].contract_comparison = None
    raw["manifest"].requirements_trace = []


def _generated_file(raw: dict[str, Any], target_path: str) -> GeneratedMaterialFile | None:
    for item in raw.get("generated_files", []):
        if item.path == target_path:
            return item
    return None


def _placeholder_generated_file_for_missing_target(
    raw: dict[str, Any],
    issue: MaterialIssue,
) -> GeneratedMaterialFile | None:
    if not issue.target_path:
        return None
    if _material_issue_targets_dependency_manifest(issue):
        expected_target = _contract_dependency_manifest_target(raw)
        if issue.target_path != expected_target:
            return None
        return GeneratedMaterialFile.from_text(
            path=issue.target_path,
            content="# material dependency manifest placeholder for repair\n",
            kind=_manifest_kind_for_path(issue.target_path),
        )
    if issue.issue_type == "missing_test_contract" and _looks_like_pytest_file_path(issue.target_path):
        return GeneratedMaterialFile.from_text(
            path=issue.target_path,
            content="# material pytest placeholder for repair\n",
            kind="test",
        )
    if issue.target_path.endswith(".py"):
        return GeneratedMaterialFile.from_text(
            path=issue.target_path,
            content="# material python module placeholder for repair\n",
            kind="python",
        )
    return None


def _material_issue_targets_dependency_manifest(issue: MaterialIssue) -> bool:
    if issue.issue_type == "missing_dependency_strategy":
        return bool(issue.details.get("undeclared_external_imports"))
    if issue.issue_type != "dependency_strategy_mismatch":
        return False
    observed_issue_type = str(issue.details.get("observed_issue_type") or "")
    return observed_issue_type == "missing_dependency_declaration" and bool(
        issue.details.get("dependency_name") or issue.details.get("module")
    )


def _replace_generated_file(raw: dict[str, Any], replacement: GeneratedMaterialFile) -> None:
    files = list(raw.get("generated_files", []))
    for index, item in enumerate(files):
        if item.path == replacement.path:
            files[index] = replacement
            raw["generated_files"] = files
            return
    files.append(replacement)
    raw["generated_files"] = files


def _ensure_manifest_file(raw: dict[str, Any], path: str, *, kind: str) -> None:
    if any(item.path == path for item in raw["manifest"].files):
        return
    raw["manifest"].files.append(
        MaterialManifestFile(
            path=path,
            purpose="Dependency/runtime strategy manifest added during evidence-driven repair.",
            state="generated",
            kind=kind,
        )
    )


def _manifest_hash(raw: dict[str, Any], target_path: str) -> str | None:
    for item in raw["manifest"].files:
        if item.path == target_path:
            return item.content_hash
    return None


def _mark_file_repaired(raw: dict[str, Any], target_path: str, after_sha256: str) -> None:
    for item in raw["manifest"].files:
        if item.path == target_path:
            item.state = "repaired"
            item.content_hash = after_sha256
            item.repair_round += 1
            return
    raw["manifest"].files.append(
        MaterialManifestFile(
            path=target_path,
            purpose="Material file added during evidence-driven repair.",
            state="repaired",
            kind=_manifest_kind_for_path(target_path),
            content_hash=after_sha256,
            repair_round=1,
        )
    )


def _mark_manifest_file_written(raw: dict[str, Any], target_path: str, sha256: str, *, producer: str) -> None:
    for item in raw["manifest"].files:
        if item.path == target_path:
            item.state = "workspace_written"
            item.content_hash = sha256
            item.producer = producer
            return
    raw["manifest"].files.append(
        MaterialManifestFile(
            path=target_path,
            purpose="Validation command evidence written before artifact packaging.",
            state="workspace_written",
            kind=_manifest_kind_for_path(target_path),
            content_hash=sha256,
            producer=producer,
        )
    )


def _manifest_kind_for_path(path: str) -> str:
    filename = path.rsplit("/", 1)[-1].casefold()
    if filename in {"pyproject.toml", "requirements.txt", "setup.cfg", "setup.py"}:
        return "config"
    if path.endswith(".py"):
        return "python"
    if filename in {"package.json", "package-lock.json"}:
        return "config"
    return "other"


def _issue_contract(issue: MaterialIssue) -> dict[str, object]:
    validation_profile = issue.details.get("profile")
    expected_exports = _expected_python_exports_for_issue(issue)
    call_expectations = _pytest_call_expectations_from_issue(issue)
    repair_intent = [
        "repair the smallest coherent defect shown by validation evidence",
        "preserve the generated project architecture and owner boundaries",
    ]
    acceptance = [
        f"{validation_profile} validation passes after patch apply"
        if validation_profile
        else "the failed validation passes after patch apply",
        "the patch touches only the requested target path",
    ]
    if expected_exports:
        repair_intent.append(
            "ensure the target Python module provides every top-level export required by validation evidence"
        )
        acceptance.append(
            "the target Python module exposes these names at top level: " + ", ".join(expected_exports)
        )
    if _material_issue_has_local_import_cycle_evidence(issue):
        repair_intent.append("break the local Python import cycle shown by validation evidence")
        acceptance.append(
            "the repaired modules no longer import each other through a package root during module initialization"
        )
    if call_expectations:
        repair_intent.append(
            "align callable signatures with how generated tests invoke them, preserving CLI/runtime behavior"
        )
        acceptance.extend(
            "callable {name} accepts at least {count} positional argument(s) as shown by validation evidence".format(
                name=str(item.get("function_name") or item.get("callable") or "target"),
                count=int(item.get("minimum_positional_arguments") or 0),
            )
            for item in call_expectations
        )
        for item in call_expectations:
            if item.get("expected_behavior") == "cli_help":
                acceptance.append(
                    "callable {name} handles help-style argv and writes usage/help text to stdout".format(
                        name=str(item.get("function_name") or item.get("callable") or "target"),
                    )
                )
    repair_obligations = issue.details.get("repair_obligations")
    if isinstance(repair_obligations, list) and repair_obligations:
        repair_intent.append("satisfy every deterministic repair obligation attached to this issue")
        for obligation in repair_obligations[:8]:
            if not isinstance(obligation, dict):
                continue
            if obligation.get("kind") == "invalid_test_module_dependency":
                repair_intent.append(
                    "remove runtime/source dependency on generated test modules while preserving test coverage"
                )
            obligation_acceptance = obligation.get("acceptance")
            if isinstance(obligation_acceptance, list):
                acceptance.extend(str(item) for item in obligation_acceptance[:4])
    return {
        "schema_version": "material_issue.v3.2",
        "issue_id": issue.issue_id,
        "issue_type": issue.issue_type,
        "severity": "repairable",
        "target_kind": issue.target_kind,
        "requirement_refs": issue.requirement_refs,
        "contract_refs": issue.contract_refs,
        "repair_intent": repair_intent,
        "acceptance": acceptance,
        "related_context": [issue.target_path] if issue.target_path else [],
        "repair_obligations": repair_obligations if isinstance(repair_obligations, list) else [],
        "patch_rejections": [
            rejection.model_dump(mode="json") for rejection in issue.patch_rejections
        ],
    }


def _material_issue_has_local_import_cycle_evidence(issue: MaterialIssue) -> bool:
    evidence = _material_issue_evidence_text(issue).casefold()
    return (
        "partially initialized module" in evidence
        or "circular import" in evidence
        or bool(_forbidden_local_imports_from_rejections(issue))
    )


def _current_content_context(content: str, *, issue: MaterialIssue) -> dict[str, object]:
    lines = content.splitlines()
    target_lines = _evidence_line_numbers(issue)
    if not target_lines:
        target_lines = [1]
    windows: list[dict[str, object]] = []
    for line_number in target_lines[:5]:
        start = max(1, line_number - 20)
        end = min(len(lines), line_number + 20)
        window_lines = lines[start - 1 : end]
        windows.append(
            {
                "start_line": start,
                "end_line": end,
                "content": "\n".join(window_lines),
            }
        )
    context: dict[str, object] = {
        "line_count": len(lines),
        "target_lines": target_lines[:5],
        "windows": windows,
        "content_available": True,
        "expected_symbols": _expected_python_exports_for_issue(issue),
        "missing_expected_symbols": _missing_expected_python_exports(issue, content),
    }
    symbol_obligations = _expected_symbol_obligations_from_issue(issue)
    if symbol_obligations:
        context["symbol_obligations"] = symbol_obligations
    call_expectations = _pytest_call_expectations_from_issue(issue)
    if not call_expectations:
        inferred_call_expectation = _pytest_inferred_call_expectation_from_content(issue, content)
        if inferred_call_expectation:
            call_expectations = [inferred_call_expectation]
    if not call_expectations:
        inferred_cli_expectation = _cli_runtime_call_expectation_from_content(issue, content)
        if inferred_cli_expectation:
            call_expectations = [inferred_cli_expectation]
    if call_expectations:
        context["call_expectations"] = call_expectations
    repair_obligations = issue.details.get("repair_obligations")
    if isinstance(repair_obligations, list) and repair_obligations:
        context["repair_obligations"] = repair_obligations[:16]
    return context


def _expected_symbol_obligations_from_issue(issue: MaterialIssue) -> list[dict[str, object]]:
    expected_symbols = _expected_python_exports_for_issue(issue)
    if not expected_symbols:
        return []
    evidence = _target_scoped_material_issue_evidence_text(issue)
    profile = str(issue.details.get("profile") or issue.details.get("validation_profile") or "")
    obligations: list[dict[str, object]] = []
    for symbol in expected_symbols:
        symbol_lines = [
            line.strip()
            for line in evidence.splitlines()
            if symbol and symbol in line
        ][:8]
        obligation_kind = "importable_top_level_export"
        repair_guidance = [
            "make the symbol importable from the target module at top level",
            "preserve existing valid target behavior while adding the missing export",
            "do not satisfy the export with pass-only, Ellipsis, NotImplemented, or return-None placeholder behavior",
        ]
        if profile == "cli" and symbol == "main":
            obligation_kind = "cli_entrypoint_export"
            repair_guidance.extend(
                [
                    "implement the entrypoint as a callable suitable for command-line invocation",
                    "accept an optional argv-style argument when validation evidence calls the entrypoint with arguments",
                    "handle help-style invocation without raising an import or signature error",
                ]
            )
        elif any("cannot import name" in line and symbol in line for line in symbol_lines):
            obligation_kind = "missing_import_export"
        obligations.append(
            {
                "symbol": symbol,
                "kind": obligation_kind,
                "source_profile": profile or None,
                "evidence": symbol_lines,
                "repair_guidance": repair_guidance,
            }
        )
    return obligations


def _pytest_call_expectations_from_issue(issue: MaterialIssue) -> list[dict[str, object]]:
    evidence = _target_scoped_material_issue_evidence_text(issue)
    if not evidence:
        return []
    expectations: list[dict[str, object]] = []
    expected_exception = _pytest_expected_exception_from_evidence(evidence)
    for line_match in re.finditer(r"(?m)^\s*>\s*(.+)$", evidence):
        line = line_match.group(1)
        for match in re.finditer(
            r"\b((?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
            line,
        ):
            callable_expr = match.group(1)
            if callable_expr == "pytest.raises":
                continue
            args_expr = match.group(2).strip()
            positional_arg_count = 0 if not args_expr else max(1, args_expr.count(",") + 1)
            behavior = _pytest_call_behavior_expectation(evidence=evidence, call_line=line, args_expr=args_expr)
            expected_return_value = _pytest_expected_return_value_from_call_line(line, callable_expr)
            expectation: dict[str, object] = {
                "callable": callable_expr,
                "function_name": callable_expr.rsplit(".", 1)[-1],
                "minimum_positional_arguments": positional_arg_count,
                "evidence": line_match.group(0).strip(),
            }
            if behavior:
                expectation.update(behavior)
            if expected_return_value is not _NO_EXPECTED_RETURN:
                expectation["expected_return_value"] = expected_return_value
            if expected_exception:
                expectation["expected_exception"] = expected_exception
            expectations.append(
                expectation
            )
    for match in re.finditer(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\(\)\s+takes\s+(\d+)\s+positional arguments?\s+but\s+(\d+)\s+",
        evidence,
    ):
        expected = int(match.group(3))
        expectations.append(
            {
                "callable": match.group(1),
                "function_name": match.group(1),
                "minimum_positional_arguments": expected,
                "evidence": match.group(0),
            }
        )
    deduped: list[dict[str, object]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in expectations:
        key = (
            str(item.get("function_name") or ""),
            int(item.get("minimum_positional_arguments") or 0),
            json.dumps(item.get("expected_return_value"), sort_keys=True, default=str),
            str(item.get("expected_exception") or ""),
            json.dumps(item.get("expected_stdout_contains"), sort_keys=True, default=str),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:8]


_NO_EXPECTED_RETURN = object()


def _pytest_expected_return_value_from_call_line(line: str, callable_expr: str) -> object:
    escaped = re.escape(callable_expr)
    match = re.search(rf"\b{escaped}\s*\([^)]*\)\s*==\s*([^\n#]+)", line)
    if not match:
        return _NO_EXPECTED_RETURN
    return _safe_pytest_literal(match.group(1))


def _safe_pytest_literal(raw: str) -> object:
    value = raw.strip()
    value = re.split(r"\s+(?:#|and|or)\s+", value, maxsplit=1)[0].strip()
    value = value.rstrip(",); ")
    if not value:
        return _NO_EXPECTED_RETURN
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return _NO_EXPECTED_RETURN
    if isinstance(parsed, (str, int, float, bool)) or parsed is None:
        return parsed
    return _NO_EXPECTED_RETURN


def _pytest_inferred_call_expectation_from_content(
    issue: MaterialIssue,
    content: str,
) -> dict[str, object] | None:
    evidence = _target_scoped_material_issue_evidence_text(issue)
    expected_exception = _pytest_expected_exception_from_evidence(evidence)
    if not expected_exception:
        return None
    function_name = _primary_python_callable_name(content)
    if not function_name:
        return None
    args_expr = "['--help']" if "--help" in evidence else ""
    behavior = _pytest_call_behavior_expectation(evidence=evidence, call_line="", args_expr=args_expr)
    expectation: dict[str, object] = {
        "callable": function_name,
        "function_name": function_name,
        "minimum_positional_arguments": 1 if args_expr else 0,
        "expected_exception": expected_exception,
        "evidence": f"pytest.raises({expected_exception})",
    }
    if behavior:
        expectation.update(behavior)
    return expectation


def _cli_runtime_call_expectation_from_content(
    issue: MaterialIssue,
    content: str,
) -> dict[str, object] | None:
    evidence = _target_scoped_material_issue_evidence_text(issue)
    normalized = evidence.casefold()
    if "argparse.argumenterror" not in normalized or "--help" not in normalized:
        return None
    function_name = (
        _traceback_user_callable_name(evidence)
        or ("main" if "main" in _expected_python_exports_for_issue(issue) else "")
        or _primary_python_callable_name(content)
        or "main"
    )
    behavior = _pytest_call_behavior_expectation(
        evidence=f"{evidence}\nusage:",
        call_line=f"{function_name}(['--help'])",
        args_expr="['--help']",
    )
    expectation: dict[str, object] = {
        "callable": function_name,
        "function_name": function_name,
        "minimum_positional_arguments": 1,
        "evidence": "argparse --help conflict during CLI validation",
    }
    if behavior:
        expectation.update(behavior)
    return expectation


def _traceback_user_callable_name(evidence: str) -> str:
    names = [
        match.group(1)
        for match in re.finditer(r'File "[^"]+", line \d+, in ([A-Za-z_][A-Za-z0-9_]*)', evidence)
        if match.group(1) != "__main__"
    ]
    for name in names:
        if name != "<module>":
            return name
    return ""


def _pytest_expected_exception_from_evidence(evidence: str) -> str:
    for pattern in (
        r"pytest\.raises\(\s*([A-Za-z_][A-Za-z0-9_.]*)",
        r"DID NOT RAISE\s+([A-Za-z_][A-Za-z0-9_.]*)",
    ):
        match = re.search(pattern, evidence)
        if match:
            return match.group(1).rsplit(".", 1)[-1]
    return ""


def _primary_python_callable_name(content: str) -> str:
    try:
        module = ast.parse(content)
    except SyntaxError:
        return ""
    names = [
        node.name
        for node in module.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    if "main" in names:
        return "main"
    return names[0] if len(names) == 1 else ""


def _pytest_call_behavior_expectation(
    *,
    evidence: str,
    call_line: str,
    args_expr: str,
) -> dict[str, object]:
    evidence_lower = evidence.casefold()
    call_lower = call_line.casefold()
    args_lower = args_expr.casefold()
    args_include_help = "--help" in args_lower or _pytest_args_variable_contains_help(evidence, args_expr)
    expected_stdout_contains = _pytest_expected_cli_help_fragments(evidence) or ["usage:"]
    if args_include_help and ("usage:" in evidence_lower or "help" in evidence_lower):
        return {
            "expected_behavior": "cli_help",
            "argv_contains": ["--help"],
            "expected_stdout_contains": expected_stdout_contains,
            "repair_guidance": [
                "implement help-style CLI behavior for argv containing --help",
                "write usage/help text to stdout rather than treating --help as an ordinary value",
                "return normally or raise SystemExit(0) only when the validation evidence allows it",
            ],
        }
    if "systemexit" in evidence_lower and (args_include_help or "help" in evidence_lower):
        return {
            "expected_behavior": "cli_help",
            "expected_stdout_contains": expected_stdout_contains,
            "repair_guidance": [
                "preserve or implement command-line help output shown by validation evidence",
            ],
        }
    if "usage:" in evidence_lower and ("help" in call_lower or "argv" in call_lower):
        return {
            "expected_behavior": "cli_help",
            "expected_stdout_contains": expected_stdout_contains,
            "repair_guidance": [
                "preserve or implement command-line help output shown by validation evidence",
            ],
        }
    return {}


def _pytest_args_variable_contains_help(evidence: str, args_expr: str) -> bool:
    name = args_expr.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return False
    return bool(re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*\[[^\]\n]*['\"]--help['\"]", evidence))


def _pytest_expected_cli_help_fragments(evidence: str) -> list[str]:
    fragments: list[str] = []
    patterns = (
        r"\bexpected\s*=\s*(['\"])(usage:[^'\"]+)\1",
        r"\bassert\s+(['\"])(usage:[^'\"]+)\1\s+in\b",
        r"\bAssertionError:\s*assert\s+(['\"])(usage:[^'\"]+)\1\s+in\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, evidence, flags=re.IGNORECASE):
            fragment = match.group(2).strip()
            if fragment and fragment not in fragments:
                fragments.append(fragment)
    return fragments


def _local_import_cycle_context(raw: dict[str, Any], issue: MaterialIssue) -> dict[str, object]:
    evidence = _material_issue_evidence_text(issue)
    normalized_evidence = evidence.casefold()
    forbidden_imports = _forbidden_local_imports_from_rejections(issue)
    if (
        issue.issue_type != "local_import_cycle"
        and str(issue.details.get("observed_issue_type") or "") != "local_import_cycle"
        and "partially initialized module" not in normalized_evidence
        and "circular import" not in normalized_evidence
        and "local import cycle" not in normalized_evidence
        and not forbidden_imports
    ):
        return {}
    known_paths = {item.path for item in raw["manifest"].files} | {item.path for item in raw.get("generated_files", [])}
    candidate_paths: list[str] = []
    if issue.target_path:
        candidate_paths.append(issue.target_path)
    if issue.target_resolution is not None:
        candidate_paths.extend(issue.target_resolution.related_targets)
        candidate_paths.extend(issue.target_resolution.candidate_targets)
    cycle_paths = issue.details.get("cycle_paths")
    if isinstance(cycle_paths, list):
        candidate_paths.extend(str(path).strip() for path in cycle_paths)
    related_targets = issue.details.get("related_targets")
    if isinstance(related_targets, list):
        candidate_paths.extend(str(path).strip() for path in related_targets)
    candidate_paths.extend(_known_targets_from_evidence(evidence, known_paths=known_paths))
    project_root = str(raw["manifest"].project_root or "")
    involved_targets: list[dict[str, object]] = []
    for path in _dedupe_strings([path for path in candidate_paths if path in known_paths and path.endswith(".py")]):
        involved_targets.append(
            {
                "path": path,
                "module": _python_module_name_for_path(project_root, path),
                "role": "primary" if path == issue.target_path else "related",
            }
        )
    partially_initialized_modules = _dedupe_strings(
        match.group(1).strip()
        for match in re.finditer(r"partially initialized module ['\"]([^'\"]+)['\"]", evidence)
        if match.group(1).strip()
    )
    partially_initialized_modules = _dedupe_strings(
        [
            *partially_initialized_modules,
            *[module.split(".", 1)[0] for module in forbidden_imports if module],
        ]
    )
    cycle_modules = issue.details.get("cycle_modules")
    if isinstance(cycle_modules, list):
        partially_initialized_modules = _dedupe_strings(
            [*partially_initialized_modules, *[str(module).strip() for module in cycle_modules if str(module).strip()]]
        )
    if not involved_targets and not partially_initialized_modules:
        return {}
    return {
        "issue_type": "local_import_cycle",
        "partially_initialized_modules": partially_initialized_modules,
        "involved_targets": involved_targets,
        "repair_guidance": [
            "break local import cycles at module import time",
            "avoid child modules importing symbols from a package root that imports the child module",
            "place shared constants or helpers in the child module itself or in a separate planned local module",
            "when a package root re-exports child symbols, keep the child independent from the package root",
        ],
    }


def _forbidden_local_imports_from_rejections(issue: MaterialIssue) -> list[str]:
    imports: list[str] = []
    for rejection in issue.patch_rejections:
        raw_imports = rejection.diagnostics.get("forbidden_imports")
        if isinstance(raw_imports, str):
            imports.append(raw_imports)
        elif isinstance(raw_imports, list):
            imports.extend(str(item).strip() for item in raw_imports)
    return _dedupe_strings([module for module in imports if module])


def _repair_target_bundle(
    raw: dict[str, Any],
    issue: MaterialIssue,
    *,
    primary_file: GeneratedMaterialFile,
    primary_sha256: str,
) -> list[dict[str, object]]:
    if not issue.target_path:
        return []
    candidate_paths = [issue.target_path]
    if issue.target_resolution is not None:
        candidate_paths.extend(issue.target_resolution.related_targets)
        candidate_paths.extend(issue.target_resolution.candidate_targets)
    bundle: list[dict[str, object]] = []
    for path in _dedupe_strings(candidate_paths):
        generated = primary_file if path == primary_file.path else _generated_file(raw, path)
        if generated is None:
            continue
        expected_hash = primary_sha256 if path == issue.target_path else (_manifest_hash(raw, path) or generated.sha256)
        content = generated.content
        content_limit = 50000
        content_truncated = len(content) > content_limit
        if content_truncated:
            content = content[:content_limit]
        bundle.append(
            {
                "path": path,
                "role": "primary" if path == issue.target_path else "related",
                "kind": _target_kind_for_path(raw, path),
                "expected_current_sha256": expected_hash,
                "content": content,
                "content_truncated": content_truncated,
            }
        )
    return bundle


def _evidence_line_numbers(issue: MaterialIssue) -> list[int]:
    candidates: list[int] = []
    for key in ("line", "lineno", "line_number"):
        value = issue.details.get(key)
        if isinstance(value, int) and value > 0:
            candidates.append(value)
    evidence = _material_issue_evidence_text(issue)
    for match in re.finditer(r"\bline\s+(\d+)\b", evidence, re.IGNORECASE):
        candidates.append(int(match.group(1)))
    if issue.target_path:
        target_name = issue.target_path.replace("\\", "/").rsplit("/", 1)[-1]
        escaped = re.escape(target_name)
        for match in re.finditer(rf"(?:^|\n)\s*(?:[^\s:\n]*/)?{escaped}:(\d+):", evidence):
            candidates.append(int(match.group(1)))
    for match in re.finditer(r"\bline\s+(\d+)\b", _issue_details_text(issue), re.IGNORECASE):
        candidates.append(int(match.group(1)))
    return _dedupe_ints(candidates)


def _issue_details_text(issue: MaterialIssue) -> str:
    parts = [issue.issue_type, issue.target_path or ""]
    for value in issue.details.values():
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def _patch_blueprints(request: MaterialSessionRequest) -> list[dict[str, object]]:
    raw = request.material_builder_context.get("patch_blueprints")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _patch_set_blueprints(request: MaterialSessionRequest) -> list[dict[str, object]]:
    raw = request.material_builder_context.get("patch_set_blueprints")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _replacement_blueprints(request: MaterialSessionRequest) -> list[dict[str, object]]:
    raw = request.material_builder_context.get("replacement_blueprints")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _regeneration_blueprints(request: MaterialSessionRequest) -> list[dict[str, object]]:
    raw = request.material_builder_context.get("regeneration_blueprints")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _normalize_repair_proposal(proposal: object) -> MaterialRepairProposal:
    if isinstance(proposal, MaterialRepairProposal):
        return proposal
    if isinstance(proposal, MaterialPatchProposal):
        return MaterialRepairProposal(patch=proposal)
    if isinstance(proposal, MaterialPatchSetProposal):
        return MaterialRepairProposal(patch_set=proposal)
    if isinstance(proposal, MaterialReplacementProposal):
        return MaterialRepairProposal(replacement=proposal)
    if isinstance(proposal, MaterialRegenerateFromContractProposal):
        return MaterialRepairProposal(regeneration=proposal)
    return MaterialRepairProposal()


def _patch_set_contract_mismatch(
    raw: dict[str, Any],
    issue: MaterialIssue,
    patch_set: MaterialPatchSetProposal,
) -> dict[str, Any]:
    if patch_set.issue_id != issue.issue_id:
        return {
            "expected_issue_id": issue.issue_id,
            "patch_set_issue_id": patch_set.issue_id,
        }
    if not patch_set.requirement_refs or not patch_set.contract_refs:
        return {
            "missing_requirement_refs": not bool(patch_set.requirement_refs),
            "missing_contract_refs": not bool(patch_set.contract_refs),
        }
    target_paths = [patch.target_path for patch in patch_set.patches]
    if len(target_paths) != len(set(target_paths)):
        return {"duplicate_target_paths": sorted({path for path in target_paths if target_paths.count(path) > 1})}
    allowed_targets = {issue.target_path} if issue.target_path else set()
    if issue.target_resolution is not None:
        allowed_targets.update(issue.target_resolution.related_targets)
        allowed_targets.update(issue.target_resolution.candidate_targets)
    unexpected = sorted(set(target_paths) - allowed_targets)
    if unexpected:
        return {"unexpected_target_paths": unexpected, "allowed_target_paths": sorted(allowed_targets)}
    if issue.target_path and issue.target_path not in target_paths:
        return {"primary_target_missing": issue.target_path, "target_paths": target_paths}
    hash_mismatches: list[dict[str, object]] = []
    for patch in patch_set.patches:
        diff_target_mismatch = _patch_diff_target_mismatch(patch.unified_diff, patch.target_path)
        if diff_target_mismatch:
            return diff_target_mismatch
        expected_hash = _manifest_hash(raw, patch.target_path)
        if expected_hash is None:
            hash_mismatches.append(
                {
                    "target_path": patch.target_path,
                    "reason": "target_not_in_manifest",
                    "patch_expected_current_sha256": patch.expected_current_sha256,
                }
            )
            continue
        if patch.expected_current_sha256 != expected_hash:
            hash_mismatches.append(
                {
                    "target_path": patch.target_path,
                    "manifest_sha256": expected_hash,
                    "patch_expected_current_sha256": patch.expected_current_sha256,
                }
            )
    if hash_mismatches:
        return {"hash_mismatches": hash_mismatches}
    return {}


def _patch_diff_target_mismatch(unified_diff: str, target_path: str) -> dict[str, Any]:
    expected = _normalize_diff_header_path(target_path)
    headers: list[str] = []
    for line in unified_diff.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            raw_path = line[4:].strip().split("\t", 1)[0].strip()
            if raw_path == "/dev/null":
                continue
            headers.append(_normalize_diff_header_path(raw_path))
    mismatched = [header for header in headers if header != expected]
    if mismatched:
        return {
            "message": "patch unified diff headers must match the governed repair target",
            "target_path": target_path,
            "diff_header_paths": headers,
            "unexpected_diff_header_paths": sorted(set(mismatched)),
        }
    return {}


def _normalize_diff_header_path(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/").lstrip("./")
    for prefix in ("a/", "b/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized


def _dedupe_strings(values: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _bounded_diagnostic_text(value: Any, *, limit: int = 2048) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    suffix = "... [truncated]"
    return f"{text[: max(0, limit - len(suffix))]}{suffix}"


def _dedupe_ints(values: list[int]) -> list[int]:
    seen = set()
    result: list[int] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _builder_model_route(
    material_builder: MaterialBuilderClient,
    *,
    session_id: str,
    task_id: str,
    phase: str,
) -> dict[str, object]:
    route = getattr(material_builder, "model_route", None)
    if not callable(route):
        return {}
    value = route(session_id=session_id, task_id=task_id, phase=phase)
    return value if isinstance(value, dict) else {}


def _remote_source_requires_acquisition(raw: dict[str, Any]) -> bool:
    if raw.get("source_evidence_context"):
        return False
    request = raw.get("request")
    if not isinstance(request, MaterialSessionRequest):
        return False
    return bool(_material_remote_source(request))


def _material_remote_source(request: MaterialSessionRequest) -> dict[str, object]:
    raw = request.material_builder_context.get("material_source")
    if not isinstance(raw, dict):
        return {}
    if str(raw.get("kind") or "") != "git_remote":
        return {}
    url = str(raw.get("url") or "").strip()
    if not url:
        return {}
    return dict(raw)


def _remote_source_destination(source: dict[str, object]) -> str:
    repo_name = str(source.get("repo_name") or "").strip()
    if not repo_name:
        url = str(source.get("url") or "").rstrip("/")
        repo_name = url.rsplit(":", 1)[-1] if url.startswith("git@") and ":" in url else url.rsplit("/", 1)[-1]
        repo_name = repo_name.removesuffix(".git")
    return f".remote_sources/{_safe_path_segment(repo_name or 'repository')}"


def _request_with_source_evidence(
    request: MaterialSessionRequest,
    evidence_context: dict[str, object],
) -> MaterialSessionRequest:
    builder_context = dict(request.material_builder_context)
    builder_context["evidence_context"] = evidence_context
    return request.model_copy(update={"material_builder_context": builder_context})


def _builder_request_context(
    request: MaterialSessionRequest,
) -> tuple[dict[str, object], dict[str, object], str | None, str]:
    builder_constraints = request.constraints.model_dump(mode="json")
    builder_constraints.update(request.material_builder_context)
    language_context_payload = request.language_context.model_dump(mode="json")
    if isinstance(request.material_builder_context.get("language_context"), dict):
        language_context_payload.update(request.material_builder_context["language_context"])
    original_query = (
        str(request.material_builder_context.get("original_query"))
        if request.material_builder_context.get("original_query") is not None
        else None
    )
    original_language = request.language_context.source_variant or request.language_context.original_language
    return builder_constraints, language_context_payload, original_query, original_language


def _coverage_issue_payload(issue: PlanCoverageIssueProposal) -> dict[str, object]:
    return {
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "message": issue.message,
        "details": issue.details,
        "acceptance": issue.acceptance,
    }


def _single_file_plan(plan: MaterialPlanProposal, planned_file: PlannedMaterialFile) -> MaterialPlanProposal:
    selected_paths = {planned_file.path}
    included_artifact_ids = {
        item.artifact_id
        for item in plan.artifact_expectations
        if set(item.file_refs).issubset(selected_paths)
    }
    included_validation_profiles = set(plan.required_validation_profiles) | set(plan.optional_validation_profiles)
    return MaterialPlanProposal(
        project_root=plan.project_root,
        files=[planned_file],
        requirements=list(plan.requirements),
        intended_interfaces=[
            item
            for item in plan.intended_interfaces
            if set(item.file_refs).issubset(selected_paths)
        ],
        required_validation_profiles=list(plan.required_validation_profiles),
        optional_validation_profiles=list(plan.optional_validation_profiles),
        validation_commands=dict(plan.validation_commands),
        artifact_expectations=[
            item
            for item in plan.artifact_expectations
            if set(item.file_refs).issubset(selected_paths)
        ],
        completion_criteria=[
            item
            for item in plan.completion_criteria
            if set(item.artifact_refs).issubset(included_artifact_ids)
            and set(item.validation_refs).issubset(included_validation_profiles)
        ],
        dependency_strategy=plan.dependency_strategy,
        architecture_notes=list(plan.architecture_notes),
        variation_reason=plan.variation_reason,
        model_route=dict(plan.model_route),
    )


def _artifact_publish_target(
    request: MaterialSessionRequest,
    *,
    session_id: str,
    artifact_path: str,
) -> dict[str, object]:
    filename = _safe_path_segment(str(artifact_path).rsplit("/", 1)[-1] or f"{session_id}.tar.gz")
    if "." not in filename:
        filename = f"{filename}.tar.gz"
    task_segment = _safe_path_segment(request.task_id)
    root = str(request.constraints.publish_destination_root or "").strip().rstrip("/")
    use_direct_root = bool(request.constraints.publish_direct_to_destination_root)
    materialize_root = root if use_direct_root else f"{root}/{task_segment}" if root else ""
    target: dict[str, object] = {
        "store": request.constraints.publish_store,
        "zone": request.constraints.publish_zone,
        "logical_name": filename,
        "content_type": "application/gzip",
    }
    if materialize_root:
        target["materialize_destination_path"] = f"{materialize_root}/{filename}"
        target["extract_archive"] = True
        target["extract_destination_path"] = materialize_root
        if use_direct_root:
            target["overwrite_materialized"] = True
    return target


def _safe_path_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value.strip()).strip(".-_:")
    return cleaned[:128] or "artifact"


__all__ = [
    "ArtifactEvidence",
    "CommandRunEvidence",
    "MaterialManifest",
    "MaterialExecutionConstraints",
    "MaterialIssue",
    "MaterialSessionNotFound",
    "MaterialSessionStore",
]
