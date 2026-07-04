"""Archive verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from storage_guardian.compressors import get_compressor
from storage_guardian.hashing import hash_file
from storage_guardian.types import ArchivePlan


def verify_archive(plan: ArchivePlan, archive_path: Path) -> dict[str, Any]:
    compressor = get_compressor(plan.backend)
    members = compressor.list_members(archive_path)
    expected = {record.relative_path for record in plan.files}
    found = set(members)
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    archive_hash = hash_file(archive_path)
    verified = not missing and not extra and archive_path.stat().st_size > 0
    return {
        "archive_id": plan.archive_id,
        "verified": verified,
        "archive_path": str(archive_path),
        "archive_hash": archive_hash,
        "expected_members": len(expected),
        "found_members": len(found),
        "missing_members": missing,
        "extra_members": extra,
    }

