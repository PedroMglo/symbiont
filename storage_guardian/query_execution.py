"""Execution layer for storage_guardian natural-language storage queries."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from storage_guardian.query_intents import (
    is_archive_recovery_request,
    needs_storage_context,
    parse_storage_request,
)
from storage_guardian.service import StorageGuardianService


def execute_storage_query(
    service: StorageGuardianService,
    *,
    query: str,
    budget_tokens: int = 2000,
    metadata: dict[str, Any] | None = None,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    """Execute or inspect a storage request and return a feature-style response."""

    start = time.time()
    metadata = metadata or {}
    request = parse_storage_request(query)
    if (
        is_archive_recovery_request(query)
        and _metadata_workspace(metadata, workspace_path)
        and (request is None or request.operation == "archive")
    ):
        return _archive_recovery_response(
            query=query,
            budget_tokens=budget_tokens,
            metadata=metadata,
            workspace_path=workspace_path,
            latency_ms=_latency_ms(start),
        )
    if request is None:
        if is_archive_recovery_request(query):
            return _archive_recovery_response(
                query=query,
                budget_tokens=budget_tokens,
                metadata=metadata,
                workspace_path=workspace_path,
                latency_ms=_latency_ms(start),
            )
        if needs_storage_context(query):
            return _status_response(
                service,
                query=query,
                latency_ms=_latency_ms(start),
            )
        return _response(
            content="",
            success=False,
            latency_ms=_latency_ms(start),
            metadata={"operation": "unknown", "query": query},
            error="No storage operation found in query",
        )

    try:
        if request.operation == "archive":
            data = service.archive_paths(
                list(request.paths),
                tier=request.tier,
                requested_by=str(metadata.get("requested_by") or "@"),
                placement_mode=request.placement_mode,
                replace_sources=request.replace_sources,
            )
            content = format_storage_archive(data)
        elif request.operation == "restore":
            if request.manifest_path:
                data = service.restore(request.manifest_path)
            elif request.archive_id:
                data = _restore_archive_id(service, request.archive_id)
            else:
                return _response(
                    content="",
                    success=False,
                    latency_ms=_latency_ms(start),
                    metadata={"operation": "restore"},
                    error="No manifest path or archive_id found",
                )
            content = format_storage_restore(data)
        elif request.operation == "read":
            manifest_path = request.manifest_path
            if not manifest_path and request.archive_id:
                archive = _archive_by_id(service, request.archive_id)
                manifest_path = str(archive.get("manifest_path") or "")
            if not manifest_path or not request.relative_path:
                data = _storage_archive_search(service, query)
                return _response(
                    content=format_storage_search(data),
                    success=True,
                    latency_ms=_latency_ms(start),
                    metadata={
                        "operation": "search",
                        "reason": "read_missing_manifest_or_member",
                        "storage_response": data,
                    },
                )
            data = service.read_archive_text(manifest_path, request.relative_path, 12000)
            content = format_storage_read(data)
        elif request.operation == "cycle":
            data = service.run_cycle()
            content = format_storage_archive(data)
        else:
            data = _storage_archive_search(service, query)
            content = format_storage_search(data)

        return _response(
            content=content,
            success=True,
            latency_ms=_latency_ms(start),
            metadata={"operation": request.operation, "storage_response": data},
        )
    except Exception as exc:
        return _response(
            content="",
            success=False,
            latency_ms=_latency_ms(start),
            metadata={"operation": request.operation},
            error=str(exc)[:300],
        )


def _archive_recovery_response(
    *,
    query: str,
    budget_tokens: int,
    metadata: dict[str, Any],
    workspace_path: str | None,
    latency_ms: float,
) -> dict[str, Any]:
    from storage_guardian.recovery_inspection import (
        build_archive_recovery_report,
        format_archive_recovery_report,
        resolve_recovery_workspace,
    )

    workspace_input = workspace_path or _metadata_workspace(metadata, None)
    workspace = resolve_recovery_workspace(
        workspace_input or "",
        host_home_prefix=None,
    )
    if workspace is None:
        return _response(
            content="",
            success=False,
            latency_ms=latency_ms,
            metadata={"operation": "archive_recovery", "mode": "recovery_plan"},
            error="archive_recovery_workspace_not_found",
        )
    report = build_archive_recovery_report(workspace.path)
    content = format_archive_recovery_report(report)
    return _response(
        content=content,
        success=True,
        token_estimate=max(1, len(content) // 4, min(budget_tokens, len(content) // 4)),
        latency_ms=latency_ms,
        metadata={
            "operation": "archive_recovery",
            "mode": "recovery_plan",
            "workspace": str(workspace.path),
            "mapped_from": workspace.mapped_from,
            "delegated_service": "storage_guardian",
            "storage_mutation_performed": False,
            "analysis_mode": "storage_guardian_read_only_archive_recovery",
            "policy": report.get("policy", {}),
            "summary": report.get("summary", {}),
        },
    )


def _status_response(service: StorageGuardianService, *, query: str, latency_ms: float) -> dict[str, Any]:
    health = {"ok": True, "status": "ok"}
    status = service.status()
    content = format_storage_status_context(health=health, status=status)
    return _response(
        content=content,
        success=bool(content),
        latency_ms=latency_ms,
        metadata={"operation": "status", "query": query, "health": health, "status": status},
    )


def _response(
    *,
    content: str,
    success: bool,
    latency_ms: float,
    metadata: dict[str, Any],
    token_estimate: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "content": content,
        "source": "storage",
        "token_estimate": token_estimate if token_estimate is not None else max(1, len(content) // 4) if content else 0,
        "success": success,
        "latency_ms": latency_ms,
        "metadata": metadata,
        "error": error,
    }


def _latency_ms(start: float) -> float:
    return (time.time() - start) * 1000


def _metadata_workspace(metadata: dict[str, Any], workspace_path: str | None) -> str:
    if workspace_path:
        return workspace_path
    for key in ("client_cwd", "workspace_path", "workspace", "cwd"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _archive_by_id(service: StorageGuardianService, archive_id: str) -> dict[str, object]:
    matches = [item for item in service.archives() if item.get("archive_id") == archive_id]
    if not matches:
        raise KeyError("archive not found")
    return matches[0]


def _restore_archive_id(service: StorageGuardianService, archive_id: str) -> dict[str, object]:
    archive = _archive_by_id(service, archive_id)
    return service.restore(str(archive["manifest_path"]))


def _storage_archive_search(service: StorageGuardianService, query: str) -> dict[str, Any]:
    archives = service.archives()
    terms = _meaningful_terms(query)
    matches: list[dict[str, Any]] = []
    for archive in archives:
        summary = ""
        summary_path = archive.get("summary_path")
        if summary_path:
            path = Path(str(summary_path))
            if path.exists():
                summary = path.read_text(encoding="utf-8")[:2000]
        haystack = " ".join(str(archive.get(key, "")) for key in archive) + " " + summary
        score = sum(1 for term in terms if term in haystack.lower())
        if score or not terms:
            matches.append({"score": score, "archive": archive, "summary": summary[:2000]})
    matches.sort(key=lambda item: item["score"], reverse=True)
    return {
        "query": query,
        "matches": matches[:10],
        "archives_seen": len(archives),
        "matched_without_decompressing": True,
    }


def format_storage_status_context(*, health: dict[str, Any], status: dict[str, Any]) -> str:
    lines = ["storage_guardian status read-only"]
    health_state = health.get("status") or ("ok" if health.get("ok") else "unknown")
    lines.append(f"health: {health_state}")
    if health.get("mode") or status.get("mode"):
        lines.append(f"mode: {health.get('mode') or status.get('mode')}")
    if health.get("storage_root") or status.get("storage_root"):
        lines.append(f"storage_root: {health.get('storage_root') or status.get('storage_root')}")
    mounts = status.get("mounts") or health.get("mounts") or status.get("critical_mounts")
    if mounts:
        lines.append(f"mounts: {mounts}")
    flags = status.get("prewarnings") or status.get("flags") or health.get("prewarnings")
    if flags:
        lines.append(f"prewarnings: {flags}")
    lines.append(
        "policy: archive/restore/delete/promote remain gated by explicit approval; "
        "this probe did not execute a mutable storage action."
    )
    return "\n".join(lines)


def format_storage_archive(data: dict[str, Any]) -> str:
    lines = [
        "Storage archive request completed",
        f"status: {data.get('status')}",
        f"cycle_id: {data.get('cycle_id')}",
        f"archives_created: {data.get('archives_created')}",
        f"files_seen: {data.get('files_seen')}",
    ]
    for result in data.get("results", []):
        lines.extend(
            [
                f"archive_id: {result.get('archive_id')}",
                f"archive_path: {result.get('archive_path')}",
                f"manifest_path: {result.get('manifest_path')}",
                f"summary_path: {result.get('summary_path')}",
                f"original_size_bytes: {result.get('original_size_bytes')}",
                f"archive_size_bytes: {result.get('archive_size_bytes')}",
                f"verified: {result.get('verified')}",
                f"target: {result.get('storage_target')}",
            ]
        )
    if data.get("skipped"):
        lines.append(f"skipped: {data.get('skipped')}")
    return "\n".join(lines)


def format_storage_restore(data: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Storage restore completed",
            f"restore_id: {data.get('restore_id')}",
            f"archive_id: {data.get('archive_id')}",
            f"restore_root: {data.get('restore_root')}",
            f"files_count: {data.get('files_count')}",
            f"verified: {data.get('verified')}",
        ]
    )


def format_storage_read(data: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Storage archive text read completed",
            f"relative_path: {data.get('relative_path')}",
            f"bytes_read: {data.get('bytes_read')}",
            "text:",
            str(data.get("text", "")),
        ]
    )


def format_storage_search(data: dict[str, Any]) -> str:
    lines = [
        "Storage archive search completed without decompression",
        f"archives_seen: {data.get('archives_seen')}",
        f"matches: {len(data.get('matches', []))}",
    ]
    for match in data.get("matches", [])[:5]:
        archive = match.get("archive", {})
        lines.extend(
            [
                f"archive_id: {archive.get('archive_id')}",
                f"score: {match.get('score')}",
                f"manifest_path: {archive.get('manifest_path')}",
                f"summary_path: {archive.get('summary_path')}",
                f"archive_path: {archive.get('archive_path')}",
                f"summary_excerpt: {str(match.get('summary', ''))[:500]}",
            ]
        )
    return "\n".join(lines)


def _meaningful_terms(query: str) -> set[str]:
    stop = {
        "arquivo",
        "arquivos",
        "archive",
        "archives",
        "comprimir",
        "comprime",
        "consulta",
        "consultar",
        "descrição",
        "descricao",
        "manifest",
        "manifesto",
        "sem",
        "sumario",
        "sumário",
        "the",
        "without",
    }
    return {
        term
        for term in re.findall(r"[\w.-]{3,}", (query or "").lower())
        if term not in stop
    }
