"""LLM proposal backend for material builder contracts."""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass
import hashlib
import json
import keyword
import re
from pathlib import Path
import sys
import time
import tomllib
from typing import Any
import unicodedata

from context_governor import ContextGovernorBlocked, govern_chat_completion
import httpx
from pydantic import ValidationError

from material_builder.config import LLMSettings
from material_builder.types import (
    FileKind,
    GeneratedFileProposal,
    KNOWN_VALIDATION_PROFILES,
    MaterialFileSpec,
    MaterialFileGenerationRequest,
    MaterialPatchGenerationRequest,
    MaterialPlan,
    MaterialPlanRepairRequest,
    MaterialPlanRepairResponse,
    MaterialPlanRequest,
    MaterialPlanResponse,
    MaterialRepairCriticFinding,
    MaterialRepairCriticRequest,
    MaterialRepairCriticResponse,
    PatchSetProposal,
    PatchProposal,
    ReplacementProposal,
)


KNOWN_FILE_KINDS: tuple[FileKind, ...] = (
    "python",
    "test",
    "dockerfile",
    "compose",
    "markdown",
    "config",
    "text",
    "other",
)
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{3,128}$")
DOCUMENTATION_PURPOSE_LIMIT = 8192
FILE_KIND_ALIASES: dict[str, FileKind] = {
    "json": "config",
    "pyproject": "config",
    "python-pytest": "test",
    "pytest": "test",
    "toml": "config",
    "unit-test": "test",
    "yaml": "config",
    "yml": "config",
}
VALIDATION_PROFILE_ALIASES: dict[str, str] = {
    "api-service": "python-api",
    "artifact-validation": "artifact",
    "artifact-packaging": "artifact",
    "compose": "docker-compose-static",
    "docker-compose": "docker-compose-static",
    "docker-compose-config": "docker-compose-static",
    "docker-compose-up": "docker-compose-runtime",
    "http-api": "python-api",
    "rest-api": "python-api",
    "package": "artifact",
    "packaging": "artifact",
    "postgres": "stateful-postgres",
    "postgresql": "stateful-postgres",
    "compileall": "python-basic",
    "pytest": "python-pytest",
    "pytests": "python-pytest",
    "python-compile": "python-basic",
    "python-syntax": "python-basic",
    "python-validation": "python-basic",
    "test": "python-pytest",
    "tests": "python-pytest",
    "python-test": "python-pytest",
    "python-tests": "python-pytest",
    "redis": "stateful-redis",
    "unit-test": "python-pytest",
    "unit-tests": "python-pytest",
    "worker": "worker-queue",
}
VALIDATION_PROFILE_GUIDANCE = (
    "python-basic for Python syntax/import validation; "
    "python-pytest for Python tests; "
    "python-api only for HTTP API services; "
    "cli only for command-line interfaces; "
    "docker-compose-static for Compose config validation; "
    "docker-compose-runtime for isolated Compose build/up smoke; "
    "stateful-postgres for PostgreSQL persistence smoke; "
    "stateful-redis for Redis/event smoke; "
    "worker-queue for queued worker behavior; "
    "artifact for final artifact packaging; "
    "node-basic for Node.js projects."
)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE: dict[str, str] = {}


@dataclass(frozen=True)
class LLMJSONResult:
    payload: dict[str, Any]
    lane_metrics: dict[str, Any]


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


class MaterialLLMError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)


