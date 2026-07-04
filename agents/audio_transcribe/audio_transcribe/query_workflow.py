"""Natural-language audio transcription workflow owned by audio_transcribe."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from audio_transcribe.config import get_config
from audio_transcribe.jobs import (
    create_job,
    find_reusable_active_job,
    find_reusable_completed_job,
    get_active_job_count,
    get_job,
)
from audio_transcribe.queue import get_queue
from audio_transcribe.security import validate_input_path
from audio_transcribe.storage_guardian_api import find_published_transcription, read_storage_object_text
from audio_transcribe.errors import JobNotFoundError
from audio_transcribe.types import (
    AudioQueryRequest,
    AudioQueryResponse,
    JobStatus,
    TranscriptionOptions,
)


def _audio_extensions() -> frozenset[str]:
    return frozenset(get_config().security.allowed_input_extensions)


def extract_audio_paths(query: str) -> list[str]:
    """Extract candidate file or directory paths from a user transcription query."""

    paths: list[str] = []

    quoted = re.findall(r'["\']([^"\']+)["\']', query)
    for value in quoted:
        if "/" in value or "\\" in value:
            paths.append(value)

    ext_pattern = "|".join(re.escape(ext) for ext in sorted(_audio_extensions()))
    spaced_paths = re.findall(
        rf'(?<![\w.-])((?:/|~/)[^\'"]*?\.(?:{ext_pattern}))(?=$|[\s"\'.,;:!?)])',
        query,
        re.IGNORECASE,
    )
    for value in spaced_paths:
        candidate = value.strip()
        if candidate not in paths and not any(candidate in existing for existing in paths):
            paths.append(candidate)

    unquoted = re.findall(r'(?:^|\s)((?:/|~/)[^\s"\']+)', query)
    for value in unquoted:
        candidate = value.strip()
        if candidate not in paths and not any(candidate in existing for existing in paths):
            if not any(existing.startswith(candidate) for existing in paths):
                paths.append(candidate)

    preposition_paths = re.findall(
        r'(?:em|in|na|no|da|do|pasta|folder|directory|dir)\s+["\']?([^\s"\']+(?:\s+[^\s"\']+)*?)["\']?(?:\s|$)',
        query,
        re.IGNORECASE,
    )
    for value in preposition_paths:
        candidate = value.strip()
        if ("/" in candidate or "\\" in candidate) and candidate not in paths:
            if not any(candidate in existing for existing in paths):
                paths.append(candidate)

    return [os.path.expanduser(path.strip()) for path in paths if "\x00" not in path]


def explicit_audio_paths_from_metadata(metadata: dict[str, Any] | None) -> list[str]:
    """Return caller-provided evidence paths, resolving relatives against workspace."""

    if not isinstance(metadata, dict):
        return []
    raw_paths = metadata.get("input_paths")
    if not isinstance(raw_paths, list):
        return []
    workspace = str(metadata.get("workspace") or "").strip()
    paths: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        value = str(raw or "").strip()
        if not value or "\x00" in value:
            continue
        if value.startswith(("~", "/", "./", "../")):
            candidate = os.path.expanduser(value)
        elif workspace:
            candidate = str(Path(workspace) / value)
        else:
            candidate = value
        if candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
    return paths


def map_host_path_to_service_path(user_path: str) -> str:
    """Map a host home path into the container-visible audio service path."""

    if "\x00" in user_path:
        raise ValueError("Invalid audio path")
    expanded = os.path.expanduser(user_path.strip())
    host_home = os.path.realpath(
        os.path.abspath(os.environ.get("HOST_HOME_PREFIX", os.path.expanduser("~")))
    )
    target = os.path.realpath(os.path.abspath(expanded))
    container_mount = os.environ.get("AUDIO_TRANSCRIBE_HOST_HOME_MOUNT", "/host_home").rstrip("/")
    try:
        if os.path.commonpath([host_home, target]) == host_home:
            relative = os.path.relpath(target, host_home)
            return f"{container_mount}/{relative}" if relative != "." else container_mount
    except ValueError:
        pass
    return target


def _normalize_language_hint(value: object) -> str:
    text = str(value or "").strip().casefold().replace("_", "-")
    if not text:
        return "auto"
    if text in {"auto", "detect", "detetar", "detectar"}:
        return "auto"
    if text.startswith("pt") or text in {"português", "portugues", "portuguese"}:
        return "pt"
    if text.startswith("en") or text in {"inglês", "ingles", "english"}:
        return "en"
    if text.startswith("es") or text in {"espanhol", "spanish"}:
        return "es"
    return "auto"


def _detect_language_hint(query: str) -> str:
    q_lower = query.casefold()
    if re.search(r"\b(portugu[eê]s|portugues|portuguese|pt(?:-[a-z]{2})?)\b", q_lower):
        return "pt"
    if re.search(r"\b(ingl[eê]s|ingles|english|en(?:-[a-z]{2})?)\b", q_lower):
        return "en"
    if re.search(r"\b(espanhol|spanish|es(?:-[a-z]{2})?)\b", q_lower):
        return "es"
    return "auto"


def _transcription_language_from_request(request: AudioQueryRequest) -> str:
    metadata = request.metadata or {}
    for key in ("audio_language", "language", "language_hint", "user_language"):
        language = _normalize_language_hint(metadata.get(key))
        if language != "auto":
            return language
    if metadata.get("query_is_system_generated"):
        return "auto"
    return _detect_language_hint(request.query)


def _is_audio_file(path: Path) -> bool:
    return path.suffix.lstrip(".").lower() in _audio_extensions()


def _expand_audio_target(user_path: str) -> tuple[list[Path], list[str]]:
    service_path = map_host_path_to_service_path(user_path)
    target = Path(service_path)
    warnings: list[str] = []
    if target.is_dir():
        files = sorted(item for item in target.iterdir() if item.is_file() and _is_audio_file(item))
        return [validate_input_path(str(item)) for item in files], warnings
    if target.exists():
        return [validate_input_path(str(target))], warnings
    try:
        validate_input_path(str(target))
    except Exception as exc:
        warnings.append(f"{user_path}: {exc}")
    return [], warnings


def _output_root() -> str:
    return "storage_guardian://audio_outputs"


def _job_metadata(
    record: Any,
    *,
    source_path: Path,
    reused_result: bool = False,
    reused_existing_job: bool = False,
) -> dict[str, Any]:
    metadata = {
        "job_id": record.job_id,
        "file": source_path.name,
        "input_path": str(source_path),
        "status": record.status.value,
        "stage": record.stage.value,
        "progress": record.progress,
        "status_url": f"/transcriptions/{record.job_id}",
        "result_url": f"/transcriptions/{record.job_id}/result",
        "output_uri": f"{_output_root()}/{record.job_id[:8]}...",
        "reused_result": reused_result,
        "reused_existing_job": reused_existing_job,
    }
    if reused_result:
        result = record.to_result_response()
        metadata["outputs"] = result.outputs
        metadata["summary"] = result.summary
    return metadata


def _published_job_metadata(result: dict[str, Any], *, source_path: Path) -> dict[str, Any]:
    """Render a durable Storage Guardian reuse hit as query job metadata."""

    outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    job_id = str(result.get("job_id") or "storage-reuse")
    return {
        "job_id": job_id,
        "file": source_path.name,
        "input_path": str(source_path),
        "status": JobStatus.COMPLETED.value,
        "stage": JobStatus.COMPLETED.value,
        "progress": 100.0,
        "status_url": "",
        "result_url": "",
        "output_uri": f"{_output_root()}/{job_id[:8]}...",
        "reused_result": True,
        "reused_existing_job": False,
        "reused_from_storage_guardian": True,
        "outputs": outputs,
        "summary": summary,
        "reuse_metadata": metadata,
    }


async def _wait_for_jobs(
    jobs: list[dict[str, Any]],
    *,
    wait_seconds: float,
    poll_interval_seconds: float,
) -> None:
    if wait_seconds <= 0 or not jobs:
        return
    elapsed = 0.0
    interval = poll_interval_seconds
    terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    while elapsed < wait_seconds:
        all_terminal = True
        for job in jobs:
            if job.get("reused_from_storage_guardian") or not job.get("status_url"):
                continue
            try:
                record = get_job(job["job_id"])
            except JobNotFoundError as exc:
                job["status"] = JobStatus.FAILED.value
                job["stage"] = JobStatus.FAILED.value
                job["progress"] = job.get("progress") or 0.0
                job["error"] = exc.message
                continue
            job["status"] = record.status.value
            job["stage"] = record.stage.value
            job["progress"] = record.progress
            job["error"] = record.error
            if record.status == JobStatus.COMPLETED:
                result = record.to_result_response()
                job["outputs"] = result.outputs
                job["summary"] = result.summary
            if record.status not in terminal:
                all_terminal = False
        if all_terminal:
            return
        await asyncio.sleep(interval)
        elapsed += interval
        if elapsed > 60 and interval < 8.0:
            interval = 8.0


def _render_response(*, jobs: list[dict[str, Any]], warnings: list[str], paths: list[str]) -> str:
    if not paths:
        return (
            "Não consegui detectar nenhum caminho de ficheiro ou pasta na tua mensagem.\n"
            "Exemplo: transcreve o audio em /home/user/ficheiro.mp3"
        )
    if not jobs:
        lines = ["Nenhum ficheiro de áudio válido para transcrever."]
        if warnings:
            lines.extend(f"- {warning}" for warning in warnings[:5])
        return "\n".join(lines)

    completed = sum(1 for job in jobs if job["status"] == JobStatus.COMPLETED.value)
    failed = sum(1 for job in jobs if job["status"] in {JobStatus.FAILED.value, JobStatus.CANCELLED.value})
    reused = sum(1 for job in jobs if job.get("reused_result"))
    active = len(jobs) - completed - failed
    lines = [
        f"Transcrição de áudio aceite: {len(jobs)} job(s).",
        f"Estado: {completed} completo(s), {active} em curso/fila, {failed} falhado(s), {reused} reutilizado(s).",
        f"Output: `{_output_root()}/`",
        "",
    ]
    for job in jobs:
        reuse_label = ", reused result" if job.get("reused_result") else ", existing job" if job.get("reused_existing_job") else ""
        detail = f" - {job['file']} -> job `{job['job_id'][:8]}...` ({job['status']}{reuse_label}"
        if job.get("progress"):
            detail += f", {int(job['progress'])}%"
        detail += ")"
        lines.append(detail)
    if warnings:
        lines.append("")
        lines.extend(f"Aviso: {warning}" for warning in warnings[:5])
    return "\n".join(lines)


def _storage_transcript_excerpts(outputs: dict[str, Any]) -> list[str]:
    excerpts: list[str] = []
    preferred_artifacts = ("transcript_txt", "transcript_md", "transcript_clean_json", "rag_ready_json")
    for artifact in preferred_artifacts:
        uri = str(outputs.get(artifact) or "").strip()
        if not uri.startswith("storage_guardian://"):
            continue
        payload = read_storage_object_text(uri, max_bytes=12_000)
        text = _transcript_excerpt_from_text(str(payload.get("text") or ""))
        if len(" ".join(text.split())) >= 40:
            excerpts.append(text)
        if len(excerpts) >= 2:
            break
    return excerpts


def _transcript_excerpt_from_text(text: str) -> str:
    compact = " ".join(str(text or "").split())
    if not compact:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _compact_transcript_excerpt(compact)
    extracted = _extract_text_from_json_payload(payload)
    return _compact_transcript_excerpt(extracted or compact)


def _extract_text_from_json_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("transcript", "text", "clean_text", "content", "summary"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for key in ("segments", "chunks", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                parts = [_extract_text_from_json_payload(item) for item in value[:20]]
                joined = " ".join(part for part in parts if part)
                if joined:
                    return joined
    if isinstance(payload, list):
        parts = [_extract_text_from_json_payload(item) for item in payload[:20]]
        return " ".join(part for part in parts if part)
    return ""


def _compact_transcript_excerpt(text: str, *, limit: int = 1_800) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) > limit:
        return f"{compact[:limit].rstrip()}..."
    return compact


def _audio_semantic_digest(jobs: list[dict[str, Any]], *, paths: list[str]) -> dict[str, Any]:
    excerpts: list[str] = []
    storage_refs: list[str] = []
    completed = 0
    reused = 0
    for job in jobs:
        if job.get("status") == JobStatus.COMPLETED.value:
            completed += 1
        if job.get("reused_result"):
            reused += 1
        outputs = job.get("outputs")
        if isinstance(outputs, dict):
            storage_refs.extend(
                str(value)
                for value in outputs.values()
                if str(value).startswith("storage_guardian://")
            )
        summary = job.get("summary")
        if isinstance(summary, dict):
            for key in ("transcript_excerpt", "text_excerpt", "summary"):
                value = str(summary.get(key) or "").strip()
                if len(" ".join(value.split())) >= 40:
                    excerpts.append(value)
        if isinstance(outputs, dict):
            excerpts.extend(_storage_transcript_excerpts(outputs))
    missing: list[str] = []
    if jobs and not excerpts:
        if storage_refs:
            missing.append("storage refs are available but bounded transcript text could not be read")
        else:
            missing.append("audio transcript excerpt unavailable for job metadata/refs")
    return {
        "contract_version": "semantic_evidence_digest.v1",
        "provider": "audio_transcribe",
        "capability": "audio_transcription",
        "digest_kind": "audio",
        "source_paths": paths,
        "semantic_content_available": bool(excerpts),
        "excerpts": excerpts[:6],
        "summary": {
            "jobs": len(jobs),
            "completed": completed,
            "reused": reused,
        },
        "storage_refs": list(dict.fromkeys(storage_refs))[:12],
        "missing_semantic_evidence": missing,
    }


async def execute_audio_query(request: AudioQueryRequest) -> AudioQueryResponse:
    """Execute a natural-language transcription request inside the audio service."""

    cfg = get_config()
    metadata = request.metadata or {}
    paths = explicit_audio_paths_from_metadata(metadata) or extract_audio_paths(request.query)
    warnings: list[str] = []
    files_to_transcribe: list[Path] = []
    for user_path in paths:
        files, file_warnings = _expand_audio_target(user_path)
        warnings.extend(file_warnings)
        files_to_transcribe.extend(files)

    unique_files = list(dict.fromkeys(files_to_transcribe))
    jobs: list[dict[str, Any]] = []
    language = _transcription_language_from_request(request)
    options = TranscriptionOptions(language=language, vad=True, rag_ready=True)
    files_to_create: list[Path] = []
    reuse_only = bool(metadata.get("reuse_only")) or metadata.get("reuse_policy") == "reuse_only"
    for path in unique_files:
        if metadata.get("reuse_policy") != "force_reprocess":
            reused = find_reusable_completed_job(str(path), options=options)
            if reused is not None:
                jobs.append(_job_metadata(reused, source_path=path, reused_result=True))
                continue
            active = find_reusable_active_job(str(path), options=options)
            if active is not None:
                jobs.append(_job_metadata(active, source_path=path, reused_existing_job=True))
                continue
            published = find_published_transcription(path, options=options)
            if published is not None:
                jobs.append(_published_job_metadata(published, source_path=path))
                continue
        if reuse_only:
            warnings.append(f"{path}: no reusable transcription found for reuse_only request")
            continue
        files_to_create.append(path)

    if files_to_create and get_active_job_count() + len(files_to_create) > cfg.jobs.max_queued_jobs:
        raise HTTPException(status_code=429, detail="Job queue is full")

    queue = get_queue()
    for path in files_to_create:
        record = create_job(input_path=str(path), input_filename=path.name, options=options)
        await queue.enqueue(record.job_id)
        jobs.append(_job_metadata(record, source_path=path))

    await _wait_for_jobs(
        jobs,
        wait_seconds=request.wait_seconds,
        poll_interval_seconds=request.poll_interval_seconds,
    )
    content = _render_response(jobs=jobs, warnings=warnings, paths=paths)
    return AudioQueryResponse(
        content=content,
        success=bool(jobs),
        token_estimate=max(1, len(content) // 4),
        metadata={
            "paths": paths,
            "jobs": jobs,
            "warnings": warnings,
            "output_root": _output_root(),
            "owner": "audio_transcribe",
            "query_action": _query_action(jobs),
            "semantic_digest": _audio_semantic_digest(jobs, paths=paths),
        },
    )


def _query_action(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "none"
    if all(job.get("reused_result") for job in jobs):
        return "reused_result"
    if any(job.get("reused_existing_job") for job in jobs):
        return "reused_existing_job"
    return "created_job"
