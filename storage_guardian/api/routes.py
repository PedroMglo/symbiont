"""Internal HTTP API."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage_guardian.contracts import (
    StorageDirectoryCreate,
    StorageMaterializeRequest,
    StorageObjectCopyMoveRequest,
    StorageObjectCreate,
    StorageObjectHardPurgeRequest,
    StorageObjectRenameRequest,
    StorageObjectTextRead,
    StorageQueryRequest,
    StorageUploadCommit,
    StorageUploadSessionCreate,
)

from storage_guardian.query_execution import execute_storage_query
from storage_guardian.relocation import RelocationError
from storage_guardian.restore_execution import RestoreExecutionError, execute_restore_test
from storage_guardian.service import StorageGuardianService
from storage_guardian.scheduler import Scheduler
from storage_guardian.storage_control import StorageControlError


class RestoreRequest(BaseModel):
    manifest_path: str
    restore_name: str | None = None


class RestoreTestRequest(BaseModel):
    volume: str
    requested_by: str = "storage_guardian.api"


class ArchiveReadRequest(BaseModel):
    manifest_path: str
    relative_path: str
    max_bytes: int | None = None


class ArchivePathRequest(BaseModel):
    paths: list[str]
    tier: str = "cold"
    requested_by: str = "orcai"
    placement_mode: str = "configured"
    replace_sources: bool = False


class ArchiveRecoveryInspectionRequest(BaseModel):
    query: str = ""
    workspace_path: str | None = None
    mode: str = "recovery_plan"
    budget_tokens: int = 2000
    max_member_bytes: int = 64 * 1024 * 1024
    metadata: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class RelocatePathRequest(BaseModel):
    paths: list[str]
    requested_by: str = "system"
    target_root: str | None = None
    replace_with: str = "symlink"
    dry_run: bool = False


class StoragePromoteRequest(BaseModel):
    agent: str
    target_zone: str


class StorageDeleteRequest(BaseModel):
    agent: str
    reason: str | None = None


def create_app(service: StorageGuardianService | None = None) -> FastAPI:
    service = service or StorageGuardianService()
    app = FastAPI(title="storage_guardian", version="0.1.0")

    @app.exception_handler(StorageControlError)
    async def storage_control_error(request: Request, exc: StorageControlError) -> JSONResponse:
        await _record_storage_rejection(request, exc)
        return JSONResponse(status_code=exc.status_code, content={"allowed": False, "reason": exc.reason, "detail": str(exc)})

    @app.exception_handler(RelocationError)
    async def relocation_error(_: Request, exc: RelocationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"allowed": False, "reason": "relocation_rejected", "detail": str(exc)})

    @app.middleware("http")
    async def record_request_latency(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return await call_next(request)
        finally:
            service.record_http_request(latency_seconds=time.perf_counter() - started)

    @app.on_event("startup")
    def start_scheduler() -> None:
        _require_configured_internal_token()
        try:
            service.sync_pending()
        except Exception:
            pass
        try:
            service.cleanup_storage_control()
        except Exception:
            pass
        scheduler_cfg = service.config.root.get("scheduler", {})
        if not scheduler_cfg.get("enabled", True):
            return
        thread = threading.Thread(target=Scheduler(service).run_forever, name="storage-guardian-scheduler", daemon=True)
        thread.start()

    def require_token(x_internal_token: Annotated[str | None, Header()] = None) -> None:
        api_cfg = service.config.root.get("api", {})
        if not api_cfg.get("require_internal_token", True):
            return
        token = _configured_internal_token()
        if not token:
            raise HTTPException(status_code=503, detail="internal token is not configured")
        if not x_internal_token or not secrets.compare_digest(x_internal_token, token):
            raise HTTPException(status_code=401, detail="invalid internal token")

    def _configured_internal_token() -> str:
        api_cfg = service.config.root.get("api", {})
        token_env_var = str(api_cfg.get("token_env_var", "STORAGE_GUARDIAN_INTERNAL_TOKEN"))
        token = os.getenv(token_env_var, "").strip()
        token_file = os.getenv(f"{token_env_var}_FILE", "").strip()
        if not token and token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
        return token

    def _require_configured_internal_token() -> None:
        api_cfg = service.config.root.get("api", {})
        if api_cfg.get("require_internal_token", True) and not _configured_internal_token():
            raise RuntimeError("storage_guardian internal token is required but not configured")

    async def _record_storage_rejection(request: Request, exc: StorageControlError) -> None:
        if not request.url.path.startswith("/internal/storage"):
            return
        payload = await _safe_json_payload(request)
        agent = str(payload.get("agent") or (payload.get("authority") or {}).get("actor") or "unknown")
        metadata = {
            "method": request.method,
            "path": request.url.path,
            "status_code": exc.status_code,
            "payload": _audit_payload(payload),
        }
        try:
            service.record_storage_control_rejection(
                reason=exc.reason,
                action=_storage_action(request),
                agent=agent,
                object_id=_storage_object_id(request),
                metadata=metadata,
            )
        except Exception:
            pass

    async def _safe_json_payload(request: Request) -> dict[str, object]:
        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type:
            return {}
        try:
            raw = await request.body()
            if not raw:
                return {}
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _audit_payload(payload: dict[str, object]) -> dict[str, object]:
        hidden = {"content_base64"}
        return {key: value for key, value in payload.items() if key not in hidden}

    def _storage_action(request: Request) -> str:
        path = request.url.path
        if path == "/internal/storage/objects":
            return "create"
        if path == "/internal/storage/directories":
            return "create_directory"
        if path == "/internal/storage/uploads":
            return "upload"
        if path.endswith("/commit"):
            return "commit"
        if "/internal/storage/copy/" in path:
            return "copy_object"
        if "/internal/storage/move/" in path:
            return "move_object"
        if "/internal/storage/rename/" in path:
            return "rename_object"
        if path == "/internal/storage/materialize":
            return "materialize"
        if "/internal/storage/promote/" in path:
            return "promote"
        if "/internal/storage/delete/" in path:
            return "delete"
        if request.method == "PUT" and "/internal/storage/uploads/" in path:
            return "upload"
        return "storage_control"

    def _storage_object_id(request: Request) -> str | None:
        path = request.url.path.rstrip("/")
        if any(item in path for item in ("/internal/storage/promote/", "/internal/storage/delete/", "/internal/storage/copy/", "/internal/storage/move/", "/internal/storage/rename/", "/internal/storage/hard-purge/")):
            return path.rsplit("/", 1)[-1]
        return None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    def status(_: None = Depends(require_token)) -> dict[str, object]:
        return service.status()

    @app.get("/stores")
    def stores(_: None = Depends(require_token)) -> list[dict[str, object]]:
        return [store.__dict__ | {"path": str(store.path)} for store in service.config.stores]

    @app.get("/storage/schema")
    def storage_schema(_: None = Depends(require_token)) -> dict[str, object]:
        return service.storage_schema()

    @app.get("/archives")
    def archives(_: None = Depends(require_token)) -> list[dict[str, object]]:
        return service.archives()

    @app.get("/storage/policies")
    def storage_policies(_: None = Depends(require_token)) -> dict[str, object]:
        return service.storage_policies()

    @app.get("/storage/objects")
    def storage_objects(
        agent: str | None = None,
        zone: str | None = None,
        status: str | None = None,
        _: None = Depends(require_token),
    ) -> list[dict[str, object]]:
        return service.storage_objects(agent=agent, zone=zone, status=status)

    @app.get("/storage/directories")
    def storage_directories(
        service_name: str | None = None,
        store: str | None = None,
        status: str | None = None,
        _: None = Depends(require_token),
    ) -> list[dict[str, object]]:
        return service.storage_directories(service=service_name, store=store, status=status)

    @app.get("/storage/operations")
    def storage_operations(
        operation_type: str | None = None,
        actor: str | None = None,
        status: str | None = None,
        _: None = Depends(require_token),
    ) -> list[dict[str, object]]:
        return service.storage_operations(operation_type=operation_type, actor=actor, status=status)

    @app.get("/storage/objects/{object_id}")
    def storage_object(object_id: str, _: None = Depends(require_token)) -> dict[str, object]:
        item = service.storage_object(object_id)
        if item is None:
            raise HTTPException(status_code=404, detail="storage object not found")
        return item

    @app.post("/internal/storage/objects/{object_id}/read-text")
    def read_storage_object_text(
        object_id: str,
        req: StorageObjectTextRead,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return service.read_storage_object_text(object_id, req.model_dump(exclude_none=True))

    @app.get("/archives/{archive_id}")
    def archive(archive_id: str, _: None = Depends(require_token)) -> dict[str, object]:
        matches = [item for item in service.archives() if item.get("archive_id") == archive_id]
        if not matches:
            raise HTTPException(status_code=404, detail="archive not found")
        return matches[0]

    @app.get("/archives/{archive_id}/summary")
    def archive_summary(archive_id: str, _: None = Depends(require_token)) -> str:
        matches = [item for item in service.archives() if item.get("archive_id") == archive_id]
        if not matches:
            raise HTTPException(status_code=404, detail="archive not found")
        summary_path = Path(str(matches[0]["summary_path"]))
        if not summary_path.exists():
            raise HTTPException(status_code=404, detail="summary not found")
        return summary_path.read_text(encoding="utf-8")

    @app.post("/internal/scan")
    def scan(_: None = Depends(require_token)) -> dict[str, int]:
        return {"files_seen": len(service.scan())}

    @app.post("/internal/plan")
    def plan(_: None = Depends(require_token)) -> dict[str, int]:
        cycle_plan = service.plan()
        return {"files_seen": len(cycle_plan.files), "archives_planned": len(cycle_plan.archive_plans), "skipped": len(cycle_plan.skipped)}

    @app.post("/internal/run-cycle")
    def run_cycle(_: None = Depends(require_token)) -> dict[str, object]:
        return service.run_cycle()

    @app.post("/internal/sync-pending")
    def sync_pending(_: None = Depends(require_token)) -> dict[str, object]:
        return service.sync_pending()

    @app.post("/internal/archive-path")
    def archive_path(req: ArchivePathRequest, _: None = Depends(require_token)) -> dict[str, object]:
        return service.archive_paths(
            req.paths,
            tier=req.tier,
            requested_by=req.requested_by,
            placement_mode=req.placement_mode,
            replace_sources=req.replace_sources,
        )

    @app.post("/internal/archive-recovery/inspect")
    def archive_recovery_inspect(
        req: ArchiveRecoveryInspectionRequest,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        from storage_guardian.recovery_inspection import (
            build_archive_recovery_report,
            format_archive_recovery_report,
            resolve_recovery_workspace,
        )

        workspace_input = req.workspace_path or ""
        if not workspace_input:
            for key in ("client_cwd", "workspace_path", "workspace", "cwd"):
                value = req.metadata.get(key)
                if isinstance(value, str) and value.strip():
                    workspace_input = value
                    break
        workspace = resolve_recovery_workspace(
            workspace_input,
            host_home_prefix=os.environ.get("HOST_HOME_PREFIX"),
        )
        if workspace is None:
            return {
                "content": "",
                "success": False,
                "token_estimate": 0,
                "metadata": {"operation": "archive_recovery", "mode": req.mode},
                "error": "archive_recovery_workspace_not_found",
            }
        report = build_archive_recovery_report(
            workspace.path,
            max_member_bytes=req.max_member_bytes,
        )
        content = format_archive_recovery_report(report)
        return {
            "content": content,
            "success": True,
            "token_estimate": max(1, len(content) // 4),
            "metadata": {
                "operation": "archive_recovery",
                "mode": req.mode,
                "workspace": str(workspace.path),
                "mapped_from": workspace.mapped_from,
                "storage_mutation_performed": False,
                "analysis_mode": "storage_guardian_read_only_archive_recovery",
                "policy": report.get("policy", {}),
                "summary": report.get("summary", {}),
            },
            "error": None,
        }

    @app.post("/internal/storage/query")
    def storage_query(req: StorageQueryRequest, _: None = Depends(require_token)) -> dict[str, object]:
        return execute_storage_query(
            service,
            query=req.query,
            budget_tokens=req.budget_tokens,
            metadata=req.metadata,
            workspace_path=req.workspace_path,
        )

    @app.post("/internal/storage/restore-tests")
    def storage_restore_test(req: RestoreTestRequest, _: None = Depends(require_token)) -> dict[str, object]:
        try:
            return execute_restore_test(
                req.volume,
                root=service.config.project_root,
                requested_by=req.requested_by,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown volume") from exc
        except RestoreExecutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/internal/relocate-path")
    def relocate_path(req: RelocatePathRequest, _: None = Depends(require_token)) -> dict[str, object]:
        return service.relocate_paths(
            req.paths,
            requested_by=req.requested_by,
            target_root=req.target_root,
            replace_with=req.replace_with,
            dry_run=req.dry_run,
        )

    @app.post("/internal/storage/objects")
    def create_storage_object(
        req: StorageObjectCreate,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return service.create_storage_object(req.model_dump(exclude_none=True), idempotency_key=idempotency_key)

    @app.post("/internal/storage/directories")
    def create_storage_directory(
        req: StorageDirectoryCreate,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return service.create_storage_directory(req.model_dump(exclude_none=True), idempotency_key=idempotency_key)

    @app.post("/internal/storage/uploads")
    def create_upload_session(req: StorageUploadSessionCreate, _: None = Depends(require_token)) -> dict[str, object]:
        return service.create_upload_session(req.model_dump(exclude_none=True))

    @app.put("/internal/storage/uploads/{upload_id}")
    async def upload_bytes(upload_id: str, request: Request, _: None = Depends(require_token)) -> dict[str, object]:
        return service.append_upload_bytes(upload_id, await request.body())

    @app.post("/internal/storage/uploads/{upload_id}/commit")
    def commit_upload(
        upload_id: str,
        req: StorageUploadCommit,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return service.commit_upload(upload_id, req.model_dump(exclude_none=True), idempotency_key=idempotency_key)

    @app.post("/internal/storage/materialize")
    def materialize_storage_artifact(
        req: StorageMaterializeRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return service.materialize_storage_artifact(req.model_dump(exclude_none=True), idempotency_key=idempotency_key)

    @app.post("/internal/storage/copy/{object_id}")
    def copy_storage_object(
        object_id: str,
        req: StorageObjectCopyMoveRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return service.copy_storage_object(object_id, req.model_dump(exclude_none=True), idempotency_key=idempotency_key)

    @app.post("/internal/storage/move/{object_id}")
    def move_storage_object(
        object_id: str,
        req: StorageObjectCopyMoveRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return service.move_storage_object(object_id, req.model_dump(exclude_none=True), idempotency_key=idempotency_key)

    @app.post("/internal/storage/rename/{object_id}")
    def rename_storage_object(
        object_id: str,
        req: StorageObjectRenameRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return service.rename_storage_object(object_id, req.model_dump(exclude_none=True), idempotency_key=idempotency_key)

    @app.post("/internal/storage/hard-purge/{object_id}")
    def hard_purge_storage_object(
        object_id: str,
        req: StorageObjectHardPurgeRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return service.hard_purge_storage_object(object_id, req.model_dump(exclude_none=True), idempotency_key=idempotency_key)

    @app.post("/internal/storage/promote/{object_id}")
    def promote_storage_object(object_id: str, req: StoragePromoteRequest, _: None = Depends(require_token)) -> dict[str, object]:
        return service.promote_storage_object(object_id, req.model_dump(exclude_none=True))

    @app.post("/internal/storage/delete/{object_id}")
    def soft_delete_storage_object(object_id: str, req: StorageDeleteRequest, _: None = Depends(require_token)) -> dict[str, object]:
        return service.soft_delete_storage_object(object_id, req.model_dump(exclude_none=True))

    @app.post("/internal/restore/{archive_id}")
    def restore_by_archive_id(archive_id: str, _: None = Depends(require_token)) -> dict[str, object]:
        matches = [item for item in service.archives() if item.get("archive_id") == archive_id]
        if not matches:
            raise HTTPException(status_code=404, detail="archive not found")
        return service.restore(str(matches[0]["manifest_path"]))

    @app.post("/internal/restore")
    def restore(req: RestoreRequest, _: None = Depends(require_token)) -> dict[str, object]:
        return service.restore(req.manifest_path, req.restore_name)

    @app.get("/archives/{archive_id}/members")
    def archive_members(archive_id: str, _: None = Depends(require_token)) -> list[dict[str, object]]:
        matches = [item for item in service.archives() if item.get("archive_id") == archive_id]
        if not matches:
            raise HTTPException(status_code=404, detail="archive not found")
        return service.archive_members(str(matches[0]["manifest_path"]))

    @app.post("/internal/read-archive-text")
    def read_archive_text(req: ArchiveReadRequest, _: None = Depends(require_token)) -> dict[str, object]:
        return service.read_archive_text(req.manifest_path, req.relative_path, req.max_bytes)

    @app.get("/metrics")
    def metrics(_: None = Depends(require_token)) -> dict[str, object]:
        return service.metrics()

    @app.get("/effective-config")
    def effective_config(_: None = Depends(require_token)) -> dict[str, object]:
        return service.effective_config()

    return app
