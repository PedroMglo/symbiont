"""Query-to-job intent parsing for extrator.

The orchestrator may route a request to extrator, but the extrator service owns
how a natural-language query becomes an extraction or conversion job.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

_ABS_PATH_RE = re.compile(r"(?<![\w.-])(?P<path>/(?:[^\s'\"`<>])+)")
_QUOTED_PATH_RE = re.compile(r"['\"](?P<path>/(?:[^'\"])+)['\"]")
_DESTINATION_CLAUSE_RE = re.compile(
    r"(?:"
    r"(?:guarda|guardar|salva|salvar|save|store|coloca|colocar|mete|meter)"
    r"(?:\s+(?:o|os|a|as))?"
    r"(?:\s+(?:output|outputs|resultado|resultados|ficheiro|ficheiros|arquivo|arquivos|pdf|pdfs))?"
    r"|"
    r"(?:output|outputs|resultado|resultados|pdf|pdfs)"
    r")"
    r"\s+(?:na|no|em|para|in|to)"
    r"(?:\s+(?:a|o))?"
    r"(?:\s+(?:pasta|folder|diret[oó]rio|direct[oó]rio|directory))?"
    r"\s+(?P<path>/[^\n\r]+?)\s*(?=$|[\n\r])",
    re.IGNORECASE,
)
_QUOTED_DESTINATION_PAIR_RE = re.compile(
    r"['\"](?P<src>/[^'\"]+)['\"]"
    r"\s+(?:para|to|em|in|na|no)"
    r"(?:\s+(?:a|o))?"
    r"(?:\s+(?:pasta|folder|diret[oó]rio|direct[oó]rio|directory))?"
    r"\s+['\"](?P<dest>/[^'\"]+)['\"]",
    re.IGNORECASE,
)
_PLAIN_DESTINATION_PAIR_RE = re.compile(
    r"(?P<src>/[^\s'\"`<>]+)"
    r"\s+(?:para|to|em|in|na|no)"
    r"(?:\s+(?:a|o))?"
    r"(?:\s+(?:pasta|folder|diret[oó]rio|direct[oó]rio|directory))?"
    r"\s+(?P<dest>/[^\n\r,;]+?)"
    r"(?=\s+(?:e|and)\s+/[^\s'\"`<>]+|[,;\n\r]|$)",
    re.IGNORECASE,
)

_DATA_EXTENSIONS = frozenset({
    "csv",
    "tsv",
    "json",
    "jsonl",
    "ndjson",
    "csv.gz",
    "tsv.gz",
    "jsonl.gz",
    "ndjson.gz",
})

_CONVERSION_FORMATS = frozenset({"markdown", "md", "html", "pdf", "docx", "json", "parquet"})
_CONVERSION_SOURCE_EXTENSIONS = frozenset({
    "csv",
    "doc",
    "docx",
    "html",
    "htm",
    "json",
    "md",
    "markdown",
    "odt",
    "pdf",
    "pptx",
    "rtf",
    "txt",
    "xls",
    "xlsx",
})
_EXTRACTION_QUERY_CAPABILITIES = frozenset({"document_etl", "document_extraction", "rag_bundle"})
_CONVERSION_QUERY_CAPABILITIES = frozenset({"file_conversion"})
_EXTRACTION_QUERY_ACTIONS = frozenset({"extract", "extraction"})
_CONVERSION_QUERY_ACTIONS = frozenset({"convert", "conversion"})
_CAPABILITY_METADATA_KEYS = (
    "capability",
    "capabilities",
    "requested_capability",
    "requested_capabilities",
    "workflow_capability",
    "workflow_capabilities",
)
_ACTION_METADATA_KEYS = ("action", "actions", "requested_action", "requested_actions", "workflow_action")


@dataclass(frozen=True)
class ExtratorPathRequest:
    """Path job selected from an extrator query."""

    original_path: str
    input_path: str
    recursive: bool
    force: bool
    conversion_format: str | None = None
    output_path: str | None = None
    output_paths: dict[str, str] = field(default_factory=dict)


def extract_absolute_path(query: str) -> str | None:
    paths = extract_absolute_paths(query)
    return paths[0] if paths else None


def extract_absolute_paths(query: str) -> tuple[str, ...]:
    """Extract absolute paths while preserving order and removing duplicates."""

    text = query or ""
    found: list[str] = []
    for match in _QUOTED_PATH_RE.finditer(text):
        found.append(match.group("path").rstrip(".,;:)])}"))
    for match in _ABS_PATH_RE.finditer(text):
        found.append(match.group("path").rstrip(".,;:)])}"))
    unique: list[str] = []
    seen: set[str] = set()
    for path in found:
        if path and path not in seen:
            unique.append(path)
            seen.add(path)
    return tuple(unique)


def select_path_request(query: str) -> ExtratorPathRequest | None:
    """Select the best extraction/conversion path request from a query."""

    data_requests = data_path_requests(query)
    if data_requests:
        return data_requests[0]
    return parse_path_request(query)


def select_path_request_from_metadata(metadata: dict) -> ExtratorPathRequest | None:
    """Select an extraction/conversion path from typed caller metadata.

    Natural-language path parsing remains useful for direct human calls, but
    service-to-service calls should not depend on embedding filesystem paths in
    prose. This keeps paths with spaces, accents, or punctuation intact.
    """

    if not isinstance(metadata, dict):
        return None
    original = _metadata_path(metadata)
    if not original:
        return None
    return ExtratorPathRequest(
        original_path=original,
        input_path=map_host_path_to_container(original),
        recursive=_metadata_bool(metadata, "recursive"),
        force=_metadata_bool(metadata, "force"),
        conversion_format=_metadata_conversion_format(metadata),
        output_path=_metadata_output_path(metadata),
        output_paths=_metadata_output_paths(metadata),
    )


def resolve_path_request_job_mode(
    selected: ExtratorPathRequest,
    metadata: dict,
) -> tuple[ExtratorPathRequest, str, str]:
    """Resolve job mode from typed metadata before natural-language fallback.

    The query text parser is intentionally retained for direct feature calls.
    When a runtime caller supplies owner-published capability/action metadata,
    that typed contract is the authority for extraction versus conversion.
    """

    metadata_job_kind = _metadata_job_kind(metadata)
    if metadata_job_kind == "extraction":
        if selected.conversion_format or selected.output_path or selected.output_paths:
            selected = replace(
                selected,
                conversion_format=None,
                output_path=None,
                output_paths={},
            )
        return selected, "extraction", "metadata_capability"
    if metadata_job_kind == "conversion" and selected.conversion_format:
        return selected, "conversion", "metadata_capability"
    if selected.conversion_format:
        return selected, "conversion", "query_text"
    return selected, "extraction", "query_text"


def data_path_requests(query: str) -> list[ExtratorPathRequest]:
    """Prefer dataset roots over incidental instruction files."""

    if not _query_mentions_dataset(query):
        return []
    originals: list[str] = []
    seen: set[str] = set()
    for original in extract_absolute_paths(query):
        ext = _path_extension(original)
        if ext not in _DATA_EXTENSIONS:
            continue
        if original in seen:
            continue
        seen.add(original)
        originals.append(original)
        if len(originals) >= 12:
            break
    if not originals:
        return []
    if len(originals) == 1:
        original = originals[0]
        return [
            ExtratorPathRequest(
                original_path=original,
                input_path=map_host_path_to_container(original),
                recursive=False,
                force=True,
            )
        ]
    common = os.path.commonpath(originals)
    if _path_extension(common) in _DATA_EXTENSIONS:
        common = common.rsplit("/", 1)[0] or common
    return [
        ExtratorPathRequest(
            original_path=common,
            input_path=map_host_path_to_container(common),
            recursive=True,
            force=True,
        )
    ]


def parse_path_request(query: str) -> ExtratorPathRequest | None:
    original = extract_absolute_path(query)
    if not original:
        return None

    lower = (query or "").lower()
    suffix = PurePosixPath(original).suffix
    recursive = (
        not suffix
        or any(term in lower for term in ("pasta", "folder", "diretoria", "directoria", "directory", "recursiv"))
    )
    force = any(term in lower for term in ("force", "força", "forca", "reprocessa", "reprocessar", "sobrescreve"))
    return ExtratorPathRequest(
        original_path=original,
        input_path=map_host_path_to_container(original),
        recursive=recursive,
        force=force,
        conversion_format=_conversion_format(query),
        output_path=_destination_path(query, original),
        output_paths=_destination_overrides(query),
    )


def map_host_path_to_container(path: str) -> str:
    """Map host-visible paths into the mounts used by the extrator service."""

    raw = path.strip()
    if raw.startswith(("/host_home/", "/projects/", "/data/input/", "/data/uploads/")):
        return raw

    host_home = _host_home_prefix()
    project_prefix = f"{host_home}/_projects"
    if raw == project_prefix:
        return "/projects"
    if raw.startswith(f"{project_prefix}/"):
        return "/projects/" + raw[len(project_prefix) + 1 :]

    if raw == host_home:
        return "/host_home"
    if raw.startswith(f"{host_home}/"):
        return "/host_home/" + raw[len(host_home) + 1 :]

    return raw


def _host_home_prefix() -> str:
    return os.environ.get("HOST_HOME_PREFIX", "").strip().rstrip("/") or os.path.expanduser("~").rstrip("/")


def _query_mentions_dataset(query: str) -> bool:
    q = (query or "").lower()
    return any(
        term in q
        for term in (
            "accounts.csv",
            "csv",
            "data pipeline",
            "dataset",
            "datasets",
            "drift",
            "events-",
            "expected_schema",
            "jsonl",
            "ndjson",
            "retention",
            "schema",
            "streaming",
            "users.csv",
        )
    )


def _metadata_job_kind(metadata: dict) -> str | None:
    capabilities = set(_metadata_values(metadata, _CAPABILITY_METADATA_KEYS))
    if capabilities & _EXTRACTION_QUERY_CAPABILITIES:
        return "extraction"
    if capabilities & _CONVERSION_QUERY_CAPABILITIES:
        return "conversion"

    actions = set(_metadata_values(metadata, _ACTION_METADATA_KEYS))
    if actions & _EXTRACTION_QUERY_ACTIONS:
        return "extraction"
    if actions & _CONVERSION_QUERY_ACTIONS:
        return "conversion"
    return None


def _metadata_path(metadata: dict) -> str | None:
    scalar_keys = (
        "input_path",
        "source_path",
        "path",
        "file_path",
        "document_path",
    )
    for key in scalar_keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip().startswith("/"):
            return value.strip()
    for key in ("input_paths", "source_paths", "paths", "file_paths", "document_paths"):
        value = metadata.get(key)
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            text = str(item).strip()
            if text.startswith("/"):
                return text
    workspace = str(metadata.get("workspace") or "").strip().rstrip("/")
    relative = str(metadata.get("relative_path") or metadata.get("path_relative_to_workspace") or "").strip()
    if workspace.startswith("/") and relative and not relative.startswith("/"):
        return f"{workspace}/{relative.lstrip('/')}"
    return None


def _metadata_conversion_format(metadata: dict) -> str | None:
    value = str(metadata.get("conversion_format") or metadata.get("output_format") or "").strip().lower()
    if value == "md":
        value = "markdown"
    return value if value in _CONVERSION_FORMATS else None


def _metadata_output_path(metadata: dict) -> str | None:
    value = metadata.get("output_path")
    if isinstance(value, str) and value.strip().startswith("/"):
        return map_host_path_to_container(value.strip())
    return None


def _metadata_output_paths(metadata: dict) -> dict[str, str]:
    value = metadata.get("output_paths")
    if not isinstance(value, dict):
        return {}
    mapped: dict[str, str] = {}
    for key, item in value.items():
        source = str(key).strip()
        destination = str(item).strip()
        if source.startswith("/") and destination.startswith("/"):
            mapped[map_host_path_to_container(source)] = map_host_path_to_container(destination)
    return mapped


def _metadata_bool(metadata: dict, key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "sim"}
    return False


def _metadata_values(metadata: dict, keys: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    if not isinstance(metadata, dict):
        return ()
    for key in keys:
        raw = metadata.get(key)
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, (list, tuple, set)):
            values.extend(str(item) for item in raw if item is not None)
    return tuple(value.strip().lower() for value in values if value and value.strip())


def _path_extension(path: str) -> str:
    lowered = (path or "").lower().rstrip(".,;:)])}")
    for ext in sorted(_DATA_EXTENSIONS, key=len, reverse=True):
        if lowered.endswith(f".{ext}"):
            return ext
    suffix = lowered.rsplit("/", 1)[-1].rsplit(".", 1)
    return suffix[1] if len(suffix) == 2 else ""


def _conversion_format(query: str) -> str | None:
    lower = (query or "").lower()
    match = re.search(r"\b(?:para|to)\s+(markdown|md|html|pdf|docx|json|parquet)\b", lower)
    if not match:
        return None
    fmt = match.group(1)
    if fmt == "md":
        return "markdown"
    return fmt if fmt in _CONVERSION_FORMATS else None


def _destination_path(query: str, input_path: str) -> str | None:
    if not _conversion_format(query):
        return None
    match = _DESTINATION_CLAUSE_RE.search(query or "")
    if not match:
        paths = extract_absolute_paths(query)
        if len(paths) < 2:
            return None
        candidate = paths[1]
    else:
        candidate = _clean_destination_path(match.group("path"))
    if not candidate or candidate == input_path:
        return None
    return map_host_path_to_container(candidate)


def _destination_overrides(query: str) -> dict[str, str]:
    if not _conversion_format(query):
        return {}
    overrides: dict[str, str] = {}
    for pattern in (_QUOTED_DESTINATION_PAIR_RE, _PLAIN_DESTINATION_PAIR_RE):
        for match in pattern.finditer(query or ""):
            source = _clean_destination_path(match.group("src"))
            destination = _clean_destination_path(match.group("dest"))
            if not source or not destination:
                continue
            if _path_extension(source) not in _CONVERSION_SOURCE_EXTENSIONS:
                continue
            overrides[map_host_path_to_container(source)] = map_host_path_to_container(destination)
    return overrides


def _clean_destination_path(path: str) -> str:
    cleaned = (path or "").strip().strip("'\"`")
    return cleaned.rstrip(".,;:)]}")
