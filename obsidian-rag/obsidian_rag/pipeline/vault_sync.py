"""Cross-platform vault sync — direct backend only.

Reads the Obsidian vault in-place (no copy). This is the simplest and most
efficient approach: zero disk I/O overhead, cross-platform, instant.

Usage:
    effective_dir = sync_vault(vault_dir, cfg)
    # Pass effective_dir to IngestPipeline via IngestSource(path=effective_dir)
"""

from __future__ import annotations

import logging
from fnmatch import fnmatch
from pathlib import Path

from obsidian_rag.config import _DEFAULT_EXCLUDE_PATTERNS, SyncConfig

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_vault(
    vault_dir: Path,
    cfg: SyncConfig | None = None,
) -> Path:
    """Validate vault and return it for direct reading.

    Direct mode always reads from *vault_dir*.
    """
    _validate_vault(vault_dir)
    log.info("Sync backend: direct")
    print(f"==> [Sync] Modo directo — a ler de {vault_dir}")
    return vault_dir


def resolve_effective_backend(backend: str) -> str:
    """Return the backend that would actually be used.

    Always returns ``"direct"`` — other backends have been removed.
    """
    return "direct"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_vault(vault_dir: Path) -> None:
    if not vault_dir.exists():
        raise SystemExit(
            f"Vault não encontrado: {vault_dir}\n"
            "Verifica [paths] vault_dir em config/rag/user.toml"
        )
    if not vault_dir.is_dir():
        raise SystemExit(f"vault_dir não é um directório: {vault_dir}")


# ---------------------------------------------------------------------------
# Exclusion logic (used by direct scanning in iter_note_files)
# ---------------------------------------------------------------------------


def _should_exclude(rel_path: Path, patterns: tuple[str, ...]) -> bool:
    """Return True if *rel_path* matches any exclusion pattern."""
    parts = rel_path.parts
    for pattern in patterns:
        # Match against any path component (directory names)
        for part in parts:
            if fnmatch(part, pattern):
                return True
        # Match against the full relative path (for file patterns like *.pdf)
        if fnmatch(rel_path.name, pattern):
            return True
    return False


def should_exclude(rel_path: Path, patterns: tuple[str, ...] | None = None) -> bool:
    """Public wrapper — uses default patterns when none provided."""
    if patterns is None:
        patterns = _DEFAULT_EXCLUDE_PATTERNS
    return _should_exclude(rel_path, patterns)
