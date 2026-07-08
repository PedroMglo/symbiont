"""Natural-language storage query intent parsing.

The orchestrator may route a prompt to the storage source, but storage_guardian
owns how that prompt becomes a storage operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ABS_PATH_RE = re.compile(r"(?<![\w.-])(?P<path>/(?:[^\s'\"`<>])+)")
_QUOTED_PATH_RE = re.compile(r"['\"](?P<path>/(?:[^'\"])+)['\"]")

_STORAGE_ARCHIVE_TERMS = frozenset(
    {
        "archive",
        "arquiva",
        "arquivar",
        "comprime",
        "comprimir",
        "compacta",
        "compactar",
        "compress",
        "compression",
    }
)
_STORAGE_RESTORE_TERMS = frozenset(
    {
        "restore",
        "restaura",
        "restaurar",
        "descomprime",
        "descomprimir",
        "descompacta",
        "descompactar",
        "decompress",
        "extract",
    }
)
_STORAGE_READ_TERMS = frozenset({"consulta", "consultar", "ler", "read", "abre", "abrir"})
_STORAGE_SEARCH_TERMS = frozenset(
    {
        "arquivo",
        "arquivos",
        "archive",
        "archives",
        "manifesto",
        "manifest",
        "manifestos",
        "summary",
        "sumário",
        "sumario",
        "descrição",
        "descricao",
        "descrições",
        "descricoes",
    }
)

_ARCHIVE_RECOVERY_TERMS = frozenset(
    {
        "archive",
        "archives",
        "arquivo",
        "arquivos",
        "backup",
        "backups",
        "checksum",
        "checksums",
        "extract",
        "extraction",
        "extrai",
        "extrair",
        "manifest",
        "manifesto",
        "recover",
        "recovered",
        "recovery",
        "recupera",
        "recuperar",
        "restore",
        "restaura",
        "restaurar",
        "safe",
        "seguro",
        "symlink",
        "symlinks",
        "tar",
        "traversal",
        "unsafe",
        "zip",
    }
)

_ARCHIVE_RECOVERY_PHRASES = (
    "archive inventory",
    "archive recovery",
    "backup recovery",
    "corrupt archive",
    "corrupt archives",
    "lista de ficheiros recuperados",
    "path traversal",
    "recovered file list",
    "safe extraction",
    "unsafe entries",
)


@dataclass(frozen=True)
class StorageRequest:
    """Parsed storage-control request from a user query."""

    operation: str
    paths: tuple[str, ...] = ()
    query: str = ""
    tier: str = "cold"
    archive_id: str | None = None
    manifest_path: str | None = None
    relative_path: str | None = None
    placement_mode: str = "configured"
    replace_sources: bool = False


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


def parse_storage_request(query: str) -> StorageRequest | None:
    """Parse archive/restore/read/search storage requests."""

    lower = (query or "").lower()
    words = {w.strip(".,!?:;\"'()[]{}") for w in lower.split()}
    paths = extract_absolute_paths(query)
    manifest_path = next((path for path in paths if path.endswith(".manifest.json")), None)

    if words & _STORAGE_ARCHIVE_TERMS and paths:
        return StorageRequest(
            operation="archive",
            paths=paths,
            query=query,
            tier=_storage_tier(lower),
            placement_mode=_storage_placement_mode(lower),
            replace_sources=_storage_replace_sources(lower),
        )

    if words & _STORAGE_ARCHIVE_TERMS and any(term in lower for term in ("antigos", "antigo", "old", "lifecycle", "ciclo", "regras")):
        return StorageRequest(operation="cycle", query=query)

    if words & _STORAGE_RESTORE_TERMS and (paths or _looks_like_archive_reference(lower)):
        return StorageRequest(
            operation="restore",
            paths=paths,
            query=query,
            archive_id=_archive_reference(lower),
            manifest_path=manifest_path,
        )

    if words & _STORAGE_READ_TERMS and (manifest_path or _looks_like_archive_reference(lower)):
        return StorageRequest(
            operation="read",
            paths=paths,
            query=query,
            archive_id=_archive_reference(lower),
            manifest_path=manifest_path,
            relative_path=_relative_archive_member(query, paths),
        )

    if ("sem descomprimir" in lower or "without decompress" in lower or "correspond" in lower or "correspondem" in lower) and (
        words & _STORAGE_SEARCH_TERMS
    ):
        return StorageRequest(operation="search", paths=paths, query=query)

    if (words & _STORAGE_SEARCH_TERMS) and any(term in lower for term in ("lista", "listar", "mostra", "procura", "search", "find")):
        return StorageRequest(operation="search", paths=paths, query=query)

    return None


def needs_storage_context(query: str) -> bool:
    """Return True when storage_guardian should provide read-only context."""

    lower = (query or "").lower()
    words = {w.strip(".,!?:;\"'()[]{}") for w in lower.split()}
    if parse_storage_request(query) is not None:
        return True
    if is_archive_recovery_request(query):
        return True
    if any(term in lower for term in ("storage_guardian", "storage guardian", "storage.status")):
        return True
    if any(term in lower for term in ("prewarning", "prewarnings", "pre-aviso", "pre-avisos")):
        return True
    if any(term in lower for term in ("archive/restore", "archive restore", "restore/archive")):
        return True
    if "storage" in words and any(
        term in lower
        for term in (
            "mount",
            "mounts",
            "ssd",
            "external",
            "externo",
            "policy",
            "política",
            "politica",
            "archive",
            "restore",
            "approval",
            "aprovação",
            "aprovacao",
        )
    ):
        return True
    return False


def is_archive_recovery_request(query: str) -> bool:
    """Return True for safe archive/backup recovery investigations."""

    lower = " ".join((query or "").lower().split())
    if not lower:
        return False
    if any(phrase in lower for phrase in _ARCHIVE_RECOVERY_PHRASES):
        return True
    words = {w.strip(".,!?:;\"'()[]{}") for w in lower.split()}
    hits = words & _ARCHIVE_RECOVERY_TERMS
    action_hits = hits & {
        "extract",
        "extraction",
        "extrai",
        "extrair",
        "recover",
        "recovered",
        "recovery",
        "recupera",
        "recuperar",
        "restore",
        "restaura",
        "restaurar",
    }
    artifact_hits = hits & {
        "archive",
        "archives",
        "arquivo",
        "arquivos",
        "backup",
        "backups",
        "checksum",
        "checksums",
        "manifest",
        "manifesto",
        "symlink",
        "symlinks",
        "tar",
        "traversal",
        "unsafe",
        "zip",
    }
    return bool(action_hits and artifact_hits)


def _storage_tier(lower: str) -> str:
    return "warm" if any(term in lower for term in ("warm", "morno", "tempor", "temporário", "temporario")) else "cold"


def _storage_placement_mode(lower: str) -> str:
    configured_terms = (
        "ssd",
        "externo",
        "external",
        "archive_target",
        "storage target",
        "destino",
        "destination",
        "para /",
        "em /",
    )
    return "configured" if any(term in lower for term in configured_terms) else "source_directory"


def _storage_replace_sources(lower: str) -> bool:
    return any(
        term in lower
        for term in (
            "remove original",
            "remove os originais",
            "remove o original",
            "apaga original",
            "apaga os originais",
            "substitui",
            "substituir",
            "replace source",
            "liberta espaço",
            "liberta espaco",
        )
    )


def _looks_like_archive_reference(lower: str) -> bool:
    return ".manifest.json" in lower or "archive_id" in lower or "manual_" in lower or "cycle_" in lower


def _archive_reference(lower: str) -> str | None:
    match = re.search(r"\b(?:manual|cycle|[a-z0-9_-]+)_[a-z0-9_.-]{8,}\b", lower)
    return match.group(0) if match else None


def _relative_archive_member(query: str, paths: tuple[str, ...]) -> str | None:
    path_set = set(paths)
    for token in re.findall(r"[\w./-]+\.[A-Za-z0-9]{1,10}", query or ""):
        if token in path_set or token.endswith(".manifest.json"):
            continue
        if token.startswith("/"):
            continue
        return token.strip(".,;:)])}")
    return None
