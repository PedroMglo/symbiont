"""Document-root chunking for non-Git folders configured under [repos]."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Iterator

from chunking.markdown import Chunk
from metadata import stable_source_id
from rag_config import settings

_TEXT_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".adoc",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".log",
    ".sh",
    ".zsh",
    ".bash",
    ".ps1",
    ".sql",
    ".xml",
    ".env",
}
_CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".mjs",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".cxx",
    ".cc",
    ".hpp",
    ".hxx",
    ".cs",
    ".rb",
}
_TABLE_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls"}
_OFFICE_EXTENSIONS = {".docx", ".doc", ".pptx", ".ppt"}
_PDF_EXTENSIONS = {".pdf"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mkv", ".webm", ".mov", ".avi"}
_EXTRATOR_EXTENSIONS = _TABLE_EXTENSIONS | _OFFICE_EXTENSIONS | _PDF_EXTENSIONS
_DOCUMENT_EXTENSIONS = (
    _TEXT_EXTENSIONS | _CODE_EXTENSIONS | _TABLE_EXTENSIONS | _OFFICE_EXTENSIONS | _PDF_EXTENSIONS | _AUDIO_EXTENSIONS
)

_SKIP_DIRS = {
    ".git",
    ".cache",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".Trash",
    "Trash",
}


def _compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _matches_exclude(rel_path: str, name: str) -> bool:
    for pattern in settings.sync.exclude_patterns:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def iter_document_files(root: Path | str) -> Iterator[Path]:
    """Yield supported files from a generic document folder."""
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        return

    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root_path)
        parts = rel.parts
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in parts[:-1]):
            continue
        rel_text = rel.as_posix()
        suffix = path.suffix.lower()
        if suffix not in _DOCUMENT_EXTENSIONS:
            continue
        if suffix not in _AUDIO_EXTENSIONS and _matches_exclude(rel_text, path.name):
            continue
        yield path


def chunk_document_file(path: Path, source_dir: Path, cfg) -> list[Chunk]:
    """Parse a supported document file and convert it into RAG chunks."""
    path = Path(path)
    source_dir = Path(source_dir)
    suffix = path.suffix.lower()
    rel_path = path.relative_to(source_dir).as_posix()

    if suffix in _AUDIO_EXTENSIONS:
        return _chunk_audio_file(path, source_dir, cfg)
    if suffix in _CODE_EXTENSIONS:
        from chunking.code import chunk_file

        chunks = chunk_file(path, source_dir, settings.repos.chunking)
        for chunk in chunks:
            chunk.metadata.setdefault("source_name", source_dir.name)
            chunk.metadata.setdefault("repo_name", source_dir.name)
        return chunks
    if suffix in _EXTRATOR_EXTENSIONS:
        return _chunk_extrator_file(path, source_dir, cfg)

    text, source_type = _extract_text(path)
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    title = path.stem
    source_name = source_dir.name
    source_id = stable_source_id(source_name, source_dir)
    chunks: list[Chunk] = []

    for index, chunk_text in enumerate(_split_text(text, cfg.max_chars, cfg.overlap_chars)):
        display = chunk_text.strip()
        if len(display) < cfg.min_chars:
            continue
        if cfg.contextual_prefix:
            embedding_text = f"Fonte: {source_name} | Ficheiro: {rel_path} | Tipo: {source_type}\n{display}"
        else:
            embedding_text = display
        chunk_id = _compute_hash(f"document-v1:{source_id}:{rel_path}:{index}:{display}")
        chunks.append(
            Chunk(
                id=chunk_id,
                text=embedding_text,
                metadata={
                    "source_id": source_id,
                    "source_path": rel_path,
                    "source_type": source_type,
                    "source_name": source_name,
                    "repo_name": source_name,
                    "note_title": title,
                    "section_header": "",
                    "symbol_type": "document",
                    "chunk_index": index,
                    "display_text": display,
                    "content_hash": _compute_hash(display),
                },
            )
        )
    return chunks


def _chunk_extrator_file(path: Path, source_dir: Path, cfg) -> list[Chunk]:
    from integrations.external_services import extract_chunks_with_extrator

    rel_path = path.relative_to(source_dir).as_posix()
    source_name = source_dir.name
    source_id = stable_source_id(source_name, source_dir)
    normalized_chunks = extract_chunks_with_extrator(path)
    chunks: list[Chunk] = []

    for index, item in enumerate(normalized_chunks):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        policy = str(item.get("embedding_policy") or "").lower()
        if policy == "skip":
            continue
        display = text
        source_type = str(item.get("source_type") or path.suffix.lower().lstrip(".") or "document")
        if cfg.contextual_prefix:
            embedding_text = f"Fonte: {source_name} | Ficheiro: {rel_path} | Tipo: {source_type}\n{display}"
        else:
            embedding_text = display
        chunk_key = str(item.get("chunk_id") or index)
        content_hash = str(item.get("content_hash") or _compute_hash(display))
        chunks.append(
            Chunk(
                id=_compute_hash(f"extrator-v1:{source_id}:{rel_path}:{chunk_key}:{content_hash}"),
                text=embedding_text,
                metadata={
                    "source_id": source_id,
                    "source_path": rel_path,
                    "source_type": source_type,
                    "source_name": source_name,
                    "repo_name": source_name,
                    "note_title": str(item.get("title") or path.stem),
                    "section_header": str(item.get("section") or ""),
                    "symbol_type": "document",
                    "chunk_index": index,
                    "display_text": display,
                    "content_hash": content_hash,
                    "parser": str(item.get("parser") or "extrator"),
                    "embedding_policy": policy or "embed",
                    "external_doc_id": str(item.get("doc_id") or ""),
                    "external_chunk_id": chunk_key,
                    "page_start": item.get("page_start"),
                    "page_end": item.get("page_end"),
                },
            )
        )
    return chunks


def _split_text(text: str, max_chars: int, overlap: int) -> Iterator[str]:
    if len(text) <= max_chars:
        yield text
        return

    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind("\n\n", start, end)
            if cut <= start:
                cut = text.rfind(". ", start, end)
            if cut > start:
                end = cut + 1
        yield text[start:end].strip()
        if end >= len(text):
            break
        next_start = end - overlap
        start = next_start if next_start > start else end


def _extract_text(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        return _read_text(path), _source_type_for_suffix(suffix)
    return "", suffix.lstrip(".") or "document"


def _source_type_for_suffix(suffix: str) -> str:
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix == ".json":
        return "json"
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    if suffix == ".toml":
        return "toml"
    if suffix in {".sh", ".zsh", ".bash", ".ps1"}:
        return "script"
    if suffix in {".sql", ".xml", ".env", ".ini"}:
        return "config"
    return "text"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _chunk_audio_file(path: Path, source_dir: Path, cfg) -> list[Chunk]:
    transcript = _try_audio_transcription(path)
    if not transcript:
        from integrations.external_services import ExternalServicePending

        raise ExternalServicePending("audio_transcribe", "transcription completed without transcript text")
    return _chunk_text_as_audio(path, source_dir, cfg, transcript, "audio_transcript")


def _chunk_text_as_audio(path: Path, source_dir: Path, cfg, text: str, source_type: str) -> list[Chunk]:
    rel_path = path.relative_to(source_dir).as_posix()
    source_name = source_dir.name
    source_id = stable_source_id(source_name, source_dir)
    chunks: list[Chunk] = []
    for index, chunk_text in enumerate(_split_text(text, cfg.max_chars, cfg.overlap_chars)):
        display = chunk_text.strip()
        if not display:
            continue
        embedding_text = f"Fonte: {source_name} | Ficheiro: {rel_path} | Tipo: {source_type}\n{display}"
        chunks.append(
            Chunk(
                id=_compute_hash(f"audio-v1:{source_id}:{rel_path}:{index}:{display}"),
                text=embedding_text,
                metadata={
                    "source_id": source_id,
                    "source_path": rel_path,
                    "source_type": source_type,
                    "source_name": source_name,
                    "repo_name": source_name,
                    "note_title": path.stem,
                    "section_header": "",
                    "symbol_type": "audio",
                    "chunk_index": index,
                    "display_text": display,
                    "content_hash": _compute_hash(display),
                },
            )
        )
    return chunks


def _try_audio_transcription(path: Path) -> str:
    from integrations.external_services import (
        ExternalServicePending,
        ensure_lifecycle_service,
        request_background_lease,
    )

    url = settings.sync.audio_transcribe_url.rstrip("/")
    key = _read_optional_secret(settings.sync.audio_transcribe_api_key_file)
    if not url or not key:
        raise ExternalServicePending("audio_transcribe", "audio transcription URL or API key is not configured")
    try:
        import httpx
    except ImportError:
        raise ExternalServicePending("audio_transcribe", "httpx is not installed")

    timeout = max(1, settings.sync.audio_transcribe_timeout_seconds)
    verify = _httpx_verify()
    lease = request_background_lease(
        service_name="audio_transcribe",
        capability="audio_transcribe_gpu",
        resource_class="vram",
        path=path,
        estimated_ram_mb=1024,
        estimated_vram_mb=2048,
        estimated_duration_seconds=max(60, timeout),
    )
    try:
        ensure_lifecycle_service("audio_transcribe", url, timeout_seconds=max(45, timeout))
        client_timeout = httpx.Timeout(connect=2.0, read=10.0, write=30.0, pool=2.0)
        with httpx.Client(timeout=client_timeout, verify=verify) as client:
            health = client.get(f"{url}/health")
            if health.status_code >= 400:
                raise ExternalServicePending("audio_transcribe", f"healthcheck failed: {health.status_code}")
            with path.open("rb") as handle:
                response = client.post(
                    f"{url}/transcriptions/upload",
                    headers={"X-API-Key": key},
                    files={"file": (path.name, handle, "application/octet-stream")},
                    params={"rag_ready": "true"},
                )
            if response.status_code >= 400:
                raise ExternalServicePending("audio_transcribe", f"upload failed: {response.text[:300]}")
            job_id = response.json().get("job_id")
            if not job_id:
                raise ExternalServicePending("audio_transcribe", "job was not created")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                status = client.get(f"{url}/transcriptions/{job_id}", headers={"X-API-Key": key})
                if status.status_code >= 400:
                    raise ExternalServicePending("audio_transcribe", f"status failed: {status.text[:300]}")
                payload = status.json()
                if payload.get("status") == "completed":
                    result = client.get(f"{url}/transcriptions/{job_id}/result", headers={"X-API-Key": key})
                    if result.status_code >= 400:
                        raise ExternalServicePending("audio_transcribe", f"result failed: {result.text[:300]}")
                    return _extract_transcript_from_result(result.json())
                if payload.get("status") in {"failed", "cancelled"}:
                    raise RuntimeError(f"audio transcription failed for {path.name}: {payload}")
                time.sleep(3)
    except ExternalServicePending:
        raise
    except Exception:
        raise
    finally:
        lease.release()
    raise ExternalServicePending("audio_transcribe", f"job did not finish within {timeout}s")


def _extract_transcript_from_result(payload: dict) -> str:
    text = payload.get("transcript_text") or payload.get("text")
    if text:
        return str(text)
    summary = payload.get("summary")
    if isinstance(summary, dict) and summary.get("transcript_text"):
        return str(summary["transcript_text"])
    outputs = payload.get("outputs") or payload.get("artifacts")
    if isinstance(outputs, dict):
        for key in ("transcript_txt", "transcript_md"):
            artifact_text = _read_transcript_artifact(outputs.get(key))
            if artifact_text:
                return artifact_text
        rag_ready = _read_json_artifact(outputs.get("rag_ready_json"))
        if rag_ready:
            return _rag_ready_text(rag_ready)
    return ""


def _read_transcript_artifact(value: object) -> str:
    artifact = _resolve_audio_artifact_path(value)
    if artifact is None:
        return ""
    try:
        return artifact.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _read_json_artifact(value: object) -> dict:
    artifact = _resolve_audio_artifact_path(value)
    if artifact is None:
        return {}
    try:
        return json.loads(artifact.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _resolve_audio_artifact_path(value: object) -> Path | None:
    if not value:
        return None
    raw = str(value)
    path = Path(raw)
    if path.is_absolute() and path.is_file():
        return path

    output_dir = settings.sync.audio_transcribe_output_dir.strip()
    if not output_dir:
        return None
    output_root = Path(output_dir)
    data_prefix = "/data/output/"
    if raw.startswith(data_prefix):
        return output_root / raw[len(data_prefix):]
    if not path.is_absolute():
        return output_root / raw
    return None


def _rag_ready_text(payload: dict) -> str:
    parts: list[str] = []
    summary = payload.get("summary")
    if isinstance(summary, dict):
        for key in ("short", "detailed"):
            value = summary.get(key)
            if value:
                parts.append(str(value))
        topics = summary.get("topics")
        if isinstance(topics, list) and topics:
            parts.append("Topics: " + ", ".join(str(topic) for topic in topics))
    for key in ("decisions", "action_items", "technical_topics", "entities", "key_quotes"):
        values = payload.get(key)
        if isinstance(values, list) and values:
            parts.append(f"{key}: {json.dumps(values, ensure_ascii=False)}")
    references = payload.get("references")
    if isinstance(references, list):
        excerpts = [str(item.get("text", "")).strip() for item in references if isinstance(item, dict)]
        excerpts = [excerpt for excerpt in excerpts if excerpt]
        if excerpts:
            parts.append("\n".join(excerpts))
    return "\n\n".join(parts).strip()


def _read_optional_secret(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _httpx_verify():
    cert = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if cert and Path(cert).is_file():
        return cert
    return True
