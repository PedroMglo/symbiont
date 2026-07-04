"""Command line entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from storage_guardian.config import load_config
from storage_guardian.relocation import RelocationError
from storage_guardian.service import StorageGuardianService
from storage_guardian.storage_control import StorageControlError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="storage-guardian")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("effective-config")
    sub.add_parser("scan")
    sub.add_parser("plan")
    sub.add_parser("run-cycle")
    sub.add_parser("sync-pending")
    reconcile_structure = sub.add_parser("reconcile-structure")
    reconcile_structure.add_argument("--scope", choices=["external", "local", "all"], default="all")
    reconcile_structure.add_argument("--apply", action="store_true")
    archive_path = sub.add_parser("archive-path")
    archive_path.add_argument("paths", nargs="+")
    archive_path.add_argument("--tier", choices=["warm", "cold"], default="cold")
    archive_path.add_argument("--requested-by", default="orcai")
    archive_path.add_argument("--placement-mode", choices=["configured", "source_directory"], default="configured")
    archive_path.add_argument("--replace-sources", action="store_true")
    relocate_path = sub.add_parser("relocate-path")
    relocate_path.add_argument("paths", nargs="+")
    relocate_path.add_argument("--requested-by", default="system")
    relocate_path.add_argument("--target-root", default=None)
    relocate_path.add_argument("--replace-with", choices=["symlink", "none"], default="symlink")
    relocate_path.add_argument("--dry-run", action="store_true")
    restore = sub.add_parser("restore")
    restore.add_argument("manifest_path")
    restore.add_argument("--restore-name", default=None)
    members = sub.add_parser("archive-members")
    members.add_argument("manifest_path")
    read_text = sub.add_parser("read-archive-text")
    read_text.add_argument("manifest_path")
    read_text.add_argument("relative_path")
    read_text.add_argument("--max-bytes", type=int, default=None)
    sub.add_parser("storage-policies")
    sub.add_parser("storage-schema")
    directories = sub.add_parser("directories")
    directories.add_argument("--service", default=None)
    directories.add_argument("--store", default=None)
    directories.add_argument("--status", default=None)
    operations = sub.add_parser("operations")
    operations.add_argument("--operation-type", default=None)
    operations.add_argument("--actor", default=None)
    operations.add_argument("--status", default=None)
    objects = sub.add_parser("storage-objects")
    objects.add_argument("--agent", default=None)
    objects.add_argument("--zone", default=None)
    objects.add_argument("--status", default=None)
    create_object = sub.add_parser("create-object")
    create_object.add_argument("--agent", required=True)
    create_object.add_argument("--store", default=None)
    create_object.add_argument("--zone", default="ingest")
    create_object.add_argument("--logical-name", required=True)
    create_object.add_argument("--content-base64", required=True)
    create_object.add_argument("--content-type", default="application/octet-stream")
    create_object.add_argument("--sha256", default=None)
    create_object.add_argument("--parent-object-id", default=None)
    create_object.add_argument("--metadata-json", default=None)
    create_object.add_argument("--idempotency-key", required=True)
    create_dir = sub.add_parser("create-dir")
    create_dir.add_argument("--agent", required=True)
    create_dir.add_argument("--store", default=None)
    create_dir.add_argument("--zone", default="ingest")
    create_dir.add_argument("--relative-path", required=True)
    create_dir.add_argument("--metadata-json", default=None)
    create_dir.add_argument("--idempotency-key", required=True)
    create_upload = sub.add_parser("create-upload")
    create_upload.add_argument("--agent", required=True)
    create_upload.add_argument("--store", default=None)
    create_upload.add_argument("--zone", default="ingest")
    create_upload.add_argument("--logical-name", required=True)
    create_upload.add_argument("--expected-size", type=int, required=True)
    create_upload.add_argument("--sha256", required=True)
    create_upload.add_argument("--content-type", default="application/octet-stream")
    create_upload.add_argument("--ttl-seconds", type=int, default=900)
    create_upload.add_argument("--metadata-json", default=None)
    append_upload = sub.add_parser("append-upload")
    append_upload.add_argument("upload_id")
    append_upload.add_argument("file_path")
    commit_upload = sub.add_parser("commit-upload")
    commit_upload.add_argument("upload_id")
    commit_upload.add_argument("--sha256", default=None)
    commit_upload.add_argument("--metadata-json", default=None)
    commit_upload.add_argument("--idempotency-key", required=True)
    promote_object = sub.add_parser("promote-object")
    promote_object.add_argument("object_id")
    promote_object.add_argument("--agent", required=True)
    promote_object.add_argument("--target-zone", required=True)
    copy_object = sub.add_parser("copy-object")
    copy_object.add_argument("object_id")
    copy_object.add_argument("--agent", required=True)
    copy_object.add_argument("--target-store", default=None)
    copy_object.add_argument("--target-zone", default=None)
    copy_object.add_argument("--target-logical-name", default=None)
    copy_object.add_argument("--metadata-json", default=None)
    copy_object.add_argument("--idempotency-key", required=True)
    move_object = sub.add_parser("move-object")
    move_object.add_argument("object_id")
    move_object.add_argument("--agent", required=True)
    move_object.add_argument("--target-store", default=None)
    move_object.add_argument("--target-zone", default=None)
    move_object.add_argument("--target-logical-name", default=None)
    move_object.add_argument("--metadata-json", default=None)
    move_object.add_argument("--idempotency-key", required=True)
    rename_object = sub.add_parser("rename-object")
    rename_object.add_argument("object_id")
    rename_object.add_argument("--agent", required=True)
    rename_object.add_argument("--logical-name", required=True)
    rename_object.add_argument("--metadata-json", default=None)
    rename_object.add_argument("--idempotency-key", required=True)
    delete_object = sub.add_parser("delete-object")
    delete_object.add_argument("object_id")
    delete_object.add_argument("--agent", required=True)
    delete_object.add_argument("--reason", default=None)
    hard_purge = sub.add_parser("hard-purge-object")
    hard_purge.add_argument("object_id")
    hard_purge.add_argument("--agent", required=True)
    hard_purge.add_argument("--reason", default=None)
    hard_purge.add_argument("--confirm", action="store_true")
    hard_purge.add_argument("--idempotency-key", required=True)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    service = StorageGuardianService(config)
    try:
        try:
            if args.command == "status":
                _print(service.status())
            elif args.command == "effective-config":
                _print(service.effective_config())
            elif args.command == "scan":
                _print({"files_seen": len(service.scan())})
            elif args.command == "plan":
                plan = service.plan()
                _print({"cycle_id": plan.cycle_id, "files_seen": len(plan.files), "archives_planned": len(plan.archive_plans), "skipped": len(plan.skipped)})
            elif args.command == "run-cycle":
                _print(service.run_cycle())
            elif args.command == "sync-pending":
                _print(service.sync_pending())
            elif args.command == "reconcile-structure":
                _print(service.reconcile_structure(scope=args.scope, apply=args.apply))
            elif args.command == "archive-path":
                _print(
                    service.archive_paths(
                        args.paths,
                        tier=args.tier,
                        requested_by=args.requested_by,
                        placement_mode=args.placement_mode,
                        replace_sources=args.replace_sources,
                    )
                )
            elif args.command == "relocate-path":
                _print(
                    service.relocate_paths(
                        args.paths,
                        requested_by=args.requested_by,
                        target_root=args.target_root,
                        replace_with=args.replace_with,
                        dry_run=args.dry_run,
                    )
                )
            elif args.command == "restore":
                _print(service.restore(Path(args.manifest_path), args.restore_name))
            elif args.command == "archive-members":
                _print(service.archive_members(Path(args.manifest_path)))
            elif args.command == "read-archive-text":
                _print(service.read_archive_text(Path(args.manifest_path), args.relative_path, args.max_bytes))
            elif args.command == "storage-policies":
                _print(service.storage_policies())
            elif args.command == "storage-schema":
                _print(service.storage_schema())
            elif args.command == "directories":
                _print(service.storage_directories(service=args.service, store=args.store, status=args.status))
            elif args.command == "operations":
                _print(service.storage_operations(operation_type=args.operation_type, actor=args.actor, status=args.status))
            elif args.command == "storage-objects":
                _print(service.storage_objects(agent=args.agent, zone=args.zone, status=args.status))
            elif args.command == "create-object":
                _print(
                    service.create_storage_object(
                        {
                            "agent": args.agent,
                            "store": args.store,
                            "zone": args.zone,
                            "logical_name": args.logical_name,
                            "content_base64": args.content_base64,
                            "content_type": args.content_type,
                            "sha256": args.sha256,
                            "parent_object_id": args.parent_object_id,
                            "metadata": _json_obj(args.metadata_json),
                        },
                        idempotency_key=args.idempotency_key,
                    )
                )
            elif args.command == "create-dir":
                _print(
                    service.create_storage_directory(
                        {
                            "agent": args.agent,
                            "store": args.store,
                            "zone": args.zone,
                            "relative_path": args.relative_path,
                            "metadata": _json_obj(args.metadata_json),
                        },
                        idempotency_key=args.idempotency_key,
                    )
                )
            elif args.command == "create-upload":
                _print(
                    service.create_upload_session(
                        {
                            "agent": args.agent,
                            "store": args.store,
                            "zone": args.zone,
                            "logical_name": args.logical_name,
                            "expected_size": args.expected_size,
                            "sha256": args.sha256,
                            "content_type": args.content_type,
                            "ttl_seconds": args.ttl_seconds,
                            "metadata": _json_obj(args.metadata_json),
                        }
                    )
                )
            elif args.command == "append-upload":
                _print(service.append_upload_bytes(args.upload_id, Path(args.file_path).read_bytes()))
            elif args.command == "commit-upload":
                payload = {"metadata": _json_obj(args.metadata_json)}
                if args.sha256:
                    payload["sha256"] = args.sha256
                _print(service.commit_upload(args.upload_id, payload, idempotency_key=args.idempotency_key))
            elif args.command == "promote-object":
                _print(service.promote_storage_object(args.object_id, {"agent": args.agent, "target_zone": args.target_zone}))
            elif args.command == "copy-object":
                _print(
                    service.copy_storage_object(
                        args.object_id,
                        {
                            "agent": args.agent,
                            "target_store": args.target_store,
                            "target_zone": args.target_zone,
                            "target_logical_name": args.target_logical_name,
                            "metadata": _json_obj(args.metadata_json),
                        },
                        idempotency_key=args.idempotency_key,
                    )
                )
            elif args.command == "move-object":
                _print(
                    service.move_storage_object(
                        args.object_id,
                        {
                            "agent": args.agent,
                            "target_store": args.target_store,
                            "target_zone": args.target_zone,
                            "target_logical_name": args.target_logical_name,
                            "metadata": _json_obj(args.metadata_json),
                        },
                        idempotency_key=args.idempotency_key,
                    )
                )
            elif args.command == "rename-object":
                _print(
                    service.rename_storage_object(
                        args.object_id,
                        {
                            "agent": args.agent,
                            "logical_name": args.logical_name,
                            "metadata": _json_obj(args.metadata_json),
                        },
                        idempotency_key=args.idempotency_key,
                    )
                )
            elif args.command == "delete-object":
                _print(service.soft_delete_storage_object(args.object_id, {"agent": args.agent, "reason": args.reason}))
            elif args.command == "hard-purge-object":
                _print(
                    service.hard_purge_storage_object(
                        args.object_id,
                        {"agent": args.agent, "reason": args.reason, "confirm": args.confirm},
                        idempotency_key=args.idempotency_key,
                    )
                )
        except StorageControlError as exc:
            _print({"allowed": False, "reason": exc.reason, "detail": str(exc)})
            return 2
        except RelocationError as exc:
            _print({"allowed": False, "reason": "relocation_rejected", "detail": str(exc)})
            return 2
    finally:
        service.close()
    return 0


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _json_obj(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("--metadata-json must decode to an object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