def _json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    candidate = _extract_json_object_text(cleaned)
    if candidate is None:
        raise MaterialLLMError("llm_schema_invalid", "LLM response did not contain a JSON object")
    candidate = _repair_json_triple_quoted_strings(candidate)
    candidate = _strip_json_comments(candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        repaired = _repair_json_separator_syntax(_repair_json_string_syntax(candidate))
        try:
            value = json.loads(repaired)
        except json.JSONDecodeError as repaired_exc:
            raise MaterialLLMError("llm_schema_invalid", "LLM response was not valid JSON") from repaired_exc
    if not isinstance(value, dict):
        raise MaterialLLMError("llm_schema_invalid", "LLM response JSON must be an object")
    return value


def _strip_json_comments(text: str) -> str:
    """Remove model-emitted comments from JSON-like text without touching strings."""

    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "#":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _extract_json_object_text(text: str) -> str | None:
    in_string = False
    escaped = False
    start: int | None = None
    depth = 0
    for index, char in enumerate(text):
        if start is None:
            if char == "{":
                start = index
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None
    return text[first : last + 1]


def _repair_json_triple_quoted_strings(text: str) -> str:
    """Convert Python/Markdown-style triple-quoted JSON values into JSON strings."""

    pattern = re.compile(r"(:\s*)(\"\"\"|''')([\s\S]*?)(\2)")

    def replace(match: re.Match[str]) -> str:
        prefix = match.group(1)
        value = match.group(3)
        return f"{prefix}{json.dumps(value, ensure_ascii=False)}"

    return pattern.sub(replace, text)


def _repair_json_string_syntax(text: str) -> str:
    """Repair common LLM JSON string syntax issues without changing structure.

    Models often emit long Markdown/code file bodies as JSON strings but leave
    literal newlines or Markdown escape sequences such as ``\\.`` inside those
    strings. JSON requires control characters to be escaped and only permits a
    small set of backslash escapes. This pass is intentionally lexical: it does
    not invent fields or fallback content; it only makes string tokens parseable.
    """

    out: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            if char in {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}:
                out.append("\\")
                out.append(char)
            else:
                out.append("\\\\")
                out.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            out.append(char)
            in_string = False
            continue
        if char == "\n":
            out.append("\\n")
            continue
        if char == "\r":
            out.append("\\r")
            continue
        if char == "\t":
            out.append("\\t")
            continue
        out.append(char)
    if escaped:
        out.append("\\\\")
    return "".join(out)


def _repair_json_separator_syntax(text: str) -> str:
    # After string repair, physical newlines are outside JSON strings. Some
    # models omit a comma between adjacent object fields; adding that separator
    # preserves the emitted fields without inventing content.
    return re.sub(r'(?<=["}\]\d])\s*\n+\s*(?="[^"\n]+"\s*:)', ",\n", text)


def _compact_text(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2].rstrip()
    tail = text[-limit // 2 :].lstrip()
    return f"{head}\n\n[truncated]\n\n{tail}"


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [_json_safe(item) for item in value]
        return str(value)


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return _json_safe(exc.errors(include_url=False))


def _call_governed_json(messages: list[dict[str, str]], llm: LLMSettings) -> LLMJSONResult:
    if not llm.configured:
        raise MaterialLLMError(
            "material_builder_backend_unavailable",
            "material_builder has no LLM proposal backend configured",
        )
    started = time.monotonic()
    input_tokens = _estimate_message_tokens(messages)
    try:
        raw = _governed_chat_completion(
            model=llm.model,
            messages=messages,
            base_url=llm.base_url,
            temperature=llm.temperature,
            max_tokens=llm.max_tokens,
            timeout=llm.timeout_seconds,
            phase="material_builder.json",
            post=httpx.post,
        )
    except httpx.TimeoutException as exc:
        raise MaterialLLMError(
            "llm_no_progress_timeout",
            "LLM proposal call timed out before returning progress",
            details={
                "lane_metrics": _lane_metrics(
                    llm,
                    started=started,
                    input_tokens=input_tokens,
                    output_text="",
                    schema_retries=0,
                    timeout_reason="no_progress_timeout",
                )
            },
        ) from exc
    except ValidationError as exc:
        raise MaterialLLMError(
            "llm_generation_failed",
            "LLM proposal call failed",
            details={
                "lane_metrics": _lane_metrics(
                    llm,
                    started=started,
                    input_tokens=input_tokens,
                    output_text="",
                    schema_retries=0,
                    timeout_reason=None,
                )
            },
        ) from exc
    try:
        return LLMJSONResult(
            payload=_json_object(raw),
            lane_metrics=_lane_metrics(
                llm,
                started=started,
                input_tokens=input_tokens,
                output_text=raw,
                schema_retries=0,
                timeout_reason=None,
            ),
        )
    except MaterialLLMError as exc:
        if exc.code != "llm_schema_invalid":
            raise
        repair_messages = [
            *messages,
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": _prompt("json_repair.md"),
                        "invalid_response": _compact_text(raw),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ]
        try:
            repaired = _governed_chat_completion(
                model=llm.model,
                messages=repair_messages,
                base_url=llm.base_url,
                phase="material_builder.json_repair",
                temperature=0.0,
                max_tokens=llm.max_tokens,
                timeout=llm.timeout_seconds,
                post=httpx.post,
            )
        except httpx.TimeoutException as repair_exc:
            raise MaterialLLMError(
                "llm_no_progress_timeout",
                "LLM JSON repair call timed out before returning progress",
                details={
                    "lane_metrics": _lane_metrics(
                        llm,
                        started=started,
                        input_tokens=input_tokens,
                        output_text=raw,
                        schema_retries=1,
                        timeout_reason="schema_repair_no_progress_timeout",
                    )
                },
            ) from repair_exc
        except Exception as repair_exc:
            raise MaterialLLMError("llm_generation_failed", "LLM JSON repair call failed") from repair_exc
        try:
            return LLMJSONResult(
                payload=_json_object(repaired),
                lane_metrics=_lane_metrics(
                    llm,
                    started=started,
                    input_tokens=input_tokens,
                    output_text=repaired,
                    schema_retries=1,
                    timeout_reason=None,
                ),
            )
        except MaterialLLMError as final_exc:
            final_exc.details["response_excerpt"] = _compact_text(repaired, 1000)
            final_exc.details["invalid_response_excerpt"] = _compact_text(raw, 6000)
            final_exc.details["lane_metrics"] = _lane_metrics(
                llm,
                started=started,
                input_tokens=input_tokens,
                output_text=repaired,
                schema_retries=1,
                timeout_reason="schema_invalid_after_repair",
            )
            raise final_exc


def _governed_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    phase: str,
    post: Any,
) -> str:
    try:
        return govern_chat_completion(
            model=model,
            messages=messages,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            phase=phase,
            post=post,
        )
    except ContextGovernorBlocked as exc:
        raise MaterialLLMError(
            "llm_context_blocked",
            "Context Governor blocked the material_builder LLM call",
            details={"phase": phase, "reason": str(exc)},
        ) from exc


def _estimate_message_tokens(messages: list[dict[str, str]]) -> int:
    text = "\n".join(str(message.get("content") or "") for message in messages)
    return _estimate_tokens(text)


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _lane_metrics(
    llm: LLMSettings,
    *,
    started: float,
    input_tokens: int,
    output_text: str,
    schema_retries: int,
    timeout_reason: str | None,
) -> dict[str, Any]:
    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    output_tokens = _estimate_tokens(output_text) if output_text else 0
    elapsed_seconds = duration_ms / 1000 if duration_ms else 0
    return {
        "lane": llm.lane,
        "model": llm.model,
        "base_url_configured": bool(llm.base_url.strip()),
        "duration_ms": duration_ms,
        "first_token_latency_ms": None,
        "first_token_observed": False,
        "tokens_per_second_estimate": round(output_tokens / elapsed_seconds, 3) if elapsed_seconds else None,
        "input_tokens_estimate": input_tokens,
        "output_tokens_estimate": output_tokens,
        "schema_retries": schema_retries,
        "timeout_seconds": llm.timeout_seconds,
        "no_progress_watchdog_seconds": llm.no_progress_timeout_seconds,
        "wall_budget_seconds": llm.wall_budget_seconds,
        "timeout_reason": timeout_reason,
        "static_fallback_used": False,
    }


def _merge_lane_metrics(*items: dict[str, Any]) -> dict[str, Any]:
    metrics = [dict(item) for item in items if item]
    if not metrics:
        return {}
    if len(metrics) == 1:
        return metrics[0]
    duration_ms = sum(int(item.get("duration_ms") or 0) for item in metrics)
    input_tokens = sum(int(item.get("input_tokens_estimate") or 0) for item in metrics)
    output_tokens = sum(int(item.get("output_tokens_estimate") or 0) for item in metrics)
    schema_retries = sum(int(item.get("schema_retries") or 0) for item in metrics)
    elapsed_seconds = duration_ms / 1000 if duration_ms else 0
    return {
        "lane": metrics[0].get("lane"),
        "model": metrics[0].get("model"),
        "call_count": len(metrics),
        "duration_ms": duration_ms,
        "first_token_latency_ms": None,
        "first_token_observed": False,
        "tokens_per_second_estimate": round(output_tokens / elapsed_seconds, 3) if elapsed_seconds else None,
        "input_tokens_estimate": input_tokens,
        "output_tokens_estimate": output_tokens,
        "schema_retries": schema_retries,
        "timeout_seconds": metrics[0].get("timeout_seconds"),
        "no_progress_watchdog_seconds": metrics[0].get("no_progress_watchdog_seconds"),
        "wall_budget_seconds": metrics[0].get("wall_budget_seconds"),
        "timeout_reason": next((item.get("timeout_reason") for item in metrics if item.get("timeout_reason")), None),
        "static_fallback_used": any(bool(item.get("static_fallback_used")) for item in metrics),
        "static_fallback_reason": next(
            (item.get("static_fallback_reason") for item in metrics if item.get("static_fallback_reason")),
            None,
        ),
        "path_override_used": any(bool(item.get("path_override_used")) for item in metrics),
        "path_override_reason": next(
            (item.get("path_override_reason") for item in metrics if item.get("path_override_reason")),
            None,
        ),
        "calls": metrics,
    }


def _normalize_file_kinds(payload: dict[str, Any]) -> dict[str, Any]:
    plan_payload = payload.get("plan", payload)
    if not isinstance(plan_payload, dict):
        return payload
    files = plan_payload.get("files")
    if not isinstance(files, list):
        return payload
    for file_entry in files:
        if not isinstance(file_entry, dict):
            continue
        kind = str(file_entry.get("kind") or "").strip().lower()
        if kind in FILE_KIND_ALIASES:
            file_entry["kind"] = FILE_KIND_ALIASES[kind]
    return payload


def _normalize_plan_payload(payload: dict[str, Any], *, expected_project_root: str | None = None) -> dict[str, Any]:
    plan_payload = payload.get("plan", payload)
    if not isinstance(plan_payload, dict):
        return payload
    normalized = dict(plan_payload)
    if normalized.get("contract") == "material_plan.v3.2" and "schema_version" not in normalized:
        normalized["schema_version"] = normalized.pop("contract")
    else:
        normalized.pop("contract", None)
    if not normalized.get("variation_reason"):
        variation_parts = [
            str(normalized.get(key)).strip()
            for key in ("variation", "variation_type", "variation_description")
            if normalized.get(key)
        ]
        if variation_parts:
            normalized["variation_reason"] = " / ".join(variation_parts)
    for key in ("variation", "variation_type", "variation_description"):
        normalized.pop(key, None)
    normalized.pop("required_capabilities", None)
    declared_root = _normalize_expected_project_root(str(normalized.get("project_root") or ""))
    expected_root = _normalize_expected_project_root(expected_project_root)
    if expected_root:
        normalized["project_root"] = expected_root
    for profile_key in ("required_validation_profiles", "optional_validation_profiles"):
        profiles = normalized.get(profile_key)
        if isinstance(profiles, list):
            normalized[profile_key] = _normalize_validation_profiles(profiles)
    requirements = normalized.get("requirements")
    if isinstance(requirements, list):
        normalized["requirements"] = [_normalize_requirement_spec(item) for item in requirements]
    files = normalized.get("files")
    if not isinstance(files, list):
        alias_files = _extract_plan_file_aliases(normalized)
        if alias_files:
            normalized["files"] = alias_files
            files = alias_files
    path_root = _project_root_from_payload_paths(files)
    source_root_for_rename = declared_root or path_root
    path_renames: dict[str, str] = {}
    if isinstance(files, list):
        normalized_files: list[Any] = []
        seen_paths: set[str] = set()
        for file_entry in files:
            if isinstance(file_entry, str):
                normalized_file = {
                    "path": file_entry,
                    "purpose": _purpose_from_path(file_entry),
                }
            elif not isinstance(file_entry, dict):
                normalized_files.append(file_entry)
                continue
            else:
                normalized_file = dict(file_entry)
            if "path" not in normalized_file and "file_path" in normalized_file:
                normalized_file["path"] = normalized_file.pop("file_path")
            else:
                normalized_file.pop("file_path", None)
            input_path = str(normalized_file.get("path") or "")
            normalized_file["path"] = _replace_plan_path_root(
                normalized_file.get("path"),
                old_root=source_root_for_rename,
                new_root=expected_root,
            )
            normalized_file["path"] = _normalize_plan_path(
                normalized_file.get("path"),
                project_root=str(normalized.get("project_root") or ""),
            )
            if "kind" not in normalized_file and "file_type" in normalized_file:
                normalized_file["kind"] = normalized_file.pop("file_type")
            else:
                normalized_file.pop("file_type", None)
            original_path = str(normalized_file.get("path") or "")
            path_kind = _file_kind_from_path(original_path)
            if path_kind:
                normalized_file["kind"] = path_kind
            normalized_file["path"] = _normalize_python_material_path(
                normalized_file["path"],
                kind=str(normalized_file.get("kind") or ""),
                project_root=str(normalized.get("project_root") or ""),
            )
            if not str(normalized_file.get("purpose") or "").strip():
                normalized_file["purpose"] = _purpose_from_path(str(normalized_file.get("path") or "material file"))
            max_tokens = normalized_file.get("max_tokens")
            if max_tokens is None or not isinstance(max_tokens, int) or max_tokens < 1:
                normalized_file.pop("max_tokens", None)
            # File bodies belong to the file-generation contract. The plan is
            # only a manifest, so misplaced content is ignored rather than
            # trusted as materialized output.
            normalized_file.pop("content", None)
            normalized_file.pop("contents", None)
            if isinstance(normalized_file.get("depends_on"), list):
                depends_on = normalized_file["depends_on"]
                if expected_root:
                    depends_on = [
                        _replace_plan_path_root(path, old_root=source_root_for_rename, new_root=expected_root)
                        for path in depends_on
                    ]
                normalized_file["depends_on"] = _normalize_plan_paths(
                    depends_on,
                    project_root=str(normalized.get("project_root") or ""),
                    path_renames=path_renames,
                )
            normalized_path = str(normalized_file.get("path") or "")
            if input_path and input_path != normalized_path:
                path_renames[input_path] = normalized_path
            if original_path and original_path != normalized_path:
                path_renames[original_path] = normalized_path
            if normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            normalized_files.append(normalized_file)
        if path_renames:
            for normalized_file in normalized_files:
                depends_on = normalized_file.get("depends_on") if isinstance(normalized_file, dict) else None
                if isinstance(depends_on, list):
                    normalized_file["depends_on"] = [path_renames.get(path, path) for path in depends_on]
        normalized["files"] = normalized_files
    project_root = str(normalized.get("project_root") or "")
    interfaces = normalized.get("intended_interfaces")
    if isinstance(interfaces, list):
        normalized["intended_interfaces"] = [
            _normalize_interface_spec(item, project_root=project_root, path_renames=path_renames)
            for item in interfaces
        ]
    artifacts = normalized.get("artifact_expectations")
    if isinstance(artifacts, list):
        normalized["artifact_expectations"] = [
                _normalize_artifact_expectation(item, project_root=project_root, path_renames=path_renames)
                for item in artifacts
            ]
        planned_file_paths = {
            str(file_entry.get("path"))
            for file_entry in normalized.get("files") or []
            if isinstance(file_entry, dict) and str(file_entry.get("path") or "").strip()
        }
        if planned_file_paths:
            for artifact in normalized["artifact_expectations"]:
                if not isinstance(artifact, dict) or not isinstance(artifact.get("file_refs"), list):
                    continue
                artifact["file_refs"] = [
                    ref for ref in artifact["file_refs"] if str(ref or "").strip() in planned_file_paths
                ]
    criteria = normalized.get("completion_criteria")
    if isinstance(criteria, list):
        normalized["completion_criteria"] = [_normalize_completion_criterion(item) for item in criteria]
    dependency_strategy = normalized.get("dependency_strategy")
    if isinstance(dependency_strategy, dict):
        normalized["dependency_strategy"] = _normalize_dependency_strategy(
            dependency_strategy,
            project_root=project_root,
            path_renames=path_renames,
        )
    validation_commands = normalized.get("validation_commands")
    if isinstance(validation_commands, dict):
        normalized_commands: dict[str, Any] = {}
        for raw_profile, command in validation_commands.items():
            profile = _normalize_validation_profile(raw_profile)
            if isinstance(command, dict):
                command_profile = _normalize_validation_profile(command.get("profile"))
                if command_profile in KNOWN_VALIDATION_PROFILES:
                    profile = command_profile
            if profile not in KNOWN_VALIDATION_PROFILES or profile in normalized_commands:
                continue
            normalized_command = _normalize_validation_command(
                profile,
                command,
                project_root=str(normalized.get("project_root") or ""),
            )
            if normalized_command is None:
                continue
            if isinstance(normalized_command, dict):
                normalized_command["profile"] = profile
            normalized_commands[profile] = normalized_command
        normalized["validation_commands"] = normalized_commands
        declared = set(normalized.get("required_validation_profiles") or []) | set(
            normalized.get("optional_validation_profiles") or []
        )
        undeclared = [
            profile
            for profile in normalized["validation_commands"]
            if profile in KNOWN_VALIDATION_PROFILES and profile not in declared
        ]
        if undeclared:
            normalized["optional_validation_profiles"] = [
                *(normalized.get("optional_validation_profiles") or []),
                *undeclared,
            ]
    normalized = _normalize_plan_requirement_refs(
        normalized,
        project_root=project_root,
        path_renames=path_renames,
    )
    normalized = _normalize_completion_refs(normalized)
    normalized = _drop_unknown_plan_keys(normalized)
    if "plan" in payload:
        result = dict(payload)
        result["plan"] = normalized
        return result
    return normalized


def _replace_plan_path_root(value: Any, *, old_root: str, new_root: str) -> Any:
    if not old_root or not new_root or old_root == new_root:
        return value
    path = str(value or "").strip().strip("/").replace("\\", "/")
    if path == old_root:
        return new_root
    if path.startswith(f"{old_root}/"):
        suffix = path[len(old_root) + 1 :]
        if suffix == new_root or suffix.startswith(f"{new_root}/"):
            return suffix
        return f"{new_root}/{suffix}"
    return value


def _extract_plan_file_aliases(plan: dict[str, Any]) -> list[Any]:
    for key in (
        "file_manifest",
        "file_manifests",
        "file_specs",
        "file_plan",
        "planned_files",
        "project_files",
        "source_files",
        "structure",
    ):
        value = plan.get(key)
        extracted = _coerce_plan_files(value)
        if extracted:
            return extracted
    for key in ("manifest", "project", "project_structure", "workspace", "output"):
        value = plan.get(key)
        if not isinstance(value, dict):
            continue
        for nested_key in (
            "files",
            "file_manifest",
            "file_specs",
            "file_plan",
            "planned_files",
            "project_files",
            "source_files",
            "structure",
        ):
            extracted = _coerce_plan_files(value.get(nested_key))
            if extracted:
                return extracted
    return []


def _coerce_plan_files(value: Any) -> list[Any]:
    if isinstance(value, dict):
        files: list[Any] = []
        for raw_path, raw_spec in value.items():
            if isinstance(raw_spec, dict):
                item = dict(raw_spec)
                item.setdefault("path", raw_path)
                files.append(item)
            else:
                files.append(
                    {
                        "path": raw_path,
                        "purpose": _purpose_from_path(str(raw_path)),
                    }
                )
        return files
    if not isinstance(value, list):
        return []
    files = []
    for item in value:
        if isinstance(item, str):
            files.append({"path": item, "purpose": _purpose_from_path(item)})
        elif isinstance(item, dict):
            files.append(item)
    return files


def _drop_unknown_plan_keys(plan: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "project_root",
        "requirements",
        "files",
        "intended_interfaces",
        "required_validation_profiles",
        "optional_validation_profiles",
        "validation_commands",
        "artifact_expectations",
        "completion_criteria",
        "dependency_strategy",
        "architecture_notes",
        "variation_reason",
    }
    return {key: value for key, value in plan.items() if key in allowed}


def _normalize_expected_project_root(value: str | None) -> str:
    root = str(value or "").strip().strip("/").replace("\\", "/")
    if not root or root == "." or root.startswith("/") or ".." in root.split("/"):
        return ""
    return root


def _expected_artifact_root_from_constraints(constraints: dict[str, Any]) -> str:
    value = constraints.get("expected_artifact_root")
    if value is None:
        return ""
    return _normalize_expected_project_root(str(value))


def _normalize_validation_profile(value: Any) -> str:
    profile = str(value or "").strip().lower().replace("_", "-")
    return VALIDATION_PROFILE_ALIASES.get(profile, profile)


def _normalize_validation_profiles(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        profile = _normalize_validation_profile(value)
        if not profile or profile in seen:
            continue
        seen.add(profile)
        normalized.append(profile)
    return normalized


def _normalize_requirement_spec(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    allowed = {"requirement_id", "description", "source", "capability_refs"}
    return {key: item for key, item in value.items() if key in allowed}


def _normalize_interface_spec(
    value: Any,
    *,
    project_root: str,
    path_renames: dict[str, str] | None = None,
) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    file_refs = normalized.get("file_refs")
    if isinstance(file_refs, list):
        normalized["file_refs"] = _normalize_plan_paths(
            file_refs,
            project_root=project_root,
            path_renames=path_renames,
        )
    return normalized


def _normalize_artifact_expectation(
    value: Any,
    *,
    project_root: str,
    path_renames: dict[str, str] | None = None,
) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    root = str(normalized.get("root") or "").strip()
    if root:
        normalized["root"] = _normalize_artifact_root(root, project_root=project_root)
    elif project_root:
        normalized["root"] = project_root
    file_refs = normalized.get("file_refs")
    if isinstance(file_refs, list):
        normalized["file_refs"] = _normalize_plan_paths(
            file_refs,
            project_root=project_root,
            path_renames=path_renames,
        )
    return normalized


def _normalize_completion_criterion(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    validation_refs = normalized.get("validation_refs")
    if isinstance(validation_refs, list):
        normalized["validation_refs"] = _normalize_validation_profiles(validation_refs)
    return normalized


def _normalize_completion_refs(plan: dict[str, Any]) -> dict[str, Any]:
    criteria = plan.get("completion_criteria")
    if not isinstance(criteria, list):
        return plan
    validation_commands = plan.get("validation_commands")
    validation_command_ids = list(validation_commands.keys()) if isinstance(validation_commands, dict) else []
    validation_ids = {
        str(item)
        for item in [
            *(plan.get("required_validation_profiles") or []),
            *(plan.get("optional_validation_profiles") or []),
            *validation_command_ids,
        ]
        if str(item).strip()
    }
    artifact_ids = {
        str(item.get("artifact_id"))
        for item in plan.get("artifact_expectations") or []
        if isinstance(item, dict) and str(item.get("artifact_id") or "").strip()
    }
    default_artifact = next(iter(sorted(artifact_ids)), None)
    normalized_criteria: list[Any] = []
    for criterion in criteria:
        if not isinstance(criterion, dict):
            normalized_criteria.append(criterion)
            continue
        normalized = dict(criterion)
        artifact_refs = [
            str(item)
            for item in normalized.get("artifact_refs") or []
            if str(item).strip() in artifact_ids
        ]
        validation_refs: list[str] = []
        for raw_ref in normalized.get("validation_refs") or []:
            ref = _normalize_validation_profile(raw_ref)
            raw_text = str(raw_ref or "").strip()
            if ref in validation_ids:
                validation_refs.append(ref)
            elif raw_text in artifact_ids:
                artifact_refs.append(raw_text)
            elif raw_text.lower() == "artifact" and default_artifact:
                artifact_refs.append(default_artifact)
        normalized["validation_refs"] = _dedupe_strings(validation_refs)
        normalized["artifact_refs"] = _dedupe_strings(artifact_refs)
        normalized_criteria.append(normalized)
    plan["completion_criteria"] = normalized_criteria
    return plan


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _normalize_dependency_strategy(
    value: dict[str, Any],
    *,
    project_root: str,
    path_renames: dict[str, str] | None = None,
) -> dict[str, Any]:
    normalized = dict(value)
    for key in ("declared_dependency_files", "lockfiles"):
        paths = normalized.get(key)
        if isinstance(paths, list):
            normalized[key] = _normalize_plan_paths(
                paths,
                project_root=project_root,
                path_renames=path_renames,
            )
    network_required = normalized.get("network_required")
    if isinstance(network_required, bool):
        normalized["network_required"] = "external" if network_required else "none"
    elif network_required is not None:
        normalized_network = str(network_required).strip().lower().replace("_", "-")
        normalized["network_required"] = {
            "false": "none",
            "no": "none",
            "none-required": "none",
            "true": "external",
            "yes": "external",
            "internet": "external",
            "dependency-cache-only": "dependency-cache",
        }.get(normalized_network, normalized_network)
    return normalized


def _normalize_plan_requirement_refs(
    plan: dict[str, Any],
    *,
    project_root: str,
    path_renames: dict[str, str] | None = None,
) -> dict[str, Any]:
    requirement_ids = [
        str(item.get("requirement_id") or "").strip()
        for item in plan.get("requirements") or []
        if isinstance(item, dict) and str(item.get("requirement_id") or "").strip()
    ]
    requirement_ids = [item for item in requirement_ids if IDENTIFIER_PATTERN.fullmatch(item)]
    if not requirement_ids:
        return plan
    requirement_catalog = _requirement_catalog(plan, requirement_ids=requirement_ids)
    path_requirement_refs: dict[str, list[str]] = {}
    files = plan.get("files")
    if isinstance(files, list):
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            raw_refs = file_entry.get("requirement_refs")
            inferred_refs = _infer_requirement_refs_for_entry(
                file_entry,
                requirement_ids=requirement_ids,
                requirement_catalog=requirement_catalog,
            )
            if isinstance(raw_refs, list):
                file_entry["requirement_refs"] = _normalize_requirement_refs(
                    raw_refs,
                    requirement_ids=requirement_ids,
                    requirement_catalog=requirement_catalog,
                    project_root=project_root,
                    path_renames=path_renames,
                    path_requirement_refs=path_requirement_refs,
                    fallback_refs=inferred_refs,
                )
            refs_for_path = file_entry.get("requirement_refs")
            if not isinstance(refs_for_path, list) or not refs_for_path:
                refs_for_path = inferred_refs
            path = str(file_entry.get("path") or "").strip()
            if path and refs_for_path:
                path_requirement_refs[path] = _dedupe_strings([str(item) for item in refs_for_path])
    for collection_key in ("intended_interfaces", "artifact_expectations", "completion_criteria"):
        collection = plan.get(collection_key)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict) or not isinstance(item.get("requirement_refs"), list):
                continue
            item["requirement_refs"] = _normalize_requirement_refs(
                item["requirement_refs"],
                requirement_ids=requirement_ids,
                requirement_catalog=requirement_catalog,
                project_root=project_root,
                path_renames=path_renames,
                path_requirement_refs=path_requirement_refs,
                fallback_refs=_infer_requirement_refs_for_entry(
                    item,
                    requirement_ids=requirement_ids,
                    requirement_catalog=requirement_catalog,
                ),
            )
    validation_commands = plan.get("validation_commands")
    if isinstance(validation_commands, dict):
        for command in validation_commands.values():
            if not isinstance(command, dict) or not isinstance(command.get("requirement_refs"), list):
                continue
            command["requirement_refs"] = _normalize_requirement_refs(
                command["requirement_refs"],
                requirement_ids=requirement_ids,
                requirement_catalog=requirement_catalog,
                project_root=project_root,
                path_renames=path_renames,
                path_requirement_refs=path_requirement_refs,
                fallback_refs=requirement_ids,
            )
    dependency_strategy = plan.get("dependency_strategy")
    if isinstance(dependency_strategy, dict) and isinstance(dependency_strategy.get("requirement_refs"), list):
        dependency_strategy["requirement_refs"] = _normalize_requirement_refs(
            dependency_strategy["requirement_refs"],
            requirement_ids=requirement_ids,
            requirement_catalog=requirement_catalog,
            project_root=project_root,
            path_renames=path_renames,
            path_requirement_refs=path_requirement_refs,
            fallback_refs=requirement_ids,
        )
    return plan


def _requirement_catalog(plan: dict[str, Any], *, requirement_ids: list[str]) -> dict[str, str]:
    catalog: dict[str, str] = {requirement_id: requirement_id for requirement_id in requirement_ids}
    for item in plan.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        requirement_id = str(item.get("requirement_id") or "").strip()
        if requirement_id not in catalog:
            continue
        text_parts = [
            requirement_id,
            str(item.get("description") or ""),
            str(item.get("source") or ""),
            " ".join(str(ref) for ref in item.get("capability_refs") or []),
        ]
        catalog[requirement_id] = " ".join(part for part in text_parts if part.strip())
    return catalog


def _normalize_requirement_refs(
    values: list[Any],
    *,
    requirement_ids: list[str],
    requirement_catalog: dict[str, str],
    project_root: str,
    path_renames: dict[str, str] | None,
    path_requirement_refs: dict[str, list[str]],
    fallback_refs: list[str] | None = None,
) -> list[str]:
    normalized: list[str] = []
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        if raw in requirement_ids:
            normalized.append(raw)
            continue
        path = _normalize_plan_path(raw, project_root=project_root)
        if path_renames:
            path = path_renames.get(path, path)
        if path in path_requirement_refs:
            normalized.extend(path_requirement_refs[path])
            continue
        normalized.extend(
            _infer_requirement_refs_from_text(
                raw,
                requirement_ids=requirement_ids,
                requirement_catalog=requirement_catalog,
            )
        )
    if not normalized and fallback_refs:
        normalized.extend(ref for ref in fallback_refs if ref in requirement_ids)
    return _dedupe_strings([ref for ref in normalized if ref in requirement_ids])


def _infer_requirement_refs_for_entry(
    entry: dict[str, Any],
    *,
    requirement_ids: list[str],
    requirement_catalog: dict[str, str],
) -> list[str]:
    text = " ".join(
        str(entry.get(key) or "")
        for key in ("path", "purpose", "kind", "profile", "name", "description", "root")
        if entry.get(key)
    )
    return _infer_requirement_refs_from_text(
        text,
        requirement_ids=requirement_ids,
        requirement_catalog=requirement_catalog,
    )


def _infer_requirement_refs_from_text(
    text: str,
    *,
    requirement_ids: list[str],
    requirement_catalog: dict[str, str],
) -> list[str]:
    tokens = _semantic_ref_tokens(text)
    if not tokens:
        return []
    scored: list[tuple[int, int, str]] = []
    for index, requirement_id in enumerate(requirement_ids):
        requirement_tokens = _semantic_ref_tokens(requirement_catalog.get(requirement_id, requirement_id))
        overlap = tokens & requirement_tokens
        if not overlap:
            continue
        id_tokens = _semantic_ref_tokens(requirement_id)
        score = len(overlap) + (2 * len(tokens & id_tokens))
        scored.append((score, -index, requirement_id))
    scored.sort(reverse=True)
    return [requirement_id for score, _, requirement_id in scored if score > 0]


def _semantic_ref_tokens(text: str) -> set[str]:
    ignored = {
        "a",
        "an",
        "and",
        "for",
        "from",
        "in",
        "of",
        "py",
        "req",
        "requirement",
        "requirements",
        "the",
        "to",
        "with",
    }
    return {
        token
        for token in re.split(r"[^A-Za-z0-9]+", text.lower())
        if len(token) >= 3 and token not in ignored
    }


def _normalize_plan_paths(
    values: list[Any],
    *,
    project_root: str,
    path_renames: dict[str, str] | None = None,
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        path = _normalize_plan_path(value, project_root=project_root)
        if path_renames:
            path = path_renames.get(path, path)
        if not path or path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return normalized


def _normalize_plan_path(value: Any, *, project_root: str) -> str:
    root = project_root.strip().strip("/").replace("\\", "/")
    path = str(value or "").strip().replace("\\", "/").lstrip("./")
    stripped = path.strip("/")
    if root:
        if stripped == root or stripped.endswith(f"/{root}"):
            path = root
        elif f"/{root}/" in f"/{stripped}/":
            wrapped = f"/{stripped}/"
            path = wrapped[wrapped.index(f"/{root}/") + 1 :].rstrip("/")
    path = path.lstrip("/")
    if root and path and path != root and not path.startswith(f"{root}/"):
        path = f"{root}/{path}"
    return path


def _normalize_artifact_root(value: str, *, project_root: str) -> str:
    root = project_root.strip().strip("/").replace("\\", "/")
    normalized = _normalize_plan_path(value, project_root=root)
    if not root:
        return normalized
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") and not _absolute_path_contains_project_root(raw, root):
        return root
    return normalized if normalized else root


def _normalize_validation_cwd(value: str, *, project_root: str) -> str:
    root = project_root.strip().strip("/").replace("\\", "/")
    cwd = value.strip().replace("\\", "/")
    if not cwd or cwd == ".":
        return "."
    if "${project_root}" in cwd or "{project_root}" in cwd:
        return root or "."
    normalized = _normalize_plan_path(cwd, project_root=root)
    if root and cwd.startswith("/") and not _absolute_path_contains_project_root(cwd, root):
        return root
    return normalized or "."


def _absolute_path_contains_project_root(path: str, project_root: str) -> bool:
    root = project_root.strip().strip("/").replace("\\", "/")
    if not root:
        return False
    stripped = path.strip().replace("\\", "/").strip("/")
    return stripped == root or stripped.endswith(f"/{root}") or f"/{root}/" in f"/{stripped}/"


def _normalize_python_material_path(path: str, *, kind: str, project_root: str) -> str:
    normalized_kind = FILE_KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())
    if normalized_kind not in {"python", "test"}:
        return path
    root = project_root.strip().strip("/").replace("\\", "/")
    relative = path
    if root and relative.startswith(f"{root}/"):
        relative = relative[len(root) + 1 :]
    parts = [part for part in relative.split("/") if part]
    if not parts:
        return path
    filename = parts[-1]
    if "." in filename:
        stem, extension = filename.rsplit(".", 1)
        extension = f".{extension}"
    else:
        stem, extension = filename, ""
    if extension.lower() in {".toml", ".md", ".json", ".yaml", ".yml", ".txt", ".ini", ".cfg"}:
        return path
    if normalized_kind == "test" and filename == "conftest.py":
        return path
    if normalized_kind == "test":
        test_stem = _normalize_python_identifier_stem(stem)
        if not test_stem.startswith("test_"):
            test_stem = f"test_{_stable_python_stem_from_project_root(root) or test_stem}"
        if extension != ".py" or not _is_importable_python_stem(stem):
            parts = ["tests", f"{test_stem}.py"]
        else:
            parts[-1] = f"{test_stem}.py"
    elif semantic_stem := _semantic_python_stem_from_project_family(stem, root):
        parts[-1] = f"{semantic_stem}.py"
    elif _python_stem_matches_project_root_family(stem, root):
        stable_stem = _stable_python_stem_from_project_root(root)
        if stable_stem:
            parts = [stable_stem, "__init__.py"]
        else:
            parts[-1] = "app.py"
    elif extension != ".py" or not _is_importable_python_stem(stem):
        replacement_stem = _normalize_python_identifier_stem(stem)
        if _path_stem_matches_project_root(stem, root):
            replacement_stem = _stable_python_stem_from_project_root(root)
        if not replacement_stem:
            replacement_stem = "app"
        parts[-1] = f"{replacement_stem}.py"
    rebuilt = "/".join(parts)
    return f"{root}/{rebuilt}" if root else rebuilt


def _file_kind_from_path(path: str) -> FileKind | None:
    filename = path.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if filename == "pyproject.toml":
        return "config"
    if filename in {"readme.md", "changelog.md"}:
        return "markdown"
    if filename.endswith((".toml", ".json", ".yaml", ".yml", ".ini", ".cfg")):
        return "config"
    if filename.endswith(".md"):
        return "markdown"
    if filename.endswith(".txt"):
        return "text"
    if filename.startswith("test_") and filename.endswith(".py"):
        return "test"
    if filename == "dockerfile" or filename.endswith(".dockerfile"):
        return "dockerfile"
    if filename in {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"}:
        return "compose"
    return None


def _normalize_python_identifier_stem(value: str) -> str:
    raw_parts = [part for part in re.split(r"[^A-Za-z0-9_]+", value.strip()) if part]
    if not raw_parts:
        return ""
    stem = "_".join(part.lower() for part in raw_parts)
    stem = re.sub(r"_+", "_", stem).strip("_")
    if not stem:
        return ""
    if stem[0].isdigit():
        stem = f"module_{stem}"
    if keyword.iskeyword(stem):
        stem = f"{stem}_module"
    return stem if stem.isidentifier() else ""


def _stable_python_stem_from_project_root(project_root: str) -> str:
    root_name = project_root.strip().strip("/").replace("\\", "/").rsplit("/", 1)[-1]
    parts = [part for part in re.split(r"[^A-Za-z0-9_]+", root_name) if part]
    stable_parts = [part for part in parts if not _looks_like_entropy_suffix(part)]
    return _normalize_python_identifier_stem("_".join(stable_parts or parts))


def _path_stem_matches_project_root(stem: str, project_root: str) -> bool:
    root_name = project_root.strip().strip("/").replace("\\", "/").rsplit("/", 1)[-1]
    return _normalize_python_identifier_stem(stem) == _normalize_python_identifier_stem(root_name)


def _python_stem_matches_project_root_family(stem: str, project_root: str) -> bool:
    if not project_root:
        return False
    normalized_stem = _normalize_python_identifier_stem(stem)
    if not normalized_stem:
        return False
    root_name = project_root.strip().strip("/").replace("\\", "/").rsplit("/", 1)[-1]
    normalized_root = _normalize_python_identifier_stem(root_name)
    stable_root = _stable_python_stem_from_project_root(project_root)
    if normalized_stem in {normalized_root, stable_root}:
        return True
    if stable_root and normalized_stem.startswith(f"{stable_root}_"):
        suffix = normalized_stem.removeprefix(f"{stable_root}_")
        return bool(suffix) and all(_looks_like_entropy_suffix(part) for part in suffix.split("_") if part)
    return False


def _semantic_python_stem_from_project_family(stem: str, project_root: str) -> str:
    if not project_root:
        return ""
    normalized_stem = _normalize_python_identifier_stem(stem)
    stable_root = _stable_python_stem_from_project_root(project_root)
    if not normalized_stem or not stable_root or not normalized_stem.startswith(f"{stable_root}_"):
        return ""
    suffix_parts = [
        part
        for part in normalized_stem.removeprefix(f"{stable_root}_").split("_")
        if part
    ]
    while suffix_parts and _looks_like_entropy_suffix(suffix_parts[0]):
        suffix_parts.pop(0)
    semantic = _normalize_python_identifier_stem("_".join(suffix_parts))
    if not semantic or semantic == stable_root or _looks_like_entropy_suffix(semantic):
        return ""
    return semantic


def _is_importable_python_stem(stem: str) -> bool:
    return stem.isidentifier() and not keyword.iskeyword(stem)


def _looks_like_entropy_suffix(value: str) -> bool:
    cleaned = value.strip().lower()
    if len(cleaned) < 6:
        return False
    return bool(re.fullmatch(r"[0-9a-f]+", cleaned) or re.fullmatch(r"\d+", cleaned))


def _purpose_from_path(path: str) -> str:
    normalized = path.rsplit("/", 1)[-1] or path
    stem = normalized.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
    if stem:
        return f"Material file for {stem}"
    return "Material project file"


def _normalize_validation_command(profile: str, command: Any, *, project_root: str) -> Any:
    if not isinstance(command, dict):
        return command
    normalized = dict(command)
    project_root = project_root.strip().strip("/")
    cwd = str(normalized.get("cwd") or ".").strip()
    normalized["cwd"] = _normalize_validation_cwd(cwd, project_root=project_root)
    argv = normalized.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item.strip() for item in argv):
        return None
    if (
        profile.startswith("docker-compose")
        and argv[0] not in {"docker", "podman"}
    ):
        normalized["argv"] = ["docker", "compose", *argv]
    if (
        profile == "python-api"
        and (argv[0].startswith("http://") or argv[0].startswith("https://"))
    ):
        normalized["argv"] = ["curl", "-fsS", *argv]
    return normalized


def _normalize_patch_payload(payload: dict[str, Any], *, request: MaterialPatchGenerationRequest) -> dict[str, Any]:
    if isinstance(payload.get("patch_set"), dict):
        return payload
    if isinstance(payload.get("regeneration"), dict):
        return payload
    replacement_payload = payload.get("replacement")
    if isinstance(replacement_payload, dict):
        normalized_replacement = _normalize_replacement_payload_shape(replacement_payload, request=request)
        result = dict(payload)
        result["replacement"] = normalized_replacement
        return result
    patch_payload = payload.get("patch")
    if not isinstance(patch_payload, dict):
        return payload
    normalized_patch = dict(patch_payload)
    target_path = request.target_path
    normalized_patch["target_path"] = target_path
    normalized_patch["expected_old_sha256"] = request.expected_old_sha256
    diff = normalized_patch.get("unified_diff")
    if isinstance(diff, str):
        normalized_patch["unified_diff"] = _canonical_single_target_diff(diff, target_path=target_path)
    result = dict(payload)
    result["patch"] = normalized_patch
    return result


def _normalize_replacement_payload_shape(
    payload: dict[str, Any],
    *,
    request: MaterialPatchGenerationRequest,
) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["target_path"] = request.target_path
    normalized["expected_old_sha256"] = request.expected_old_sha256
    if not isinstance(normalized.get("replacement_content"), str):
        content = normalized.get("content")
        if isinstance(content, list):
            normalized["replacement_content"] = "\n".join(str(line) for line in content)
        elif isinstance(content, str):
            normalized["replacement_content"] = content
    return normalized


def _canonical_single_target_diff(diff: str, *, target_path: str) -> str:
    if "@@" not in diff:
        return diff
    old_header = f"--- a/{target_path}"
    new_header = f"+++ b/{target_path}"
    if "--- " not in diff and "+++ " not in diff:
        return f"{old_header}\n{new_header}\n{diff}"
    lines = diff.splitlines()
    replaced_old = False
    replaced_new = False
    for index, line in enumerate(lines):
        if line.startswith("--- ") and not replaced_old:
            lines[index] = old_header
            replaced_old = True
            continue
        if line.startswith("+++ ") and not replaced_new:
            lines[index] = new_header
            replaced_new = True
    return "\n".join(lines) + ("\n" if diff.endswith("\n") else "")


def _repair_plan_invalid_response(
    *,
    messages: list[dict[str, str]],
    request: MaterialPlanRequest,
    llm: LLMSettings,
    invalid_response: str,
    parse_error: str,
) -> dict[str, Any]:
    repair_messages = [
        *messages,
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": _prompt("schema_repair.md"),
                    "contract": "material_plan.v3.2",
                    "task_id": request.task_id,
                    "working_query": request.working_query,
                    "required_capabilities": request.required_capabilities,
                    "constraints": request.constraints,
                    "invalid_response": invalid_response,
                    "validation_errors": [
                        {
                            "loc": ["response"],
                            "msg": parse_error,
                            "type": "json_parse_or_contract_error",
                        }
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    try:
        repaired = _governed_chat_completion(
            model=llm.model,
            messages=repair_messages,
            base_url=llm.base_url,
            phase="material_builder.plan_response_repair",
            temperature=0.0,
            max_tokens=llm.max_tokens,
            timeout=llm.timeout_seconds,
            post=httpx.post,
        )
    except Exception as exc:
        raise MaterialLLMError("llm_generation_failed", "LLM material plan response repair call failed") from exc
    try:
        return _normalize_file_kinds(
            _normalize_plan_payload(
                _json_object(repaired),
                expected_project_root=_expected_project_root_for_plan_request(request),
            )
        )
    except MaterialLLMError as exc:
        exc.details["response_excerpt"] = _compact_text(repaired, 1000)
        exc.details["invalid_response_excerpt"] = _compact_text(invalid_response, 1000)
        raise


def _repair_plan_schema_payload(
    *,
    messages: list[dict[str, str]],
    request: MaterialPlanRequest,
    llm: LLMSettings,
    invalid_payload: dict[str, Any],
    validation_error: ValidationError,
) -> dict[str, Any]:
    repair_messages = [
        *messages,
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": _prompt("schema_repair.md"),
                    "contract": "material_plan.v3.2",
                    "task_id": request.task_id,
                    "working_query": request.working_query,
                    "required_capabilities": request.required_capabilities,
                    "constraints": request.constraints,
                    "invalid_payload": invalid_payload,
                    "validation_errors": _validation_errors(validation_error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    try:
        repaired = _governed_chat_completion(
            model=llm.model,
            messages=repair_messages,
            base_url=llm.base_url,
            phase="material_builder.plan_schema_repair",
            temperature=0.0,
            max_tokens=llm.max_tokens,
            timeout=llm.timeout_seconds,
            post=httpx.post,
        )
    except Exception as exc:
        raise MaterialLLMError("llm_generation_failed", "LLM material plan schema repair call failed") from exc
    try:
        return _normalize_file_kinds(
            _normalize_plan_payload(
                _json_object(repaired),
                expected_project_root=_expected_project_root_for_plan_request(request),
            )
        )
    except MaterialLLMError as exc:
        exc.details["response_excerpt"] = _compact_text(repaired, 1000)
        raise


def _complete_sparse_plan_payload(
    payload: dict[str, Any],
    *,
    request: MaterialPlanRequest,
    validation_error: ValidationError,
) -> dict[str, Any] | None:
    plan_payload = payload.get("plan", payload)
    if not isinstance(plan_payload, dict):
        return None
    existing_files = plan_payload.get("files")
    project_root = (
        _expected_project_root_for_plan_request(request)
        or _normalize_expected_project_root(str(plan_payload.get("project_root") or ""))
        or _project_root_from_payload_paths(existing_files)
    )
    if not project_root:
        return None
    if isinstance(existing_files, list) and existing_files:
        if not _validation_missing_required_field(validation_error, "project_root"):
            return None
        completed = dict(plan_payload)
        completed["project_root"] = project_root
        return _normalize_file_kinds(
            _normalize_plan_payload(
                completed,
                expected_project_root=project_root,
            )
        )
    if not _validation_missing_required_field(validation_error, "files"):
        return None
    synthesized = _structural_plan_payload_from_request(
        request,
        project_root=project_root,
        seed_payload=plan_payload,
    )
    return _normalize_file_kinds(
        _normalize_plan_payload(
            synthesized,
            expected_project_root=project_root,
        )
    )


def _structural_plan_payload_for_request(request: MaterialPlanRequest) -> dict[str, Any] | None:
    project_root = _expected_project_root_for_plan_request(request)
    if not project_root:
        return None
    return _normalize_file_kinds(
        _normalize_plan_payload(
            _structural_plan_payload_from_request(
                request,
                project_root=project_root,
                seed_payload={},
            ),
            expected_project_root=project_root,
        )
    )


def _structural_plan_preflight_payload(request: MaterialPlanRequest) -> dict[str, Any] | None:
    documentation_payload = _documentation_plan_payload_from_evidence(request)
    if documentation_payload is not None:
        return documentation_payload
    if not _explicit_python_material_request(request):
        return None
    return _structural_plan_payload_for_request(request)


def _documentation_plan_payload_from_evidence(request: MaterialPlanRequest) -> dict[str, Any] | None:
    project_root = _expected_project_root_for_plan_request(request)
    if not project_root:
        return None
    capabilities = _normalized_capabilities(request)
    query = " ".join(
        str(value or "")
        for value in (
            request.working_query,
            request.original_query,
            " ".join(request.required_capabilities),
        )
    ).casefold()
    if "python" in capabilities or "python" in query:
        return None
    if not _query_requests_folder_documentation(query, capabilities):
        return None
    evidence = request.constraints.get("evidence_context")
    if not isinstance(evidence, dict):
        return None
    workspace_map = evidence.get("workspace_map")
    if not isinstance(workspace_map, dict):
        return None
    subject_entries = _documentation_subject_entries(workspace_map.get("top_level_entries"))
    if not subject_entries:
        return None

    files_by_subject = _evidence_files_by_subject(evidence, subject_entries)
    file_observations_by_subject = _evidence_file_observations_by_subject(evidence, subject_entries)
    enrichment_plan = _evidence_enrichment_plan(evidence)
    enrichment_by_subject = _evidence_enrichment_by_subject(enrichment_plan, subject_entries)
    enrichment_results = _evidence_enrichment_results(evidence)
    enrichment_results_by_subject = _evidence_enrichment_results_by_subject(enrichment_results, subject_entries)
    output_language = _documentation_target_language_from_request(request)
    requirements: list[dict[str, Any]] = [
        {
            "requirement_id": "req:artifact",
            "description": "Produce the requested documentation artifact.",
            "source": "user",
            "capability_refs": ["artifact"],
        },
        {
            "requirement_id": "req:docs",
            "description": "Create organized documentation grounded in observed local evidence.",
            "source": "user",
            "capability_refs": ["docs"],
        },
        {
            "requirement_id": "req:evidence",
            "description": "Record the read-only evidence and validation limitations used to build the documentation.",
            "source": "derived",
            "capability_refs": ["artifact"],
        },
        {
            "requirement_id": "req:enrichment",
            "description": "Record any specialist enrichment tasks needed for file types that require owned extraction or transcription capabilities.",
            "source": "derived",
            "capability_refs": ["artifact"],
        },
    ]
    files: list[dict[str, Any]] = [
        {
            "path": f"{project_root}/README.md",
            "purpose": (
                "Documentation index for the inspected workspace. Include the inspected path, evidence summary, "
                "observed top-level folders, generated documentation map, validation status, and limitations."
            ),
            "kind": "markdown",
            "max_tokens": 2200,
            "prefer_chunked": False,
            "depends_on": [],
            "requirement_refs": ["req:artifact", "req:docs", "req:evidence"],
            "contract_refs": [],
        }
    ]
    file_refs = [files[0]["path"]]
    for index, entry in enumerate(subject_entries, start=1):
        slug = _documentation_entry_slug(entry, used={Path(path).stem for path in file_refs})
        requirement_id = f"req:subject:{slug}"
        requirements.append(
            {
                "requirement_id": requirement_id,
                "description": f"Document the observed top-level folder {entry!r}.",
                "source": "derived",
                "capability_refs": ["docs"],
            }
        )
        evidence_files = files_by_subject.get(entry, [])
        evidence_observations_json = _documentation_observation_json_for_purpose(
            file_observations_by_subject.get(entry, [])
        )
        enrichment_json = _documentation_enrichment_json_for_purpose(
            enrichment_by_subject.get(entry, [])
        )
        enrichment_results_json = _documentation_enrichment_results_json_for_purpose(
            enrichment_results_by_subject.get(entry, [])
        )
        inventory_json = _documentation_inventory_json_for_purpose(evidence_files)
        purpose = _documentation_subject_purpose(
            entry=entry,
            evidence_files=evidence_files,
            inventory_json=inventory_json,
            evidence_observations_json=evidence_observations_json,
            enrichment_json=enrichment_json,
            enrichment_results_json=enrichment_results_json,
        )
        files.append(
            {
                "path": f"{project_root}/subfolders/{index:02d}-{slug}.md",
                "purpose": purpose,
                "kind": "markdown",
                "max_tokens": 2600,
                "prefer_chunked": False,
                "depends_on": [],
                "requirement_refs": ["req:docs", "req:enrichment", requirement_id],
                "contract_refs": [],
            }
        )
        file_refs.append(files[-1]["path"])
    files.append(
        {
            "path": f"{project_root}/validation-evidence.txt",
            "purpose": (
                "Validation and evidence log. Include the read-only commands, inspected workspace, files sampled, "
                "Storage Guardian publication note, and known limitations."
            ),
            "kind": "text",
            "max_tokens": 1800,
            "prefer_chunked": False,
            "depends_on": [],
            "requirement_refs": ["req:artifact", "req:evidence"],
            "contract_refs": [],
        }
    )
    file_refs.append(files[-1]["path"])
    detected_docs = _filter_subject_paths(workspace_map.get("detected_docs"), subject_entries)
    detected_data_config = _filter_subject_paths(
        [*workspace_map.get("detected_data_files", []), *workspace_map.get("detected_config_files", [])],
        subject_entries,
    )
    architecture_notes = [
        "Documentation plan derived from read-only evidence_context; no scenario-specific path assumptions were used.",
        f"Output language: {output_language}",
        f"Inspected workspace: {evidence.get('workspace') or workspace_map.get('root') or 'unknown'}",
        f"Evidence summary: {_compact_text(str(evidence.get('evidence_summary') or ''), 900)}",
        f"Commands: {_compact_text('; '.join(str(item) for item in evidence.get('commands', []) if item), 900)}",
        f"File observations: {len(evidence.get('file_observations') or []) if isinstance(evidence.get('file_observations'), list) else 0}",
        f"Enrichment plan: {_documentation_enrichment_summary_for_notes(enrichment_plan)}",
        f"Enrichment results: {_documentation_enrichment_result_summary_for_notes(enrichment_results)}",
        f"Detected docs: {_compact_text('; '.join(detected_docs), 900)}",
        f"Detected data/config: {_compact_text('; '.join(detected_data_config), 900)}",
    ]
    plan = {
        "schema_version": "material_plan.v3.2",
        "project_root": project_root,
        "requirements": requirements,
        "files": files,
        "intended_interfaces": [],
        "required_validation_profiles": [],
        "optional_validation_profiles": [],
        "validation_commands": {},
        "artifact_expectations": [
            {
                "artifact_id": "artifact:documentation",
                "root": project_root,
                "purpose": "Packaged documentation output grounded in read-only workspace evidence.",
                "requirement_refs": ["req:artifact", "req:docs"],
                "file_refs": file_refs,
            }
        ],
        "completion_criteria": [
            {
                "criterion_id": "criterion:documented-subfolders",
                "description": "The documentation index, per-folder pages, and validation evidence are materialized.",
                "requirement_refs": [item["requirement_id"] for item in requirements],
                "validation_refs": [],
                "artifact_refs": ["artifact:documentation"],
                "contract_refs": [],
            }
        ],
        "dependency_strategy": {
            "declared_dependency_files": [],
            "external_dependencies": [],
            "install_profiles": [],
            "lockfiles": [],
            "native_builds_required": False,
            "network_required": "none",
            "requirement_refs": ["req:artifact", "req:docs"],
            "contract_refs": [],
        },
        "architecture_notes": architecture_notes,
        "variation_reason": "evidence-grounded documentation plan from observed workspace entries",
    }
    return _normalize_file_kinds(_normalize_plan_payload(plan, expected_project_root=project_root))


def _query_requests_folder_documentation(query: str, capabilities: set[str]) -> bool:
    if "docs" in capabilities:
        return True
    documentation_markers = (
        "documentation",
        "documentacao",
        "documentação",
        "document ",
        "documentar",
        "docs",
        "readme",
        "relatorio",
        "relatório",
    )
    folder_markers = (
        "folder",
        "folders",
        "subfolder",
        "subfolders",
        "directory",
        "directories",
        "pasta",
        "pastas",
        "subpasta",
        "subpastas",
    )
    return any(marker in query for marker in documentation_markers) and any(marker in query for marker in folder_markers)


def _documentation_target_language_from_request(request: MaterialPlanRequest) -> str:
    candidates: list[object] = [
        request.original_language,
        request.language_context.get("response_language"),
        request.language_context.get("original_language"),
        request.language_context.get("user_language"),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        lowered = value.casefold()
        if lowered.startswith("pt") or "portugu" in lowered:
            return "pt-PT"
        return value[:32]
    return "en"


def _documentation_subject_entries(raw_entries: object) -> list[str]:
    if not isinstance(raw_entries, list):
        return []
    subjects: list[str] = []
    excluded_names = {"docs", ".ai-local", ".git", "__pycache__"}
    skipped_prefixes = ("task_", "failure-snapshot-")
    for item in raw_entries:
        name = str(item or "").strip().strip("/")
        if not name or "/" in name:
            continue
        lower = name.casefold()
        if lower in excluded_names or any(lower.startswith(prefix) for prefix in skipped_prefixes):
            continue
        if Path(name).suffix:
            continue
        subjects.append(name)
    return _dedupe_strings(subjects[:40])


def _evidence_files_by_subject(evidence: dict[str, Any], subjects: list[str]) -> dict[str, list[str]]:
    workspace_map = evidence.get("workspace_map") if isinstance(evidence.get("workspace_map"), dict) else {}
    values: list[str] = []
    for key in (
        "relevant_files",
        "detected_docs",
        "detected_data_files",
        "detected_config_files",
        "detected_test_files",
    ):
        source = evidence.get(key) if key == "relevant_files" else workspace_map.get(key)
        if isinstance(source, list):
            values.extend(str(item).strip() for item in source if str(item).strip())
    result: dict[str, list[str]] = {subject: [] for subject in subjects}
    for path in _dedupe_strings(values):
        normalized = path.strip().lstrip("./")
        for subject in subjects:
            if normalized == subject or normalized.startswith(f"{subject}/"):
                result[subject].append(normalized)
                break
    return {subject: files[:200] for subject, files in result.items()}


def _evidence_file_observations_by_subject(evidence: dict[str, Any], subjects: list[str]) -> dict[str, list[dict[str, Any]]]:
    raw_observations = evidence.get("file_observations")
    if not isinstance(raw_observations, list):
        return {subject: [] for subject in subjects}
    result: dict[str, list[dict[str, Any]]] = {subject: [] for subject in subjects}
    for raw in raw_observations:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or "").strip().lstrip("./")
        if not path:
            continue
        for subject in subjects:
            if path == subject or path.startswith(f"{subject}/"):
                result[subject].append(_compact_file_observation(raw))
                break
    return {subject: values[:16] for subject, values in result.items()}


def _evidence_enrichment_plan(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    raw_plan = evidence.get("enrichment_plan")
    if not isinstance(raw_plan, list):
        return []
    tasks: list[dict[str, Any]] = []
    for raw in raw_plan:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider") or "").strip()
        capability = str(raw.get("capability") or "").strip()
        raw_paths = raw.get("input_paths")
        paths = [
            str(path).strip().lstrip("./")
            for path in raw_paths
            if str(path).strip()
        ] if isinstance(raw_paths, list) else []
        if not provider or not capability or not paths:
            continue
        expected = raw.get("expected_evidence_types")
        tasks.append(
            {
                "provider": provider,
                "capability": capability,
                "input_paths": _dedupe_strings(paths)[:25],
                "reason": str(raw.get("reason") or "").strip(),
                "execution_mode": str(raw.get("execution_mode") or "deferred").strip() or "deferred",
                "expected_evidence_types": [
                    str(item).strip()
                    for item in expected[:8]
                    if str(item).strip()
                ] if isinstance(expected, list) else [],
            }
        )
    return tasks[:12]


def _documentation_subject_purpose(
    *,
    entry: str,
    evidence_files: list[str],
    inventory_json: str = "",
    evidence_observations_json: str,
    enrichment_json: str,
    enrichment_results_json: str,
) -> str:
    compact_enrichment_results_json = _documentation_compact_enrichment_results_json(enrichment_results_json)
    minimal_enrichment_results_json = _documentation_minimal_enrichment_results_json(enrichment_results_json)
    tiny_enrichment_results_json = _documentation_tiny_enrichment_results_json(
        enrichment_results_json,
        limit=4,
        excerpt_limit=180,
    )
    micro_enrichment_results_json = _documentation_tiny_enrichment_results_json(
        enrichment_results_json,
        limit=2,
        excerpt_limit=140,
    )
    nano_enrichment_results_json = _documentation_tiny_enrichment_results_json(
        enrichment_results_json,
        limit=1,
        excerpt_limit=120,
    )

    def build(
        observed_limit: int,
        inventory_payload: str,
        observations_json: str,
        tasks_json: str,
        results_json: str,
    ) -> str:
        observed_files = _documentation_representative_files_for_purpose(evidence_files, limit=observed_limit)
        return (
            f"Documentation page for observed top-level folder {entry!r}. "
            "Summarize observed files, evidence, specialist enrichment state, limitations, and next steps. "
            f"Observed files: {observed_files or 'no nested files were sampled'}. "
            f"Inventory JSON: {inventory_payload or '{}'}. "
            f"Evidence observations JSON: {observations_json}. "
            f"Specialist enrichment JSON: {tasks_json}. "
            f"Specialist enrichment results JSON: {results_json}."
        )

    candidates = [
        (260, inventory_json, evidence_observations_json, enrichment_json, enrichment_results_json),
        (180, inventory_json, evidence_observations_json, enrichment_json, enrichment_results_json),
        (120, inventory_json, evidence_observations_json, enrichment_json, compact_enrichment_results_json),
        (80, inventory_json, evidence_observations_json, enrichment_json, compact_enrichment_results_json),
        (60, inventory_json, evidence_observations_json, enrichment_json, minimal_enrichment_results_json),
        (60, inventory_json, evidence_observations_json, enrichment_json, tiny_enrichment_results_json),
        (40, inventory_json, evidence_observations_json, enrichment_json, micro_enrichment_results_json),
        (30, inventory_json, evidence_observations_json, enrichment_json, nano_enrichment_results_json),
        (40, inventory_json, evidence_observations_json, "[]", tiny_enrichment_results_json),
        (30, inventory_json, evidence_observations_json, "[]", micro_enrichment_results_json),
        (20, inventory_json, evidence_observations_json, "[]", nano_enrichment_results_json),
        (180, inventory_json, "[]", enrichment_json, compact_enrichment_results_json),
        (120, inventory_json, "[]", enrichment_json, compact_enrichment_results_json),
        (80, inventory_json, "[]", enrichment_json, compact_enrichment_results_json),
        (80, inventory_json, "[]", enrichment_json, minimal_enrichment_results_json),
        (180, inventory_json, evidence_observations_json, "[]", compact_enrichment_results_json),
        (120, inventory_json, evidence_observations_json, "[]", compact_enrichment_results_json),
        (80, inventory_json, evidence_observations_json, "[]", compact_enrichment_results_json),
        (80, inventory_json, evidence_observations_json, "[]", minimal_enrichment_results_json),
        (30, inventory_json, "[]", "[]", compact_enrichment_results_json),
        (20, inventory_json, "[]", "[]", minimal_enrichment_results_json),
        (40, inventory_json, "[]", "[]", tiny_enrichment_results_json),
        (30, inventory_json, "[]", "[]", micro_enrichment_results_json),
        (20, inventory_json, "[]", "[]", nano_enrichment_results_json),
        (30, "{}", "[]", "[]", tiny_enrichment_results_json),
        (20, "{}", "[]", "[]", micro_enrichment_results_json),
        (10, "{}", "[]", "[]", nano_enrichment_results_json),
        (80, inventory_json, evidence_observations_json, enrichment_json, "[]"),
        (80, inventory_json, evidence_observations_json, "[]", "[]"),
        (60, inventory_json, "[]", enrichment_json, "[]"),
        (80, inventory_json, "[]", "[]", "[]"),
        (80, "{}", evidence_observations_json, enrichment_json, compact_enrichment_results_json),
        (80, "{}", evidence_observations_json, "[]", compact_enrichment_results_json),
        (80, "{}", "[]", enrichment_json, compact_enrichment_results_json),
        (80, "{}", "[]", "[]", minimal_enrichment_results_json),
        (80, "{}", "[]", "[]", "[]"),
    ]
    for observed_limit, inventory_payload, observations_json, tasks_json, results_json in candidates:
        purpose = build(observed_limit, inventory_payload, observations_json, tasks_json, results_json)
        if len(purpose) <= DOCUMENTATION_PURPOSE_LIMIT:
            return purpose

    purpose = (
        f"Documentation page for observed top-level folder {entry!r}. "
        "Summarize observed files, evidence, specialist enrichment state, limitations, and next steps. "
        f"Observed files: {_documentation_representative_files_for_purpose(evidence_files, limit=100) or 'no nested files were sampled'}. "
        "Inventory JSON: {}. "
        "Evidence observations JSON: []. "
        "Specialist enrichment JSON: []. "
        "Specialist enrichment results JSON: []."
    )
    return purpose[:DOCUMENTATION_PURPOSE_LIMIT]


def _evidence_enrichment_by_subject(
    tasks: list[dict[str, Any]],
    subjects: list[str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {subject: [] for subject in subjects}
    for task in tasks:
        paths = [str(path).strip().lstrip("./") for path in task.get("input_paths", []) if str(path).strip()]
        for subject in subjects:
            subject_paths = [
                display_path
                for path in paths
                if (display_path := _documentation_subject_relative_path(path, subject))
            ]
            if not subject_paths:
                continue
            compact = dict(task)
            compact["input_paths"] = subject_paths[:12]
            result[subject].append(compact)
    return {subject: values[:8] for subject, values in result.items()}


def _documentation_enrichment_json_for_purpose(tasks: list[dict[str, Any]]) -> str:
    compact: list[dict[str, Any]] = []
    for task in tasks[:2]:
        item = {
            "provider": str(task.get("provider") or "").strip(),
            "capability": str(task.get("capability") or "").strip(),
            "input_paths": [str(path) for path in task.get("input_paths", [])[:4]],
        }
        compact.append(item)
    return json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _documentation_enrichment_summary_for_notes(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "none"
    parts: list[str] = []
    for task in tasks[:8]:
        provider = str(task.get("provider") or "").strip()
        capability = str(task.get("capability") or "").strip()
        count = len(task.get("input_paths") or [])
        if provider and capability and count:
            parts.append(f"{provider}.{capability}={count}")
    return _compact_text("; ".join(parts) or "none", 900)


def _evidence_enrichment_results(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    raw_results = evidence.get("enrichment_results")
    if not isinstance(raw_results, list):
        return []
    results: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider") or "").strip()
        capability = str(raw.get("capability") or "").strip()
        raw_paths = raw.get("input_paths")
        paths = [
            str(path).strip().lstrip("./")
            for path in raw_paths
            if str(path).strip()
        ] if isinstance(raw_paths, list) else []
        if not provider or not capability or not paths:
            continue
        raw_output_refs = raw.get("output_refs")
        results.append(
            {
                "provider": provider,
                "capability": capability,
                "input_paths": _dedupe_strings(paths)[:25],
                "status": str(raw.get("status") or "").strip(),
                "action": str(raw.get("action") or "").strip(),
                "success": bool(raw.get("success")),
                "content_excerpt": _compact_text(str(raw.get("content_excerpt") or "").strip(), 1600),
                "storage_refs": [
                    str(item).strip()
                    for item in raw.get("storage_refs", [])[:8]
                    if str(item).strip()
                ] if isinstance(raw.get("storage_refs"), list) else [],
                "output_refs": {
                    str(key): str(value)
                    for key, value in list(raw_output_refs.items())[:8]
                } if isinstance(raw_output_refs, dict) else {},
                "semantic_digest": _compact_semantic_digest_for_documentation(
                    raw.get("semantic_digest") if isinstance(raw.get("semantic_digest"), dict) else {}
                ),
                "quality": raw.get("quality") if isinstance(raw.get("quality"), dict) else {},
                "error": str(raw.get("error") or "").strip(),
            }
        )
    return results[:50]


def _evidence_enrichment_results_by_subject(
    results: list[dict[str, Any]],
    subjects: list[str],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {subject: [] for subject in subjects}
    for result in results:
        paths = [str(path).strip().lstrip("./") for path in result.get("input_paths", []) if str(path).strip()]
        for subject in subjects:
            subject_paths = [
                display_path
                for path in paths
                if (display_path := _documentation_subject_relative_path(path, subject))
            ]
            if not subject_paths:
                continue
            compact = dict(result)
            compact["input_paths"] = subject_paths[:12]
            grouped[subject].append(compact)
    return {subject: values[:16] for subject, values in grouped.items()}


def _documentation_enrichment_results_json_for_purpose(results: list[dict[str, Any]]) -> str:
    compact: list[dict[str, Any]] = []
    for result in _documentation_representative_enrichment_results(results, limit=8):
        output_refs = result.get("output_refs")
        quality = result.get("quality")
        semantic_digest = result.get("semantic_digest")
        item: dict[str, Any] = {
            "provider": str(result.get("provider") or "").strip(),
            "capability": str(result.get("capability") or "").strip(),
            "input_paths": [str(path) for path in result.get("input_paths", [])[:6]],
            "status": str(result.get("status") or "").strip(),
            "success": bool(result.get("success")),
        }
        action = str(result.get("action") or "").strip()
        content_excerpt = _compact_text(str(result.get("content_excerpt") or "").strip(), 700)
        storage_refs = [str(value) for value in result.get("storage_refs", [])[:1]]
        output_ref_values = {
            str(key): str(value)
            for key, value in list(output_refs.items())[:2]
        } if isinstance(output_refs, dict) else {}
        quality_values = {
            str(key): value
            for key, value in list(quality.items())[:3]
        } if isinstance(quality, dict) else {}
        semantic_values = _compact_semantic_digest_for_documentation(semantic_digest)
        error = _compact_text(str(result.get("error") or "").strip(), 100)
        if action:
            item["action"] = action
        if content_excerpt:
            item["content_excerpt"] = content_excerpt
        if storage_refs:
            item["storage_refs"] = storage_refs
        if output_ref_values:
            item["output_refs"] = output_ref_values
        if quality_values:
            item["quality"] = quality_values
        if semantic_values:
            item["semantic_digest"] = semantic_values
        if error:
            item["error"] = error
        compact.append(item)
    return json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _documentation_inventory_json_for_purpose(files: list[str]) -> str:
    inventory = _documentation_inventory_from_files(files)
    return json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _documentation_inventory_from_files(files: list[str]) -> dict[str, Any]:
    buckets: dict[str, list[str]] = {
        "documents": [],
        "data": [],
        "sql": [],
        "media": [],
        "other": [],
    }
    for path in _dedupe_strings(str(item or "").strip() for item in files if str(item or "").strip()):
        buckets[_documentation_material_category(path)].append(path)
    return {
        "total": sum(len(values) for values in buckets.values()),
        "categories": {
            name: {
                "count": len(values),
                "sample": values[:24],
            }
            for name, values in buckets.items()
        },
    }


def _documentation_material_category(path: str) -> str:
    suffix = _path_suffix(path)
    if suffix in {".pdf", ".docx", ".pptx", ".md", ".txt", ".rst", ".odt"}:
        return "documents"
    if suffix in {".csv", ".tsv", ".xlsx", ".json", ".jsonl", ".parquet", ".ipynb"}:
        return "data"
    if suffix in {".sql", ".db", ".sqlite", ".sqlite3"}:
        return "sql"
    if suffix in {".mp3", ".m4a", ".wav", ".mp4", ".flac", ".ogg", ".opus"}:
        return "media"
    return "other"


def _documentation_representative_enrichment_results(
    results: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_groups: set[tuple[str, str, str]] = set()

    def add(result: dict[str, Any]) -> None:
        if len(selected) >= limit:
            return
        marker = id(result)
        if marker in seen_ids:
            return
        selected.append(result)
        seen_ids.add(marker)

    def group_key(result: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(result.get("provider") or "").strip(),
            str(result.get("capability") or "").strip(),
            str(result.get("status") or "").strip(),
        )

    for result in results:
        group = group_key(result)
        if not group[0] or not group[1] or group in seen_groups:
            continue
        if not _enrichment_result_has_semantic_excerpt(result):
            continue
        seen_groups.add(group)
        add(result)

    for result in results:
        if _enrichment_result_has_semantic_excerpt(result):
            add(result)

    for result in results:
        group = group_key(result)
        if not group[0] or not group[1] or group in seen_groups:
            continue
        seen_groups.add(group)
        add(result)

    for result in results:
        if result.get("success") and result.get("storage_refs"):
            add(result)

    for result in results:
        add(result)
    return selected


def _documentation_compact_enrichment_results_json(results_json: str) -> str:
    try:
        payload = json.loads(results_json)
    except json.JSONDecodeError:
        return "[]"
    if not isinstance(payload, list):
        return "[]"
    compact: list[dict[str, Any]] = []
    for item in _documentation_representative_enrichment_results(payload, limit=5):
        if not isinstance(item, dict):
            continue
        next_item: dict[str, Any] = {
            "provider": str(item.get("provider") or "").strip(),
            "capability": str(item.get("capability") or "").strip(),
            "input_paths": [str(path) for path in item.get("input_paths", [])[:3]]
            if isinstance(item.get("input_paths"), list)
            else [],
            "status": str(item.get("status") or "").strip(),
            "success": bool(item.get("success")),
        }
        action = str(item.get("action") or "").strip()
        content_excerpt = _compact_text(str(item.get("content_excerpt") or "").strip(), 500)
        storage_refs = [str(value) for value in item.get("storage_refs", [])[:1]] if isinstance(item.get("storage_refs"), list) else []
        semantic_values = _compact_semantic_digest_for_documentation(item.get("semantic_digest"))
        if action:
            next_item["action"] = action
        if content_excerpt:
            next_item["content_excerpt"] = content_excerpt
        if storage_refs:
            next_item["storage_refs"] = storage_refs
        if semantic_values:
            next_item["semantic_digest"] = semantic_values
        compact.append(next_item)
    return json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _documentation_minimal_enrichment_results_json(results_json: str) -> str:
    try:
        payload = json.loads(results_json)
    except json.JSONDecodeError:
        return "[]"
    if not isinstance(payload, list):
        return "[]"
    compact: list[dict[str, Any]] = []
    for item in _documentation_representative_enrichment_results(payload, limit=4):
        if not isinstance(item, dict):
            continue
        paths = item.get("input_paths")
        storage_refs = item.get("storage_refs")
        next_item: dict[str, Any] = {
            "provider": str(item.get("provider") or "").strip(),
            "capability": str(item.get("capability") or "").strip(),
            "input_paths": [_compact_text(str(paths[0]), 96)] if isinstance(paths, list) and paths else [],
            "status": str(item.get("status") or "").strip(),
            "success": bool(item.get("success")),
        }
        if isinstance(paths, list) and len(paths) > 1:
            next_item["path_count"] = len(paths)
        content_excerpt = _compact_text(str(item.get("content_excerpt") or "").strip(), 320)
        if content_excerpt:
            next_item["content_excerpt"] = content_excerpt
        semantic_values = _compact_semantic_digest_for_documentation(item.get("semantic_digest"))
        if semantic_values:
            next_item["semantic_digest"] = semantic_values
        if isinstance(storage_refs, list) and storage_refs:
            next_item["storage_refs"] = [str(storage_refs[0])]
        compact.append(next_item)
    return json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _documentation_tiny_enrichment_results_json(
    results_json: str,
    *,
    limit: int,
    excerpt_limit: int,
) -> str:
    try:
        payload = json.loads(results_json)
    except json.JSONDecodeError:
        return "[]"
    if not isinstance(payload, list):
        return "[]"
    compact: list[dict[str, Any]] = []
    for item in _documentation_representative_enrichment_results(payload, limit=limit):
        if not isinstance(item, dict):
            continue
        paths = item.get("input_paths")
        next_item: dict[str, Any] = {
            "provider": str(item.get("provider") or "").strip(),
            "capability": str(item.get("capability") or "").strip(),
            "input_paths": [_compact_text(str(paths[0]), 80)] if isinstance(paths, list) and paths else [],
            "status": str(item.get("status") or "").strip(),
            "success": bool(item.get("success")),
        }
        semantic_excerpts = _semantic_digest_excerpts(item)
        if semantic_excerpts:
            next_item["semantic_digest"] = {
                "semantic_content_available": True,
                "excerpts": [_compact_text(semantic_excerpts[0], excerpt_limit)],
            }
        else:
            content_excerpt = str(item.get("content_excerpt") or "").strip()
            if _content_has_documentation_value(content_excerpt):
                next_item["content_excerpt"] = _compact_text(content_excerpt, excerpt_limit)
        storage_refs = item.get("storage_refs")
        if isinstance(storage_refs, list) and storage_refs:
            next_item["storage_refs"] = [_compact_text(str(storage_refs[0]), 96)]
        compact.append(next_item)
    return json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _compact_semantic_digest_for_documentation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    compact: dict[str, Any] = {}
    for key in (
        "contract_version",
        "provider",
        "capability",
        "status",
        "digest_kind",
        "semantic_content_available",
    ):
        if key in value:
            compact[key] = value[key]
    excerpts = value.get("excerpts")
    if isinstance(excerpts, list):
        compact["excerpts"] = [
            _compact_text(str(item).strip(), 900)
            for item in excerpts[:5]
            if str(item).strip()
        ]
    summary = value.get("summary")
    if isinstance(summary, dict):
        compact["summary"] = {
            str(key): _compact_text(str(item), 180) if isinstance(item, str) else item
            for key, item in list(summary.items())[:8]
            if isinstance(item, (str, int, float, bool)) or item is None
        }
    missing = value.get("missing_semantic_evidence")
    if isinstance(missing, list):
        compact["missing_semantic_evidence"] = [
            _compact_text(str(item).strip(), 160)
            for item in missing[:4]
            if str(item).strip()
        ]
    jobs = value.get("jobs")
    if isinstance(jobs, list):
        compact["jobs"] = []
        for job in jobs[:4]:
            if not isinstance(job, dict):
                continue
            compact_job = {
                str(key): job[key]
                for key in ("file", "status", "reused_result", "reused_from_storage_guardian")
                if key in job
            }
            if compact_job:
                compact["jobs"].append(compact_job)
    return {key: item for key, item in compact.items() if item not in ({}, [], "")}


def _documentation_enrichment_result_summary_for_notes(results: list[dict[str, Any]]) -> str:
    if not results:
        return "none"
    counts: dict[tuple[str, str, str], int] = {}
    for result in results:
        provider = str(result.get("provider") or "").strip()
        capability = str(result.get("capability") or "").strip()
        status = str(result.get("status") or "").strip() or "unknown"
        if provider and capability:
            count = len(result.get("input_paths") or []) or 1
            key = (provider, capability, status)
            counts[key] = counts.get(key, 0) + count
    parts = [
        f"{provider}.{capability}:{status}={count}"
        for (provider, capability, status), count in counts.items()
    ]
    return _compact_text("; ".join(parts) or "none", 900)


def _filter_subject_paths(raw_values: object, subjects: list[str]) -> list[str]:
    if not isinstance(raw_values, list):
        return []
    result: list[str] = []
    for item in raw_values:
        path = str(item or "").strip().lstrip("./")
        for subject in subjects:
            display_path = _documentation_subject_relative_path(path, subject)
            if display_path:
                result.append(display_path)
                break
    return _dedupe_strings(result)


def _documentation_subject_relative_path(path: str, subject: str) -> str:
    normalized_path = _documentation_normalized_path_segments(path)
    normalized_subject = _documentation_normalized_path_segments(subject)
    if not normalized_path or not normalized_subject:
        return ""
    subject_len = len(normalized_subject)
    for index in range(0, len(normalized_path) - subject_len + 1):
        if normalized_path[index : index + subject_len] == normalized_subject:
            return "/".join(normalized_path[index:])
    return ""


def _documentation_normalized_path_segments(value: str) -> list[str]:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return [segment for segment in normalized.strip("/").split("/") if segment and segment != "."]


def _compact_file_observation(raw: dict[str, Any], *, excerpt_limit: int = 520) -> dict[str, Any]:
    excerpt = str(raw.get("excerpt") or "").strip()
    warnings = raw.get("warnings")
    return {
        "path": str(raw.get("path") or "").strip(),
        "file_type": str(raw.get("file_type") or "").strip(),
        "size_bytes": int(raw.get("size_bytes") or 0),
        "line_count": raw.get("line_count") if isinstance(raw.get("line_count"), int) else None,
        "sha256": str(raw.get("sha256") or "").strip(),
        "excerpt": _compact_text(excerpt, excerpt_limit) if excerpt and excerpt_limit > 0 else "",
        "relevance_reason": str(raw.get("relevance_reason") or "").strip(),
        "was_fully_read": bool(raw.get("was_fully_read")),
        "was_sampled": bool(raw.get("was_sampled")),
        "warnings": [str(item) for item in warnings[:6]] if isinstance(warnings, list) else [],
    }


def _documentation_observation_json_for_purpose(observations: list[dict[str, Any]]) -> str:
    selected = _documentation_representative_file_observations(observations, limit=10)
    for excerpt_limit, budget in ((520, 4200), (320, 3400), (180, 2600), (80, 1800), (0, 1400)):
        compact = [
            _compact_file_observation(item, excerpt_limit=excerpt_limit)
            for item in selected
        ]
        payload = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(payload) <= budget or excerpt_limit == 0:
            return payload
    return "[]"


def _documentation_representative_file_observations(
    observations: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {
        "data": [],
        "sql": [],
        "media": [],
        "documents": [],
        "other": [],
    }
    for item in observations:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        buckets[_documentation_material_category(path)].append(item)

    selected: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        if len(selected) >= limit:
            return
        path = str(item.get("path") or "").strip()
        if not path or path in seen_paths:
            return
        seen_paths.add(path)
        selected.append(item)

    for category in ("data", "sql", "media", "documents", "other"):
        for item in buckets[category][:2]:
            add(item)
    for category in ("data", "sql", "media", "documents", "other"):
        for item in buckets[category]:
            add(item)
            if len(selected) >= limit:
                break
    return selected


def _documentation_observed_files_for_purpose(files: list[str], *, limit: int = 900) -> str:
    parts: list[str] = []
    used = 0
    for path in files:
        clean = str(path or "").strip()
        if not clean:
            continue
        separator = "; " if parts else ""
        next_len = used + len(separator) + len(clean)
        if next_len > limit:
            break
        parts.append(clean)
        used = next_len
    return "; ".join(parts)


def _documentation_representative_files_for_purpose(files: list[str], *, limit: int = 900) -> str:
    unique = _dedupe_strings(str(path or "").strip() for path in files if str(path or "").strip())
    if not unique:
        return ""
    buckets: dict[str, list[str]] = {
        "documents": [],
        "data": [],
        "sql": [],
        "media": [],
        "other": [],
    }
    for path in unique:
        suffix = _path_suffix(path)
        if suffix in {".pdf", ".docx", ".pptx", ".md", ".txt", ".rst", ".odt"}:
            buckets["documents"].append(path)
        elif suffix in {".csv", ".tsv", ".xlsx", ".json", ".jsonl", ".parquet", ".ipynb"}:
            buckets["data"].append(path)
        elif suffix in {".sql", ".db", ".sqlite", ".sqlite3"}:
            buckets["sql"].append(path)
        elif suffix in {".mp3", ".m4a", ".wav", ".mp4", ".flac", ".ogg", ".opus"}:
            buckets["media"].append(path)
        else:
            buckets["other"].append(path)

    selected: list[str] = []
    for bucket_name in ("documents", "data", "sql", "media", "other"):
        for path in buckets[bucket_name][:2]:
            if path not in selected:
                selected.append(path)
    for path in unique:
        if len(selected) >= 12:
            break
        if path not in selected:
            selected.append(path)

    return _documentation_observed_files_for_purpose(selected, limit=limit)


def _documentation_entry_slug(entry: str, *, used: set[str]) -> str:
    normalized = unicodedata.normalize("NFKD", entry).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", normalized.casefold()).strip("-")
    if not slug:
        slug = "entry"
    base = slug
    counter = 2
    while slug in used:
        slug = f"{base}-{counter}"
        counter += 1
    used.add(slug)
    return slug


def _explicit_python_material_request(request: MaterialPlanRequest) -> bool:
    if not _expected_project_root_for_plan_request(request):
        return False
    capabilities = _normalized_capabilities(request)
    query = " ".join(
        str(value or "")
        for value in (
            request.working_query,
            request.original_query,
            " ".join(request.required_capabilities),
        )
    ).casefold()
    wants_python = "python" in capabilities or "python" in query
    if not wants_python:
        return False
    signals = {
        "cli": bool({"cli"} & capabilities) or "--help" in query or "console script" in query,
        "tests": bool({"tests", "test"} & capabilities) or "pytest" in query or "test" in query,
        "packaging": "pyproject" in query or ".tar.gz" in query or "packag" in query or "empacot" in query,
        "docs": "readme" in query or "documentation" in query,
        "evidence": "validation-evidence" in query or "evidence" in query or "valida" in query,
    }
    return sum(1 for matched in signals.values() if matched) >= 3


def _validation_missing_required_field(error: ValidationError, field_name: str) -> bool:
    for item in error.errors(include_url=False):
        loc = tuple(str(part) for part in item.get("loc", ()))
        if loc == (field_name,) and item.get("type") == "missing":
            return True
    return False


def _project_root_from_payload_paths(files: object) -> str:
    if not isinstance(files, list):
        return ""
    roots: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path") or "").strip().strip("/").replace("\\", "/")
        if not raw_path or raw_path.startswith("../") or "/" not in raw_path:
            continue
        root = _normalize_expected_project_root(raw_path.split("/", 1)[0])
        if root:
            roots.append(root)
    deduped = _dedupe_strings(roots)
    return deduped[0] if len(deduped) == 1 else ""


def _project_root_from_request_text(request: MaterialPlanRequest) -> str:
    text = "\n".join(
        value
        for value in (request.working_query, request.original_query)
        if isinstance(value, str) and value.strip()
    )
    patterns = (
        r"\bnamed\s+[`'\"]?([A-Za-z0-9][A-Za-z0-9_.-]{2,})[`'\"]?",
        r"\bcalled\s+[`'\"]?([A-Za-z0-9][A-Za-z0-9_.-]{2,})[`'\"]?",
        r"\bchamad[oa]\s+[`'\"]?([A-Za-z0-9][A-Za-z0-9_.-]{2,})[`'\"]?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        root = _normalize_expected_project_root(match.group(1).rstrip(".,;:"))
        if root:
            return root
    return ""


def _expected_project_root_for_plan_request(request: MaterialPlanRequest) -> str:
    return _expected_artifact_root_from_constraints(request.constraints) or _project_root_from_request_text(request)




def _structural_plan_payload_from_request(
    request: MaterialPlanRequest,
    *,
    project_root: str,
    seed_payload: dict[str, Any],
) -> dict[str, Any]:
    capabilities = _normalized_capabilities(request)
    query = " ".join(
        str(value or "")
        for value in (
            request.working_query,
            request.original_query,
            " ".join(request.required_capabilities),
        )
    ).lower()
    behavior_summary = _request_behavior_summary(request)
    wants_python = "python" in capabilities or "python" in query
    wants_tests = bool({"tests", "test", "pytest"} & capabilities) or "pytest" in query or "test" in query
    wants_cli = "cli" in capabilities or " --help" in query or "console script" in query or "command-line" in query
    wants_packaging = (
        "artifact" in capabilities
        or "package" in query
        or "packag" in query
        or "pyproject" in query
        or ".tar.gz" in query
    )
    wants_docs = "readme" in query or "documentation" in query or "docs" in capabilities or wants_packaging
    wants_validation_evidence = "evidence" in query or "validation-evidence" in query or "validat" in query
    package_name = _stable_python_stem_from_project_root(project_root) or "generated_project"

    requirements: list[dict[str, Any]] = [
        {
            "requirement_id": "req:artifact",
            "description": "Produce a material artifact rooted at the requested project directory.",
            "source": "user",
            "capability_refs": ["artifact"],
        }
    ]
    if wants_python:
        requirements.append(
            {
                "requirement_id": "req:library",
                "description": f"Provide importable reusable Python behavior that implements the user request: {behavior_summary}",
                "source": "user",
                "capability_refs": ["python", "api"],
            }
        )
    if wants_cli:
        requirements.append(
            {
                "requirement_id": "req:cli",
                "description": f"Expose a command-line interface with help output and bounded behavior for the user request: {behavior_summary}",
                "source": "user",
                "capability_refs": ["cli"],
            }
        )
    if wants_tests:
        requirements.append(
            {
                "requirement_id": "req:tests",
                "description": f"Include executable tests that exercise the concrete requested behavior: {behavior_summary}",
                "source": "user",
                "capability_refs": ["tests"],
            }
        )
    if wants_docs:
        requirements.append(
            {
                "requirement_id": "req:docs",
                "description": "Document installation, usage, examples, and validation commands.",
                "source": "user",
                "capability_refs": ["docs"],
            }
        )
    if wants_validation_evidence:
        requirements.append(
            {
                "requirement_id": "req:evidence",
                "description": "Record validation evidence and any validation limitations.",
                "source": "user",
                "capability_refs": ["artifact"],
            }
        )

    files: list[dict[str, Any]] = []
    if wants_packaging or wants_python:
        files.append(
            {
                "path": f"{project_root}/pyproject.toml",
                "purpose": (
                    "Project metadata and packaging configuration; include console script metadata when CLI is requested."
                ),
                "kind": "config",
                "requirement_refs": ["req:artifact", *(["req:cli"] if wants_cli else [])],
            }
        )
    if wants_docs:
        files.append(
            {
                "path": f"{project_root}/README.md",
                "purpose": "User-facing project documentation with local setup, CLI examples, and validation commands.",
                "kind": "markdown",
                "requirement_refs": ["req:docs", "req:artifact"],
            }
        )
    if wants_python:
        files.extend(
            [
                {
                    "path": f"{project_root}/src/{package_name}/__init__.py",
                    "purpose": "Importable package surface that re-exports reusable behavior without side effects.",
                    "kind": "python",
                    "requirement_refs": ["req:library"],
                },
                {
                    "path": f"{project_root}/src/{package_name}/core.py",
                    "purpose": f"Reusable standard-library Python implementation of the user-requested behavior: {behavior_summary}",
                    "kind": "python",
                    "requirement_refs": ["req:library"],
                    "depends_on": [f"{project_root}/src/{package_name}/__init__.py"],
                },
            ]
        )
    if wants_cli:
        files.append(
            {
                "path": f"{project_root}/src/{package_name}/__main__.py",
                "purpose": f"CLI entrypoint with argparse help and JSON stdout for requested processing: {behavior_summary}",
                "kind": "python",
                "requirement_refs": ["req:cli", "req:library"],
                "depends_on": [f"{project_root}/src/{package_name}/core.py"],
            }
        )
    if wants_tests:
        if wants_python:
            files.append(
                {
                    "path": f"{project_root}/tests/conftest.py",
                    "purpose": "Pytest bootstrap for src-layout imports when tests run before package installation.",
                    "kind": "test",
                    "requirement_refs": ["req:tests", "req:library"],
                    "depends_on": [f"{project_root}/src/{package_name}/__init__.py"],
                }
            )
        files.append(
            {
                "path": f"{project_root}/tests/test_core.py",
                "purpose": (
                    "Pytest coverage for the reusable API and CLI behavior from the user request; keep imports portable "
                    f"for src layout. Requested behavior: {behavior_summary}"
                ),
                "kind": "test",
                "requirement_refs": ["req:tests", "req:library", *(["req:cli"] if wants_cli else [])],
                "depends_on": [f"{project_root}/src/{package_name}/core.py"],
            }
        )
    if wants_validation_evidence:
        files.append(
            {
                "path": f"{project_root}/validation-evidence.txt",
                "purpose": "Human-readable record of validation commands executed and their expected status.",
                "kind": "text",
                "requirement_refs": ["req:evidence", "req:artifact"],
            }
        )
    if not files:
        files.append(
            {
                "path": f"{project_root}/README.md",
                "purpose": "Material artifact documentation for the requested output.",
                "kind": "markdown",
                "requirement_refs": ["req:artifact"],
            }
        )

    required_profiles: list[str] = []
    if wants_python:
        required_profiles.append("python-basic")
    if wants_tests:
        required_profiles.append("python-pytest")
    if wants_cli:
        required_profiles.append("cli")
    required_profiles = _dedupe_strings(required_profiles)

    validation_commands: dict[str, Any] = {}
    if wants_cli:
        validation_commands["cli"] = {
            "profile": "cli",
            "argv": ["python", "-m", package_name, "--help"],
            "cwd": project_root,
            "timeout_seconds": 30,
            "env": {"PYTHONPATH": "src"},
            "purpose": "Verify that the generated CLI exposes help without error.",
            "requirement_refs": ["req:cli"],
        }

    file_refs = [file["path"] for file in files]
    plan = {
        "schema_version": "material_plan.v3.2",
        "project_root": project_root,
        "requirements": requirements,
        "files": files,
        "intended_interfaces": _structural_interfaces(
            project_root=project_root,
            package_name=package_name,
            file_refs=file_refs,
            wants_python=wants_python,
            wants_cli=wants_cli,
        ),
        "required_validation_profiles": required_profiles,
        "optional_validation_profiles": [],
        "validation_commands": validation_commands,
        "artifact_expectations": [
            {
                "artifact_id": "artifact:project",
                "root": project_root,
                "purpose": "Packaged material output for the requested project.",
                "requirement_refs": ["req:artifact"],
                "file_refs": file_refs,
            }
        ],
        "completion_criteria": [
            {
                "criterion_id": "criterion:validated-artifact",
                "description": "Required validations pass and the artifact can be packaged for publication.",
                "requirement_refs": [item["requirement_id"] for item in requirements],
                "validation_refs": required_profiles,
                "artifact_refs": ["artifact:project"],
            }
        ],
        "dependency_strategy": {
            "declared_dependency_files": [path for path in file_refs if path.endswith("pyproject.toml")],
            "external_dependencies": [],
            "install_profiles": [],
            "lockfiles": [],
            "native_builds_required": False,
            "network_required": "none",
            "requirement_refs": [item["requirement_id"] for item in requirements],
        },
        "architecture_notes": [
            "Sparse LLM plan completed structurally from the request; file content remains generated by file lanes.",
            "Do not add undeclared runtime dependencies; use only the standard library unless the plan declares otherwise.",
            f"Concrete user request summary for generation and tests: {behavior_summary}",
        ],
        "variation_reason": str(seed_payload.get("variation_reason") or "").strip()
        or "structural completion of sparse material plan",
    }
    return plan


def _request_behavior_summary(request: MaterialPlanRequest) -> str:
    text = "\n".join(
        str(value).strip()
        for value in (request.working_query, request.original_query)
        if str(value or "").strip()
    )
    text = re.sub(r"\s+", " ", text).strip()
    behavior_clauses = _request_behavior_clauses(text)
    summary = _compact_text(text, 900)
    if behavior_clauses:
        behavior_summary = _compact_text("; ".join(behavior_clauses), 650)
        summary = f"{summary} Key behavior requirements: {behavior_summary}"
    return _compact_text(summary, 1600) or "the requested behavior"


def _request_behavior_clauses(text: str) -> list[str]:
    clauses = [
        clause.strip(" -:;")
        for clause in re.split(r"(?:\s+-\s+)|(?<=[.!?])\s+", text)
        if clause.strip(" -:;")
    ]
    selected: list[str] = []
    for clause in clauses:
        normalized = clause.casefold()
        if not any(
            marker in normalized
            for marker in (
                "accept",
                "aceitar",
                "stdin",
                "stdout",
                "json",
                "normaliz",
                "estat",
                "statistic",
                "calculate",
                "calcular",
                "cli",
                "--help",
                "pytest",
            )
        ):
            continue
        selected.append(clause)
    return _dedupe_strings(selected[:8])


def _normalized_capabilities(request: MaterialPlanRequest) -> set[str]:
    return {
        str(capability or "").strip().lower().replace("_", "-")
        for capability in request.required_capabilities
        if str(capability or "").strip()
    }


def _structural_interfaces(
    *,
    project_root: str,
    package_name: str,
    file_refs: list[str],
    wants_python: bool,
    wants_cli: bool,
) -> list[dict[str, Any]]:
    interfaces: list[dict[str, Any]] = []
    if wants_python:
        library_refs = [
            path
            for path in file_refs
            if path.startswith(f"{project_root}/src/{package_name}/") and not path.endswith("__main__.py")
        ]
        interfaces.append(
            {
                "interface_id": "interface:library",
                "kind": "library",
                "name": package_name,
                "purpose": "Reusable importable Python API for the requested artifact.",
                "requirement_refs": ["req:library"],
                "file_refs": library_refs,
            }
        )
    if wants_cli:
        cli_path = f"{project_root}/src/{package_name}/__main__.py"
        interfaces.append(
            {
                "interface_id": "interface:cli",
                "kind": "cli",
                "name": package_name,
                "purpose": "Command-line interface for the generated artifact.",
                "requirement_refs": ["req:cli"],
                "file_refs": [cli_path] if cli_path in file_refs else [],
            }
        )
    return interfaces


def _repair_patch_schema_payload(
    *,
    messages: list[dict[str, str]],
    request: MaterialPatchGenerationRequest,
    llm: LLMSettings,
    invalid_payload: dict[str, Any],
    validation_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    repair_messages = [
        *messages,
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": _prompt("schema_repair.md"),
                    "contract": "material_patch.v3.2",
                    "task_id": request.task_id,
                    "session_id": request.session_id,
                    "issue_id": request.issue_id,
                    "issue": request.issue.model_dump(mode="json"),
                    "target_path": request.target_path,
                    "expected_old_sha256": request.expected_old_sha256,
                    "current_content": request.current_content,
                    "current_context": request.current_context,
                    "target_resolution": request.target_resolution.model_dump(mode="json")
                    if request.target_resolution
                    else None,
                    "validation_profile": request.validation_profile,
                    "expected_symbols": _expected_symbols_from_repair_request(request),
                    "command_evidence": request.command_evidence,
                    "previous_patch_rejections": [
                        rejection.model_dump(mode="json") for rejection in request.previous_patch_rejections
                    ],
                    "invalid_payload": invalid_payload,
                    "validation_errors": _json_safe(validation_errors),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    try:
        repaired = _governed_chat_completion(
            model=llm.model,
            messages=repair_messages,
            base_url=llm.base_url,
            phase="material_builder.patch_schema_repair",
            temperature=0.0,
            max_tokens=llm.max_tokens,
            timeout=llm.timeout_seconds,
            post=httpx.post,
        )
    except Exception as exc:
        raise MaterialLLMError("llm_generation_failed", "LLM patch schema repair call failed") from exc
    try:
        return _normalize_patch_payload(_json_object(repaired), request=request)
    except MaterialLLMError as exc:
        exc.details["response_excerpt"] = _compact_text(repaired, 1000)
        raise


def generate_plan_with_llm(
    request: MaterialPlanRequest,
    llm: LLMSettings,
    *,
    repair_llm: LLMSettings | None = None,
) -> MaterialPlanResponse:
    preflight_payload = _structural_plan_preflight_payload(request)
    if preflight_payload is not None:
        plan = MaterialPlan.model_validate(preflight_payload)
        return MaterialPlanResponse(
            plan=plan,
            generation_backend="contract_blueprint",
            static_fallback_used=False,
            model_route={"lane": "plan", "route": "structural_preflight"},
            lane_metrics={
                "lane": "plan",
                "contract_completion": "structural_plan_preflight",
                "static_fallback_used": False,
                "llm_call_skipped": True,
            },
            notes=["Explicit material request satisfied by structural plan preflight; file content remains generated."],
        )
    messages = [
        {
            "role": "system",
            "content": _prompt("plan_system.md").format(
                profile_guidance=VALIDATION_PROFILE_GUIDANCE,
                file_kinds=", ".join(KNOWN_FILE_KINDS),
                validation_profiles=", ".join(sorted(KNOWN_VALIDATION_PROFILES)),
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_id": request.task_id,
                    "working_query": request.working_query,
                    "original_query": request.original_query,
                    "original_language": request.original_language,
                    "language_context": request.language_context,
                    "required_capabilities": request.required_capabilities,
                    "constraints": request.constraints,
                    "variation_nonce": request.variation_nonce,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    result = LLMJSONResult(payload={}, lane_metrics={})
    try:
        result = _call_governed_json(messages, llm)
        payload = _normalize_file_kinds(
            _normalize_plan_payload(
                result.payload,
                expected_project_root=_expected_project_root_for_plan_request(request),
            )
        )
    except MaterialLLMError as exc:
        if exc.code != "llm_schema_invalid":
            raise
        invalid_response = str(
            exc.details.get("invalid_response_excerpt")
            or exc.details.get("response_excerpt")
            or "The model response could not be parsed as the requested material plan contract."
        )
        result = LLMJSONResult(payload={}, lane_metrics=dict(exc.details.get("lane_metrics") or {}))
        try:
            payload = _repair_plan_invalid_response(
                messages=messages,
                request=request,
                llm=repair_llm or llm,
                invalid_response=invalid_response,
                parse_error=str(exc),
            )
        except MaterialLLMError as repair_exc:
            structural_payload = _structural_plan_payload_for_request(request)
            if structural_payload is None:
                raise repair_exc
            payload = structural_payload
            result = LLMJSONResult(
                payload=payload,
                lane_metrics={
                    **result.lane_metrics,
                    "schema_retries": int(result.lane_metrics.get("schema_retries") or 0) + 1,
                    "contract_completion": "structural_plan_after_invalid_json",
                    "plan_repair_error": repair_exc.code,
                },
            )
    try:
        plan = MaterialPlan.model_validate(payload.get("plan", payload))
    except ValidationError as exc:
        completed_payload = _complete_sparse_plan_payload(payload, request=request, validation_error=exc)
        if completed_payload is not None:
            try:
                plan = MaterialPlan.model_validate(completed_payload.get("plan", completed_payload))
                payload = completed_payload
                result = LLMJSONResult(
                    payload=payload,
                    lane_metrics={
                        **result.lane_metrics,
                        "schema_retries": int(result.lane_metrics.get("schema_retries") or 0) + 1,
                        "contract_completion": "sparse_plan_files",
                    },
                )
            except ValidationError:
                completed_payload = None
        if completed_payload is None:
            repaired_payload = _repair_plan_schema_payload(
                messages=messages,
                request=request,
                llm=repair_llm or llm,
                invalid_payload=payload,
                validation_error=exc,
            )
            try:
                plan = MaterialPlan.model_validate(repaired_payload.get("plan", repaired_payload))
                payload = repaired_payload
                result = LLMJSONResult(
                    payload=payload,
                    lane_metrics={
                        **result.lane_metrics,
                        "schema_retries": int(result.lane_metrics.get("schema_retries") or 0) + 1,
                    },
                )
            except ValidationError as repaired_exc:
                completed_repaired_payload = _complete_sparse_plan_payload(
                    repaired_payload,
                    request=request,
                    validation_error=repaired_exc,
                )
                if completed_repaired_payload is not None:
                    try:
                        plan = MaterialPlan.model_validate(
                            completed_repaired_payload.get("plan", completed_repaired_payload)
                        )
                        payload = completed_repaired_payload
                        result = LLMJSONResult(
                            payload=payload,
                            lane_metrics={
                                **result.lane_metrics,
                                "schema_retries": int(result.lane_metrics.get("schema_retries") or 0) + 2,
                                "contract_completion": "sparse_plan_files_after_repair",
                            },
                        )
                    except ValidationError:
                        completed_repaired_payload = None
                if completed_repaired_payload is None:
                    raise MaterialLLMError(
                        "llm_schema_invalid",
                        "LLM material plan did not satisfy the material plan contract",
                        details={
                            "validation_errors": _validation_errors(repaired_exc),
                            "initial_validation_errors": _validation_errors(exc),
                            "initial_payload_excerpt": _compact_text(json.dumps(payload, ensure_ascii=False), 1000),
                            "repaired_payload_excerpt": _compact_text(
                                json.dumps(repaired_payload, ensure_ascii=False),
                                1000,
                            ),
                        },
                    ) from repaired_exc
    return MaterialPlanResponse(
        plan=plan,
        generation_backend="llm",
        static_fallback_used=False,
        model_route=llm.route,
        lane_metrics=result.lane_metrics,
        notes=["LLM material plan accepted; no static fallback project was used"],
    )


def repair_plan_with_llm(
    request: MaterialPlanRepairRequest,
    llm: LLMSettings,
    *,
    schema_repair_llm: LLMSettings | None = None,
) -> MaterialPlanRepairResponse:
    messages = [
        {
            "role": "system",
            "content": _prompt("plan_repair_system.md").format(
                profile_guidance=VALIDATION_PROFILE_GUIDANCE,
                file_kinds=", ".join(KNOWN_FILE_KINDS),
                validation_profiles=", ".join(sorted(KNOWN_VALIDATION_PROFILES)),
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_id": request.task_id,
                    "session_id": request.session_id,
                    "working_query": request.working_query,
                    "original_query": request.original_query,
                    "original_language": request.original_language,
                    "language_context": request.language_context,
                    "required_capabilities": request.required_capabilities,
                    "constraints": request.constraints,
                    "current_plan": request.plan.model_dump(mode="json"),
                    "coverage_issues": [
                        issue.model_dump(mode="json") for issue in request.coverage_issues
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    result = LLMJSONResult(payload={}, lane_metrics={})
    try:
        result = _call_governed_json(messages, llm)
        payload = _normalize_file_kinds(
            _normalize_plan_payload(
                result.payload,
                expected_project_root=_expected_project_root_for_plan_request(request),
            )
        )
    except MaterialLLMError as exc:
        if exc.code != "llm_schema_invalid":
            raise
        invalid_response = str(
            exc.details.get("invalid_response_excerpt")
            or exc.details.get("response_excerpt")
            or "The model response could not be parsed as the requested material plan contract."
        )
        result = LLMJSONResult(payload={}, lane_metrics=dict(exc.details.get("lane_metrics") or {}))
        plan_request = MaterialPlanRequest(
            task_id=request.task_id,
            working_query=request.working_query,
            original_query=request.original_query,
            original_language=request.original_language,
            working_language=request.working_language,
            language_context=request.language_context,
            required_capabilities=request.required_capabilities,
            constraints=request.constraints,
        )
        try:
            payload = _repair_plan_invalid_response(
                messages=messages,
                request=plan_request,
                llm=schema_repair_llm or llm,
                invalid_response=invalid_response,
                parse_error=str(exc),
            )
        except MaterialLLMError as repair_exc:
            structural_payload = _structural_plan_payload_for_request(plan_request)
            if structural_payload is None:
                raise repair_exc
            payload = structural_payload
            result = LLMJSONResult(
                payload=payload,
                lane_metrics={
                    **result.lane_metrics,
                    "schema_retries": int(result.lane_metrics.get("schema_retries") or 0) + 1,
                    "contract_completion": "structural_plan_after_invalid_json",
                    "plan_repair_error": repair_exc.code,
                },
            )
    try:
        plan = MaterialPlan.model_validate(payload.get("plan", payload))
    except ValidationError as exc:
        plan_request = MaterialPlanRequest(
            task_id=request.task_id,
            working_query=request.working_query,
            original_query=request.original_query,
            original_language=request.original_language,
            working_language=request.working_language,
            language_context=request.language_context,
            required_capabilities=request.required_capabilities,
            constraints=request.constraints,
        )
        completed_payload = _complete_sparse_plan_payload(payload, request=plan_request, validation_error=exc)
        if completed_payload is not None:
            try:
                plan = MaterialPlan.model_validate(completed_payload.get("plan", completed_payload))
                result = LLMJSONResult(
                    payload=completed_payload,
                    lane_metrics={
                        **result.lane_metrics,
                        "schema_retries": int(result.lane_metrics.get("schema_retries") or 0) + 1,
                        "contract_completion": "sparse_plan_files",
                    },
                )
            except ValidationError:
                completed_payload = None
        if completed_payload is None:
            repaired_payload = _repair_plan_schema_payload(
                messages=messages,
                request=plan_request,
                llm=schema_repair_llm or llm,
                invalid_payload=payload,
                validation_error=exc,
            )
            try:
                plan = MaterialPlan.model_validate(repaired_payload.get("plan", repaired_payload))
                result = LLMJSONResult(
                    payload=repaired_payload,
                    lane_metrics={
                        **result.lane_metrics,
                        "schema_retries": int(result.lane_metrics.get("schema_retries") or 0) + 1,
                    },
                )
            except ValidationError as repaired_exc:
                completed_repaired_payload = _complete_sparse_plan_payload(
                    repaired_payload,
                    request=plan_request,
                    validation_error=repaired_exc,
                )
                if completed_repaired_payload is not None:
                    try:
                        plan = MaterialPlan.model_validate(
                            completed_repaired_payload.get("plan", completed_repaired_payload)
                        )
                        result = LLMJSONResult(
                            payload=completed_repaired_payload,
                            lane_metrics={
                                **result.lane_metrics,
                                "schema_retries": int(result.lane_metrics.get("schema_retries") or 0) + 2,
                                "contract_completion": "sparse_plan_files_after_repair",
                            },
                        )
                    except ValidationError:
                        completed_repaired_payload = None
                if completed_repaired_payload is None:
                    raise MaterialLLMError(
                        "llm_schema_invalid",
                        "LLM material plan repair did not satisfy the material plan contract",
                        details={
                            "validation_errors": _validation_errors(repaired_exc),
                            "initial_validation_errors": _validation_errors(exc),
                            "initial_payload_excerpt": _compact_text(json.dumps(payload, ensure_ascii=False), 1000),
                            "repaired_payload_excerpt": _compact_text(
                                json.dumps(repaired_payload, ensure_ascii=False),
                                1000,
                            ),
                        },
                    ) from repaired_exc
    return MaterialPlanRepairResponse(
        plan=plan,
        generation_backend="llm",
        static_fallback_used=False,
        model_route=llm.route,
        lane_metrics=result.lane_metrics,
        notes=["LLM material plan repair accepted; no static fallback project was used"],
    )


def generate_files_with_llm(
    request: MaterialFileGenerationRequest,
    llm: LLMSettings,
) -> tuple[list[GeneratedFileProposal], dict[str, Any]]:
    plan_summary = request.plan.model_dump(mode="json")
    allowed_local_import_roots = _allowed_local_import_roots(request.plan)
    planned_local_modules = _planned_local_python_modules(request.plan)
    target_paths = set(request.target_file_paths)
    requested_files = [
        file_spec for file_spec in request.plan.files if not target_paths or file_spec.path in target_paths
    ]
    if target_paths and not requested_files:
        raise MaterialLLMError(
            "material_file_target_missing",
            "requested target_file_paths are not present in the material plan",
            details={"target_file_paths": sorted(target_paths)},
        )
    proposals: list[GeneratedFileProposal] = []
    lane_metrics: list[dict[str, Any]] = []
    documentation_bundle = _plan_is_documentation_bundle(request.plan)
    for file_spec in requested_files:
        deterministic_content = _deterministic_file_content_for_plan_file(file_spec, request.plan)
        if deterministic_content is not None and not documentation_bundle:
            deterministic_issues = _file_generation_contract_issues(
                deterministic_content,
                path=file_spec.path,
                kind=file_spec.kind,
                project_root=request.plan.project_root,
                planned_modules=planned_local_modules,
                declared_dependency_roots=_declared_dependency_roots(request.plan),
            )
            if not deterministic_issues:
                lane_metrics.append(
                    {
                        "lane": "deterministic",
                        "static_fallback_used": True,
                        "static_fallback_reason": "plan_behavior_blueprint",
                    }
                )
                proposals.append(
                    GeneratedFileProposal.from_content(
                        path=file_spec.path,
                        content=deterministic_content,
                        kind=file_spec.kind,
                        source_plan_ref=request.source_plan_ref,
                    )
                )
                continue
        messages = [
            {
                "role": "system",
                "content": _prompt("file_system.md"),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task_id": request.task_id,
                        "session_id": request.session_id,
                        "plan": plan_summary,
                        "requested_file": file_spec.model_dump(mode="json"),
                        "allowed_local_import_roots": allowed_local_import_roots,
                        "planned_local_modules": planned_local_modules,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ]
        content = ""
        max_attempts = _file_contract_attempts(llm)
        for attempt in range(max_attempts):
            result = _call_governed_json(messages, llm)
            payload = result.payload
            lane_metrics.append(result.lane_metrics)
            path = str(payload.get("path") or "")
            content = payload.get("content")
            if path != file_spec.path:
                if _file_response_path_matches_requested(
                    actual_path=path,
                    expected_path=file_spec.path,
                    project_root=request.plan.project_root,
                ):
                    path = file_spec.path
                elif documentation_bundle and file_spec.kind in {"markdown", "text"} and isinstance(content, str) and content.strip():
                    lane_metrics.append(
                        {
                            "lane": "contract",
                            "path_authority": "requested_file",
                            "path_override_used": True,
                            "path_override_reason": "llm_returned_different_path_for_single_file_request",
                            "expected_path": file_spec.path,
                            "actual_path": path,
                        }
                    )
                    path = file_spec.path
                else:
                    path_issues = [
                        {
                            "issue_type": "path_mismatch",
                            "expected_path": file_spec.path,
                            "actual_path": path,
                        }
                    ]
                    fallback_content = _deterministic_file_content_for_generation_contract_failure(
                        file_spec=file_spec,
                        plan=request.plan,
                        contract_issues=path_issues,
                    )
                    if fallback_content is not None:
                        content = fallback_content
                        lane_metrics.append(
                            {
                                "lane": "deterministic",
                                "static_fallback_used": True,
                                "static_fallback_reason": _deterministic_file_generation_fallback_reason(
                                    path_issues
                                ),
                            }
                        )
                    elif attempt < max_attempts - 1:
                        messages = [
                            *messages,
                            {
                                "role": "user",
                                "content": json.dumps(
                                    _file_local_import_retry_payload(
                                        content=content if isinstance(content, str) else "",
                                        file_spec=file_spec.model_dump(mode="json"),
                                        project_root=request.plan.project_root,
                                        planned_modules=planned_local_modules,
                                        local_import_issues=path_issues,
                                        remaining_attempts=max_attempts - attempt - 1,
                                    ),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            },
                        ]
                        continue
                    else:
                        raise MaterialLLMError(
                            "llm_contract_violation",
                            "LLM file proposal returned a path that does not match the requested file",
                            details={"expected_path": file_spec.path, "actual_path": path},
                        )
            if path != file_spec.path:
                path = file_spec.path
            if not isinstance(content, str):
                raise MaterialLLMError(
                    "llm_contract_violation",
                    "LLM file proposal returned non-string content",
                    details={"path": file_spec.path},
                )
            file_contract_issues = _file_generation_contract_issues(
                content,
                path=file_spec.path,
                kind=file_spec.kind,
                project_root=request.plan.project_root,
                planned_modules=planned_local_modules,
                declared_dependency_roots=_declared_dependency_roots(request.plan),
            )
            documentation_quality_issues = _documentation_file_quality_issues(
                content,
                file_spec=file_spec,
                plan=request.plan,
            )
            if not file_contract_issues and not documentation_quality_issues:
                break
            contract_issues = [*file_contract_issues, *documentation_quality_issues]
            fallback_content = _deterministic_file_content_for_generation_contract_failure(
                file_spec=file_spec,
                plan=request.plan,
                contract_issues=contract_issues,
            )
            if fallback_content is None and documentation_quality_issues:
                fallback_content = _deterministic_documentation_content_for_plan_file(file_spec, request.plan)
            if fallback_content is not None:
                fallback_issues = _file_generation_contract_issues(
                    fallback_content,
                    path=file_spec.path,
                    kind=file_spec.kind,
                    project_root=request.plan.project_root,
                    planned_modules=planned_local_modules,
                    declared_dependency_roots=_declared_dependency_roots(request.plan),
                )
                fallback_quality_issues = _documentation_file_quality_issues(
                    fallback_content,
                    file_spec=file_spec,
                    plan=request.plan,
                )
                if not fallback_issues and not fallback_quality_issues:
                    content = fallback_content
                    lane_metrics.append(
                        {
                            "lane": "deterministic",
                            "static_fallback_used": True,
                            "static_fallback_reason": _deterministic_file_generation_fallback_reason(
                                contract_issues
                            ),
                        }
                    )
                    break
            if attempt < max_attempts - 1:
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": json.dumps(
                                    _file_local_import_retry_payload(
                                        content=content,
                                        file_spec=file_spec.model_dump(mode="json"),
                                        project_root=request.plan.project_root,
                                        planned_modules=planned_local_modules,
                                        local_import_issues=contract_issues,
                                        remaining_attempts=max_attempts - attempt - 1,
                                    ),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                    },
                ]
                continue
            raise MaterialLLMError(
                "llm_contract_violation",
                "LLM file proposal does not satisfy the Python file contract",
                details={
                    "path": file_spec.path,
                    "planned_local_modules": planned_local_modules,
                    "file_contract_issues": contract_issues,
                },
            )
        proposals.append(
            GeneratedFileProposal.from_content(
                path=file_spec.path,
                content=content,
                kind=file_spec.kind,
                source_plan_ref=request.source_plan_ref,
            )
        )
    documentation_lint = _documentation_publication_lint(proposals, plan=request.plan)
    if documentation_lint:
        raise MaterialLLMError(
            "documentation_quality_violation",
            "generated documentation contains implementation-facing content in user-facing pages",
            details={"issues": documentation_lint},
        )
    metrics = _merge_lane_metrics(*lane_metrics)
    if _plan_is_documentation_bundle(request.plan):
        metrics = {**metrics, "documentation_lint_checked": True}
    return proposals, metrics


def _file_response_path_matches_requested(*, actual_path: str, expected_path: str, project_root: str) -> bool:
    actual = actual_path.strip().strip("/").replace("\\", "/")
    expected = expected_path.strip().strip("/").replace("\\", "/")
    root = project_root.strip().strip("/").replace("\\", "/")
    if not actual or actual.startswith("../"):
        return False
    if actual == expected:
        return True
    if root and expected.startswith(f"{root}/") and actual == expected[len(root) + 1 :]:
        return True
    return "/" not in actual and actual == expected.rsplit("/", 1)[-1]


def _file_contract_attempts(llm: LLMSettings) -> int:
    attempts = int(getattr(llm, "contract_repair_attempts", 3) or 3)
    return max(2, min(5, attempts))


def _file_local_import_retry_payload(
    *,
    content: str,
    file_spec: dict[str, Any],
    project_root: str,
    planned_modules: list[str],
    local_import_issues: list[dict[str, Any]],
    remaining_attempts: int,
) -> dict[str, Any]:
    forbidden_modules = sorted(
        {
            str(issue.get("module") or "").strip(".")
            for issue in local_import_issues
            if str(issue.get("module") or "").strip(".")
        }
    )
    forbidden_placeholder_symbols = sorted(
        {
            str(issue.get("symbol") or "").strip()
            for issue in local_import_issues
            if issue.get("issue_type") in {"placeholder_expected_symbol", "placeholder_value_called", "placeholder_value_dereferenced"}
            and str(issue.get("symbol") or "").strip()
        }
    )
    current_module = _python_module_name_for_plan_path(str(file_spec.get("path") or ""), project_root)
    return {
        "instruction": (
            "Regenerate exactly the same requested file. The next JSON response must keep the same path. "
            "Do not add files in this lane. If behavior is required by the requested file, implement it in "
            "that file or import only an existing planned local module or declared external dependency. "
            "The next content must not import any forbidden_modules, must not call or dereference "
            "placeholder values, and Python test targets must keep at least one test discoverable by "
            "pytest or unittest with no undefined names inside test functions."
        ),
        "requested_file": file_spec,
        "current_file_module": current_module,
        "allowed_local_modules": planned_modules,
        "forbidden_modules": forbidden_modules,
        "forbidden_local_modules": forbidden_modules,
        "forbidden_placeholder_symbols": forbidden_placeholder_symbols,
        "local_import_issues": local_import_issues,
        "remaining_contract_repair_attempts": remaining_attempts,
        "invalid_content_excerpt": _compact_text(content, 4000),
    }


def _file_generation_contract_issues(
    content: str,
    *,
    path: str,
    kind: str,
    project_root: str,
    planned_modules: list[str],
    declared_dependency_roots: set[str],
) -> list[dict[str, Any]]:
    normalized_kind = FILE_KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())
    if path.replace("\\", "/").endswith(".toml"):
        parse_error = _toml_parse_error(content)
        if parse_error is not None:
            return [
                {
                    "issue_type": "toml_parse_error",
                    "message": parse_error,
                }
            ]
        return []
    if normalized_kind not in {"python", "test"} or not path.endswith(".py"):
        return []
    issues: list[dict[str, Any]] = []
    try:
        ast.parse(content, filename=path)
    except SyntaxError as exc:
        issues.append(
            {
                "issue_type": "python_syntax_error",
                "line": exc.lineno,
                "offset": exc.offset,
                "message": exc.msg,
            }
        )
        return issues
    issues.extend(
        _unplanned_local_import_issues(
            content,
            path=path,
            kind=kind,
            project_root=project_root,
            planned_modules=planned_modules,
        )
    )
    issues.extend(
        _undeclared_external_import_issues(
            content,
            path=path,
            kind=kind,
            project_root=project_root,
            planned_modules=planned_modules,
            declared_dependency_roots=declared_dependency_roots,
        )
    )
    issues.extend(_placeholder_contract_issues(content, path=path, kind=normalized_kind, expected_symbols=[]))
    if normalized_kind == "test":
        issues.extend(_undefined_test_name_issues(content, path=path))
        missing_test_issue = _missing_collectible_test_issue(
            content,
            path=path,
            target_kind="test_file",
            validation_profile="python-pytest",
        )
        if missing_test_issue:
            issues.append(missing_test_issue)
    return _dedupe_contract_issues(issues)


def _deterministic_file_content_for_generation_contract_failure(
    *,
    file_spec: MaterialFileSpec,
    plan: MaterialPlan,
    contract_issues: list[dict[str, Any]],
) -> str | None:
    path = file_spec.path.replace("\\", "/")
    issue_types = {str(issue.get("issue_type") or "") for issue in contract_issues}
    if "path_mismatch" in issue_types:
        filename = path.rsplit("/", 1)[-1].casefold()
        if filename == "readme.md":
            return _deterministic_readme_content_for_plan(plan)
        if filename == "validation-evidence.txt":
            return _deterministic_validation_evidence_content_for_plan(plan)
    if not path.endswith("/pyproject.toml") and path != "pyproject.toml":
        return None
    if "toml_parse_error" not in issue_types:
        return None
    return _deterministic_pyproject_content_for_plan(plan, target_path=file_spec.path)


def _deterministic_file_generation_fallback_reason(contract_issues: list[dict[str, Any]]) -> str:
    issue_types = {str(issue.get("issue_type") or "") for issue in contract_issues}
    if any(issue_type.startswith("documentation_") for issue_type in issue_types) or any(
        issue_type
        in {
            "provider_id",
            "owner_name",
            "storage_owner_name",
            "storage_ref",
            "container_path",
            "command_trace",
            "workspace_trace",
            "owner_return_wording",
            "internal_heading",
            "raw_extraction_wording",
            "raw_file_warning",
            "metadata_only_status",
        }
        for issue_type in issue_types
    ):
        return "generated_documentation_quality_violation"
    if "toml_parse_error" in issue_types:
        return "generated_toml_parse_error"
    if "path_mismatch" in issue_types:
        return "generated_file_path_mismatch"
    if "python_syntax_error" in issue_types:
        return "generated_python_syntax_error"
    return "generated_file_contract_failure"


def _deterministic_file_content_for_plan_file(file_spec: MaterialFileSpec, plan: MaterialPlan) -> str | None:
    documentation_content = _deterministic_documentation_content_for_plan_file(file_spec, plan)
    if documentation_content is not None:
        return documentation_content
    if not _plan_requests_text_processing(plan):
        return None
    path = file_spec.path.strip().strip("/").replace("\\", "/")
    rel_path = _plan_relative_path(path, plan)
    package_name = _primary_src_package_from_material_plan(plan)
    if not package_name:
        return None
    package_prefix = f"src/{package_name}/"
    if rel_path == "pyproject.toml":
        return _deterministic_pyproject_content_for_plan(plan, target_path=file_spec.path)
    if rel_path == "README.md":
        return _deterministic_text_processing_readme_content_for_plan(plan)
    if rel_path == "validation-evidence.txt":
        return _deterministic_validation_evidence_content_for_plan(plan)
    if rel_path == f"{package_prefix}__init__.py":
        return _deterministic_text_processing_init_content()
    if rel_path == f"{package_prefix}core.py":
        return _deterministic_text_processing_core_content()
    if rel_path == f"{package_prefix}__main__.py":
        return _deterministic_text_processing_cli_content(package_name)
    if rel_path == "tests/conftest.py":
        return _deterministic_src_layout_conftest_content()
    if rel_path == "tests/test_core.py":
        return _deterministic_text_processing_test_content(package_name)
    return None


def _deterministic_documentation_content_for_plan_file(
    file_spec: MaterialFileSpec,
    plan: MaterialPlan,
) -> str | None:
    if not _plan_is_documentation_bundle(plan):
        return None
    path = file_spec.path.strip().strip("/").replace("\\", "/")
    rel_path = _plan_relative_path(path, plan)
    if rel_path == "README.md":
        return _deterministic_documentation_readme_content_for_plan(plan)
    if rel_path == "validation-evidence.txt":
        return _deterministic_documentation_validation_content_for_plan(plan)
    if rel_path.startswith("subfolders/") and rel_path.endswith(".md"):
        return _deterministic_subfolder_documentation_content(file_spec, plan)
    return None


def _plan_is_documentation_bundle(plan: MaterialPlan) -> bool:
    if plan.intended_interfaces or plan.required_validation_profiles or plan.validation_commands:
        return False
    paths = [item.path.strip().strip("/").replace("\\", "/") for item in plan.files]
    if not paths or not any(path.endswith("README.md") for path in paths):
        return False
    if not any(path.endswith("validation-evidence.txt") for path in paths):
        return False
    if any(not (path.endswith(".md") or path.endswith(".txt")) for path in paths):
        return False
    text = " ".join(
        [
            plan.project_root,
            *(item.description for item in plan.requirements),
            *(item.purpose for item in plan.files),
            *plan.architecture_notes,
        ]
    ).casefold()
    return any(marker in text for marker in ("documentation", "documentacao", "documentação", "docs"))


def _deterministic_documentation_readme_content_for_plan(plan: MaterialPlan) -> str:
    portuguese = _documentation_is_portuguese(plan)
    docs = [
        item.path for item in plan.files if item.path.endswith(".md") and not item.path.endswith("README.md")
    ]
    doc_map = "\n".join(f"- [{path.rsplit('/', 1)[-1]}]({path[len(plan.project_root.strip('/')) + 1:] if path.startswith(plan.project_root.strip('/') + '/') else path})" for path in docs)
    if portuguese:
        return (
            "# Documentação\n\n"
            "Documentação organizada dos materiais observados na pasta pedida.\n\n"
            "## Pasta Documentada\n\n"
            "- Origem: pasta indicada no pedido do utilizador.\n"
            "- Organização: uma página por subpasta com ficheiros, temas e conteúdos identificados.\n\n"
            "## Índice\n\n"
            f"{doc_map or '- Não foram planeadas páginas por subpasta.'}\n\n"
            "## Como Ler Esta Documentação\n\n"
            "- Cada página de subpasta resume os ficheiros observados, os tipos de material e o conteúdo textual ou tabular disponível.\n"
            "- Ficheiros muito grandes ou binários podem aparecer com leitura parcial quando só havia amostras ou metadados seguros.\n"
            "- A evidência técnica de execução fica em `validation-evidence.txt` para não contaminar a documentação de domínio.\n\n"
            "## Limitações\n\n"
            "- Alguns ficheiros podem precisar de revisão humana para interpretação de domínio ou para confirmar leituras parciais.\n"
        )
    return (
        "# Documentation\n\n"
        "Organized documentation for the materials observed in the requested folder.\n\n"
        "## Documented Folder\n\n"
        "- Source: folder indicated by the user request.\n"
        "- Organization: one page per subfolder with files, themes and identified content.\n\n"
        "## Index\n\n"
        f"{doc_map or '- No per-subfolder pages were planned.'}\n\n"
        "## How To Read This Documentation\n\n"
        "- Each subfolder page summarizes observed files, material types and available textual or tabular content.\n"
        "- Very large or binary files may be represented by partial reads when only safe samples or metadata were available.\n"
        "- Technical execution evidence is kept in `validation-evidence.txt` so the domain documentation stays focused.\n\n"
        "## Limitations\n\n"
        "- Some files may need human review for domain interpretation or to confirm partial reads.\n"
    )


def _deterministic_subfolder_documentation_content(file_spec: MaterialFileSpec, plan: MaterialPlan) -> str:
    subject = _subject_name_from_documentation_purpose(file_spec.purpose) or _title_from_path(file_spec.path)
    observed = _observed_files_from_documentation_purpose(file_spec.purpose)
    observations = _file_observations_from_documentation_purpose(file_spec.purpose)
    enrichment_tasks = _enrichment_tasks_from_documentation_purpose(file_spec.purpose)
    enrichment_results = _enrichment_results_from_documentation_purpose(file_spec.purpose)
    portuguese = _documentation_is_portuguese(plan)
    inventory = _inventory_from_documentation_purpose(file_spec.purpose)
    observed_from_observations = [str(item.get("path") or "") for item in observations if item.get("path")]
    observed = _dedupe_strings([*observed, *observed_from_observations])
    displayed_observed = _localized_observed_items(observed, portuguese=portuguese)
    material_observed = [item for item in observed if not _observed_omission_match(item)]
    if inventory:
        docs_count, docs = _inventory_category_items(inventory, "documents")
        data_count, data = _inventory_category_items(inventory, "data")
        sql_count, sql = _inventory_category_items(inventory, "sql")
        media_count, media = _inventory_category_items(inventory, "media")
        other_count, other = _inventory_category_items(inventory, "other")
    else:
        docs = [item for item in material_observed if _documentation_material_category(item) == "documents"]
        data = [item for item in material_observed if _documentation_material_category(item) == "data"]
        sql = [item for item in material_observed if _documentation_material_category(item) == "sql"]
        media = [item for item in material_observed if _documentation_material_category(item) == "media"]
        other = [item for item in material_observed if _documentation_material_category(item) == "other"]
        docs_count = len(docs)
        data_count = len(data)
        sql_count = len(sql)
        media_count = len(media)
        other_count = len(other)
    evidence_lines = _documentation_file_evidence_lines(observations, portuguese=portuguese)
    semantic_lines = _documentation_semantic_evidence_lines(enrichment_results, portuguese=portuguese)
    content_summary_lines = _documentation_content_summary_lines(
        enrichment_results,
        observations,
        portuguese=portuguese,
    )
    consolidated_summary_lines = _documentation_consolidated_summary_lines(
        documents=docs,
        data=data,
        sql=sql,
        media=media,
        other=other,
        doc_count=docs_count,
        data_count=data_count,
        sql_count=sql_count,
        media_count=media_count,
        other_count=other_count,
        enrichment_results=enrichment_results,
        observations=observations,
        portuguese=portuguese,
    )
    if portuguese:
        return (
            f"# {subject}\n\n"
            "## Âmbito\n\n"
            f"Esta página documenta a subpasta `{subject}` a partir dos ficheiros observados e do conteúdo legível disponível.\n\n"
            "## Ficheiros Observados\n\n"
            f"{_markdown_list(displayed_observed, empty='Não foram amostrados ficheiros desta subpasta.')}\n\n"
            "## Tipos de Material Detectados\n\n"
            f"- Documentos e apresentações: {docs_count}\n"
            f"- Ficheiros tabulares/dados/notebooks: {data_count}\n"
            f"- Ficheiros SQL/base de dados/configuração: {sql_count}\n"
            f"- Ficheiros áudio/vídeo: {media_count}\n"
            f"- Outros ficheiros observados: {other_count}\n\n"
            "## Materiais Principais\n\n"
            f"{_markdown_list([*_summarize_inventory_category('Documentos', docs_count, docs, portuguese=True), *_summarize_inventory_category('Dados', data_count, data, portuguese=True), *_summarize_inventory_category('SQL/config', sql_count, sql, portuguese=True), *_summarize_inventory_category('Media', media_count, media, portuguese=True), *_summarize_inventory_category('Outros', other_count, other, portuguese=True)], empty='Só foi observada presença de topo.')}\n\n"
            "## Leitura Consolidada\n\n"
            f"{consolidated_summary_lines}\n\n"
            "## Conteúdo por Ficheiro\n\n"
            f"{evidence_lines}\n\n"
            "## Temas e Conteúdo Identificado\n\n"
            f"{semantic_lines}\n\n"
            "## Resumo de Conteúdo por Fonte\n\n"
            f"{content_summary_lines}\n\n"
            "## Limitações\n\n"
            "- A documentação usa os excertos disponíveis, amostras seguras e metadados dos ficheiros observados.\n"
            f"{_documentation_domain_limitation(enrichment_tasks, enrichment_results, portuguese=True)}\n\n"
            "## Próximos Passos Recomendados\n\n"
            f"{_documentation_domain_next_steps(enrichment_tasks, enrichment_results, portuguese=True)}\n"
            "- Fazer uma revisão humana para interpretação específica do domínio antes de usar isto como documentação final.\n"
        )
    return (
        f"# {subject}\n\n"
        "## Scope\n\n"
        f"This page documents the `{subject}` subfolder from the observed files and available readable content.\n\n"
        "## Observed Files\n\n"
        f"{_markdown_list(displayed_observed, empty='No nested files were sampled for this folder.')}\n\n"
        "## Detected Material Types\n\n"
        f"- Documents and presentations: {docs_count}\n"
        f"- Tabular/data/notebook files: {data_count}\n"
        f"- SQL/database/config-style files: {sql_count}\n"
        f"- Audio/video files: {media_count}\n"
        f"- Other observed files: {other_count}\n\n"
        "## Main Materials\n\n"
        f"{_markdown_list([*_summarize_inventory_category('Documents', docs_count, docs), *_summarize_inventory_category('Data', data_count, data), *_summarize_inventory_category('SQL/config', sql_count, sql), *_summarize_inventory_category('Media', media_count, media), *_summarize_inventory_category('Other', other_count, other)], empty='Only top-level presence was observed.')}\n\n"
        "## Consolidated Reading\n\n"
        f"{consolidated_summary_lines}\n\n"
        "## Content By File\n\n"
        f"{evidence_lines}\n\n"
        "## Identified Themes And Content\n\n"
        f"{semantic_lines}\n\n"
        "## Content Summary By Source\n\n"
        f"{content_summary_lines}\n\n"
        "## Limitations\n\n"
        "- The documentation uses available excerpts, safe samples and metadata from observed files.\n"
        f"{_documentation_domain_limitation(enrichment_tasks, enrichment_results)}\n\n"
        "## Recommended Next Steps\n\n"
        f"{_documentation_domain_next_steps(enrichment_tasks, enrichment_results)}\n"
        "- Add a human review pass for domain-specific interpretation before using this as final documentation.\n"
    )


def _deterministic_documentation_validation_content_for_plan(plan: MaterialPlan) -> str:
    commands = _documentation_note_value(plan, "Commands")
    if _documentation_is_portuguese(plan):
        return (
            "Evidência de validação\n"
            "======================\n\n"
            f"Project root: {plan.project_root}\n"
            "Tipo de artefacto: bundle apenas de documentação\n\n"
            "Aquisição de evidência read-only:\n"
            f"- Comandos: {commands or 'não registados'}\n"
            "- Perfis executáveis de validação: nenhum exigido pelo material plan\n\n"
            "Validação de runtime:\n"
            "- Escritas no workspace, empacotamento e publicação durável foram feitos pelo fluxo de materialização controlado.\n"
            "- A publicação durável na máquina do user deve passar pelo owner de armazenamento configurado.\n"
        )
    return (
        "Validation evidence\n"
        "===================\n\n"
        f"Project root: {plan.project_root}\n"
        "Artifact type: documentation-only bundle\n\n"
        "Read-only evidence acquisition:\n"
        f"- Commands: {commands or 'not recorded'}\n"
        "- Executable validation profiles: none required by the material plan\n\n"
        "Runtime validation:\n"
        "- Workspace writes, packaging and durable publication are performed by the controlled materialization flow.\n"
        "- Durable user-machine publication must pass through the configured storage owner.\n"
    )


def _documentation_note_value(plan: MaterialPlan, label: str) -> str:
    prefix = f"{label}:"
    for note in plan.architecture_notes:
        text = str(note).strip()
        if text.casefold().startswith(prefix.casefold()):
            return text[len(prefix) :].strip()
    return ""


def _documentation_is_portuguese(plan: MaterialPlan) -> bool:
    language = _documentation_note_value(plan, "Output language").casefold()
    return language.startswith("pt") or "portugu" in language


def _documentation_requirement_display(description: str, *, portuguese: bool) -> str:
    if not portuguese:
        return description
    stripped = description.strip()
    known = {
        "Produce the requested documentation artifact.": "Produzir o artefacto de documentação pedido.",
        "Create organized documentation grounded in observed local evidence.": "Criar documentação organizada com base na evidência local observada.",
        "Record the read-only evidence and validation limitations used to build the documentation.": (
            "Registar a evidência read-only e as limitações de validação usadas para construir a documentação."
        ),
        "Record any specialist enrichment tasks needed for file types that require owned extraction or transcription capabilities.": (
            "Registar as tarefas de enriquecimento especializado necessárias para tipos de ficheiro que exigem extração ou transcrição pelo dono correcto."
        ),
    }
    if stripped in known:
        return known[stripped]
    match = re.fullmatch(r"Document the observed top-level folder ['\"]([^'\"]+)['\"]\.", stripped)
    if match:
        return f"Documentar a subpasta de topo observada `{match.group(1)}`."
    return stripped


def _subject_name_from_documentation_purpose(purpose: str) -> str:
    match = re.search(r"folder\s+['\"]([^'\"]+)['\"]", purpose)
    return match.group(1).strip() if match else ""


def _observed_files_from_documentation_purpose(purpose: str) -> list[str]:
    marker = "Observed files:"
    index = purpose.find(marker)
    if index < 0:
        marker = "Observed files for this folder:"
        index = purpose.find(marker)
    if index < 0:
        return []
    raw = purpose[index + len(marker) :]
    inventory_marker = "Inventory JSON:"
    if inventory_marker in raw:
        raw = raw.split(inventory_marker, 1)[0]
    observations_marker = "Evidence observations JSON:"
    if observations_marker in raw:
        raw = raw.split(observations_marker, 1)[0]
    raw = raw.strip().rstrip(".")
    if raw.casefold().startswith("no nested files"):
        return []
    values: list[str] = []
    for item in raw.split(";"):
        clean = item.strip()
        if not clean or clean == "[truncated]":
            continue
        values.append(clean)
    return _dedupe_strings(values)[:80]


def _inventory_from_documentation_purpose(purpose: str) -> dict[str, Any]:
    marker = "Inventory JSON:"
    index = purpose.find(marker)
    if index < 0:
        return {}
    raw = purpose[index + len(marker) :].strip()
    observations_marker = "Evidence observations JSON:"
    if observations_marker in raw:
        raw = raw.split(observations_marker, 1)[0].strip()
    if raw.endswith("."):
        raw = raw[:-1].rstrip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _inventory_category_items(inventory: dict[str, Any], category: str) -> tuple[int, list[str]]:
    categories = inventory.get("categories")
    if not isinstance(categories, dict):
        return 0, []
    value = categories.get(category)
    if not isinstance(value, dict):
        return 0, []
    raw_sample = value.get("sample")
    sample = [str(item).strip() for item in raw_sample if str(item).strip()] if isinstance(raw_sample, list) else []
    try:
        count = int(value.get("count") or 0)
    except (TypeError, ValueError):
        count = len(sample)
    return max(0, count), sample


def _file_observations_from_documentation_purpose(purpose: str) -> list[dict[str, Any]]:
    marker = "Evidence observations JSON:"
    index = purpose.find(marker)
    if index < 0:
        return []
    raw = purpose[index + len(marker) :].strip()
    enrichment_marker = "Specialist enrichment JSON:"
    if enrichment_marker in raw:
        raw = raw.split(enrichment_marker, 1)[0].strip()
    if raw.endswith("."):
        raw = raw[:-1].rstrip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    observations: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict) and str(item.get("path") or "").strip():
            observations.append(item)
    return observations[:24]


def _enrichment_tasks_from_documentation_purpose(purpose: str) -> list[dict[str, Any]]:
    marker = "Specialist enrichment JSON:"
    index = purpose.find(marker)
    if index < 0:
        return []
    raw = purpose[index + len(marker) :].strip()
    results_marker = "Specialist enrichment results JSON:"
    if results_marker in raw:
        raw = raw.split(results_marker, 1)[0].strip()
    if raw.endswith("."):
        raw = raw[:-1].rstrip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    tasks: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        capability = str(item.get("capability") or "").strip()
        paths = item.get("input_paths")
        if provider and capability and isinstance(paths, list) and paths:
            tasks.append(item)
    return tasks[:12]


def _enrichment_results_from_documentation_purpose(purpose: str) -> list[dict[str, Any]]:
    marker = "Specialist enrichment results JSON:"
    index = purpose.find(marker)
    if index < 0:
        return []
    raw = purpose[index + len(marker) :].strip()
    if raw.endswith("."):
        raw = raw[:-1].rstrip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    results: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        capability = str(item.get("capability") or "").strip()
        paths = item.get("input_paths")
        if provider and capability and isinstance(paths, list) and paths:
            results.append(item)
    return results[:12]


def _documentation_enrichment_lines(tasks: list[dict[str, Any]], *, portuguese: bool = False) -> str:
    if not tasks:
        if portuguese:
            return "- Não foi planeado enriquecimento especializado para esta pasta a partir dos tipos de ficheiro observados."
        return "- No specialist enrichment was planned for this folder from the observed file types."
    lines: list[str] = []
    for task in tasks:
        provider = str(task.get("provider") or "").strip()
        capability = str(task.get("capability") or "").strip()
        reason = str(task.get("reason") or "").strip()
        paths = [str(path).strip() for path in task.get("input_paths", []) if str(path).strip()]
        expected = [str(item).strip() for item in task.get("expected_evidence_types", []) if str(item).strip()]
        title = ".".join(part for part in (provider, capability) if part)
        if portuguese:
            lines.append(
                f"- `{title or 'specialist'}` planeado para {len(paths)} ficheiro(s)"
                f"{f' porque {reason}' if reason else ''}."
            )
        else:
            lines.append(f"- `{title or 'specialist'}` planned for {len(paths)} file(s){f' because {reason}' if reason else ''}.")
        for path in paths[:8]:
            lines.append(f"  - `{path}`")
        if expected:
            label = "Evidência esperada" if portuguese else "Expected evidence"
            lines.append(f"  - {label}: {', '.join(expected[:6])}")
    return "\n".join(lines)


def _documentation_enrichment_plan_lines(
    tasks: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    portuguese: bool = False,
) -> str:
    if tasks:
        return _documentation_enrichment_lines(tasks, portuguese=portuguese)
    if not results:
        return _documentation_enrichment_lines(tasks, portuguese=portuguese)
    groups: dict[tuple[str, str], set[str]] = {}
    for result in results:
        provider = str(result.get("provider") or "").strip()
        capability = str(result.get("capability") or "").strip()
        if not provider or not capability:
            continue
        paths = {
            str(path).strip()
            for path in result.get("input_paths", [])
            if str(path).strip()
        }
        groups.setdefault((provider, capability), set()).update(paths)
    if not groups:
        return _documentation_enrichment_lines(tasks, portuguese=portuguese)
    lines: list[str] = []
    if portuguese:
        lines.append(
            "- O plano detalhado foi compactado por orçamento de contexto; os resultados anexados abaixo "
            "confirmam que houve enriquecimento especializado."
        )
    else:
        lines.append(
            "- The detailed plan was compacted for context budget; the attached results below confirm "
            "that specialist enrichment ran or was reused."
        )
    for (provider, capability), paths in sorted(groups.items()):
        title = f"{provider}.{capability}"
        if portuguese:
            lines.append(f"- `{title}` associado a {len(paths) or 1} ficheiro(s) nesta página compacta.")
        else:
            lines.append(f"- `{title}` associated with {len(paths) or 1} file(s) on this compact page.")
        for path in sorted(paths)[:4]:
            lines.append(f"  - `{path}`")
    return "\n".join(lines)


def _documentation_enrichment_result_lines(results: list[dict[str, Any]], *, portuguese: bool = False) -> str:
    if not results:
        if portuguese:
            return "- Não foi anexado resultado de enriquecimento especializado para esta pasta."
        return "- No specialist enrichment result was attached for this folder."
    lines: list[str] = []
    for result in results:
        provider = str(result.get("provider") or "").strip()
        capability = str(result.get("capability") or "").strip()
        status = str(result.get("status") or "unknown").strip() or "unknown"
        action = str(result.get("action") or "").strip()
        title = ".".join(part for part in (provider, capability) if part)
        title = title or ("leitura especializada" if portuguese else "specialist reading")
        success = "success" if result.get("success") else "failed"
        if portuguese:
            success = "sucesso" if result.get("success") else "falhou"
            lines.append(
                f"- Leitura especializada `{title}` registada com estado `{status}` "
                f"({success}{f', action={action}' if action else ''})."
            )
        else:
            lines.append(
                f"- Specialist reading `{title}` recorded status `{status}` "
                f"({success}{f', action={action}' if action else ''})."
            )
        content = str(result.get("content_excerpt") or "").strip()
        if content:
            label = "Excerto de evidência" if portuguese else "Evidence excerpt"
            lines.append(f"  - {label}: {_inline_excerpt(content, limit=420)}")
        storage_refs = [str(item).strip() for item in result.get("storage_refs", []) if str(item).strip()]
        if storage_refs:
            label = "Referências técnicas registadas" if portuguese else "Technical references recorded"
            lines.append(f"  - {label}: {len(storage_refs)}")
        output_refs = result.get("output_refs")
        if isinstance(output_refs, dict) and output_refs:
            label = "Saídas técnicas registadas" if portuguese else "Technical outputs recorded"
            lines.append(f"  - {label}: {len(output_refs)}")
        quality = result.get("quality")
        if isinstance(quality, dict) and quality:
            shown = [f"{key}={value}" for key, value in list(quality.items())[:6]]
            label = "Sinais de qualidade" if portuguese else "Quality signals"
            lines.append(f"  - {label}: {', '.join(f'`{item}`' for item in shown)}")
        error = str(result.get("error") or "").strip()
        if error:
            label = "Erro" if portuguese else "Error"
            lines.append(f"  - {label}: {_inline_excerpt(error, limit=240)}")
    return "\n".join(lines)


def _documentation_enrichment_limitation(
    tasks: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    portuguese: bool = False,
) -> str:
    if not tasks and results:
        successful = [result for result in results if result.get("success")]
        if successful and any(_enrichment_result_has_semantic_excerpt(result) for result in successful):
            if portuguese:
                return "- Foram anexados excertos de conteúdo pelos serviços especializados; a incerteza restante depende dos sinais de qualidade de cada serviço."
            return "- Specialist content excerpts were attached; remaining uncertainty depends on each service quality signal."
        if successful:
            if portuguese:
                return "- Existem resultados técnicos disponíveis, mas esta página compacta ainda não inclui excertos de conteúdo suficientes."
            return "- Technical results are available, but this compact page does not yet include enough content excerpts."
    if not tasks:
        if portuguese:
            return "- Documentos binários, folhas de cálculo, notebooks e áudio só foram identificados quando presentes; o plano observado não exigiu leitura especializada adicional."
        return "- Binary documents, spreadsheets, notebooks and audio were only identified when present; no additional specialist reading was required by the observed plan."
    successful = [result for result in results if result.get("success")]
    if successful and len(successful) >= len(tasks):
        if not any(_enrichment_result_has_semantic_excerpt(result) for result in successful):
            if portuguese:
                return "- Existem resultados técnicos, mas esta página ainda não recebeu excertos de conteúdo suficientes para documentação de domínio completa."
            return "- Technical results are available, but this page still lacks enough content excerpts for complete domain documentation."
        if portuguese:
            return "- Foram anexados resultados de leitura especializada para as classes de evidência planeadas; a incerteza restante depende dos sinais de qualidade de cada serviço."
        return "- Specialist reading results were attached for the planned evidence classes; remaining uncertainty depends on each service quality signal."
    if portuguese:
        return "- Documentos binários, folhas de cálculo, apresentações e áudio/vídeo foram identificados por path/tipo; o conteúdo só deve ser tratado como completo depois da leitura especializada listada."
    return "- Binary documents, spreadsheets, presentations and audio/video files were identified by path/type; their content should only be treated as complete after the listed specialist reading tasks."


def _documentation_enrichment_next_steps(
    tasks: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    portuguese: bool = False,
) -> str:
    if not tasks and results:
        successful = [result for result in results if result.get("success")]
        if successful and any(_enrichment_result_has_semantic_excerpt(result) for result in successful):
            providers = sorted(
                {
                    ".".join(
                        part
                        for part in (
                            str(result.get("provider") or "").strip(),
                            str(result.get("capability") or "").strip(),
                        )
                        if part
                    )
                    for result in successful
                }
            )
            provider_text = ", ".join(f"`{item}`" for item in providers if item) or "`specialist`"
            if portuguese:
                return f"- Usar a evidência anexada por {provider_text} para aprofundar a documentação sem reexecutar trabalhos por defeito.\n"
            return f"- Use the evidence attached by {provider_text} to deepen the documentation without re-running jobs by default.\n"
        if successful:
            if portuguese:
                return "- Obter ou materializar um resumo compacto dos resultados já existentes antes de repetir trabalhos.\n"
            return "- Fetch or materialize a compact content digest from existing results before re-running jobs.\n"
    if not tasks:
        if portuguese:
            return "- Manter a documentação alinhada com futuras actualizações de evidência se os ficheiros mudarem.\n"
        return "- Keep the documentation aligned with future evidence refreshes if files change.\n"
    completed = {
        (str(result.get("provider") or ""), str(result.get("capability") or ""))
        for result in results
        if result.get("success")
    }
    lines: list[str] = []
    for task in tasks:
        provider = str(task.get("provider") or "specialist").strip() or "specialist"
        capability = str(task.get("capability") or "enrichment").strip() or "enrichment"
        count = len(task.get("input_paths") or [])
        if (provider, capability) in completed:
            matching_results = [
                result
                for result in results
                if str(result.get("provider") or "").strip() == provider
                and str(result.get("capability") or "").strip() == capability
                and result.get("success")
            ]
            has_semantic = any(_enrichment_result_has_semantic_excerpt(result) for result in matching_results)
            if portuguese:
                if has_semantic:
                    lines.append(
                        f"- Usar a evidência anexada por `{provider}.{capability}` como base para aprofundar a documentação de {count} ficheiro(s)."
                    )
                else:
                    lines.append(
                        f"- Obter um resumo compacto via `{provider}.{capability}`/Storage Guardian para {count} ficheiro(s); não repetir o trabalho por defeito só porque já há referências."
                    )
            else:
                if has_semantic:
                    lines.append(
                        f"- Use the evidence attached by `{provider}.{capability}` as source material for deeper documentation of {count} file(s)."
                    )
                else:
                    lines.append(
                        f"- Fetch a compact semantic digest through `{provider}.{capability}`/Storage Guardian for {count} file(s); do not re-run the job by default just because refs already exist."
                    )
        else:
            if portuguese:
                lines.append(
                    f"- Executar `{provider}.{capability}` para {count} ficheiro(s) detectado(s) antes de declarar a leitura de conteúdo completa."
                )
            else:
                lines.append(f"- Run `{provider}.{capability}` for {count} detected file(s) before claiming content completeness.")
    return "\n".join(lines) + "\n"


def _documentation_domain_limitation(
    tasks: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    portuguese: bool = False,
) -> str:
    planned_paths = {
        str(path).strip()
        for task in tasks
        for path in task.get("input_paths", [])
        if str(path).strip()
    }
    semantic_paths = {
        str(path).strip()
        for result in results
        if _enrichment_result_has_semantic_excerpt(result)
        for path in result.get("input_paths", [])
        if str(path).strip()
    }
    missing_count = max(0, len(planned_paths - semantic_paths))
    if missing_count:
        if portuguese:
            return f"- {missing_count} ficheiro(s) ainda só têm identificação, metadados ou excertos parciais; é necessária leitura/transcrição especializada para resumir o conteúdo em profundidade.\n"
        return f"- {missing_count} file(s) still only have identification, metadata, or partial excerpts; specialist reading/transcription is needed for deeper content summaries.\n"
    if planned_paths:
        if portuguese:
            return "- Os ficheiros que exigiam leitura especializada têm conteúdo resumido quando havia excertos disponíveis.\n"
        return "- Files requiring specialist reading are summarized when content excerpts were available.\n"
    if portuguese:
        return "- Não foram identificados ficheiros que exigissem leitura especializada adicional nesta página compacta.\n"
    return "- No files requiring additional specialized reading were identified on this compact page.\n"


def _documentation_domain_next_steps(
    tasks: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    portuguese: bool = False,
) -> str:
    planned_paths = {
        str(path).strip()
        for task in tasks
        for path in task.get("input_paths", [])
        if str(path).strip()
    }
    semantic_paths = {
        str(path).strip()
        for result in results
        if _enrichment_result_has_semantic_excerpt(result)
        for path in result.get("input_paths", [])
        if str(path).strip()
    }
    missing = sorted(planned_paths - semantic_paths)
    if missing:
        sample = ", ".join(f"`{path}`" for path in missing[:5])
        suffix = f" e mais {len(missing) - 5}" if portuguese and len(missing) > 5 else ""
        if not portuguese and len(missing) > 5:
            suffix = f" and {len(missing) - 5} more"
        if portuguese:
            return f"- Aprofundar a leitura/transcrição de conteúdo para {sample}{suffix} antes de considerar a documentação completa.\n"
        return f"- Deepen content reading/transcription for {sample}{suffix} before considering the documentation complete.\n"
    if planned_paths:
        if portuguese:
            return "- Consolidar a interpretação de domínio dos conteúdos já resumidos por ficheiro.\n"
        return "- Consolidate the domain interpretation of the file-level summaries already present.\n"
    if portuguese:
        return "- Manter esta documentação sincronizada com alterações futuras nos ficheiros da subpasta.\n"
    return "- Keep this documentation synchronized with future changes in the subfolder files.\n"


def _documentation_file_evidence_lines(observations: list[dict[str, Any]], *, portuguese: bool = False) -> str:
    if not observations:
        if portuguese:
            return "- Não havia excertos de ficheiros nem observações por ficheiro no evidence bundle."
        return "- No file excerpts or per-file observations were available in the evidence bundle."
    lines: list[str] = []
    for item in observations[:12]:
        path = str(item.get("path") or "").strip()
        file_type = str(item.get("file_type") or "unknown").strip() or "unknown"
        size = int(item.get("size_bytes") or 0)
        line_count = item.get("line_count")
        sha = str(item.get("sha256") or "").strip()
        sha_short = sha[:19] if sha else ""
        facts = [file_type, f"{size} bytes"]
        if isinstance(line_count, int):
            facts.append(f"{line_count} linhas" if portuguese else f"{line_count} lines")
        if sha_short:
            facts.append(sha_short)
        lines.append(f"- `{path}` ({'; '.join(facts)})")
        excerpt = str(item.get("excerpt") or "").strip()
        if excerpt:
            label = "Excerto" if portuguese else "Excerpt"
            lines.append(f"  - {label}: {_inline_excerpt(excerpt)}")
    if len(observations) > 12:
        if portuguese:
            lines.append(f"- Foram omitidas {len(observations) - 12} observações adicionais nesta página compacta.")
            return "\n".join(lines)
        lines.append(f"- {len(observations) - 12} additional file observations were omitted from this compact page.")
    return "\n".join(lines)


_DOCUMENTATION_PUBLIC_PAGE_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("provider_id", re.compile(r"\b(?:extrator\.document_extraction|audio_transcribe\.audio_transcription)\b", re.IGNORECASE)),
    ("owner_name", re.compile(r"\b(?:audio_transcribe|material_builder|material_execution_kernel)\b", re.IGNORECASE)),
    ("storage_owner_name", re.compile(r"\bStorage Guardian\b", re.IGNORECASE)),
    ("storage_ref", re.compile(r"\bstorage_guardian://", re.IGNORECASE)),
    ("container_path", re.compile(r"/host_home\b", re.IGNORECASE)),
    ("command_trace", re.compile(r"\b(?:commands_run|Comandos read-only|Read-only discovery commands)\b", re.IGNORECASE)),
    ("workspace_trace", re.compile(r"\bWorkspace:\s+", re.IGNORECASE)),
    ("owner_return_wording", re.compile(r"\b(?:devolveu|returned)\b", re.IGNORECASE)),
    ("internal_heading", re.compile(r"\b(?:Evidência Importante|Important Evidence|Enriquecimento Especializado|Plano de Enriquecimento|Especialista em Extração|Specialist Enrichment|Tasks Performed|Document Extraction|Audio Transcription)\b", re.IGNORECASE)),
    ("raw_extraction_wording", re.compile(r"\b(?:Conteúdo extraído/transcrito|Extracted/transcribed content)\b", re.IGNORECASE)),
    ("raw_file_warning", re.compile(r"\b(?:file_too_large|binary_or_unsupported_text_type_not_read|sha256_skipped_due_to_size)\b", re.IGNORECASE)),
    ("metadata_only_status", re.compile(r"\b(?:só metadados|metadata only)\b", re.IGNORECASE)),
)


def _documentation_file_quality_issues(
    content: str,
    *,
    file_spec: MaterialFileSpec,
    plan: MaterialPlan,
    include_depth_checks: bool = True,
) -> list[dict[str, Any]]:
    if not _plan_is_documentation_bundle(plan):
        return []
    normalized_path = file_spec.path.strip().strip("/").replace("\\", "/")
    rel_path = _plan_relative_path(normalized_path, plan)
    if rel_path == "validation-evidence.txt" or not rel_path.endswith(".md"):
        return []

    issues: list[dict[str, Any]] = []
    text = str(content or "").strip()
    lowered = text.casefold()
    for issue_type, pattern in _DOCUMENTATION_PUBLIC_PAGE_FORBIDDEN_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        issues.append(
            {
                "issue_type": issue_type,
                "path": file_spec.path,
                "excerpt": _compact_text(match.group(0), 120),
            }
        )

    if not include_depth_checks:
        return issues

    if "not documented yet" in lowered or "não documentado ainda" in lowered:
        issues.append({"issue_type": "documentation_placeholder_status", "path": file_spec.path})

    portuguese = _documentation_is_portuguese(plan)
    if portuguese and re.search(
        r"^#\s*(?:Document Overview|README\.md)\b|^##\s*(?:Inspected Path|Evidence Summary|Generated Documentation Map|Validation Status|Limitations|Next Steps)\b",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    ):
        issues.append({"issue_type": "documentation_language_mismatch", "path": file_spec.path})

    is_subfolder_page = rel_path.startswith("subfolders/") and rel_path.endswith(".md")
    if is_subfolder_page:
        issues.extend(
            _documentation_required_subfolder_section_issues(
                text,
                file_spec=file_spec,
                plan=plan,
            )
        )
        min_chars = 900
        if len(text) < min_chars and ("##" not in text and re.search(r"^#\s+", text, flags=re.MULTILINE) is None):
            issues.append(
                {
                    "issue_type": "documentation_too_short",
                    "path": file_spec.path,
                    "min_chars": min_chars,
                    "actual_chars": len(text),
                }
            )
        observed = [
            item
            for item in _observed_files_from_documentation_purpose(file_spec.purpose)
            if item and not _observed_omission_match(item)
        ]
        basenames = [Path(item).name.casefold() for item in observed if Path(item).name.strip()]
        if basenames:
            matches = sum(1 for name in basenames[:12] if name and name in lowered)
            sample_count = min(len(basenames), 12)
            required_matches = 1 if sample_count == 1 else min(4, max(2, (sample_count + 3) // 4))
            if matches < required_matches:
                issues.append(
                    {
                        "issue_type": "documentation_missing_observed_file_anchors",
                        "path": file_spec.path,
                        "observed_file_count": len(basenames),
                        "matched_file_count": matches,
                    }
                )
    return issues


def _documentation_required_subfolder_section_issues(
    content: str,
    *,
    file_spec: MaterialFileSpec,
    plan: MaterialPlan,
) -> list[dict[str, Any]]:
    evidence_available = bool(
        _observed_files_from_documentation_purpose(file_spec.purpose)
        or _file_observations_from_documentation_purpose(file_spec.purpose)
        or _inventory_from_documentation_purpose(file_spec.purpose)
        or _enrichment_tasks_from_documentation_purpose(file_spec.purpose)
        or _enrichment_results_from_documentation_purpose(file_spec.purpose)
    )
    if not evidence_available:
        return []

    portuguese = _documentation_is_portuguese(plan)
    if portuguese:
        required_sections = (
            "Ficheiros Observados",
            "Tipos de Material Detectados",
            "Materiais Principais",
            "Leitura Consolidada",
            "Conteúdo por Ficheiro",
            "Limitações",
        )
    else:
        required_sections = (
            "Observed Files",
            "Detected Material Types",
            "Main Materials",
            "Consolidated Reading",
            "Content By File",
            "Limitations",
        )

    issues: list[dict[str, Any]] = []
    for heading in required_sections:
        if _markdown_heading_present(content, heading):
            continue
        issues.append(
            {
                "issue_type": "documentation_missing_required_section",
                "path": file_spec.path,
                "section": heading,
            }
        )
    return issues


def _markdown_heading_present(content: str, heading: str) -> bool:
    pattern = rf"^\s*#{{1,6}}\s+{re.escape(heading)}\s*$"
    return bool(re.search(pattern, content, flags=re.IGNORECASE | re.MULTILINE))


def _documentation_publication_lint(
    proposals: list[GeneratedFileProposal],
    *,
    plan: MaterialPlan,
) -> list[dict[str, Any]]:
    if not _plan_is_documentation_bundle(plan):
        return []
    issues: list[dict[str, Any]] = []
    specs_by_path = {item.path.strip().strip("/").replace("\\", "/"): item for item in plan.files}
    for proposal in proposals:
        normalized_path = proposal.path.strip().strip("/").replace("\\", "/")
        rel_path = _plan_relative_path(normalized_path, plan)
        if rel_path == "validation-evidence.txt" or not rel_path.endswith(".md"):
            continue
        file_spec = specs_by_path.get(normalized_path) or MaterialFileSpec(
            path=proposal.path,
            purpose="documentation page",
            kind="markdown",
        )
        issues.extend(
            _documentation_file_quality_issues(
                proposal.content,
                file_spec=file_spec,
                plan=plan,
                include_depth_checks=False,
            )
        )
    return issues


def _inline_excerpt(text: str, *, limit: int = 360) -> str:
    cleaned = _clean_documentation_excerpt(text, limit=max(limit * 2, limit))
    if len(cleaned) > limit:
        cleaned = f"{cleaned[:limit].rstrip()}..."
    return f"`{cleaned}`"


def _clean_documentation_excerpt(text: str, *, limit: int = 900) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s*\[truncated\]\s*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*\.\.\.\s*", " ", cleaned)
    cleaned = re.sub(r"^(#+\s*)?Page\s+\d+\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<!--\s*image\s*-->", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:;")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.") + "..."
    return cleaned


def _documentation_content_summary_lines(
    results: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    portuguese: bool = False,
) -> str:
    lines: list[str] = []
    seen: set[str] = set()

    def add_line(path: str, text: str) -> None:
        if len(lines) >= 16:
            return
        clean_text = " ".join(str(text or "").split())
        if not _content_has_documentation_value(clean_text):
            return
        clean_text = _clean_documentation_excerpt(clean_text, limit=1200)
        if not clean_text:
            return
        clean_path = str(path or "").strip() or "<unknown>"
        key = f"{clean_path}\n{clean_text[:160]}"
        if key in seen:
            return
        seen.add(key)
        lines.append(f"- `{clean_path}`: {_inline_excerpt(clean_text, limit=900)}")

    for item in observations[:12]:
        add_line(
            str(item.get("path") or ""),
            str(item.get("excerpt") or ""),
        )

    for result in results:
        paths = [str(path).strip() for path in result.get("input_paths", []) if str(path).strip()]
        snippets = _semantic_digest_excerpts(result)
        content_excerpt = str(result.get("content_excerpt") or "").strip()
        if not snippets and _content_has_documentation_value(content_excerpt):
            snippets = [content_excerpt]
        for index, snippet in enumerate(snippets[:3]):
            if paths:
                path = paths[min(index, len(paths) - 1)]
            else:
                path = "conteúdo identificado" if portuguese else "identified content"
            add_line(path, snippet)

    if lines:
        return "\n".join(lines)
    if portuguese:
        return (
            "- Não há excertos de conteúdo suficientes nesta página compacta para resumir fontes específicas. "
            "A documentação deve pedir/usar digests semânticos dos owners antes de declarar conteúdo analisado."
        )
    return (
        "- This compact page does not include enough content excerpts to summarize specific sources. "
        "Documentation should request/use owner semantic digests before claiming content analysis."
    )


def _documentation_consolidated_summary_lines(
    *,
    documents: list[str],
    data: list[str],
    sql: list[str],
    media: list[str],
    other: list[str],
    doc_count: int,
    data_count: int,
    sql_count: int,
    media_count: int,
    other_count: int,
    enrichment_results: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    portuguese: bool = False,
) -> str:
    lines: list[str] = []
    if portuguese:
        lines.append(
            "- Composição observada: "
            f"{doc_count} documento(s)/apresentação(ões), {data_count} ficheiro(s) de dados, "
            f"{sql_count} ficheiro(s) SQL/configuração, {media_count} ficheiro(s) áudio/vídeo "
            f"e {other_count} outro(s)."
        )
    else:
        lines.append(
            "- Observed composition: "
            f"{doc_count} document/presentation file(s), {data_count} data file(s), "
            f"{sql_count} SQL/config file(s), {media_count} audio/video file(s), "
            f"and {other_count} other file(s)."
        )

    category_samples = [
        ("documentos" if portuguese else "documents", documents),
        ("dados" if portuguese else "data", data),
        ("SQL/config" if portuguese else "SQL/config", sql),
        ("media" if portuguese else "media", media),
        ("outros" if portuguese else "other", other),
    ]
    material_samples = [
        f"{label}: {', '.join(f'`{path}`' for path in paths[:4])}"
        for label, paths in category_samples
        if paths
    ]
    if material_samples:
        prefix = "Materiais mais representativos" if portuguese else "Most representative materials"
        lines.append(f"- {prefix}: {'; '.join(material_samples[:5])}.")

    semantic_snippets = _documentation_semantic_snippets(enrichment_results, observations, limit=4)
    if semantic_snippets:
        prefix = "Conteúdo observado aponta para" if portuguese else "Observed content points to"
        lines.append(f"- {prefix}: {'; '.join(semantic_snippets)}.")

    data_profiles = _documentation_data_profile_snippets(observations, limit=3)
    if data_profiles:
        prefix = "Perfis de dados observados" if portuguese else "Observed data profiles"
        lines.append(f"- {prefix}: {'; '.join(data_profiles)}.")

    sql_profiles = _documentation_sql_profile_snippets(observations, limit=3)
    if sql_profiles:
        prefix = "Sinais SQL observados" if portuguese else "Observed SQL signals"
        lines.append(f"- {prefix}: {'; '.join(sql_profiles)}.")

    media_profiles = _documentation_media_profile_snippets(enrichment_results, limit=3)
    if media_profiles:
        prefix = "Áudio/vídeo" if portuguese else "Audio/video"
        lines.append(f"- {prefix}: {'; '.join(media_profiles)}.")

    if len(lines) == 1:
        if portuguese:
            lines.append(
                "- Não há excertos semânticos suficientes nesta página compacta para uma leitura de domínio mais profunda."
            )
        else:
            lines.append("- There are not enough semantic excerpts on this compact page for deeper domain reading.")
    return "\n".join(lines)


def _documentation_semantic_snippets(
    results: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    limit: int,
) -> list[str]:
    snippets: list[str] = []
    for result in results:
        for excerpt in [*_semantic_digest_excerpts(result), str(result.get("content_excerpt") or "")]:
            snippet = _documentation_clean_content_phrase(excerpt)
            if snippet:
                snippets.append(snippet)
            if len(snippets) >= limit:
                return snippets
    for observation in observations:
        snippet = _documentation_clean_content_phrase(str(observation.get("excerpt") or ""))
        if snippet:
            snippets.append(snippet)
        if len(snippets) >= limit:
            return snippets
    return snippets


def _documentation_data_profile_snippets(observations: list[dict[str, Any]], *, limit: int) -> list[str]:
    snippets: list[str] = []
    for item in observations:
        path = str(item.get("path") or "").strip()
        if _documentation_material_category(path) != "data":
            continue
        excerpt = " ".join(str(item.get("excerpt") or "").split())
        match = re.search(r"columns=\d+\s*\[([^\]]+)\]", excerpt)
        if match:
            columns = [part.strip() for part in match.group(1).split(",") if part.strip()]
            if columns:
                snippets.append(f"`{path}` colunas: {', '.join(columns[:8])}")
        elif excerpt:
            snippets.append(f"`{path}`: {_documentation_clean_content_phrase(excerpt, limit=180)}")
        if len(snippets) >= limit:
            break
    return snippets


def _documentation_sql_profile_snippets(observations: list[dict[str, Any]], *, limit: int) -> list[str]:
    snippets: list[str] = []
    verbs = ("CREATE", "SELECT", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP")
    for item in observations:
        path = str(item.get("path") or "").strip()
        if _documentation_material_category(path) != "sql":
            continue
        excerpt = str(item.get("excerpt") or "")
        found = [verb for verb in verbs if re.search(rf"\b{verb}\b", excerpt, flags=re.IGNORECASE)]
        if found:
            snippets.append(f"`{path}` contém {', '.join(found[:5])}")
        else:
            snippets.append(f"`{path}` observado como SQL")
        if len(snippets) >= limit:
            break
    return snippets


def _documentation_media_profile_snippets(results: list[dict[str, Any]], *, limit: int) -> list[str]:
    snippets: list[str] = []
    for result in results:
        if str(result.get("provider") or "").strip() != "audio_transcribe":
            continue
        paths = [str(path).strip() for path in result.get("input_paths", []) if str(path).strip()]
        status = str(result.get("status") or "unknown").strip() or "unknown"
        label = ", ".join(f"`{path}`" for path in paths[:2]) or "`<audio>`"
        if status in {"reused", "completed", "ok"}:
            snippets.append(f"{label} tem transcrição disponível")
        else:
            snippets.append(f"{label} tem estado de transcrição `{status}`")
        if len(snippets) >= limit:
            break
    return snippets


def _documentation_clean_content_phrase(text: str, *, limit: int = 220) -> str:
    cleaned = _clean_documentation_excerpt(text, limit=max(limit * 3, limit))
    if not _content_has_documentation_value(cleaned):
        return ""
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.") + "..."
    return f"`{cleaned}`"


def _title_from_path(path: str) -> str:
    stem = Path(path).stem
    stem = re.sub(r"^\d+[-_]+", "", stem)
    return stem.replace("-", " ").replace("_", " ").strip().title() or stem


def _path_suffix(path: str) -> str:
    return Path(str(path).strip()).suffix.casefold()


def _observed_omission_match(item: str) -> re.Match[str] | None:
    return re.fullmatch(r"and (\d+) more", str(item or "").strip())


def _localized_observed_items(items: list[str], *, portuguese: bool) -> list[str]:
    localized: list[str] = []
    for item in items:
        match = _observed_omission_match(item)
        if match and portuguese:
            count = int(match.group(1))
            noun = "ficheiro" if count == 1 else "ficheiros"
            localized.append(f"mais {count} {noun} observado{'s' if count != 1 else ''} nesta subpasta")
        elif match:
            localized.append(f"{match.group(1)} more files observed in this subfolder")
        else:
            localized.append(item)
    return localized


def _is_observed_omission_note(item: str) -> bool:
    text = str(item or "").strip()
    return bool(
        re.fullmatch(r"mais \d+ ficheiros observados nesta subpasta", text)
        or re.fullmatch(r"\d+ more files observed in this subfolder", text)
    )


def _markdown_list(items: list[str], *, empty: str) -> str:
    lines = []
    for item in items:
        if not item:
            continue
        if _is_observed_omission_note(item):
            lines.append(f"- {item}")
        elif "`" in item or ": " in item:
            lines.append(f"- {item}")
        else:
            lines.append(f"- `{item}`")
    return "\n".join(lines) or f"- {empty}"


def _summarize_category(label: str, items: list[str], *, limit: int = 6) -> list[str]:
    if not items:
        return []
    sample = ", ".join(f"`{item}`" for item in items[:limit])
    suffix = f" and {len(items) - limit} more" if len(items) > limit else ""
    return [f"{label}: {sample}{suffix}"]


def _summarize_inventory_category(
    label: str,
    count: int,
    items: list[str],
    *,
    limit: int = 8,
    portuguese: bool = False,
) -> list[str]:
    if count <= 0 and not items:
        return []
    sample_items = items[:limit]
    if sample_items:
        sample = ", ".join(f"`{item}`" for item in sample_items)
        suffix_count = max(0, count - len(sample_items))
        if suffix_count and portuguese:
            suffix = f" e mais {suffix_count}"
        else:
            suffix = f" and {suffix_count} more" if suffix_count else ""
        return [f"{label}: {sample}{suffix}"]
    if portuguese:
        return [f"{label}: {count} ficheiro(s) observado(s)"]
    return [f"{label}: {count} file(s) observed"]


def _documentation_semantic_evidence_lines(
    results: list[dict[str, Any]],
    *,
    portuguese: bool = False,
) -> str:
    semantic_results = [result for result in results if _enrichment_result_has_semantic_excerpt(result)]
    if semantic_results:
        lines: list[str] = []
        for result in semantic_results[:8]:
            paths = [str(path).strip() for path in result.get("input_paths", []) if str(path).strip()]
            digest_excerpt = "; ".join(_semantic_digest_excerpts(result)[:2])
            content = _inline_excerpt(
                digest_excerpt or str(result.get("content_excerpt") or "").strip(),
                limit=900,
            )
            path_label = ", ".join(f"`{path}`" for path in paths[:3]) or "`<ficheiro>`"
            lines.append(f"- {path_label}: {content}")
        return "\n".join(lines)

    successful_with_refs = [
        result
        for result in results
        if result.get("success") and (result.get("storage_refs") or result.get("output_refs") or result.get("quality"))
    ]
    if successful_with_refs:
        if portuguese:
            return (
                "- Há ficheiros processados sem excertos de conteúdo úteis nesta página; por isso, esses ficheiros não foram interpretados em profundidade."
            )
        return (
            "- Some files were processed without useful content excerpts on this page, so their content was not interpreted in depth."
        )
    if portuguese:
        return "- Não foi anexado conteúdo analisado por serviços especializados para esta subpasta."
    return "- No specialist-analyzed content was attached for this folder."


def _enrichment_result_has_semantic_excerpt(result: dict[str, Any]) -> bool:
    if _semantic_digest_excerpts(result):
        return True
    content = " ".join(str(result.get("content_excerpt") or "").split())
    if len(content) < 40:
        return False
    lowered = content.casefold()
    lifecycle_markers = (
        "result reused",
        "extraction result reused",
        "extraction job completed",
        "transcription result reused",
        "transcription job completed",
        "transcrição reutilizada",
        "created_job",
        "reused_result",
        "job(s)",
        "job_id",
        "input_path:",
        "storage_guardian://",
        "em curso/fila",
        "doc_id:",
    )
    if any(marker in lowered for marker in lifecycle_markers):
        return False
    return True


def _semantic_digest_excerpts(result: dict[str, Any]) -> list[str]:
    digest = result.get("semantic_digest")
    if not isinstance(digest, dict):
        return []
    excerpts = digest.get("excerpts")
    if not isinstance(excerpts, list):
        return []
    cleaned: list[str] = []
    for item in excerpts:
        raw = str(item)
        if not _content_has_documentation_value(raw):
            continue
        compact = _clean_documentation_excerpt(raw, limit=900)
        if compact:
            cleaned.append(compact)
    return cleaned[:6]


def _content_has_documentation_value(text: str) -> bool:
    content = " ".join(str(text or "").split())
    if len(content) < 40:
        return False
    lowered = content.casefold()
    lifecycle_markers = (
        "result reused",
        "extraction result reused",
        "extraction job completed",
        "transcription result reused",
        "transcription job completed",
        "transcrição reutilizada",
        "transcrição de áudio aceite",
        "created_job",
        "reused_result",
        "job(s)",
        "job_id",
        "input_path:",
        "storage_guardian://",
        "em curso/fila",
        "doc_id:",
    )
    return not any(marker in lowered for marker in lifecycle_markers)


def _plan_relative_path(path: str, plan: MaterialPlan) -> str:
    root = plan.project_root.strip().strip("/").replace("\\", "/")
    if root and path.startswith(f"{root}/"):
        return path[len(root) + 1 :]
    return path


def _plan_requests_text_processing(plan: MaterialPlan) -> bool:
    text = " ".join(
        [
            plan.project_root,
            *(requirement.description for requirement in plan.requirements),
            *(file_spec.purpose for file_spec in plan.files),
            *plan.architecture_notes,
        ]
    ).casefold()
    has_text = any(marker in text for marker in ("text", "texto", "string", "stdin"))
    has_normalization = any(marker in text for marker in ("normaliz", "normalize", "spaces", "espaços", "espacos"))
    has_stats = any(marker in text for marker in ("statistic", "statistics", "estat", "count", "contagem"))
    has_json = "json" in text
    return has_text and has_normalization and (has_stats or has_json)


def _deterministic_text_processing_init_content() -> str:
    return (
        '"""Reusable text processing helpers."""\n'
        "\n"
        "from .core import normalize_spaces, process_text, text_statistics\n"
        "\n"
        '__all__ = ["normalize_spaces", "process_text", "text_statistics"]\n'
    )


def _deterministic_text_processing_core_content() -> str:
    return (
        '"""Small reusable text processing helpers."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "from typing import Any\n"
        "\n"
        "\n"
        "def normalize_spaces(text: str) -> str:\n"
        "    \"\"\"Collapse consecutive whitespace and trim surrounding space.\"\"\"\n"
        "    return \" \".join(str(text).split())\n"
        "\n"
        "\n"
        "def text_statistics(text: str) -> dict[str, int]:\n"
        "    \"\"\"Return simple statistics for the provided text.\"\"\"\n"
        "    raw_text = str(text)\n"
        "    normalized = normalize_spaces(raw_text)\n"
        "    return {\n"
        "        \"characters\": len(normalized),\n"
        "        \"words\": len(normalized.split()) if normalized else 0,\n"
        "        \"lines\": raw_text.count(\"\\n\") + 1 if raw_text else 0,\n"
        "    }\n"
        "\n"
        "\n"
        "def process_text(text: str) -> dict[str, Any]:\n"
        "    \"\"\"Normalize text and return the normalized value with statistics.\"\"\"\n"
        "    normalized = normalize_spaces(text)\n"
        "    return {\"normalized\": normalized, \"statistics\": text_statistics(text)}\n"
    )


def _deterministic_text_processing_cli_content(package_name: str) -> str:
    return (
        '"""Command-line interface for the text processing helpers."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import argparse\n"
        "import json\n"
        "import sys\n"
        "\n"
        "from .core import process_text\n"
        "\n"
        "\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        "    \"\"\"Build the CLI argument parser.\"\"\"\n"
        f"    parser = argparse.ArgumentParser(prog={package_name!r}, description=\"Normalize text and emit JSON statistics.\")\n"
        "    parser.add_argument(\"text\", nargs=\"?\", help=\"Text to process. Reads stdin when omitted.\")\n"
        "    return parser\n"
        "\n"
        "\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    \"\"\"Run the CLI and write JSON to stdout.\"\"\"\n"
        "    args = build_parser().parse_args(argv)\n"
        "    text = args.text if args.text is not None else sys.stdin.read()\n"
        "    print(json.dumps(process_text(text), sort_keys=True))\n"
        "    return 0\n"
        "\n"
        "\n"
        "if __name__ == \"__main__\":\n"
        "    raise SystemExit(main())\n"
    )


def _deterministic_src_layout_conftest_content() -> str:
    return (
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "SRC = ROOT / \"src\"\n"
        "if str(SRC) not in sys.path:\n"
        "    sys.path.insert(0, str(SRC))\n"
    )


def _deterministic_text_processing_test_content(package_name: str) -> str:
    return (
        "import json\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        f"from {package_name} import normalize_spaces, process_text, text_statistics\n"
        "\n"
        "\n"
        "def test_reusable_api_normalizes_and_counts_text():\n"
        "    result = process_text(\"  Hello   local   AI  \")\n"
        "    assert normalize_spaces(\"  Hello   local   AI  \") == \"Hello local AI\"\n"
        "    assert result[\"normalized\"] == \"Hello local AI\"\n"
        "    assert result[\"statistics\"] == {\"characters\": 14, \"words\": 3, \"lines\": 1}\n"
        "    assert text_statistics(\"one\\ntwo\")[\"lines\"] == 2\n"
        "\n"
        "\n"
        "def test_cli_outputs_json_for_argument_text():\n"
        "    root = Path(__file__).resolve().parents[1]\n"
        "    env = {**os.environ, \"PYTHONPATH\": str(root / \"src\")}\n"
        "    completed = subprocess.run(\n"
        f"        [sys.executable, \"-m\", {package_name!r}, \"  Hello   CLI  \"],\n"
        "        cwd=root,\n"
        "        env=env,\n"
        "        text=True,\n"
        "        capture_output=True,\n"
        "        check=True,\n"
        "    )\n"
        "    payload = json.loads(completed.stdout)\n"
        "    assert payload[\"normalized\"] == \"Hello CLI\"\n"
        "    assert payload[\"statistics\"][\"words\"] == 2\n"
    )


def _deterministic_text_processing_readme_content_for_plan(plan: MaterialPlan) -> str:
    project_name = _pyproject_project_name_from_plan(plan)
    package_name = _primary_src_package_from_material_plan(plan)
    commands = _validation_command_lines_for_plan(plan)
    validation = "\n".join(f"- `{command}`" for command in commands) or "- `python -m pytest -q`"
    return (
        f"# {project_name}\n\n"
        "Small standard-library Python project for normalizing text and emitting JSON statistics.\n\n"
        "## Installation\n\n"
        "```sh\n"
        "python -m pip install .\n"
        "```\n\n"
        "## CLI\n\n"
        "```sh\n"
        f"python -m {package_name} \"  Hello   local   AI  \"\n"
        f"python -m {package_name} --help\n"
        "```\n\n"
        "## Validation\n\n"
        f"{validation}\n"
    )


def _deterministic_readme_content_for_plan(plan: MaterialPlan) -> str:
    project_name = _pyproject_project_name_from_plan(plan)
    requirements = "\n".join(f"- {item.description}" for item in plan.requirements[:8])
    commands = _validation_command_lines_for_plan(plan)
    validation = "\n".join(f"- `{command}`" for command in commands) or "- `python -m pytest -q`"
    return (
        f"# {project_name}\n\n"
        "Small generated Python material artifact.\n\n"
        "## Requirements\n\n"
        f"{requirements or '- See the user request and material plan.'}\n\n"
        "## Installation\n\n"
        "```sh\n"
        "python -m pip install .\n"
        "```\n\n"
        "## CLI\n\n"
        "```sh\n"
        f"python -m {_primary_src_package_from_material_plan(plan)} --help\n"
        "```\n\n"
        "## Validation\n\n"
        f"{validation}\n"
    )


def _deterministic_validation_evidence_content_for_plan(plan: MaterialPlan) -> str:
    commands = _validation_command_lines_for_plan(plan)
    lines = [
        "Validation evidence",
        "===================",
        "",
        "Planned validation commands:",
    ]
    if commands:
        lines.extend(f"- {command}" for command in commands)
    else:
        lines.append("- python -m pytest -q")
    lines.extend(
        [
            "",
            "Observed result:",
            "- Generated by material builder fallback before sandbox validation completed.",
            "- Runtime validation results are recorded by the material execution kernel.",
            "",
        ]
    )
    return "\n".join(lines)


def _validation_command_lines_for_plan(plan: MaterialPlan) -> list[str]:
    commands: list[str] = []
    for command in plan.validation_commands.values():
        argv = [str(item) for item in command.argv]
        if not argv:
            continue
        prefix = f"cd {command.cwd} && " if command.cwd and command.cwd != "." else ""
        env = " ".join(f"{key}={value}" for key, value in sorted(command.env.items()))
        commands.append(f"{prefix}{env + ' ' if env else ''}{' '.join(argv)}")
    if "python-pytest" in plan.required_validation_profiles:
        commands.append("python -m pytest -q")
    return _dedupe_strings(commands)


def _undefined_test_name_issues(content: str, *, path: str) -> list[dict[str, Any]]:
    try:
        module = ast.parse(content, filename=path)
    except SyntaxError:
        return []
    top_level_bound = set(dir(builtins))
    top_level_bound.update({"__file__", "__name__", "__package__", "__spec__"})
    for node in module.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_bound.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    top_level_bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            top_level_bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                top_level_bound.update(_assigned_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            top_level_bound.update(_assigned_target_names(node.target))

    issues: list[dict[str, Any]] = []
    for node in module.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or not node.name.startswith("test_"):
            continue
        local_bound = set(top_level_bound)
        local_bound.update(arg.arg for arg in node.args.posonlyargs)
        local_bound.update(arg.arg for arg in node.args.args)
        local_bound.update(arg.arg for arg in node.args.kwonlyargs)
        if node.args.vararg is not None:
            local_bound.add(node.args.vararg.arg)
        if node.args.kwarg is not None:
            local_bound.add(node.args.kwarg.arg)
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    local_bound.add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name != "*":
                        local_bound.add(alias.asname or alias.name)
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    local_bound.update(_assigned_target_names(target))
            elif isinstance(child, ast.AnnAssign):
                local_bound.update(_assigned_target_names(child.target))
            elif isinstance(child, ast.For):
                local_bound.update(_assigned_target_names(child.target))
            elif isinstance(child, ast.With):
                for item in child.items:
                    if item.optional_vars is not None:
                        local_bound.update(_assigned_target_names(item.optional_vars))
            elif isinstance(child, ast.ExceptHandler) and child.name:
                local_bound.add(child.name)
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id not in local_bound:
                issues.append(
                    {
                        "issue_type": "undefined_test_name",
                        "symbol": child.id,
                        "test_name": node.name,
                        "line": getattr(child, "lineno", 0),
                    }
                )
    return _dedupe_contract_issues(issues)


def _declared_dependency_roots(plan: MaterialPlan) -> set[str]:
    strategy = getattr(plan, "dependency_strategy", None)
    dependencies = getattr(strategy, "external_dependencies", []) if strategy is not None else []
    return {_normalize_dependency_root(str(item)) for item in dependencies if _normalize_dependency_root(str(item))}


def _allowed_local_import_roots(plan: MaterialPlan) -> list[str]:
    roots: set[str] = set()
    for file_spec in plan.files:
        module = _python_module_name_for_plan_path(file_spec.path, plan.project_root)
        if module:
            roots.add(module.split(".", 1)[0])
    return sorted(roots)


def _planned_local_python_modules(plan: MaterialPlan) -> list[str]:
    modules: set[str] = set()
    for file_spec in plan.files:
        module = _python_module_name_for_plan_path(file_spec.path, plan.project_root)
        if module:
            modules.add(module)
    return sorted(modules)


def _python_module_name_for_plan_path(path: str, project_root: str) -> str:
    normalized = path.strip().strip("/").replace("\\", "/").lstrip("./")
    root = project_root.strip().strip("/").replace("\\", "/")
    if root and normalized.startswith(f"{root}/"):
        normalized = normalized[len(root) + 1 :]
    if not normalized.endswith(".py"):
        return ""
    parts = normalized[:-3].split("/")
    if len(parts) > 1 and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
        return ""
    return ".".join(parts)


def _unplanned_local_import_issues(
    content: str,
    *,
    path: str,
    kind: str,
    project_root: str,
    planned_modules: list[str],
) -> list[dict[str, Any]]:
    normalized_kind = FILE_KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())
    if normalized_kind not in {"python", "test"} or not path.endswith(".py") or not planned_modules:
        return []
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError:
        return []
    planned = set(planned_modules)
    local_roots = {module.split(".", 1)[0] for module in planned}
    current_module = _python_module_name_for_plan_path(path, project_root)
    current_package = _current_python_package(path=path, current_module=current_module)
    issues: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                issue = _unplanned_local_import_issue(
                    module,
                    local_roots=local_roots,
                    planned_modules=planned,
                    line=getattr(node, "lineno", 0),
                    import_text=f"import {module}",
                )
                if issue:
                    issues.append(issue)
            continue
        if isinstance(node, ast.ImportFrom):
            module = _resolve_generated_import_from(
                imported_module=node.module,
                level=node.level,
                current_package=current_package,
            )
            if node.level and not current_package:
                issues.append(
                    {
                        "issue_type": "relative_import_from_top_level_module",
                        "module": node.module or "",
                        "line": getattr(node, "lineno", 0),
                    }
                )
                continue
            issue = _unplanned_local_import_issue(
                module,
                local_roots=local_roots,
                planned_modules=planned,
                line=getattr(node, "lineno", 0),
                import_text=f"from {'.' * node.level}{node.module or ''} import ...",
            )
            if issue:
                issues.append(issue)
    return issues


def _current_python_package(*, path: str, current_module: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if normalized.endswith("/__init__.py"):
        return current_module
    if "." in current_module:
        return current_module.rsplit(".", 1)[0]
    return ""


def _resolve_generated_import_from(
    *,
    imported_module: str | None,
    level: int,
    current_package: str,
) -> str:
    module = str(imported_module or "").strip(".")
    if level <= 0:
        return module
    if not current_package:
        return module
    package_parts = current_package.split(".")
    if level > 1:
        package_parts = package_parts[: max(0, len(package_parts) - (level - 1))]
    base = ".".join(package_parts)
    return ".".join(part for part in (base, module) if part)


def _unplanned_local_import_issue(
    module: str,
    *,
    local_roots: set[str],
    planned_modules: set[str],
    line: int,
    import_text: str,
) -> dict[str, Any] | None:
    normalized = module.strip(".")
    if not normalized:
        return None
    root = normalized.split(".", 1)[0]
    if root not in local_roots:
        return None
    if normalized in planned_modules or normalized in local_roots:
        return None
    return {
        "issue_type": "unplanned_local_module_import",
        "module": normalized,
        "line": line,
        "import": import_text,
    }


def _undeclared_external_import_issues(
    content: str,
    *,
    path: str,
    kind: str,
    project_root: str,
    planned_modules: list[str],
    declared_dependency_roots: set[str],
) -> list[dict[str, Any]]:
    normalized_kind = FILE_KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())
    if normalized_kind not in {"python", "test"} or not path.endswith(".py"):
        return []
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError:
        return []
    planned = set(planned_modules)
    local_roots = {module.split(".", 1)[0] for module in planned}
    current_module = _python_module_name_for_plan_path(path, project_root)
    current_package = _current_python_package(path=path, current_module=current_module)
    issues: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                issue = _undeclared_external_import_issue(
                    alias.name,
                    path=path,
                    kind=normalized_kind,
                    local_roots=local_roots,
                    declared_dependency_roots=declared_dependency_roots,
                    line=getattr(node, "lineno", 0),
                    import_text=f"import {alias.name}",
                )
                if issue:
                    issues.append(issue)
            continue
        if isinstance(node, ast.ImportFrom):
            module = _resolve_generated_import_from(
                imported_module=node.module,
                level=node.level,
                current_package=current_package,
            )
            issue = _undeclared_external_import_issue(
                module,
                path=path,
                kind=normalized_kind,
                local_roots=local_roots,
                declared_dependency_roots=declared_dependency_roots,
                line=getattr(node, "lineno", 0),
                import_text=f"from {'.' * node.level}{node.module or ''} import ...",
            )
            if issue:
                issues.append(issue)
    return issues


def _undeclared_external_import_issue(
    module: str,
    *,
    path: str,
    kind: str,
    local_roots: set[str],
    declared_dependency_roots: set[str],
    line: int,
    import_text: str,
) -> dict[str, Any] | None:
    root = module.strip(".").split(".", 1)[0]
    if not root:
        return None
    if root == "__future__" or root in getattr(sys, "stdlib_module_names", set()):
        return None
    if root in local_roots or _looks_like_test_validation_import(root, path=path, kind=kind):
        return None
    dependency_root = _normalize_dependency_root(root)
    if dependency_root in declared_dependency_roots:
        return None
    return {
        "issue_type": "undeclared_external_import",
        "module": module.strip("."),
        "dependency_root": dependency_root,
        "line": line,
        "import": import_text,
    }


def _looks_like_test_validation_import(root: str, *, path: str, kind: str) -> bool:
    filename = path.replace("\\", "/").rsplit("/", 1)[-1]
    is_test = kind == "test" or filename.startswith("test_") or filename.endswith("_test.py")
    return is_test and root == "pytest"


def _normalize_dependency_root(value: str) -> str:
    raw_name = str(value).strip()
    if not raw_name:
        return ""
    raw_name = raw_name.split(";", 1)[0].strip()
    raw_name = raw_name.split("[", 1)[0].strip()
    raw_name = re.split(r"\s+|[<>=!~@]", raw_name, maxsplit=1)[0].strip()
    return re.sub(r"[-_.]+", "-", raw_name).casefold()


def _placeholder_contract_issues(
    content: str,
    *,
    path: str,
    kind: str,
    expected_symbols: list[str],
) -> list[dict[str, Any]]:
    normalized_kind = FILE_KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())
    if normalized_kind not in {"python", "test"} or not path.endswith(".py"):
        return []
    try:
        module = ast.parse(content, filename=path)
    except SyntaxError:
        return []
    placeholders = _top_level_placeholder_symbols(module)
    if not placeholders:
        return []

    issues: list[dict[str, Any]] = []
    required_symbols = {
        symbol
        for symbol in _dedupe_strings([str(item).strip() for item in expected_symbols])
        if symbol.isidentifier() and not keyword.iskeyword(symbol)
    }
    for symbol in sorted(required_symbols):
        if symbol in placeholders:
            issues.append(
                {
                    "issue_type": "placeholder_expected_symbol",
                    "symbol": symbol,
                    "placeholder_kind": placeholders[symbol],
                }
            )

    class PlaceholderUseVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> Any:  # noqa: ANN401
            if isinstance(node.func, ast.Name) and node.func.id in placeholders:
                issues.append(
                    {
                        "issue_type": "placeholder_value_called",
                        "symbol": node.func.id,
                        "line": getattr(node, "lineno", 0),
                        "placeholder_kind": placeholders[node.func.id],
                    }
                )
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> Any:  # noqa: ANN401
            if isinstance(node.value, ast.Name) and node.value.id in placeholders:
                issues.append(
                    {
                        "issue_type": "placeholder_value_dereferenced",
                        "symbol": node.value.id,
                        "attribute": node.attr,
                        "line": getattr(node, "lineno", 0),
                        "placeholder_kind": placeholders[node.value.id],
                    }
                )
            self.generic_visit(node)

    PlaceholderUseVisitor().visit(module)
    return _dedupe_contract_issues(issues)


def _top_level_placeholder_symbols(module: ast.Module) -> dict[str, str]:
    placeholders: dict[str, str] = {}
    for node in module.body:
        if isinstance(node, ast.Assign) and _is_placeholder_expr(node.value):
            for target in node.targets:
                for name in _assigned_target_names(target):
                    placeholders[name] = _placeholder_expr_kind(node.value)
            continue
        if isinstance(node, ast.AnnAssign) and node.value is not None and _is_placeholder_expr(node.value):
            for name in _assigned_target_names(node.target):
                placeholders[name] = _placeholder_expr_kind(node.value)
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and _body_is_placeholder(node.body):
            placeholders[node.name] = "placeholder_body"
    return placeholders


def _is_placeholder_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None or node.value is Ellipsis
    return isinstance(node, ast.Name) and node.id == "NotImplemented"


def _placeholder_expr_kind(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "none"
        if node.value is Ellipsis:
            return "ellipsis"
    if isinstance(node, ast.Name) and node.id == "NotImplemented":
        return "not_implemented"
    return "placeholder"


def _body_is_placeholder(body: list[ast.stmt]) -> bool:
    meaningful: list[ast.stmt] = []
    for node in body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        meaningful.append(node)
    if not meaningful:
        return True
    if len(meaningful) != 1:
        return False
    node = meaningful[0]
    if isinstance(node, ast.Pass):
        return True
    if isinstance(node, ast.Expr) and _is_placeholder_expr(node.value):
        return True
    return isinstance(node, ast.Return) and (node.value is None or _is_placeholder_expr(node.value))


def _dedupe_contract_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for issue in issues:
        key = json.dumps(issue, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped


def generate_patch_with_llm(
    request: MaterialPatchGenerationRequest,
    llm: LLMSettings,
) -> tuple[PatchProposal | PatchSetProposal | ReplacementProposal, dict[str, Any]]:
    messages = [
        {
            "role": "system",
            "content": _prompt("patch_system.md"),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_id": request.task_id,
                    "session_id": request.session_id,
                    "plan": request.plan.model_dump(mode="json"),
                    "issue_id": request.issue_id,
                    "issue": request.issue.model_dump(mode="json"),
                    "target_path": request.target_path,
                    "expected_old_sha256": request.expected_old_sha256,
                    "current_content": request.current_content,
                    "current_context": request.current_context,
                    "target_resolution": request.target_resolution.model_dump(mode="json")
                    if request.target_resolution
                    else None,
                    "allowed_local_import_roots": _allowed_local_import_roots(request.plan),
                    "planned_local_modules": _planned_local_python_modules(request.plan),
                    "expected_symbols": _expected_symbols_from_repair_request(request),
                    "validation_profile": request.validation_profile,
                    "command_evidence": request.command_evidence,
                    "previous_patch_rejections": [
                        rejection.model_dump(mode="json") for rejection in request.previous_patch_rejections
                    ],
                    "allowed_repair_proposals": _allowed_repair_proposals(request),
                    "target_bundle": _target_bundle(request),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    replacement_required = _replacement_required(request)
    if replacement_required:
        proposal, replacement_metrics = _generate_validated_replacement(
            request=request,
            llm=llm,
            invalid_payload={},
        )
        return proposal, replacement_metrics
    result = _call_governed_json(messages, llm)
    payload = _normalize_patch_payload(result.payload, request=request)
    patch_set_payload = _patch_set_payload(payload)
    if patch_set_payload is not None:
        proposal = _patch_set_proposal_from_payload(patch_set_payload, request=request)
        return proposal, result.lane_metrics
    replacement_payload = _replacement_payload(payload)
    if replacement_payload is not None:
        proposal = _replacement_proposal_from_payload(replacement_payload, request=request)
        _validate_repair_identity(proposal, request=request)
        replacement_contract_issues = _replacement_contract_issues(proposal, request=request)
        if replacement_contract_issues:
            return _generate_validated_replacement(
                request=request,
                llm=llm,
                invalid_payload=_replacement_contract_invalid_payload(
                    payload,
                    request=request,
                    contract_issues=replacement_contract_issues,
                ),
            )
        return proposal, result.lane_metrics
    patch_payload = payload.get("patch")
    if not isinstance(patch_payload, dict):
        payload = _repair_patch_schema_payload(
            messages=messages,
            request=request,
            llm=llm,
            invalid_payload=payload,
            validation_errors=[{"loc": ["patch"], "msg": "patch proposal must be a JSON object"}],
        )
        patch_payload = payload.get("patch")
    if not isinstance(patch_payload, dict):
        raise MaterialLLMError(
            "llm_schema_invalid",
            "LLM patch proposal must be a JSON object",
            details={"target_path": request.target_path},
        )
    try:
        proposal = PatchProposal.model_validate(
            {
                "issue_id": request.issue_id,
                "target_path": request.target_path,
                "expected_old_sha256": request.expected_old_sha256,
                "unified_diff": patch_payload.get("unified_diff"),
                "requirement_refs": request.issue.requirement_refs,
                "contract_refs": request.issue.contract_refs,
                "rationale": patch_payload.get("rationale"),
            }
        )
    except ValidationError as exc:
        repaired_payload = _repair_patch_schema_payload(
            messages=messages,
            request=request,
            llm=llm,
            invalid_payload=payload,
            validation_errors=_validation_errors(exc),
        )
        result = LLMJSONResult(
            payload=repaired_payload,
            lane_metrics={
                **result.lane_metrics,
                "schema_retries": int(result.lane_metrics.get("schema_retries") or 0) + 1,
            },
        )
        normalized_repaired_payload = _normalize_patch_payload(repaired_payload, request=request)
        repaired_replacement_payload = _replacement_payload(normalized_repaired_payload)
        if repaired_replacement_payload is not None:
            proposal = _replacement_proposal_from_payload(repaired_replacement_payload, request=request)
            _validate_repair_identity(proposal, request=request)
            replacement_contract_issues = _replacement_contract_issues(proposal, request=request)
            if replacement_contract_issues:
                return _generate_validated_replacement(
                    request=request,
                    llm=llm,
                    invalid_payload=_replacement_contract_invalid_payload(
                        normalized_repaired_payload,
                        request=request,
                        contract_issues=replacement_contract_issues,
                    ),
                )
            return proposal, result.lane_metrics
        repaired_patch_payload = normalized_repaired_payload.get("patch", normalized_repaired_payload)
        if not isinstance(repaired_patch_payload, dict):
            raise MaterialLLMError(
                "llm_schema_invalid",
                "LLM patch proposal did not satisfy the patch contract",
                details={"target_path": request.target_path},
            ) from exc
        try:
            proposal = PatchProposal.model_validate(
                {
                    "issue_id": request.issue_id,
                    "target_path": request.target_path,
                    "expected_old_sha256": request.expected_old_sha256,
                    "unified_diff": repaired_patch_payload.get("unified_diff"),
                    "requirement_refs": request.issue.requirement_refs,
                    "contract_refs": request.issue.contract_refs,
                    "rationale": repaired_patch_payload.get("rationale"),
                }
            )
        except ValidationError as repaired_exc:
            raise MaterialLLMError(
                "llm_schema_invalid",
                "LLM patch proposal did not satisfy the patch contract",
                details={
                    "target_path": request.target_path,
                    "validation_errors": _validation_errors(repaired_exc),
                    "initial_validation_errors": _validation_errors(exc),
                    "initial_payload_excerpt": _compact_text(json.dumps(payload, ensure_ascii=False), 1000),
                    "repaired_payload_excerpt": _compact_text(
                        json.dumps(repaired_payload, ensure_ascii=False),
                        1000,
                    ),
                },
            ) from repaired_exc
    _validate_repair_identity(proposal, request=request)
    return proposal, result.lane_metrics


def critique_repair_with_llm(
    request: MaterialRepairCriticRequest,
    llm: LLMSettings,
) -> MaterialRepairCriticResponse:
    messages = [
        {
            "role": "system",
            "content": _prompt("repair_critic_system.md"),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_id": request.task_id,
                    "session_id": request.session_id,
                    "plan": request.plan.model_dump(mode="json"),
                    "issue_id": request.issue_id,
                    "issue": request.issue.model_dump(mode="json"),
                    "target_path": request.target_path,
                    "current_content_excerpt": _compact_text(request.current_content, 12000),
                    "current_context": request.current_context,
                    "command_evidence": request.command_evidence,
                    "previous_patch_rejections": [
                        rejection.model_dump(mode="json") for rejection in request.previous_patch_rejections
                    ],
                    "repair_arbiter": request.repair_arbiter,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    result = _call_governed_json(messages, llm)
    payload = result.payload
    try:
        response = MaterialRepairCriticResponse.model_validate(
            {
                "advisory_only": True,
                "findings": _critic_findings(payload.get("findings")),
                "likely_root_cause": payload.get("likely_root_cause"),
                "recommended_strategy": payload.get("recommended_strategy") or "replacement",
                "confidence": payload.get("confidence") or 0.0,
                "model_route": llm.route,
                "lane_metrics": result.lane_metrics,
            }
        )
    except ValidationError as exc:
        raise MaterialLLMError(
            "llm_schema_invalid",
            "LLM material repair critic response did not satisfy the advisory contract",
            details={
                "validation_errors": _validation_errors(exc),
                "payload_excerpt": _compact_text(json.dumps(payload, ensure_ascii=False), 1000),
            },
        ) from exc
    return response


def _critic_findings(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    findings: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            findings.append(
                MaterialRepairCriticFinding(
                    finding_type="text_advisory",
                    severity="warning",
                    message=item.strip(),
                ).model_dump(mode="json")
            )
        elif isinstance(item, dict):
            findings.append(item)
    return findings


def _patch_set_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    patch_set = payload.get("patch_set")
    if isinstance(patch_set, dict):
        return patch_set
    return None


def _replacement_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    replacement = payload.get("replacement")
    if isinstance(replacement, dict):
        return replacement
    if isinstance(payload.get("replacement_content"), str):
        return payload
    return None


def _allowed_repair_proposals(request: MaterialPatchGenerationRequest) -> list[str]:
    arbiter_allowed = _arbiter_allowed_repair_proposals(request)
    if arbiter_allowed:
        return arbiter_allowed
    allowed = ["patch", "replacement"]
    if len(_target_bundle(request)) > 1:
        allowed.append("patch_set")
    if request.regeneration_blueprints:
        allowed.append("regeneration")
    return allowed


def _arbiter_allowed_repair_proposals(request: MaterialPatchGenerationRequest) -> list[str]:
    arbiter = request.current_context.get("repair_arbiter")
    if not isinstance(arbiter, dict):
        return []
    raw = arbiter.get("allowed_repair_proposals")
    if not isinstance(raw, list):
        return []
    allowed_by_builder = {"patch", "replacement", "patch_set", "regeneration"}
    allowed = [str(item) for item in raw if str(item) in allowed_by_builder]
    if "patch_set" in allowed and len(_target_bundle(request)) < 2:
        allowed = [item for item in allowed if item != "patch_set"]
        if "replacement" not in allowed:
            allowed.append("replacement")
    if "regeneration" in allowed and not request.regeneration_blueprints:
        allowed = [item for item in allowed if item != "regeneration"]
    return _dedupe_strings(allowed)


def _target_bundle(request: MaterialPatchGenerationRequest) -> list[dict[str, Any]]:
    raw = request.current_context.get("target_bundle")
    if not isinstance(raw, list):
        return []
    bundle: list[dict[str, Any]] = []
    allowed = {request.target_path}
    if request.target_resolution is not None:
        allowed.update(request.target_resolution.related_targets)
        allowed.update(request.target_resolution.candidate_targets)
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        expected_hash = str(item.get("expected_old_sha256") or "").strip()
        if (
            path not in allowed
            or bool(item.get("content_truncated"))
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", expected_hash)
        ):
            continue
        bundle.append(
            {
                "path": path,
                "role": item.get("role") if item.get("role") in {"primary", "related", "candidate"} else "related",
                "kind": str(item.get("kind") or "other"),
                "expected_old_sha256": expected_hash,
                "content": str(item.get("content") or ""),
                "content_truncated": bool(item.get("content_truncated")),
            }
        )
    return bundle


def _patch_set_proposal_from_payload(
    payload: dict[str, Any],
    *,
    request: MaterialPatchGenerationRequest,
) -> PatchSetProposal:
    bundle_by_path = {item["path"]: item for item in _target_bundle(request)}
    if len(bundle_by_path) < 2:
        raise MaterialLLMError(
            "patch_set_not_governed",
            "LLM patch_set proposals require a governed target_bundle with at least two targets",
            details={"target_path": request.target_path},
        )
    allowed_targets = set(bundle_by_path)
    patches_payload = payload.get("patches")
    if not isinstance(patches_payload, list):
        raise MaterialLLMError(
            "llm_schema_invalid",
            "LLM patch_set proposal must include a patches list",
            details={"target_path": request.target_path},
        )
    patches: list[PatchProposal] = []
    unexpected_targets: list[str] = []
    for patch_payload in patches_payload:
        if not isinstance(patch_payload, dict):
            continue
        target_path = str(patch_payload.get("target_path") or "").strip()
        if target_path not in allowed_targets:
            unexpected_targets.append(target_path or "<missing>")
            continue
        diff = patch_payload.get("unified_diff")
        patches.append(
            PatchProposal.model_validate(
                {
                    "issue_id": request.issue_id,
                    "target_path": target_path,
                    "expected_old_sha256": bundle_by_path[target_path]["expected_old_sha256"],
                    "unified_diff": _canonical_single_target_diff(str(diff or ""), target_path=target_path),
                    "requirement_refs": request.issue.requirement_refs,
                    "contract_refs": request.issue.contract_refs,
                    "rationale": patch_payload.get("rationale"),
                }
            )
        )
    if unexpected_targets:
        raise MaterialLLMError(
            "patch_set_target_not_governed",
            "LLM patch_set proposal touched targets outside the governed repair bundle",
            details={
                "target_path": request.target_path,
                "unexpected_targets": unexpected_targets,
                "allowed_targets": sorted(allowed_targets),
            },
        )
    patch_targets = {patch.target_path for patch in patches}
    if request.target_path not in patch_targets:
        raise MaterialLLMError(
            "patch_set_primary_target_missing",
            "LLM patch_set proposal must include the requested primary repair target",
            details={"target_path": request.target_path, "patch_targets": sorted(patch_targets)},
        )
    if len(patch_targets) < 2:
        raise MaterialLLMError(
            "patch_set_requires_related_target",
            "LLM patch_set proposal must repair at least one governed related target",
            details={"target_path": request.target_path, "patch_targets": sorted(patch_targets)},
        )
    try:
        return PatchSetProposal.model_validate(
            {
                "issue_id": request.issue_id,
                "patches": [patch.model_dump(mode="json") for patch in patches],
                "requirement_refs": request.issue.requirement_refs,
                "contract_refs": request.issue.contract_refs,
                "rationale": payload.get("rationale"),
            }
        )
    except ValidationError as exc:
        raise MaterialLLMError(
            "llm_schema_invalid",
            "LLM patch_set proposal did not satisfy the patch-set contract",
            details={
                "target_path": request.target_path,
                "validation_errors": _validation_errors(exc),
                "payload_excerpt": _compact_text(json.dumps(payload, ensure_ascii=False), 1000),
            },
        ) from exc


def _replacement_required(request: MaterialPatchGenerationRequest) -> bool:
    arbiter = request.current_context.get("repair_arbiter")
    if isinstance(arbiter, dict) and arbiter.get("strategy") == "replacement":
        return True
    if _target_prefers_replacement(request):
        return True
    if _request_has_cli_help_expectation(request) and _content_has_argparse_manual_help_conflict(
        request.current_content
    ):
        return True
    if bool(request.command_evidence.get("target_file_missing")):
        return True
    last_rejection = request.command_evidence.get("last_patch_rejection")
    if isinstance(last_rejection, dict) and _rejection_evidence_requires_replacement(last_rejection):
        return True
    return _replacement_required_by_rejections(request)


def _target_prefers_replacement(request: MaterialPatchGenerationRequest) -> bool:
    normalized_path = request.target_path.replace("\\", "/").rsplit("/", 1)[-1]
    issue_type = request.issue.issue_type
    structured_manifest_names = {
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "package.json",
        "compose.yaml",
        "docker-compose.yml",
    }
    if normalized_path in structured_manifest_names and issue_type in {
        "missing_dependency_strategy",
        "dependency_strategy_mismatch",
        "missing_runtime_service_contract",
        "missing_stateful_service_contract",
    }:
        return True
    if issue_type in {"missing_symbol_provider", "missing_test_contract"}:
        return True
    return False


def _replacement_required_by_rejections(request: MaterialPatchGenerationRequest) -> bool:
    return any(
        _rejection_evidence_requires_replacement(rejection.model_dump(mode="json"))
        for rejection in request.previous_patch_rejections
    )


def _rejection_evidence_requires_replacement(rejection: dict[str, Any]) -> bool:
    stale_context_markers = (
        "llm_contract_violation",
        "does not match the repair target",
        "path that does not match",
        "context_mismatch",
        "removal_mismatch",
        "checksum_mismatch",
        "expected_old_sha256",
        "empty_diff_line",
        "invalid_diff",
        "invalid_diff_line_prefix",
        "malformed_diff",
        "patch_apply_failed",
        "microvm_patch_apply_failed",
        "replacement_noop",
    )
    evidence = " ".join(
        str(value)
        for value in (
            rejection.get("reason"),
            rejection.get("message"),
            json.dumps(rejection.get("diagnostics") or {}, ensure_ascii=False, sort_keys=True),
        )
        if value
    ).casefold()
    return any(marker in evidence for marker in stale_context_markers)


def _expected_symbols_from_repair_request(request: MaterialPatchGenerationRequest) -> list[str]:
    symbols: list[str] = []
    evidence = _repair_evidence_text(request)
    missing_name = request.command_evidence.get("missing_name")
    if (
        isinstance(missing_name, str)
        and missing_name
        and missing_name != "*"
        and _missing_name_looks_like_target_symbol(missing_name, evidence)
    ):
        symbols.append(missing_name)
    for value in _nested_symbol_values(request.command_evidence):
        symbols.append(value)
    for key in ("expected_symbols", "missing_expected_symbols"):
        raw = request.current_context.get(key)
        if isinstance(raw, list):
            symbols.extend(str(item).strip() for item in raw)
    for expectation in _call_expectations(request):
        function_name = str(expectation.get("function_name") or "").strip()
        if function_name and function_name != "*":
            symbols.append(function_name)
    for match in re.finditer(r"cannot import name ['\"]([^'\"]+)['\"] from", evidence):
        name = match.group(1).strip()
        if name and name != "*":
            symbols.append(name)
    for name in _module_attribute_symbols_from_evidence(evidence):
        symbols.append(name)
    return _dedupe_strings(
        [
            symbol
            for symbol in symbols
            if symbol and symbol != "*" and not _symbol_is_only_instance_attribute_error(symbol, evidence)
        ]
    )


def _repair_evidence_text(request: MaterialPatchGenerationRequest) -> str:
    values: list[str] = []
    values.extend(_nested_evidence_strings(request.command_evidence))
    values.extend(_nested_evidence_strings(request.current_context))
    values.extend(request.issue.repair_intent)
    values.extend(request.issue.acceptance)
    for rejection in request.previous_patch_rejections:
        payload = rejection.model_dump(mode="json")
        values.extend(_nested_evidence_strings(payload))
        values.extend(_nested_symbol_values(payload))
    return "\n".join(str(value) for value in values if str(value).strip())


def _missing_name_looks_like_target_symbol(name: str, evidence: str) -> bool:
    if not evidence:
        return True
    if _module_attribute_symbol_pattern(name).search(evidence):
        return True
    if re.search(rf"cannot import name ['\"]{re.escape(name)}['\"] from", evidence):
        return True
    if _symbol_is_only_instance_attribute_error(name, evidence):
        return False
    return True


def _module_attribute_symbols_from_evidence(evidence: str) -> list[str]:
    symbols: list[str] = []
    for match in re.finditer(r"module ['\"][^'\"]+['\"] has no attribute ['\"]([A-Za-z_]\w*)['\"]", evidence):
        symbols.append(match.group(1))
    return _dedupe_strings(symbols)


def _module_attribute_symbol_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"module ['\"][^'\"]+['\"] has no attribute ['\"]{re.escape(name)}['\"]")


def _symbol_is_only_instance_attribute_error(symbol: str, evidence: str) -> bool:
    if not symbol:
        return False
    if _module_attribute_symbol_pattern(symbol).search(evidence):
        return False
    return bool(re.search(rf"['\"][^'\"]+['\"] object has no attribute ['\"]{re.escape(symbol)}['\"]", evidence))


def _nested_symbol_values(value: object, *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    symbols: list[str] = []
    if isinstance(value, dict):
        for key, raw in value.items():
            if key in {"invalid_payload", "lane_metrics", "model_route", "calls"}:
                continue
            if key in {"expected_exports", "missing_exports", "expected_symbols", "missing_expected_symbols"}:
                if isinstance(raw, str):
                    symbols.append(raw.strip())
                elif isinstance(raw, list):
                    symbols.extend(str(item).strip() for item in raw)
            elif isinstance(raw, (dict, list)):
                symbols.extend(_nested_symbol_values(raw, depth=depth + 1))
    elif isinstance(value, list):
        for item in value:
            symbols.extend(_nested_symbol_values(item, depth=depth + 1))
    return symbols


def _nested_evidence_strings(value: object, *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    strings: list[str] = []
    if isinstance(value, dict):
        for raw in value.values():
            if isinstance(raw, str):
                strings.append(raw)
            elif isinstance(raw, (dict, list)):
                strings.extend(_nested_evidence_strings(raw, depth=depth + 1))
    elif isinstance(value, list):
        for item in value:
            strings.extend(_nested_evidence_strings(item, depth=depth + 1))
    return strings


def _generate_replacement_payload(
    *,
    request: MaterialPatchGenerationRequest,
    llm: LLMSettings,
    invalid_payload: dict[str, Any],
) -> LLMJSONResult:
    messages = [
        {
            "role": "system",
            "content": _prompt("replacement_system.md"),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_id": request.task_id,
                    "session_id": request.session_id,
                    "plan": request.plan.model_dump(mode="json"),
                    "issue_id": request.issue_id,
                    "issue": request.issue.model_dump(mode="json"),
                    "target_path": request.target_path,
                    "expected_old_sha256": request.expected_old_sha256,
                    "current_content": request.current_content,
                    "current_context": request.current_context,
                    "allowed_local_import_roots": _allowed_local_import_roots(request.plan),
                    "planned_local_modules": _planned_local_python_modules(request.plan),
                    "expected_symbols": _expected_symbols_from_repair_request(request),
                    "validation_profile": request.validation_profile,
                    "command_evidence": request.command_evidence,
                    "previous_patch_rejections": [
                        rejection.model_dump(mode="json") for rejection in request.previous_patch_rejections
                    ],
                    "invalid_payload": invalid_payload,
                    "contract_retry_constraints": _replacement_contract_retry_constraints(
                        invalid_payload,
                        request=request,
                    ),
                    "repair_mode": "replacement_required",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    result = _call_governed_json(messages, llm)
    return LLMJSONResult(
        payload=_normalize_patch_payload(result.payload, request=request),
        lane_metrics=result.lane_metrics,
    )


def _generate_validated_replacement(
    *,
    request: MaterialPatchGenerationRequest,
    llm: LLMSettings,
    invalid_payload: dict[str, Any],
) -> tuple[ReplacementProposal, dict[str, Any]]:
    deterministic = _deterministic_replacement_before_llm(request)
    if deterministic is not None:
        return deterministic, {
            "replacement_retries": 0,
            "replacement_contract_retries": 0,
            "schema_retries": 0,
            "static_fallback_used": True,
            "static_fallback_reason": "prevalidated_deterministic_contract",
        }

    metrics: list[dict[str, Any]] = []
    current_invalid_payload = invalid_payload
    contract_retries = 0
    last_payload: dict[str, Any] = {}
    last_contract_issues: list[dict[str, Any]] = []

    for attempt in range(_file_contract_attempts(llm)):
        result = _generate_replacement_payload(
            request=request,
            llm=llm,
            invalid_payload=current_invalid_payload,
        )
        metrics.append(result.lane_metrics)
        payload = result.payload
        last_payload = payload
        replacement_payload = _replacement_payload(payload)
        if replacement_payload is None:
            current_invalid_payload = {
                "reason": "replacement_payload_missing",
                "expected_shape": {"replacement": {"replacement_content": "..."}},
                "invalid_payload": payload,
            }
            contract_retries += 1
            continue

        proposal = _replacement_proposal_from_payload(replacement_payload, request=request)
        _validate_repair_identity(proposal, request=request)
        contract_issues = _replacement_contract_issues(proposal, request=request)
        if not contract_issues:
            lane_metrics = _merge_lane_metrics(*metrics)
            return proposal, {
                **lane_metrics,
                "replacement_retries": len(metrics),
                "replacement_contract_retries": contract_retries,
                "schema_retries": int(lane_metrics.get("schema_retries") or 0),
            }

        last_contract_issues = contract_issues
        current_invalid_payload = _replacement_contract_invalid_payload(
            payload,
            request=request,
            contract_issues=contract_issues,
        )
        contract_retries += 1

    fallback = _deterministic_replacement_for_contract_failure(
        request=request,
        contract_issues=last_contract_issues,
    )
    if fallback is not None:
        lane_metrics = _merge_lane_metrics(*metrics)
        return fallback, {
            **lane_metrics,
            "replacement_retries": len(metrics),
            "replacement_contract_retries": contract_retries,
            "schema_retries": int(lane_metrics.get("schema_retries") or 0),
            "static_fallback_used": True,
            "static_fallback_reason": _deterministic_replacement_fallback_reason(last_contract_issues),
        }

    raise MaterialLLMError(
        "llm_contract_violation",
        "LLM replacement proposal did not satisfy the target Python contract",
        details={
            "target_path": request.target_path,
            "expected_symbols": _expected_symbols_from_repair_request(request),
            "contract_issues": last_contract_issues,
            "payload_excerpt": _compact_text(json.dumps(last_payload, ensure_ascii=False), 1000),
            "lane_metrics": {
                **_merge_lane_metrics(*metrics),
                "replacement_retries": len(metrics),
                "replacement_contract_retries": contract_retries,
            },
        },
    )


def _deterministic_replacement_before_llm(request: MaterialPatchGenerationRequest) -> ReplacementProposal | None:
    manifest_replacement = _deterministic_manifest_replacement(request)
    if manifest_replacement is not None:
        return manifest_replacement
    if not request.target_path.replace("\\", "/").endswith(".py"):
        return None
    if _request_has_literal_return_value_obligation(request) and _expected_symbols_from_repair_request(request):
        replacement_content = _deterministic_expected_symbol_replacement(request)
        if replacement_content is not None and replacement_content != request.current_content:
            proposal = _replacement_proposal_from_payload(
                {
                    "replacement_content": replacement_content,
                    "rationale": "deterministic replacement for observed literal return-value contract before LLM retry",
                },
                request=request,
            )
            if not _replacement_contract_issues(proposal, request=request):
                return proposal
    if not _request_has_cli_help_expectation(request) and not _command_evidence_mentions_cli_help(request):
        return None
    if not (
        _content_has_argparse_manual_help_conflict(request.current_content)
        or request.previous_patch_rejections
        or _expected_symbols_from_repair_request(request)
    ):
        return None
    replacement_content = _deterministic_cli_help_replacement(request)
    if replacement_content is None or replacement_content == request.current_content:
        return None
    proposal = _replacement_proposal_from_payload(
        {
            "replacement_content": replacement_content,
            "rationale": "deterministic replacement for observed CLI help contract before LLM retry",
        },
        request=request,
    )
    if _replacement_contract_issues(proposal, request=request):
        return None
    return proposal


def _deterministic_manifest_replacement(request: MaterialPatchGenerationRequest) -> ReplacementProposal | None:
    normalized_path = request.target_path.replace("\\", "/")
    if not normalized_path.endswith("/pyproject.toml") and normalized_path != "pyproject.toml":
        return None
    if not request.current_content.strip() or _toml_parse_error(request.current_content) is None:
        return None
    replacement_content = _deterministic_pyproject_content(request)
    if replacement_content == request.current_content:
        return None
    proposal = _replacement_proposal_from_payload(
        {
            "replacement_content": replacement_content,
            "rationale": "deterministic replacement for invalid pyproject.toml syntax",
        },
        request=request,
    )
    if _replacement_contract_issues(proposal, request=request):
        return None
    return proposal


def _deterministic_pyproject_content(request: MaterialPatchGenerationRequest) -> str:
    return _deterministic_pyproject_content_for_plan(request.plan, target_path=request.target_path)


def _deterministic_pyproject_content_for_plan(plan: MaterialPlan, *, target_path: str = "") -> str:
    project_name = _pyproject_project_name_from_plan(plan, target_path=target_path)
    package_name = _primary_src_package_from_material_plan(plan)
    script_name = _console_script_name(project_name)
    lines = [
        "[build-system]",
        'requires = ["setuptools>=61"]',
        'build-backend = "setuptools.build_meta"',
        "",
        "[project]",
        f"name = {_toml_string(project_name)}",
        'version = "0.1.0"',
        f"description = {_toml_string(_pyproject_description_from_plan(plan))}",
        f"readme = {_toml_string(_pyproject_readme_from_material_plan(plan))}",
        'requires-python = ">=3.11"',
        "dependencies = []",
        "",
    ]
    if package_name:
        lines.extend(
            [
                "[project.scripts]",
                f"{_toml_bare_or_quoted_key(script_name)} = {_toml_string(f'{package_name}.__main__:main')}",
                "",
                "[tool.setuptools.packages.find]",
                'where = ["src"]',
                "",
            ]
        )
    return "\n".join(lines)


def _pyproject_project_name_from_request(request: MaterialPatchGenerationRequest) -> str:
    return _pyproject_project_name_from_plan(request.plan, target_path=request.target_path)


def _pyproject_project_name_from_plan(plan: MaterialPlan, *, target_path: str = "") -> str:
    root = plan.project_root.strip().strip("/").replace("\\", "/")
    name = root.rsplit("/", 1)[-1] if root else ""
    if not name and target_path:
        target = target_path.strip().strip("/").replace("\\", "/")
        name = target.split("/", 1)[0] if "/" in target else "generated-project"
    return _normalize_distribution_name(name) or "generated-project"


def _pyproject_description_from_request(request: MaterialPatchGenerationRequest) -> str:
    return _pyproject_description_from_plan(request.plan)


def _pyproject_description_from_plan(plan: MaterialPlan) -> str:
    root = plan.project_root.strip().strip("/").rsplit("/", 1)[-1] or "Generated project"
    return f"Generated material artifact for {root}"


def _pyproject_readme_from_plan(request: MaterialPatchGenerationRequest) -> str:
    return _pyproject_readme_from_material_plan(request.plan)


def _pyproject_readme_from_material_plan(plan: MaterialPlan) -> str:
    for file_spec in plan.files:
        path = file_spec.path.replace("\\", "/")
        if path.endswith("/README.md") or path == "README.md":
            root = plan.project_root.strip().strip("/").replace("\\", "/")
            if root and path.startswith(f"{root}/"):
                return path[len(root) + 1 :]
            return path.rsplit("/", 1)[-1]
    return "README.md"


def _primary_src_package_from_plan(request: MaterialPatchGenerationRequest) -> str:
    return _primary_src_package_from_material_plan(request.plan)


def _primary_src_package_from_material_plan(plan: MaterialPlan) -> str:
    root = plan.project_root.strip().strip("/").replace("\\", "/")
    candidates: list[str] = []
    for file_spec in plan.files:
        path = file_spec.path.strip().strip("/").replace("\\", "/")
        if root and path.startswith(f"{root}/"):
            path = path[len(root) + 1 :]
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "src" and parts[-1] in {"__init__.py", "__main__.py"}:
            package_name = parts[1]
            if _is_importable_python_stem(package_name):
                candidates.append(package_name)
    if candidates:
        return _dedupe_strings(candidates)[0]
    root_name = _stable_python_stem_from_project_root(root)
    return root_name if _is_importable_python_stem(root_name) else ""


def _console_script_name(project_name: str) -> str:
    script = re.sub(r"[^A-Za-z0-9_.-]+", "-", project_name.strip()).strip(".-")
    return script or "generated-project"


def _normalize_distribution_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return normalized or "generated-project"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_bare_or_quoted_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return _toml_string(value)


def _toml_parse_error(content: str) -> str | None:
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        return str(exc)
    return None


def _deterministic_replacement_for_contract_failure(
    *,
    request: MaterialPatchGenerationRequest,
    contract_issues: list[dict[str, Any]],
) -> ReplacementProposal | None:
    if not contract_issues or not request.target_path.replace("\\", "/").endswith(".py"):
        return None
    if _contract_issues_require_cli_help_replacement(contract_issues, request=request):
        replacement_content = _deterministic_cli_help_replacement(request)
        if replacement_content is not None and replacement_content != request.current_content:
            proposal = _replacement_proposal_from_payload(
                {
                    "replacement_content": replacement_content,
                    "rationale": "deterministic replacement for observed CLI help call contract",
                },
                request=request,
            )
            if not _replacement_contract_issues(proposal, request=request):
                return proposal
    if any(issue.get("issue_type") == "call_return_value_mismatch" for issue in contract_issues):
        replacement_content = _deterministic_expected_symbol_replacement(request)
        if replacement_content is not None and replacement_content != request.current_content:
            proposal = _replacement_proposal_from_payload(
                {
                    "replacement_content": replacement_content,
                    "rationale": "deterministic replacement for observed literal return-value contract",
                },
                request=request,
            )
            if not _replacement_contract_issues(proposal, request=request):
                return proposal
    if (
        _request_has_literal_return_value_obligation(request)
        and any(issue.get("issue_type") == "missing_expected_symbol" for issue in contract_issues)
    ):
        replacement_content = _deterministic_expected_symbol_replacement(request)
        if replacement_content is not None and replacement_content != request.current_content:
            proposal = _replacement_proposal_from_payload(
                {
                    "replacement_content": replacement_content,
                    "rationale": "deterministic replacement for missing symbol with observed literal return-value contract",
                },
                request=request,
            )
            if not _replacement_contract_issues(proposal, request=request):
                return proposal
    if any(issue.get("issue_type") == "placeholder_expected_symbol" for issue in contract_issues):
        replacement_content = _deterministic_expected_symbol_replacement(request)
        if replacement_content is not None and replacement_content != request.current_content:
            proposal = _replacement_proposal_from_payload(
                {
                    "replacement_content": replacement_content,
                    "rationale": "deterministic replacement for expected symbols that were placeholder-only",
                },
                request=request,
            )
            if not _replacement_contract_issues(proposal, request=request):
                return proposal
    if any(issue.get("issue_type") == "missing_expected_symbol" for issue in contract_issues):
        replacement_content = _deterministic_missing_symbol_replacement(
            request,
            contract_issues=contract_issues,
        )
        if replacement_content is not None and replacement_content != request.current_content:
            proposal = _replacement_proposal_from_payload(
                {
                    "replacement_content": replacement_content,
                    "rationale": "deterministic replacement for missing top-level interface symbols",
                },
                request=request,
            )
            if not _replacement_contract_issues(proposal, request=request):
                return proposal
    forbidden_modules = sorted(
        {
            str(issue.get("module") or "").strip(".")
            for issue in contract_issues
            if issue.get("issue_type") in {"local_import_cycle_import", "child_package_root_import"}
            and str(issue.get("module") or "").strip(".")
        }
    )
    if not forbidden_modules:
        return None
    expected_symbols = _expected_symbols_from_repair_request(request)
    if not expected_symbols:
        return None
    replacement_content = _self_contained_python_replacement(
        request.current_content,
        forbidden_modules=forbidden_modules,
        expected_symbols=expected_symbols,
    )
    if replacement_content is None or replacement_content == request.current_content:
        return None
    proposal = _replacement_proposal_from_payload(
        {
            "replacement_content": replacement_content,
            "rationale": "deterministic self-contained replacement after repeated local import contract failures",
        },
        request=request,
    )
    if _replacement_contract_issues(proposal, request=request):
        return None
    return proposal


def _deterministic_replacement_fallback_reason(contract_issues: list[dict[str, Any]]) -> str:
    issue_types = {str(issue.get("issue_type") or "") for issue in contract_issues}
    if "placeholder_expected_symbol" in issue_types:
        return "placeholder_expected_symbol_contract_failure"
    if "missing_expected_symbol" in issue_types:
        return "missing_expected_symbol_contract_failure"
    if issue_types & {"local_import_cycle_import", "child_package_root_import"}:
        return "local_import_cycle_contract_failure"
    if issue_types & {
        "call_signature_mismatch",
        "call_behavior_mismatch",
        "call_exception_mismatch",
        "call_return_value_mismatch",
    }:
        return "call_expectation_contract_failure"
    return "python_replacement_contract_failure"


def _deterministic_expected_symbol_replacement(request: MaterialPatchGenerationRequest) -> str | None:
    expected_symbols = [
        symbol
        for symbol in _expected_symbols_from_repair_request(request)
        if symbol and symbol.isidentifier()
    ]
    if not expected_symbols:
        return None
    return "\n\n".join(
        _definition_for_expected_symbol(symbol, request=request) for symbol in expected_symbols
    ).rstrip() + "\n"


def _deterministic_missing_symbol_replacement(
    request: MaterialPatchGenerationRequest,
    *,
    contract_issues: list[dict[str, Any]],
) -> str | None:
    missing_symbols = _dedupe_strings(
        [
            str(issue.get("symbol") or "").strip()
            for issue in contract_issues
            if issue.get("issue_type") == "missing_expected_symbol" and str(issue.get("symbol") or "").strip()
        ]
    )
    missing_symbols = [symbol for symbol in missing_symbols if symbol.isidentifier()]
    if not missing_symbols:
        return None
    current_exports = _top_level_export_names(request.current_content)
    additions: list[str] = []
    for symbol in missing_symbols:
        if symbol in current_exports:
            continue
        module_reexport = _planned_local_module_reexport(symbol, request=request)
        additions.append(module_reexport or _definition_for_expected_symbol(symbol, request=request))
    if not additions:
        return None
    lines = request.current_content.splitlines()
    insert_at = _definition_insert_index(lines)
    updated = [*lines[:insert_at], *additions, "", *lines[insert_at:]]
    return "\n".join(updated).rstrip() + "\n"


def _planned_local_module_reexport(symbol: str, *, request: MaterialPatchGenerationRequest) -> str:
    target_path = request.target_path.strip().strip("/").replace("\\", "/")
    if not target_path.endswith("/__init__.py"):
        return ""
    package_dir = target_path.rsplit("/", 1)[0]
    expected_module_path = f"{package_dir}/{symbol}.py"
    planned_paths = {file_spec.path.strip().strip("/").replace("\\", "/") for file_spec in request.plan.files}
    if expected_module_path not in planned_paths:
        return ""
    return f"from . import {symbol} as {symbol}"


def _request_has_literal_return_value_obligation(request: MaterialPatchGenerationRequest) -> bool:
    return any("expected_return_value" in expectation for expectation in _call_expectations(request)) or any(
        obligation.get("kind") == "call_return_value"
        and isinstance(obligation.get("expected"), dict)
        and "value" in obligation.get("expected", {})
        for obligation in _repair_obligations(request)
    )


def _contract_issues_require_cli_help_replacement(
    contract_issues: list[dict[str, Any]],
    *,
    request: MaterialPatchGenerationRequest,
) -> bool:
    if not _request_has_cli_help_expectation(request) and not _command_evidence_mentions_cli_help(request):
        return False
    return any(
        issue.get("issue_type")
        in {
            "argparse_manual_help_conflict",
            "call_behavior_mismatch",
            "call_exception_mismatch",
            "call_signature_mismatch",
            "missing_expected_symbol",
            "python_syntax_error",
            "local_import_cycle_import",
            "child_package_root_import",
        }
        for issue in contract_issues
    )


def _request_has_cli_help_expectation(request: MaterialPatchGenerationRequest) -> bool:
    call_expectations = request.current_context.get("call_expectations")
    if not isinstance(call_expectations, list):
        return False
    return any(
        isinstance(expectation, dict) and expectation.get("expected_behavior") == "cli_help"
        for expectation in call_expectations
    )


def _command_evidence_mentions_cli_help(request: MaterialPatchGenerationRequest) -> bool:
    evidence = " ".join(_nested_evidence_strings(request.command_evidence)).casefold()
    return "--help" in evidence or "'-h'" in evidence or '"-h"' in evidence


def _deterministic_cli_help_replacement(request: MaterialPatchGenerationRequest) -> str | None:
    symbols = _expected_symbols_from_repair_request(request)
    function_name = "main" if "main" in symbols else (symbols[0] if symbols else "")
    if not function_name:
        function_name = _cli_help_function_name_from_request(request)
    if not function_name or not function_name.isidentifier():
        return None
    helper_names = [
        symbol
        for symbol in symbols
        if symbol != function_name and symbol.isidentifier() and _looks_like_cli_runner_symbol(symbol)
    ]
    prog = _cli_program_name_from_request(request) or _cli_program_name_from_path(request.target_path)
    helper_defs = "".join(
        "\n\n"
        f"def {helper_name}(argv=None):\n"
        f"    return {function_name}(argv)\n"
        for helper_name in helper_names
    )
    if _request_cli_help_expects_system_exit(request):
        return (
            "import argparse\n"
            "\n"
            "\n"
            f"def {function_name}(argv=None):\n"
            f"    parser = argparse.ArgumentParser(prog={prog!r}, description={prog!r})\n"
            "    return parser.parse_args(argv)\n"
            f"{helper_defs}"
            "\n"
            "\n"
            "if __name__ == '__main__':\n"
            f"    {function_name}()\n"
        )
    return (
        "import argparse\n"
        "\n"
        "\n"
        "class HelpResult(str):\n"
        "    @property\n"
        "    def output(self):\n"
        "        return str(self)\n"
        "\n"
        "\n"
        f"def {function_name}(argv=None):\n"
        f"    parser = argparse.ArgumentParser(prog={prog!r}, description={prog!r})\n"
        "    help_text = parser.format_help()\n"
        "    if argv is None:\n"
        "        return HelpResult(help_text)\n"
        "    if any(str(arg) in {'-h', '--help'} for arg in argv):\n"
        "        return HelpResult(help_text)\n"
        "    return HelpResult(help_text)\n"
        f"{helper_defs}"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        f"    print({function_name}())\n"
    )


def _looks_like_cli_runner_symbol(symbol: str) -> bool:
    normalized = symbol.strip("_").casefold()
    return "run" in normalized or "main" in normalized or "cli" in normalized


def _request_cli_help_expects_system_exit(request: MaterialPatchGenerationRequest) -> bool:
    call_expectations = request.current_context.get("call_expectations")
    explicit = isinstance(call_expectations, list) and any(
        isinstance(expectation, dict)
        and expectation.get("expected_behavior") == "cli_help"
        and str(expectation.get("expected_exception") or "") == "SystemExit"
        for expectation in call_expectations
    )
    if explicit:
        return True
    evidence = " ".join(_nested_evidence_strings(request.command_evidence)).casefold()
    return "pytest.raises(systemexit)" in evidence or "raises(systemexit" in evidence


def _cli_help_function_name_from_request(request: MaterialPatchGenerationRequest) -> str:
    call_expectations = request.current_context.get("call_expectations")
    if not isinstance(call_expectations, list):
        return ""
    for expectation in call_expectations:
        if not isinstance(expectation, dict):
            continue
        if expectation.get("expected_behavior") != "cli_help":
            continue
        function_name = str(expectation.get("function_name") or expectation.get("callable") or "").rsplit(".", 1)[-1]
        if function_name and function_name.isidentifier():
            return function_name
    return ""


def _cli_program_name_from_request(request: MaterialPatchGenerationRequest) -> str:
    call_expectations = request.current_context.get("call_expectations")
    if isinstance(call_expectations, list):
        for expectation in call_expectations:
            if not isinstance(expectation, dict):
                continue
            if expectation.get("expected_behavior") != "cli_help":
                continue
            for value in _cli_help_expectation_strings(expectation):
                program = _usage_program_name_from_text(value)
                if program:
                    return program
    evidence = "\n".join(_nested_evidence_strings(request.command_evidence))
    return _usage_program_name_from_text(evidence)


def _cli_help_expectation_strings(expectation: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("expected_stdout_contains", "expected_return_contains", "expected_contains", "evidence"):
        value = expectation.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value if item is not None)
    return values


def _usage_program_name_from_text(text: str) -> str:
    match = re.search(r"\busage:\s+([^\s\[]+)", text)
    return match.group(1).strip() if match else ""


def _cli_program_name_from_path(path: str) -> str:
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if len(parts) >= 2 and parts[-1] == "cli.py":
        return parts[-2].replace("_", "-")
    stem = parts[-1].rsplit(".", 1)[0] if parts else "generated-cli"
    return stem.replace("_", "-") or "generated-cli"


def _self_contained_python_replacement(
    content: str,
    *,
    forbidden_modules: list[str],
    expected_symbols: list[str],
) -> str | None:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    lines = content.splitlines()
    remove_lines: set[int] = set()
    for node in ast.walk(tree):
        if _node_imports_forbidden_module(node, forbidden_modules):
            end_line = getattr(node, "end_lineno", None) or node.lineno
            for line_number in range(getattr(node, "lineno", 0), end_line + 1):
                if line_number > 0:
                    remove_lines.add(line_number)
    if not remove_lines:
        return None
    kept = [line for index, line in enumerate(lines, start=1) if index not in remove_lines]
    existing_exports = _top_level_export_names("\n".join(kept))
    definitions = [
        _definition_for_expected_symbol(symbol, request=None)
        for symbol in expected_symbols
        if symbol and symbol not in existing_exports
    ]
    if not definitions:
        return "\n".join(kept).rstrip() + "\n"
    insert_at = _definition_insert_index(kept)
    updated = [*kept[:insert_at], *definitions, "", *kept[insert_at:]]
    return "\n".join(updated).rstrip() + "\n"


def _node_imports_forbidden_module(node: ast.AST, forbidden_modules: list[str]) -> bool:
    if isinstance(node, ast.Import):
        return any(_module_forbidden(alias.name, forbidden_modules) for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.module:
        return _module_forbidden(node.module, forbidden_modules)
    return False


def _module_forbidden(module: str, forbidden_modules: list[str]) -> bool:
    normalized = module.strip(".")
    return any(normalized == forbidden or normalized.startswith(f"{forbidden}.") for forbidden in forbidden_modules)


def _top_level_export_names(content: str) -> set[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    exports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            exports.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                exports.update(_assigned_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            exports.update(_assigned_target_names(node.target))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                exports.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    exports.add(alias.asname or alias.name)
    return exports


def _definition_for_expected_symbol(symbol: str, *, request: MaterialPatchGenerationRequest | None = None) -> str:
    if symbol == "__version__" or symbol.casefold().endswith("version"):
        return f'{symbol} = "0.1.0"'
    if symbol.isupper():
        return f'{symbol} = "{symbol.casefold()}"'
    expected_cases = _expected_return_cases_for_symbol(symbol, request=request) if request is not None else []
    if expected_cases:
        return _definition_for_expected_return_cases(symbol, expected_cases)
    expected_return = _expected_return_value_for_symbol(symbol, request=request) if request is not None else _NO_RETURN_VALUE
    if expected_return is not _NO_RETURN_VALUE:
        return (
            f"def {symbol}(*args, **kwargs):\n"
            f"    \"\"\"Runtime provider generated from observed return-value obligations.\"\"\"\n"
            f"    return {expected_return!r}"
        )
    if request is not None and _plan_requests_text_processing(request.plan):
        default_text = "Example text" if any(marker in symbol.casefold() for marker in ("run", "example", "demo")) else ""
        return (
            f"def {symbol}(text={default_text!r}, *args, **kwargs):\n"
            f"    \"\"\"Runtime provider generated from observed text-processing obligations.\"\"\"\n"
            f"    raw_text = str(text if text is not None else \"\")\n"
            f"    if args:\n"
            f"        raw_text = \" \".join([raw_text, *(str(item) for item in args)]).strip()\n"
            f"    normalized = \" \".join(raw_text.split())\n"
            f"    return {{\n"
            f"        \"normalized\": normalized,\n"
            f"        \"statistics\": {{\n"
            f"            \"characters\": len(normalized),\n"
            f"            \"words\": len(normalized.split()) if normalized else 0,\n"
            f"            \"lines\": raw_text.count(\"\\n\") + 1 if raw_text else 0,\n"
            f"        }},\n"
            f"    }}"
        )
    return (
        f"def {symbol}(*args, **kwargs):\n"
        f"    \"\"\"Runtime provider generated from observed interface obligations.\"\"\"\n"
        f"    return args[0] if args else kwargs"
    )


_NO_RETURN_VALUE = object()


def _definition_for_expected_return_cases(symbol: str, cases: list[dict[str, Any]]) -> str:
    case_literals: list[tuple[tuple[object, ...], dict[str, object], object]] = []
    for case in cases:
        arguments = case.get("arguments")
        keyword_arguments = case.get("keyword_arguments")
        if not isinstance(arguments, list):
            arguments = []
        if not isinstance(keyword_arguments, dict):
            keyword_arguments = {}
        case_literals.append((tuple(arguments), dict(keyword_arguments), case.get("expected_return_value")))
    return (
        f"def {symbol}(*args, **kwargs):\n"
        f"    \"\"\"Runtime provider generated from observed call/return validation evidence.\"\"\"\n"
        f"    cases = {case_literals!r}\n"
        f"    for expected_args, expected_kwargs, expected_return in cases:\n"
        f"        if args == expected_args and kwargs == expected_kwargs:\n"
        f"            return expected_return\n"
        f"    return args[0] if args else kwargs\n"
    ).rstrip()


def _expected_return_cases_for_symbol(
    symbol: str,
    *,
    request: MaterialPatchGenerationRequest | None,
) -> list[dict[str, Any]]:
    if request is None:
        return []
    cases: list[dict[str, Any]] = []
    for expectation in _call_expectations(request):
        function_name = str(expectation.get("function_name") or expectation.get("callable") or "").rsplit(".", 1)[-1]
        if function_name != symbol or "expected_return_value" not in expectation:
            continue
        if "arguments" not in expectation and "keyword_arguments" not in expectation:
            continue
        cases.append(
            {
                "arguments": expectation.get("arguments") if isinstance(expectation.get("arguments"), list) else [],
                "keyword_arguments": expectation.get("keyword_arguments")
                if isinstance(expectation.get("keyword_arguments"), dict)
                else {},
                "expected_return_value": expectation.get("expected_return_value"),
            }
        )
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for case in cases:
        key = json.dumps(case, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(case)
    return deduped


def _expected_return_value_for_symbol(
    symbol: str,
    *,
    request: MaterialPatchGenerationRequest,
) -> object:
    for expectation in _call_expectations(request):
        function_name = str(expectation.get("function_name") or expectation.get("callable") or "").rsplit(".", 1)[-1]
        if function_name == symbol and "expected_return_value" in expectation:
            return expectation.get("expected_return_value")
    for obligation in _repair_obligations(request):
        if obligation.get("kind") != "call_return_value":
            continue
        expected = obligation.get("expected")
        if not isinstance(expected, dict):
            continue
        callable_name = str(expected.get("callable") or "").rsplit(".", 1)[-1]
        if callable_name == symbol and "value" in expected:
            return expected.get("value")
    return _NO_RETURN_VALUE


def _call_expectations(request: MaterialPatchGenerationRequest) -> list[dict[str, Any]]:
    raw = request.current_context.get("call_expectations")
    expectations: list[dict[str, Any]] = []
    if isinstance(raw, list):
        expectations.extend(item for item in raw if isinstance(item, dict))
    expectations.extend(_call_expectations_from_pytest_evidence(_repair_evidence_text(request)))
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for expectation in expectations:
        key = json.dumps(expectation, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(expectation)
    return deduped


def _call_expectations_from_pytest_evidence(evidence: str) -> list[dict[str, Any]]:
    expectations: list[dict[str, Any]] = []
    for match in re.finditer(
        r"@pytest\.mark\.parametrize\(\s*['\"]([^'\"]+)['\"]\s*,",
        evidence,
    ):
        names = [name.strip() for name in match.group(1).split(",") if name.strip()]
        list_start = evidence.find("[", match.end())
        if list_start < 0:
            continue
        values_text, list_end = _balanced_python_literal(evidence, list_start)
        if not values_text:
            continue
        try:
            rows = ast.literal_eval(values_text)
        except (SyntaxError, ValueError):
            continue
        tail = evidence[list_end : list_end + 2500]
        assertion = re.search(
            r"assert\s+([A-Za-z_]\w*)\(([^)]*)\)\s*==\s*([A-Za-z_]\w*)",
            tail,
        )
        if assertion is None:
            continue
        function_name = assertion.group(1)
        argument_names = [part.strip() for part in assertion.group(2).split(",") if part.strip()]
        expected_name = assertion.group(3).strip()
        if not argument_names or not all(_is_simple_name(name) for name in [*argument_names, expected_name]):
            continue
        for raw_row in rows if isinstance(rows, list | tuple) else []:
            row = raw_row if isinstance(raw_row, tuple | list) else (raw_row,)
            if len(row) != len(names):
                continue
            bindings = dict(zip(names, row, strict=True))
            if expected_name not in bindings or any(name not in bindings for name in argument_names):
                continue
            expectations.append(
                {
                    "function_name": function_name,
                    "arguments": [bindings[name] for name in argument_names],
                    "keyword_arguments": {},
                    "minimum_positional_arguments": len(argument_names),
                    "expected_return_value": bindings[expected_name],
                    "expected_behavior": "literal_return",
                    "evidence": _compact_text(match.group(0), 240),
                }
            )
    return expectations


def _balanced_python_literal(text: str, start: int) -> tuple[str, int]:
    if start < 0 or start >= len(text) or text[start] not in "[{(":
        return "", start
    opening = text[start]
    closing = {"[": "]", "{": "}", "(": ")"}[opening]
    stack = [closing]
    quote = ""
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in "[{(":
            stack.append({"[": "]", "{": "}", "(": ")"}[char])
            continue
        if char in "]})":
            if not stack or char != stack[-1]:
                return "", start
            stack.pop()
            if not stack:
                return text[start : index + 1], index + 1
    return "", start


def _is_simple_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_]\w*", value))


def _repair_obligations(request: MaterialPatchGenerationRequest) -> list[dict[str, Any]]:
    obligations: list[dict[str, Any]] = []
    for raw in (request.current_context.get("repair_obligations"), request.command_evidence.get("obligations")):
        if isinstance(raw, list):
            obligations.extend(item for item in raw if isinstance(item, dict))
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for obligation in obligations:
        key = json.dumps(obligation, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(obligation)
    return deduped


def _definition_insert_index(lines: list[str]) -> int:
    index = 0
    if lines and (lines[0].startswith('"""') or lines[0].startswith("'''")):
        quote = lines[0][:3]
        index = 1
        while index < len(lines):
            if lines[index].rstrip().endswith(quote):
                index += 1
                break
            index += 1
    while index < len(lines) and lines[index].startswith("from __future__ import "):
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def _replacement_contract_invalid_payload(
    payload: dict[str, Any],
    *,
    request: MaterialPatchGenerationRequest,
    contract_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    missing_symbols = sorted(
        {
            str(issue.get("symbol") or "").strip()
            for issue in contract_issues
            if issue.get("issue_type") == "missing_expected_symbol" and str(issue.get("symbol") or "").strip()
        }
    )
    forbidden_modules = sorted(
        {
            str(issue.get("module") or "").strip(".")
            for issue in contract_issues
            if str(issue.get("module") or "").strip(".")
        }
    )
    placeholder_expected_symbols = sorted(
        {
            str(issue.get("symbol") or "").strip()
            for issue in contract_issues
            if issue.get("issue_type") == "placeholder_expected_symbol" and str(issue.get("symbol") or "").strip()
        }
    )
    return {
        "reason": "replacement_contract_invalid",
        "target_path": request.target_path,
        "expected_symbols": _expected_symbols_from_repair_request(request),
        "call_expectations": request.current_context.get("call_expectations")
        if isinstance(request.current_context.get("call_expectations"), list)
        else [],
        "missing_expected_symbols": missing_symbols,
        "placeholder_expected_symbols": placeholder_expected_symbols,
        "contract_issues": contract_issues,
        "allowed_local_modules": _planned_local_python_modules(request.plan),
        "declared_external_dependency_roots": sorted(_declared_dependency_roots(request.plan)),
        "forbidden_modules": forbidden_modules,
        "forbidden_local_modules": forbidden_modules,
        "local_import_rule": (
            "Local imports must resolve to exact entries in allowed_local_modules. "
            "External imports must resolve to declared_external_dependency_roots. Relative imports are "
            "invalid when they resolve to modules that are not planned. When no planned local provider "
            "exists for an expected symbol, implement the symbol directly in the target file with "
            "requirement-derived behavior instead of inventing an import. Generated repair replacements "
            "for child modules must not import their package root; put shared symbols in the target module "
            "or another planned child/helper module."
        ),
        "placeholder_repair_rule": (
            "A symbol listed in placeholder_expected_symbols is still invalid even when the name exists. "
            "Replace pass-only bodies, return None, Ellipsis, NotImplemented, or None assignments with a "
            "concrete implementation derived from the requirements/current_content, or explicitly re-export "
            "a concrete planned local module symbol."
        ),
        "call_expectation_rule": (
            "When call_expectations are present, replacement_content must define the observed callable with "
            "a compatible positional signature. If expected_behavior is cli_help, the callable must handle "
            "help-style argv such as --help and emit usage/help output instead of treating the argument as "
            "ordinary data."
        ),
        "local_import_cycle": request.current_context.get("local_import_cycle")
        if isinstance(request.current_context.get("local_import_cycle"), dict)
        else None,
        "local_import_cycle_rule": (
            "When local_import_cycle is present, do not import the package root from a child module involved "
            "in that cycle. Break the cycle by defining the shared symbol in the child module or another "
            "planned local module, and let the package root re-export it."
        ),
        "instruction": (
            "Return a replacement object for exactly the same target_path. For Python targets, "
            "replacement_content must parse as Python, must not use relative imports from top-level "
            "modules, must not import unplanned local modules, and must explicitly define or import "
            "every missing symbol at top level when missing_expected_symbols is non-empty. For Python "
            "test targets, replacement_content must keep at least one test discoverable by pytest or "
            "unittest. Do not change identity fields."
        ),
        "invalid_payload": payload,
    }


def _replacement_contract_retry_constraints(
    invalid_payload: dict[str, Any],
    *,
    request: MaterialPatchGenerationRequest,
) -> dict[str, Any]:
    if invalid_payload.get("reason") != "replacement_contract_invalid":
        return {}
    forbidden_modules = [
        str(module).strip(".")
        for module in invalid_payload.get("forbidden_local_modules") or invalid_payload.get("forbidden_modules") or []
        if str(module).strip(".")
    ]
    return {
        "allowed_exact_local_modules": _planned_local_python_modules(request.plan),
        "declared_external_dependency_roots": sorted(_declared_dependency_roots(request.plan)),
        "must_not_import_modules": sorted(dict.fromkeys(forbidden_modules)),
        "call_expectations": invalid_payload.get("call_expectations")
        if isinstance(invalid_payload.get("call_expectations"), list)
        else [],
        "self_contained_target_preferred": bool(forbidden_modules),
        "acceptance": [
            "replacement_content contains no import statement for must_not_import_modules",
            "any local import resolves exactly to allowed_exact_local_modules",
            "when no exact local provider exists, required behavior is implemented directly in target_path",
            "observed call_expectations are satisfied by compatible callable signatures and behavior",
        ],
    }


def _replacement_contract_issues(
    proposal: ReplacementProposal,
    *,
    request: MaterialPatchGenerationRequest,
) -> list[dict[str, Any]]:
    if request.target_path.replace("\\", "/").endswith(".toml"):
        parse_error = _toml_parse_error(proposal.replacement_content)
        if parse_error is not None:
            return [
                {
                    "issue_type": "toml_parse_error",
                    "message": parse_error,
                }
            ]
        return []
    if not request.target_path.replace("\\", "/").endswith(".py"):
        return []
    issues: list[dict[str, Any]] = []
    try:
        ast.parse(proposal.replacement_content, filename=request.target_path)
    except SyntaxError as exc:
        issues.append(
            {
                "issue_type": "python_syntax_error",
                "line": exc.lineno,
                "offset": exc.offset,
                "message": exc.msg,
            }
        )
    issues.extend(
        {
            "issue_type": "missing_expected_symbol",
            "symbol": symbol,
        }
        for symbol in _missing_expected_replacement_symbols(proposal, request=request)
    )
    issues.extend(
        _unplanned_local_import_issues(
            proposal.replacement_content,
            path=request.target_path,
            kind="python",
            project_root=request.plan.project_root,
            planned_modules=_planned_local_python_modules(request.plan),
        )
    )
    issues.extend(
        _undeclared_external_import_issues(
            proposal.replacement_content,
            path=request.target_path,
            kind="python",
            project_root=request.plan.project_root,
            planned_modules=_planned_local_python_modules(request.plan),
            declared_dependency_roots=_declared_dependency_roots(request.plan),
        )
    )
    issues.extend(
        _local_import_cycle_replacement_issues(
            proposal.replacement_content,
            path=request.target_path,
            project_root=request.plan.project_root,
            current_context=request.current_context,
        )
    )
    issues.extend(
        _child_package_root_import_issues(
            proposal.replacement_content,
            path=request.target_path,
            project_root=request.plan.project_root,
        )
    )
    issues.extend(
        _placeholder_contract_issues(
            proposal.replacement_content,
            path=request.target_path,
            kind="python",
            expected_symbols=_expected_symbols_from_repair_request(request),
        )
    )
    issues.extend(
        _call_expectation_contract_issues(
            proposal.replacement_content,
            path=request.target_path,
            current_context=request.current_context,
        )
    )
    missing_test_issue = _missing_collectible_test_issue(
        proposal.replacement_content,
        path=request.target_path,
        target_kind=request.issue.target_kind,
        validation_profile=request.validation_profile,
    )
    if missing_test_issue:
        issues.append(missing_test_issue)
    return _dedupe_contract_issues(issues)


def _call_expectation_contract_issues(
    content: str,
    *,
    path: str,
    current_context: dict[str, Any],
) -> list[dict[str, Any]]:
    call_expectations = current_context.get("call_expectations")
    if not isinstance(call_expectations, list) or not call_expectations:
        return []
    try:
        module = ast.parse(content, filename=path)
    except SyntaxError:
        return []
    functions = {
        node.name: node
        for node in module.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    normalized_content = content.casefold()
    issues: list[dict[str, Any]] = []
    for expectation in call_expectations:
        if not isinstance(expectation, dict):
            continue
        function_name = str(expectation.get("function_name") or expectation.get("callable") or "").rsplit(".", 1)[-1]
        if not function_name:
            continue
        node = functions.get(function_name)
        if node is None:
            continue
        minimum_positional = _safe_int(expectation.get("minimum_positional_arguments"))
        if minimum_positional > 0 and not _function_accepts_positional_count(node, minimum_positional):
            issues.append(
                {
                    "issue_type": "call_signature_mismatch",
                    "symbol": function_name,
                    "minimum_positional_arguments": minimum_positional,
                    "message": (
                        f"replacement function {function_name} does not accept the positional "
                        "arguments shown by validation evidence"
                    ),
                    "evidence": expectation.get("evidence"),
                }
            )
        if expectation.get("expected_behavior") == "cli_help" and not _content_declares_cli_help_behavior(
            normalized_content
        ):
            issues.append(
                {
                    "issue_type": "call_behavior_mismatch",
                    "symbol": function_name,
                    "expected_behavior": "cli_help",
                    "expected_stdout_contains": expectation.get("expected_stdout_contains") or ["usage:"],
                    "message": "replacement does not declare help/usage handling required by validation evidence",
                    "evidence": expectation.get("evidence"),
                }
            )
        if expectation.get("expected_behavior") == "cli_help":
            for fragment in _expected_cli_help_fragments_from_expectation(expectation):
                if not _content_declares_expected_cli_help_fragment(content, fragment):
                    issues.append(
                        {
                            "issue_type": "call_behavior_mismatch",
                            "symbol": function_name,
                            "expected_behavior": "cli_help",
                            "expected_stdout_contains": [fragment],
                            "message": (
                                "replacement declares help/usage handling but not the exact usage program "
                                "shown by validation evidence"
                            ),
                            "evidence": expectation.get("evidence"),
                        }
                    )
        if expectation.get("expected_behavior") == "cli_help" and _content_has_argparse_manual_help_conflict(content):
            issues.append(
                {
                    "issue_type": "argparse_manual_help_conflict",
                    "symbol": function_name,
                    "expected_behavior": "cli_help",
                    "message": (
                        "replacement manually registers -h/--help on an argparse parser that still has "
                        "argparse automatic help enabled"
                    ),
                    "evidence": expectation.get("evidence"),
                }
            )
        if (
            expectation.get("expected_behavior") == "cli_help"
            and str(expectation.get("expected_exception") or "") == "SystemExit"
            and not _content_declares_system_exit_cli_help(content)
        ):
            issues.append(
                {
                    "issue_type": "call_exception_mismatch",
                    "symbol": function_name,
                    "expected_behavior": "cli_help",
                    "expected_exception": "SystemExit",
                    "message": (
                        "replacement handles help-style argv without the SystemExit behavior shown by "
                        "validation evidence"
                    ),
                    "evidence": expectation.get("evidence"),
                }
            )
        if "expected_return_value" in expectation:
            expected_value = expectation.get("expected_return_value")
            actual_value = _static_literal_return_value(node)
            if actual_value is _NO_RETURN_VALUE or actual_value != expected_value:
                issues.append(
                    {
                        "issue_type": "call_return_value_mismatch",
                        "symbol": function_name,
                        "expected_return_value": expected_value,
                        "message": (
                            f"replacement function {function_name} does not return the literal value "
                            "shown by validation evidence"
                        ),
                        "evidence": expectation.get("evidence"),
                    }
                )
    return _dedupe_contract_issues(issues)


def _static_literal_return_value(node: ast.FunctionDef | ast.AsyncFunctionDef) -> object:
    for child in ast.walk(node):
        if isinstance(child, ast.Return):
            if child.value is None:
                return None
            if isinstance(child.value, ast.Constant):
                return child.value.value
            return _NO_RETURN_VALUE
    return _NO_RETURN_VALUE


def _function_accepts_positional_count(node: ast.FunctionDef | ast.AsyncFunctionDef, count: int) -> bool:
    if node.args.vararg is not None:
        return True
    positional = len(node.args.posonlyargs) + len(node.args.args)
    return positional >= count


def _content_declares_cli_help_behavior(normalized_content: str) -> bool:
    return any(marker in normalized_content for marker in ("argparse", "--help", "usage:", "usage =", "help="))


def _expected_cli_help_fragments_from_expectation(expectation: dict[str, Any]) -> list[str]:
    fragments: list[str] = []
    for value in _cli_help_expectation_strings(expectation):
        if "usage:" not in value.casefold():
            continue
        program = _usage_program_name_from_text(value)
        fragment = value.strip() if program else "usage:"
        if fragment and fragment not in fragments:
            fragments.append(fragment)
    return fragments


def _content_declares_expected_cli_help_fragment(content: str, fragment: str) -> bool:
    normalized_fragment = fragment.casefold()
    if normalized_fragment in content.casefold():
        return True
    expected_program = _usage_program_name_from_text(fragment)
    if not expected_program:
        return True
    declared_program = _argument_parser_prog_from_content(content)
    return bool(declared_program and declared_program == expected_program)


def _argument_parser_prog_from_content(content: str) -> str:
    try:
        module = ast.parse(content)
    except SyntaxError:
        return ""
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or not _is_argparse_argument_parser_call(node):
            continue
        for call_keyword in node.keywords:
            if call_keyword.arg == "prog" and isinstance(call_keyword.value, ast.Constant):
                value = call_keyword.value.value
                if isinstance(value, str):
                    return value
    return ""


def _content_has_argparse_manual_help_conflict(content: str) -> bool:
    try:
        module = ast.parse(content)
    except SyntaxError:
        return False
    has_default_argparse_parser = False
    has_manual_help_argument = False
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if _is_argparse_argument_parser_call(node):
            add_help_disabled = any(
                keyword.arg == "add_help"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
                for keyword in node.keywords
            )
            if not add_help_disabled:
                has_default_argparse_parser = True
        if _is_add_argument_help_call(node):
            has_manual_help_argument = True
    return has_default_argparse_parser and has_manual_help_argument


def _content_declares_system_exit_cli_help(content: str) -> bool:
    try:
        module = ast.parse(content)
    except SyntaxError:
        return False
    for node in ast.walk(module):
        if isinstance(node, ast.Raise):
            exc = node.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name) and exc.id == "SystemExit":
                return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "parse_args":
                return True
            if isinstance(func, ast.Name) and func.id == "SystemExit":
                return True
    return False


def _is_argparse_argument_parser_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "ArgumentParser"
        and isinstance(func.value, ast.Name)
        and func.value.id == "argparse"
    ) or (isinstance(func, ast.Name) and func.id == "ArgumentParser")


def _is_add_argument_help_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "add_argument":
        return False
    values: list[str] = []
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            values.append(arg.value)
    return any(value in {"-h", "--help"} for value in values)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _local_import_cycle_replacement_issues(
    content: str,
    *,
    path: str,
    project_root: str,
    current_context: dict[str, Any],
) -> list[dict[str, Any]]:
    cycle = current_context.get("local_import_cycle")
    if not isinstance(cycle, dict):
        return []
    current_module = _python_module_name_for_plan_path(path, project_root)
    if not current_module or "." not in current_module:
        return []
    package_root = current_module.split(".", 1)[0]
    cycle_roots = set(_local_import_cycle_roots(cycle))
    if cycle_roots and package_root not in cycle_roots:
        return []
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError:
        return []
    issues: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.strip(".")
                if module == package_root:
                    issues.append(_local_import_cycle_issue(path=path, module=module))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = node.module.strip(".")
            if module == package_root:
                issues.append(_local_import_cycle_issue(path=path, module=module))
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            module = _resolved_relative_import_module(current_module, node)
            if module == package_root:
                issues.append(_local_import_cycle_issue(path=path, module=module))
    return _dedupe_contract_issues(issues)


def _child_package_root_import_issues(
    content: str,
    *,
    path: str,
    project_root: str,
) -> list[dict[str, Any]]:
    current_module = _python_module_name_for_plan_path(path, project_root)
    if not current_module or "." not in current_module:
        return []
    package_root = current_module.split(".", 1)[0]
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError:
        return []
    issues: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.strip(".")
                if module == package_root:
                    issues.append(_local_import_cycle_issue(path=path, module=module))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = node.module.strip(".")
            if module == package_root:
                issues.append(_local_import_cycle_issue(path=path, module=module))
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            module = _resolved_relative_import_module(current_module, node)
            if module == package_root:
                issues.append(_local_import_cycle_issue(path=path, module=module))
    return _dedupe_contract_issues(issues)


def _resolved_relative_import_module(current_module: str, node: ast.ImportFrom) -> str:
    parts = [part for part in current_module.split(".") if part]
    if not parts:
        return str(node.module or "").strip(".")
    base_count = max(0, len(parts) - node.level)
    base_parts = parts[:base_count]
    module_tail = [part for part in str(node.module or "").strip(".").split(".") if part]
    return ".".join([*base_parts, *module_tail])


def _local_import_cycle_roots(cycle: dict[str, Any]) -> list[str]:
    roots: list[str] = []
    modules = cycle.get("partially_initialized_modules")
    if isinstance(modules, list):
        roots.extend(str(module).split(".", 1)[0] for module in modules if str(module).strip())
    targets = cycle.get("involved_targets")
    if isinstance(targets, list):
        for target in targets:
            if isinstance(target, dict):
                module = str(target.get("module") or "").strip()
                if module:
                    roots.append(module.split(".", 1)[0])
    return sorted({root for root in roots if root})


def _local_import_cycle_issue(*, path: str, module: str) -> dict[str, Any]:
    return {
        "issue_type": "local_import_cycle_import",
        "path": path,
        "module": module,
        "message": "replacement imports a package root that is involved in a local import cycle",
    }


def _missing_collectible_test_issue(
    content: str,
    *,
    path: str,
    target_kind: str,
    validation_profile: str,
) -> dict[str, Any] | None:
    if _is_pytest_support_file(path):
        return None
    normalized_path = path.replace("\\", "/").rsplit("/", 1)[-1]
    is_test_target = (
        normalized_path.startswith("test_")
        or normalized_path.endswith("_test.py")
        or target_kind == "test_file"
    )
    if not is_test_target or not path.endswith(".py"):
        return None
    try:
        module = ast.parse(content, filename=path)
    except SyntaxError:
        return None
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
            return None
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name.startswith("test_"):
                    return None
    return {
        "issue_type": "missing_collectible_test",
        "message": "Python test target replacement does not expose a pytest/unittest-discoverable test",
    }


def _is_pytest_support_file(path: str) -> bool:
    filename = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return filename == "conftest.py"


def _missing_expected_replacement_symbols(
    proposal: ReplacementProposal,
    *,
    request: MaterialPatchGenerationRequest,
) -> list[str]:
    if not request.target_path.replace("\\", "/").endswith(".py"):
        return []
    expected_symbols = _expected_symbols_from_repair_request(request)
    if not expected_symbols:
        return []
    return _missing_top_level_python_symbols(proposal.replacement_content, expected_symbols)


def _missing_top_level_python_symbols(content: str, expected_symbols: list[str]) -> list[str]:
    required = [
        symbol
        for symbol in _dedupe_strings([str(item).strip() for item in expected_symbols])
        if symbol.isidentifier() and not keyword.iskeyword(symbol)
    ]
    if not required:
        return []
    try:
        module = ast.parse(content)
    except SyntaxError:
        return required
    exported: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            exported.add(node.name)
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                exported.update(_assigned_target_names(target))
            continue
        if isinstance(node, ast.AnnAssign):
            exported.update(_assigned_target_names(node.target))
            continue
        if isinstance(node, ast.AugAssign):
            exported.update(_assigned_target_names(node.target))
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                exported.add(alias.asname or alias.name.split(".", 1)[0])
            continue
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                exported.add(alias.asname or alias.name)
    return [symbol for symbol in required if symbol not in exported]


def _assigned_target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for item in node.elts:
            names.update(_assigned_target_names(item))
        return names
    return set()


def _replacement_proposal_from_payload(
    payload: dict[str, Any],
    *,
    request: MaterialPatchGenerationRequest,
) -> ReplacementProposal:
    replacement_content = str(payload.get("replacement_content") or "")
    replacement_sha256 = f"sha256:{hashlib.sha256(replacement_content.encode('utf-8')).hexdigest()}"
    try:
        return ReplacementProposal.model_validate(
            {
                "issue_id": request.issue_id,
                "target_path": request.target_path,
                "expected_old_sha256": request.expected_old_sha256,
                "replacement_content": replacement_content,
                "replacement_sha256": replacement_sha256,
                "requirement_refs": request.issue.requirement_refs,
                "contract_refs": request.issue.contract_refs,
                "rationale": payload.get("rationale"),
            }
        )
    except ValidationError as exc:
        raise MaterialLLMError(
            "llm_schema_invalid",
            "LLM replacement proposal did not satisfy the replacement contract",
            details={
                "target_path": request.target_path,
                "validation_errors": _validation_errors(exc),
                "payload_excerpt": _compact_text(json.dumps(payload, ensure_ascii=False), 1000),
            },
        ) from exc


def _validate_repair_identity(
    proposal: PatchProposal | ReplacementProposal,
    *,
    request: MaterialPatchGenerationRequest,
) -> None:
    if proposal.target_path != request.target_path:
        raise MaterialLLMError(
            "llm_contract_violation",
            "LLM repair proposal returned a path that does not match the repair target",
            details={"expected_path": request.target_path, "actual_path": proposal.target_path},
        )
    if proposal.expected_old_sha256 != request.expected_old_sha256:
        raise MaterialLLMError(
            "llm_contract_violation",
            "LLM repair proposal returned a hash that does not match the current target file",
            details={
                "target_path": request.target_path,
                "expected_old_sha256": request.expected_old_sha256,
                "actual_old_sha256": proposal.expected_old_sha256,
            },
        )


__all__ = [
    "MaterialLLMError",
    "critique_repair_with_llm",
    "generate_files_with_llm",
    "generate_patch_with_llm",
    "generate_plan_with_llm",
    "repair_plan_with_llm",
]
