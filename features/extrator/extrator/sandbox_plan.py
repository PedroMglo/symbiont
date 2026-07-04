"""Workspace execution planning for extrator jobs."""

from __future__ import annotations

from extrator.query_intents import ExtratorPathRequest
from extrator.types import (
    JobKind,
    SandboxInputPlan,
    SandboxPreparationPlan,
    SandboxPublishPlan,
    SandboxSessionPlan,
    SandboxSourceOption,
)


def build_extrator_sandbox_plan(selected: ExtratorPathRequest, *, job_kind: JobKind) -> SandboxPreparationPlan:
    """Build a non-executing sandbox preparation plan for document jobs."""

    is_conversion = job_kind == JobKind.CONVERSION
    return SandboxPreparationPlan(
        kind="workspace_execution_plan",
        owner="extrator",
        uses="workspace_execution",
        capability="workspace_sandbox_preparation_plan",
        requires_orchestrator_execution=True,
        recommended=bool(is_conversion or selected.force),
        source_options=[
            SandboxSourceOption(
                kind="workspace",
                path=selected.original_path,
                copy_required=True,
            ),
            SandboxSourceOption(
                kind="upload",
                requires="caller_attached_file_bytes",
                copy_required=True,
            ),
            SandboxSourceOption(
                kind="storage_object",
                requires="storage_guardian_object_ref",
                copy_required=True,
            ),
        ],
        session=SandboxSessionPlan(
            execution_profile="convert" if is_conversion else "inspect",
            network="disabled",
            real_host_writes=False,
        ),
        inputs=[
            SandboxInputPlan(
                original_path=selected.original_path,
                container_path=selected.input_path,
                recursive=selected.recursive,
                force=selected.force,
                conversion_format=selected.conversion_format,
            )
        ],
        checks=[
            "validate copied input visibility before extraction or conversion",
            "run conversion or parser probes only inside the disposable copy",
            "stage generated artifacts in workspace_execution when a sandbox copy is used",
            "materialize final user-machine artifacts only through storage_guardian",
        ],
        publish=SandboxPublishPlan(
            required=is_conversion,
            allowed_via="storage_guardian.materialize",
        ),
    )
