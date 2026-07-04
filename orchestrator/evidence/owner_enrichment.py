"""Owner-backed enrichment for read-only material evidence context."""

from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, Protocol


class OwnerEndpointInvoker(Protocol):
    def __call__(
        self,
        feature_name: str,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
        policy_action: str | None = None,
        auth_profile: str = "internal_api",
    ) -> Any:
        """Invoke an owner endpoint through dispatch/lifecycle infrastructure."""


def enrich_material_evidence_context(
    evidence_context: dict[str, Any],
    *,
    invoke_endpoint: OwnerEndpointInvoker,
    user_language: str = "",
    max_documents_per_subject: int = 4,
    max_document_calls: int = 32,
    max_media_files: int = 4,
    timeout_seconds: float = 45.0,
) -> dict[str, Any]:
    """Attach compact semantic digests from owner services when available."""

    context = dict(deepcopy(evidence_context))
    language_hint = _normalize_language_hint(user_language or context.get("user_language"))
    if language_hint:
        context["user_language"] = language_hint
    workspace = str(context.get("workspace") or "").rstrip("/")
    raw_workspace_map = context.get("workspace_map")
    workspace_map: dict[str, Any] = raw_workspace_map if isinstance(raw_workspace_map, dict) else {}
    subjects = _subject_entries(workspace_map.get("top_level_entries"))
    document_paths = _structured_document_paths(workspace_map.get("detected_docs"))
    media_paths = _string_list(workspace_map.get("detected_media_files"))
    enrichment_results: list[dict[str, Any]] = list(context.get("enrichment_results") or [])
    missing_evidence: list[str] = list(context.get("missing_evidence") or [])

    for relative_path in _select_by_subject(
        document_paths,
        subjects,
        per_subject=max_documents_per_subject,
        limit=max_document_calls,
    ):
        result = _invoke_extrator_digest(
            relative_path,
            workspace=workspace,
            invoke_endpoint=invoke_endpoint,
            timeout_seconds=timeout_seconds,
        )
        enrichment_results.append(result)
        if result.get("missing_semantic_evidence"):
            missing_evidence.extend(str(item) for item in result["missing_semantic_evidence"])

    selected_media = media_paths[:max_media_files]
    if selected_media:
        result = _invoke_audio_digest(
            selected_media,
            workspace=workspace,
            invoke_endpoint=invoke_endpoint,
            timeout_seconds=timeout_seconds,
            language_hint=language_hint,
        )
        enrichment_results.append(result)
        if result.get("missing_semantic_evidence"):
            missing_evidence.extend(str(item) for item in result["missing_semantic_evidence"])

    context["enrichment_results"] = enrichment_results
    if missing_evidence:
        context["missing_evidence"] = _dedupe(missing_evidence)
    if enrichment_results:
        context["evidence_summary"] = (
            f"{context.get('evidence_summary') or ''} "
            f"Specialist evidence results attached: {len(enrichment_results)}."
        ).strip()
    return context


def _invoke_extrator_digest(
    relative_path: str,
    *,
    workspace: str,
    invoke_endpoint: OwnerEndpointInvoker,
    timeout_seconds: float,
) -> dict[str, Any]:
    absolute_path = _absolute_workspace_path(workspace, relative_path)
    response = invoke_endpoint(
        "extrator",
        method="POST",
        path="/v1/extrator/query",
        payload={
            "query": "Extract or reuse semantic digest for the structured input path.",
            "budget_tokens": 1500,
            "timeout_seconds": timeout_seconds,
            "wait_seconds": 0,
            "metadata": {
                "input_path": absolute_path,
                "source_path": absolute_path,
                "relative_path": relative_path,
                "source": "orchestrator.evidence",
                "reuse_policy": "prefer_existing_valid_result",
                "evidence_role": "material_documentation_context",
            },
        },
        timeout=timeout_seconds,
        policy_action="extrator.query",
        auth_profile="internal_api",
    )
    return _response_to_enrichment_result(
        provider="extrator",
        capability="document_extraction",
        input_paths=[relative_path],
        response=response,
    )


def _invoke_audio_digest(
    relative_paths: list[str],
    *,
    workspace: str,
    invoke_endpoint: OwnerEndpointInvoker,
    timeout_seconds: float,
    language_hint: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "workspace": workspace,
        "input_paths": relative_paths,
        "reuse_policy": "prefer_existing_valid_result",
        "source": "orchestrator.evidence",
        "evidence_role": "material_documentation_context",
        "query_is_system_generated": True,
    }
    if language_hint:
        metadata["language_hint"] = language_hint
    response = invoke_endpoint(
        "audio_transcribe",
        method="POST",
        path="/v1/transcribe",
        payload={
            "query": "Reuse audio evidence for local documentation.",
            "wait_seconds": min(30.0, timeout_seconds),
            "poll_interval_seconds": 2.0,
            "metadata": metadata,
        },
        timeout=timeout_seconds,
        policy_action="audio_transcribe.transcribe",
        auth_profile="audio_transcribe_api_key",
    )
    return _response_to_enrichment_result(
        provider="audio_transcribe",
        capability="audio_transcription",
        input_paths=relative_paths,
        response=response,
    )


