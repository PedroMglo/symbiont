"""Read-only archive recovery inspection for workspace-bound tasks."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import stat
import tarfile
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

ARCHIVE_SUFFIXES = (
    ".tar.gz.part",
    ".tgz.part",
    ".tar.bz2.part",
    ".tbz2.part",
    ".tar.xz.part",
    ".txz.part",
    ".zip.part",
    ".tar.part",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".zip",
    ".tar",
)

_HASH_RE = re.compile(r"^(?P<sha>[A-Fa-f0-9]{64})\s+(?P<path>\*?.+?)\s*$")


@dataclass(frozen=True)
class RecoveryWorkspace:
    path: Path
    mapped_from: str | None = None


def resolve_recovery_workspace(path: str | None, *, host_home_prefix: str | None = None) -> RecoveryWorkspace | None:
    """Resolve a client workspace path for read-only archive inspection."""

    raw = (path or "").strip()
    if not raw or "\x00" in raw:
        return None

    candidates: list[tuple[Path, str | None]] = [(Path(raw), None)]
    host_home = (host_home_prefix or "").strip().rstrip("/")
    if host_home and raw == host_home:
        candidates.append((Path("/host_home"), raw))
    elif host_home and raw.startswith(f"{host_home}/"):
        candidates.append((Path("/host_home") / raw[len(host_home) + 1 :], raw))

    for candidate, mapped_from in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir():
            return RecoveryWorkspace(path=resolved, mapped_from=mapped_from)
    return None


def build_archive_recovery_report(workspace: Path, *, max_member_bytes: int = 64 * 1024 * 1024) -> dict[str, Any]:
    """Inspect archives and manifests without extracting to final paths."""

    root = workspace.resolve()
    scan_root = root / "incoming" if (root / "incoming").is_dir() else root
    manifest_root = root / "manifests" if (root / "manifests").is_dir() else root

    archive_paths: list[Path] = []
    non_archives: list[dict[str, Any]] = []
    for item in sorted(scan_root.iterdir(), key=lambda p: p.name) if scan_root.is_dir() else []:
        if not item.is_file() or item.name.startswith("."):
            continue
        if _archive_suffix(item):
            archive_paths.append(item)
        else:
            non_archives.append({
                "path": _rel(root, item),
                "size": _safe_size(item),
                "reason": "not_a_supported_archive_extension",
            })

    archives: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    unsafe_entries: list[dict[str, Any]] = []
    corrupt_archives: list[dict[str, Any]] = []

    for archive_path in archive_paths:
        suffix = _archive_suffix(archive_path)
        if suffix == ".zip" or suffix == ".zip.part":
            archive = _inspect_zip(root, archive_path, max_member_bytes=max_member_bytes)
        else:
            archive = _inspect_tar(root, archive_path, max_member_bytes=max_member_bytes)
        archives.append(archive["archive"])
        recovered.extend(archive["recovered"])
        unsafe_entries.extend(archive["unsafe_entries"])
        if archive["archive"]["status"] == "corrupt":
            corrupt_archives.append(archive["archive"])

    conflicts = _detect_conflicts(recovered)
    manifests = _validate_manifests(root, manifest_root, recovered, archives)
    report = {
        "workspace": str(root),
        "scan_root": str(scan_root),
        "policy": {
            "mode": "read_only_inspection",
            "final_write_authority": "storage_guardian",
            "direct_workspace_writes": False,
            "extraction_root": "extracted-safe/",
            "rules": [
                "inspect archive members before extraction",
                "reject absolute member paths",
                "reject normalized member paths containing '..'",
                "reject symlinks and hardlinks that escape extraction root",
                "require an explicit symlink policy before materializing symlinks",
                "preserve conflicts by versioning instead of overwriting",
                "use archive APIs rather than line-oriented parsing",
            ],
        },
        "archives": archives,
        "non_archives": non_archives,
        "unsafe_entries": unsafe_entries,
        "corrupt_archives": corrupt_archives,
        "recovered_files": recovered,
        "conflicts": conflicts,
        "manifest_validation": manifests,
        "summary": {
            "archives_total": len(archives),
            "archives_valid": sum(1 for item in archives if item["status"] == "valid"),
            "archives_unsafe": sum(1 for item in archives if item["status"] == "unsafe"),
            "archives_corrupt": sum(1 for item in archives if item["status"] == "corrupt"),
            "non_archives": len(non_archives),
            "unsafe_entries": len(unsafe_entries),
            "recovered_files": len(recovered),
            "path_conflicts": len(conflicts["path_conflicts"]),
            "case_conflicts": len(conflicts["case_insensitive_conflicts"]),
            "checksum_mismatches": (
                len(manifests["archive_member_mismatches"]) + len(manifests["file_checksum_mismatches"])
            ),
            "missing_manifest_items": (
                len(manifests["missing_archive_members"]) + len(manifests["missing_checksum_files"])
            ),
        },
    }
    return report


def format_archive_recovery_report(report: dict[str, Any], *, published_uri: str | None = None) -> str:
    """Render an archive recovery report as concise Markdown."""

    summary = report.get("summary", {})
    lines = [
        "# Archive recovery report",
        "",
        "## Storage policy",
        "- Performed read-only archive inspection only.",
        "- Final persistent report publication is delegated to storage_guardian.",
        "- No direct workspace writes to report/ or extracted-safe/ were performed by agents/features.",
    ]
    if published_uri:
        lines.append(f"- storage_guardian object: `{published_uri}`")
    lines.extend([
        "",
        "## Inventory",
        (
            f"- archives: {summary.get('archives_total', 0)} total, "
            f"{summary.get('archives_valid', 0)} valid, "
            f"{summary.get('archives_unsafe', 0)} unsafe, "
            f"{summary.get('archives_corrupt', 0)} corrupt"
        ),
        f"- non-archive files: {summary.get('non_archives', 0)}",
        f"- recovered file candidates: {summary.get('recovered_files', 0)}",
        f"- unsafe entries: {summary.get('unsafe_entries', 0)}",
        f"- checksum mismatches: {summary.get('checksum_mismatches', 0)}",
        "",
        "## Archive status",
    ])
    for item in report.get("archives", []):
        detail = item.get("error") or item.get("unsafe_reason") or f"{item.get('members', 0)} members"
        lines.append(f"- `{item.get('path')}`: {item.get('status')} ({detail})")

    if report.get("non_archives"):
        lines.extend(["", "## Non-archive distractors"])
        for item in report["non_archives"]:
            lines.append(f"- `{item.get('path')}`: {item.get('reason')}")

    if report.get("unsafe_entries"):
        lines.extend(["", "## Unsafe entries"])
        for item in report["unsafe_entries"][:50]:
            reason = item.get("reason")
            link = f" -> {_format_member_path(item.get('link_target'), item.get('link_target_render'))}" if item.get("link_target") else ""
            lines.append(f"- `{item.get('archive')}`: {_format_member_path(item.get('path'), item.get('path_render'))}{link}: {reason}")

    conflicts = report.get("conflicts", {})
    if conflicts.get("path_conflicts"):
        lines.extend(["", "## Path conflicts"])
        for item in conflicts["path_conflicts"][:50]:
            locations = ", ".join(f"{entry['archive']}#{entry['index']}" for entry in item.get("entries", []))
            lines.append(f"- {_format_member_path(item.get('path'), item.get('path_render'))} has different content hashes across {locations}")
    if conflicts.get("case_insensitive_conflicts"):
        lines.extend(["", "## Case-insensitive conflicts"])
        for item in conflicts["case_insensitive_conflicts"][:50]:
            paths = ", ".join(_format_member_path(path, _path_render(path)) for path in item.get("paths", []))
            lines.append(f"- {paths}")

    manifests = report.get("manifest_validation", {})
    if manifests.get("archive_member_mismatches") or manifests.get("missing_archive_members"):
        lines.extend(["", "## Manifest member validation"])
        for item in manifests.get("archive_member_mismatches", [])[:50]:
            lines.append(
                f"- mismatch `{item.get('archive')}`:{_format_member_path(item.get('path'), item.get('path_render'))} "
                f"expected sha={item.get('expected_sha256')} size={item.get('expected_size')} "
                f"actual candidates={item.get('actual')}"
            )
        for item in manifests.get("missing_archive_members", [])[:50]:
            lines.append(f"- missing member `{item.get('archive')}`:{_format_member_path(item.get('path'), item.get('path_render'))}")

    if manifests.get("file_checksum_mismatches") or manifests.get("missing_checksum_files"):
        lines.extend(["", "## File checksum validation"])
        for item in manifests.get("file_checksum_mismatches", [])[:50]:
            lines.append(
                f"- mismatch `{item.get('path')}` expected {item.get('expected_sha256')} "
                f"actual {item.get('actual_sha256')}"
            )
        for item in manifests.get("missing_checksum_files", [])[:50]:
            lines.append(f"- missing checksum target `{item.get('path')}`")

    if report.get("recovered_files"):
        lines.extend(["", "## Recovered file candidates"])
        for item in report["recovered_files"][:80]:
            lines.append(
                f"- {_format_member_path(item.get('path'), item.get('path_render'))} from `{item.get('archive')}` "
                f"size={item.get('size')} sha256={item.get('sha256')}"
            )

    lines.extend([
        "",
        "## Commands or APIs used",
        "- Python archive APIs: zipfile and tarfile.",
        "- Hashing used streaming SHA-256 over regular file members and manifest targets.",
        "- No shell extraction commands, no overwrite-prone extraction, no symlink following.",
    ])
    return "\n".join(lines).strip() + "\n"


def _inspect_zip(root: Path, archive_path: Path, *, max_member_bytes: int) -> dict[str, Any]:
    archive_rel = _rel(root, archive_path)
    archive_info = _base_archive_info(root, archive_path)
    recovered: list[dict[str, Any]] = []
    unsafe: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(archive_path) as zf:
            bad_member = zf.testzip()
            infos = zf.infolist()
            archive_info["members"] = len(infos)
            if bad_member:
                archive_info["status"] = "corrupt"
                archive_info["error"] = f"zip_crc_failed:{bad_member}"
                return {"archive": archive_info, "recovered": recovered, "unsafe_entries": unsafe}
            for index, info in enumerate(infos):
                path = info.filename
                path_issue = _member_path_issue(path)
                mode = (info.external_attr >> 16) & 0o170000
                is_symlink = stat.S_ISLNK(mode)
                if path_issue:
                    unsafe.append(_unsafe(archive_rel, path, path_issue))
                    continue
                if is_symlink:
                    unsafe.append(_unsafe(archive_rel, path, "symlink_requires_policy"))
                    continue
                if info.is_dir():
                    continue
                if info.file_size > max_member_bytes:
                    recovered.append(_recovered_stub(archive_rel, path, index, info.file_size, "member_too_large_to_hash"))
                    continue
                try:
                    with zf.open(info) as handle:
                        digest, size = _hash_stream(handle)
                except Exception as exc:
                    recovered.append(_recovered_stub(archive_rel, path, index, info.file_size, f"read_error:{exc}"))
                    continue
                recovered.append(_recovered(archive_rel, path, index, size, digest))
        archive_info["status"] = "unsafe" if unsafe else "valid"
        if unsafe:
            archive_info["unsafe_reason"] = "unsafe_members_present"
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        archive_info["status"] = "corrupt"
        archive_info["error"] = str(exc)[:300]
    return {"archive": archive_info, "recovered": recovered, "unsafe_entries": unsafe}


def _inspect_tar(root: Path, archive_path: Path, *, max_member_bytes: int) -> dict[str, Any]:
    archive_rel = _rel(root, archive_path)
    archive_info = _base_archive_info(root, archive_path)
    recovered: list[dict[str, Any]] = []
    unsafe: list[dict[str, Any]] = []
    try:
        with tarfile.open(archive_path, mode="r:*") as tf:
            members = tf.getmembers()
            archive_info["members"] = len(members)
            for index, member in enumerate(members):
                path = member.name
                path_issue = _member_path_issue(path)
                if path_issue:
                    unsafe.append(_unsafe(archive_rel, path, path_issue))
                    continue
                if member.issym() or member.islnk():
                    link_issue = _link_issue(path, member.linkname)
                    unsafe.append(_unsafe(archive_rel, path, link_issue, link_target=member.linkname))
                    continue
                if not member.isfile():
                    continue
                if member.size > max_member_bytes:
                    recovered.append(_recovered_stub(archive_rel, path, index, member.size, "member_too_large_to_hash"))
                    continue
                try:
                    handle = tf.extractfile(member)
                    if handle is None:
                        recovered.append(_recovered_stub(archive_rel, path, index, member.size, "missing_file_handle"))
                        continue
                    with handle:
                        digest, size = _hash_stream(handle)
                except Exception as exc:
                    recovered.append(_recovered_stub(archive_rel, path, index, member.size, f"read_error:{exc}"))
                    continue
                recovered.append(_recovered(archive_rel, path, index, size, digest))
        archive_info["status"] = "unsafe" if unsafe else "valid"
        if unsafe:
            archive_info["unsafe_reason"] = "unsafe_members_present"
    except (tarfile.TarError, EOFError, OSError, RuntimeError) as exc:
        archive_info["status"] = "corrupt"
        archive_info["error"] = str(exc)[:300]
    return {"archive": archive_info, "recovered": recovered, "unsafe_entries": unsafe}


def _validate_manifests(root: Path, manifest_root: Path, recovered: list[dict[str, Any]], archives: list[dict[str, Any]]) -> dict[str, Any]:
    by_member: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in recovered:
        by_member[(Path(str(item["archive"])).name, str(item["path"]))].append(item)
    archive_names = {Path(str(item["path"])).name for item in archives}

    result: dict[str, Any] = {
        "jsonl_files": [],
        "sha256_files": [],
        "invalid_jsonl_rows": [],
        "duplicate_manifest_rows": [],
        "missing_archive_members": [],
        "archive_member_mismatches": [],
        "missing_checksum_files": [],
        "file_checksum_mismatches": [],
        "invalid_checksum_rows": [],
        "stale_archive_references": [],
    }

    jsonl_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for manifest_path in sorted(manifest_root.glob("*.jsonl")):
        result["jsonl_files"].append(_rel(root, manifest_path))
        try:
            lines = manifest_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            result["invalid_jsonl_rows"].append({"file": _rel(root, manifest_path), "line": 0, "error": str(exc)})
            continue
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                result["invalid_jsonl_rows"].append({"file": _rel(root, manifest_path), "line": line_no, "error": str(exc)})
                continue
            if not isinstance(row, dict):
                continue
            archive = str(row.get("archive") or "")
            path = str(row.get("path") or "")
            if not archive or not path:
                continue
            row["_file"] = _rel(root, manifest_path)
            row["_line"] = line_no
            jsonl_rows[(archive, path)].append(row)

    for (archive, path), rows in sorted(jsonl_rows.items()):
        expected_variants = {(str(row.get("sha256")), str(row.get("size"))) for row in rows}
        if len(rows) > 1 and len(expected_variants) > 1:
            result["duplicate_manifest_rows"].append({"archive": archive, "path": path, "rows": rows})
        actuals = by_member.get((archive, path), [])
        if not actuals:
            result["missing_archive_members"].append({"archive": archive, "path": path})
            if archive not in archive_names:
                result["stale_archive_references"].append({"archive": archive, "path": path})
            continue
        for row in rows:
            expected_sha = str(row.get("sha256") or "")
            expected_size = row.get("size")
            if any(
                item.get("sha256") == expected_sha
                and (expected_size is None or int(item.get("size", -1)) == int(expected_size))
                for item in actuals
            ):
                continue
            result["archive_member_mismatches"].append({
                "archive": archive,
                "path": path,
                "expected_sha256": expected_sha,
                "expected_size": expected_size,
                "actual": [
                    {"sha256": item.get("sha256"), "size": item.get("size"), "index": item.get("index")}
                    for item in actuals
                ],
            })

    for checksum_path in sorted(manifest_root.glob("*.sha256")):
        result["sha256_files"].append(_rel(root, checksum_path))
        try:
            lines = checksum_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            result["invalid_checksum_rows"].append({"file": _rel(root, checksum_path), "line": 0, "error": str(exc)})
            continue
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            match = _HASH_RE.match(line)
            if not match:
                result["invalid_checksum_rows"].append({"file": _rel(root, checksum_path), "line": line_no, "raw": line})
                continue
            expected = match.group("sha").lower()
            rel_name = match.group("path").lstrip("*").strip()
            target = _checksum_target(root, rel_name)
            if target is None:
                result["missing_checksum_files"].append({"path": rel_name, "expected_sha256": expected})
                continue
            actual = _hash_file(target)
            if actual != expected:
                result["file_checksum_mismatches"].append({
                    "path": _rel(root, target),
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                })
    return result


def _detect_conflicts(recovered: list[dict[str, Any]]) -> dict[str, Any]:
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_lower: dict[str, set[str]] = defaultdict(set)
    for item in recovered:
        path = str(item["path"])
        by_path[path].append(item)
        by_lower[path.lower()].add(path)

    path_conflicts = []
    duplicate_same_hash = []
    for path, entries in sorted(by_path.items()):
        if len(entries) <= 1:
            continue
        hashes = {str(item.get("sha256")) for item in entries if item.get("sha256")}
        item = {"path": path, "path_render": _path_render(path), "entries": entries}
        if len(hashes) > 1:
            path_conflicts.append(item)
        else:
            duplicate_same_hash.append(item)

    case_conflicts = [
        {"folded_path": folded, "paths": sorted(paths)}
        for folded, paths in sorted(by_lower.items())
        if len(paths) > 1
    ]
    return {
        "path_conflicts": path_conflicts,
        "duplicate_same_hash": duplicate_same_hash,
        "case_insensitive_conflicts": case_conflicts,
    }


def _base_archive_info(root: Path, archive_path: Path) -> dict[str, Any]:
    return {
        "path": _rel(root, archive_path),
        "status": "unknown",
        "size": _safe_size(archive_path),
        "sha256": _hash_file(archive_path),
        "members": 0,
    }


def _member_path_issue(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute():
        return "absolute_path"
    cleaned = posixpath.normpath(normalized)
    if cleaned == "." or cleaned == ".." or cleaned.startswith("../") or "/../" in f"/{cleaned}/":
        return "path_traversal"
    if any(part in {"", "."} for part in PurePosixPath(cleaned).parts):
        return "ambiguous_member_path"
    return None


def _link_issue(path: str, link_target: str) -> str:
    if _member_path_issue(link_target) == "absolute_path":
        return "link_absolute_target"
    base = PurePosixPath(path.replace("\\", "/")).parent
    target = PurePosixPath(link_target.replace("\\", "/"))
    combined = posixpath.normpath(str(target if target.is_absolute() else base / target))
    if combined == ".." or combined.startswith("../") or "/../" in f"/{combined}/":
        return "link_escapes_extraction_root"
    return "symlink_requires_policy"


def _archive_suffix(path: Path) -> str:
    lower = path.name.lower()
    return next((suffix for suffix in ARCHIVE_SUFFIXES if lower.endswith(suffix)), "")


def _checksum_target(root: Path, rel_name: str) -> Path | None:
    candidates = [root / rel_name, root / "incoming" / rel_name]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if root in resolved.parents or resolved == root:
            if resolved.is_file():
                return resolved
    return None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_stream(handle: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


def _safe_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _unsafe(archive: str, path: str, reason: str, *, link_target: str | None = None) -> dict[str, Any]:
    item = {"archive": archive, "path": path, "path_render": _path_render(path), "reason": reason}
    if link_target is not None:
        item["link_target"] = link_target
        item["link_target_render"] = _path_render(link_target)
    return item


def _recovered(archive: str, path: str, index: int, size: int, digest: str) -> dict[str, Any]:
    return {"archive": archive, "path": path, "path_render": _path_render(path), "index": index, "size": size, "sha256": digest}


def _recovered_stub(archive: str, path: str, index: int, size: int, reason: str) -> dict[str, Any]:
    return {
        "archive": archive,
        "path": path,
        "path_render": _path_render(path),
        "index": index,
        "size": size,
        "sha256": None,
        "note": reason,
    }


def _path_render(path: str | None) -> dict[str, Any]:
    raw = str(path or "")
    normalized = unicodedata.normalize("NFC", raw)
    display = (
        normalized
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    escaped = raw.encode("unicode_escape", errors="backslashreplace").decode("ascii")
    notes = []
    if normalized != raw:
        notes.append("unicode_normalized_nfc")
    if display != normalized:
        notes.append("control_or_separator_escaped")
    if escaped != raw:
        notes.append("escaped_representation_available")
    return {
        "display": display,
        "escaped": escaped,
        "normalized": normalized,
        "notes": notes,
    }


def _format_member_path(path: Any, render: dict[str, Any] | None = None) -> str:
    rendered = render or _path_render(str(path or ""))
    display = rendered.get("display", str(path or ""))
    escaped = rendered.get("escaped", display)
    if escaped != str(path or "") or rendered.get("notes"):
        return f"`{display}` (escaped=`{escaped}`)"
    return f"`{display}`"
