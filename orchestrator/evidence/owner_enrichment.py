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
    max_research_subjects: int = 12,
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

    if workspace:
        prepare_result = _invoke_research_source_prepare(
            workspace=workspace,
            subjects=subjects,
            workspace_map=workspace_map,
            artifact_root=str((context.get("constraints") or {}).get("expected_artifact_root") or ""),
            invoke_endpoint=invoke_endpoint,
            timeout_seconds=timeout_seconds,
        )
        enrichment_results.append(prepare_result)
        if prepare_result.get("missing_semantic_evidence"):
            missing_evidence.extend(str(item) for item in prepare_result["missing_semantic_evidence"])
        source_name = _research_source_name(prepare_result, workspace)
        for subject in subjects[:max_research_subjects]:
            result = _invoke_research_subject_digest(
                subject,
                source_name=source_name,
                invoke_endpoint=invoke_endpoint,
                timeout_seconds=timeout_seconds,
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


def _invoke_research_source_prepare(
    *,
    workspace: str,
    subjects: list[str],
    workspace_map: dict[str, Any],
    artifact_root: str,
    invoke_endpoint: OwnerEndpointInvoker,
    timeout_seconds: float,
) -> dict[str, Any]:
    response = invoke_endpoint(
        "research",
        method="POST",
        path="/v1/research/sources/prepare",
        payload={
            "sources": [
                {
                    "path": workspace,
                    "source_type": "auto",
                    "exclude_patterns": _research_source_exclude_patterns(
                        workspace_map,
                        artifact_root=artifact_root,
                    ),
                }
            ],
            "target": "sources",
            "force": False,
            "wait_seconds": min(max(timeout_seconds, 0.0), 120.0),
            "poll_interval_seconds": 2.0,
        },
        timeout=max(timeout_seconds, 120.0),
        policy_action="rag.admin.prepare_sources",
        auth_profile="internal_api",
    )
    return _research_prepare_to_enrichment_result(
        input_paths=subjects or ["."],
        workspace=workspace,
        response=response,
    )


def _invoke_research_subject_digest(
    subject: str,
    *,
    source_name: str,
    invoke_endpoint: OwnerEndpointInvoker,
    timeout_seconds: float,
) -> dict[str, Any]:
    response = invoke_endpoint(
        "research",
        method="POST",
        path="/v1/research/search",
        payload={
            "query": (
                "Summarize the main topics, source files, evidence, and useful study "
                f"questions for the requested local source subsection named {subject!r}. "
                "Use retrieved evidence only."
            ),
            "budget_tokens": 2400,
            "include_code": True,
            "intent": "broad",
            "namespace": source_name,
            "metadata": {
                "source": "orchestrator.evidence",
                "evidence_role": "material_documentation_context",
                "subject": subject,
            },
        },
        timeout=timeout_seconds,
        policy_action="rag.query",
        auth_profile="internal_api",
    )
    return _research_search_to_enrichment_result(
        input_paths=[subject],
        subject=subject,
        response=response,
    )


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


def _research_prepare_to_enrichment_result(
    *,
    input_paths: list[str],
    workspace: str,
    response: Any,
) -> dict[str, Any]:
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        data = {}
    success = bool(getattr(response, "success", False)) and not str(getattr(response, "error", "") or "")
    response_error = str(getattr(response, "error", "") or data.get("error") or "").strip()
    sources = data.get("sources")
    if not isinstance(sources, list):
        sources = []
    status = str(data.get("status") or data.get("action") or ("completed" if success else "failed"))
    missing: list[str] = []
    if response_error:
        missing.append(response_error)
    if success and status not in {"completed", "submitted", "queued", "running"}:
        missing.append(f"research source preparation status: {status}")
    return {
        "provider": "research",
        "capability": "source_preparation",
        "input_paths": input_paths,
        "status": status,
        "action": "prepare_sources",
        "success": success,
        "semantic_content_available": False,
        "content_excerpt": "",
        "storage_refs": [],
        "output_refs": {},
        "quality": {
            "workspace": workspace,
            "job_id": data.get("job_id"),
            "status_url": data.get("status_url"),
            "prepared_sources": len(sources),
            "target": data.get("target"),
        },
        "missing_semantic_evidence": _dedupe(missing),
        "error": response_error,
        "prepared_sources": sources,
    }


def _research_search_to_enrichment_result(
    *,
    input_paths: list[str],
    subject: str,
    response: Any,
) -> dict[str, Any]:
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        data = {}
    response_error = str(getattr(response, "error", "") or data.get("error") or "").strip()
    success = bool(getattr(response, "success", False)) and not response_error
    content = str(data.get("content") or "").strip()
    raw_metadata = data.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    answerability = str(metadata.get("answerability") or "").strip().casefold()
    evidence_flags = _string_list(metadata.get("evidence_flags"))
    raw_results = data.get("results")
    result_count = len(raw_results) if isinstance(raw_results, list) else 0
    citations = _research_citations(raw_results)
    public_content = _research_public_content(content)
    has_insufficient_evidence = answerability == "insufficient" or "insufficient_evidence" in {
        flag.casefold() for flag in evidence_flags
    }
    semantic_available = bool(public_content) and result_count > 0 and not has_insufficient_evidence
    missing: list[str] = []
    if response_error:
        missing.append(response_error)
    if success and not semantic_available:
        missing.append(f"research returned no usable retrieved context for {subject}.")
    return {
        "provider": "research",
        "capability": "rag_context",
        "input_paths": input_paths,
        "status": str(data.get("status") or ("completed" if success else "failed")),
        "action": "search",
        "success": success,
        "semantic_content_available": semantic_available,
        "content_excerpt": _compact_excerpt([public_content]) if semantic_available else "",
        "storage_refs": [],
        "output_refs": {},
        "quality": {
            "answerability": answerability or None,
            "evidence_flags": evidence_flags,
            "result_count": result_count,
            "total_tokens": data.get("total_tokens"),
            "citations": citations[:8],
            "latency_ms": getattr(response, "latency_ms", 0.0),
        },
        "missing_semantic_evidence": _dedupe(missing),
        "error": response_error,
    }


def _research_public_content(content: str) -> str:
    """Strip research diagnostics from text before exposing it as source evidence."""
    lines = str(content or "").splitlines()
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        lowered = first.casefold()
        if lowered.startswith(
            (
                "research evidence answerability:",
                "reason:",
                "flags:",
                "plan:",
            )
        ):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def _research_source_name(prepare_result: dict[str, Any], workspace: str) -> str:
    sources = prepare_result.get("prepared_sources")
    if isinstance(sources, list):
        for item in sources:
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                return str(item["name"]).strip()
    return PurePosixPath(workspace.rstrip("/")).name or "source"


def _research_source_exclude_patterns(workspace_map: dict[str, Any], *, artifact_root: str) -> list[str]:
    patterns: list[str] = []
    root = artifact_root.strip().strip("/")
    if root:
        patterns.extend([root, f"{root}/**", f"{root}.tar.gz", f"{root}.zip"])
    patterns.extend([
        "__failure_snapshot__",
        "__failure_snapshot__/**",
        "**/__failure_snapshot__",
        "**/__failure_snapshot__/**",
        "failure-snapshot-*",
        "failure-snapshot-*/**",
        "failure-snapshot-*.tar.gz",
        "failure-snapshot-*.zip",
    ])
    for value in _string_list(workspace_map.get("generated_artifact_paths")):
        path = value.strip().strip("/")
        if not path:
            continue
        patterns.extend([path, f"{path}/**"])
    return _dedupe(patterns)


def _research_citations(raw_results: Any) -> list[str]:
    if not isinstance(raw_results, list):
        return []
    citations: list[str] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        value = str(item.get("citation_ref") or item.get("path") or item.get("source_id") or "").strip()
        if value:
            citations.append(value)
    return _dedupe(citations)


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