def _response_to_enrichment_result(
    *,
    provider: str,
    capability: str,
    input_paths: list[str],
    response: Any,
) -> dict[str, Any]:
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        data = {}
    raw_metadata = data.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_digest = metadata.get("semantic_digest")
    digest: dict[str, Any] = raw_digest if isinstance(raw_digest, dict) else {}
    excerpts = _string_list(digest.get("excerpts"))
    output_refs = _dict_of_strings(metadata.get("outputs")) or _dict_of_strings(digest.get("output_refs"))
    storage_refs = _storage_refs(digest, output_refs)
    missing = _string_list(digest.get("missing_semantic_evidence"))
    response_error = str(getattr(response, "error", "") or data.get("error") or "").strip()
    success = bool(getattr(response, "success", False)) and not response_error
    semantic_available = bool(digest.get("semantic_content_available")) or bool(excerpts)
    if not success and response_error:
        missing.append(response_error)
    if success and not semantic_available:
        missing.append(f"{provider} compact semantic excerpts unavailable for selected input.")
    return {
        "provider": provider,
        "capability": capability,
        "input_paths": input_paths,
        "status": str(metadata.get("query_action") or data.get("action") or ("completed" if success else "failed")),
        "action": str(metadata.get("query_action") or data.get("action") or ""),
        "success": success,
        "semantic_content_available": semantic_available,
        "content_excerpt": _compact_excerpt(excerpts),
        "storage_refs": storage_refs,
        "output_refs": output_refs,
        "quality": {
            "digest_summary": digest.get("summary") if isinstance(digest.get("summary"), dict) else {},
            "latency_ms": getattr(response, "latency_ms", 0.0),
            "semantic_content_available": semantic_available,
        },
        "missing_semantic_evidence": _dedupe(missing),
        "error": response_error,
    }


def _structured_document_paths(raw_paths: Any) -> list[str]:
    plain_text = {".md", ".txt", ".html", ".htm"}
    return [
        path for path in _string_list(raw_paths)
        if PurePosixPath(path).suffix.casefold() not in plain_text
    ]


def _subject_entries(raw_entries: Any) -> list[str]:
    excluded = {"docs", ".ai-local", ".git", "__pycache__"}
    subjects: list[str] = []
    for item in _string_list(raw_entries):
        path = PurePosixPath(item.strip("/"))
        if len(path.parts) != 1:
            continue
        name = path.name
        if not name or name.casefold() in excluded or PurePosixPath(name).suffix:
            continue
        subjects.append(name)
    return _dedupe(subjects)


def _select_by_subject(paths: list[str], subjects: list[str], *, per_subject: int, limit: int) -> list[str]:
    selected: list[str] = []
    for subject in subjects:
        matches = [path for path in paths if path == subject or path.startswith(f"{subject}/")]
        selected.extend(matches[:per_subject])
        if len(selected) >= limit:
            return _dedupe(selected)[:limit]
    if len(selected) < limit:
        selected.extend(path for path in paths if path not in selected)
    return _dedupe(selected)[:limit]


def _absolute_workspace_path(workspace: str, relative_path: str) -> str:
    if relative_path.startswith("/"):
        return relative_path
    return f"{workspace.rstrip('/')}/{relative_path.lstrip('/')}"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_of_strings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(item)}


def _storage_refs(digest: dict[str, Any], output_refs: dict[str, str]) -> list[str]:
    refs = _string_list(digest.get("storage_refs"))
    refs.extend(value for value in output_refs.values() if value.startswith("storage_guardian://"))
    return _dedupe(refs)[:12]


def _normalize_language_hint(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("_", "-")
    if not text or text == "unknown":
        return ""
    if text.startswith("pt") or text in {"português", "portugues", "portuguese"}:
        return "pt"
    if text.startswith("en") or text in {"inglês", "ingles", "english"}:
        return "en"
    if text.startswith("es") or text in {"espanhol", "spanish"}:
        return "es"
    return ""


def _compact_excerpt(excerpts: list[str], *, limit: int = 3600) -> str:
    compact = " ".join(" ".join(item.split()) for item in excerpts if item.strip())
    if len(compact) > limit:
        return f"{compact[:limit].rstrip()}..."
    return compact


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
