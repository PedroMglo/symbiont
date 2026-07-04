"""SQLite manifest for incremental ingest — tracks files, chunks, and runs.

Enables crash recovery: if sync is interrupted, the next run resumes
from the last checkpoint instead of reprocessing everything.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE: dict[str, str] = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


@dataclass(frozen=True)
class FileRecord:
    path: str
    repo: str
    mtime: float
    size: int
    sha256: str
    status: str
    chunk_count: int
    last_indexed_at: str


_SCHEMA = _sql("schema.sql")


class IngestManifest:
    """SQLite-backed manifest for tracking ingest state.

    Uses WAL mode for concurrent read safety and transactional writes.
    """

    def __init__(self, db_path: str | Path, *, config_version: str = "") -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._config_version = config_version

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self._db_path,
                timeout=10,
                check_same_thread=False,
            )
            self._conn.execute(_sql("execute_96.sql"))
            self._conn.execute(_sql("execute_97.sql"))
            self._conn.executescript(_SCHEMA)
            self._migrate_schema()
        return self._conn

    def _migrate_schema(self) -> None:
        """Add columns introduced after initial schema creation."""
        assert self._conn is not None
        file_info = self._conn.execute(_sql("execute_105.sql")).fetchall()
        file_cols = {r[1] for r in file_info}
        file_pk = [r[1] for r in file_info if r[5]]

        if "config_version" not in file_cols:
            self._conn.execute(_sql("execute_110.sql"))
            self._conn.commit()
            file_cols.add("config_version")

        # v1 used path as the only primary key, which caused collisions between
        # repos/vaults sharing the same relative path. Rebuild both tables with
        # a source-scoped key while preserving existing records.
        if "source_id" not in file_cols or file_pk == ["path"]:
            self._conn.executescript(
                _sql("executescript_119.sql")
            )
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            return


    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        """Transaction context manager — auto-commit on success, rollback on error."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- Runs --

    def start_run(self) -> str:
        """Create a new ingest run and return its ID."""
        run_id = uuid.uuid4().hex[:12]
        with self._tx() as cur:
            cur.execute(
                _sql("execute_213.sql"),
                (run_id,),
            )
        return run_id

    def finish_run(self, run_id: str, status: str = "completed", error: str | None = None) -> None:
        with self._tx() as cur:
            cur.execute(
                _sql("execute_221.sql"),
                (status, error, run_id),
            )

    def get_last_incomplete_run(self) -> str | None:
        """Return the run_id of the last incomplete run, if any."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                _sql("execute_230.sql")
            ).fetchone()
        return row[0] if row else None

    # -- Files --

    @staticmethod
    def _source_id(repo: str | None = None, source_id: str | None = None) -> str:
        return source_id or repo or "default"

    def needs_reindex(
        self,
        path: str,
        mtime: float,
        size: int,
        sha256: str,
        *,
        repo: str | None = None,
        source_id: str | None = None,
        mtime_shortcircuit: bool = False,
    ) -> bool:
        """Return True if file is new, has changed, or config version differs.

        When *mtime_shortcircuit* is True and mtime+size match the stored values,
        skip the SHA256 comparison entirely — avoids reading file content for
        unchanged files (critical at 300GB+ scale).
        """
        source_key = self._source_id(repo, source_id)
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                _sql("execute_261.sql"),
                (source_key, path),
            ).fetchone()
        if row is None:
            return True
        if str(row[4] or "").startswith("pending_"):
            return True
        if self._config_version and row[3] != self._config_version:
            return True
        # mtime short-circuit: if mtime AND size are identical, assume no change
        if mtime_shortcircuit and row[0] == mtime and row[1] == size:
            return False
        return bool(row[0] != mtime or row[1] != size or row[2] != sha256)

    def record_file(
        self,
        path: str,
        repo: str,
        mtime: float,
        size: int,
        sha256: str,
        chunk_count: int,
        source_id: str | None = None,
        source_type: str = "",
        status: str = "indexed",
    ) -> None:
        """Upsert a file record after successful indexing."""
        source_key = self._source_id(repo, source_id)
        with self._tx() as cur:
            cur.execute(
                _sql("execute_291.sql"),
                (source_key, source_type, path, repo, mtime, size, sha256, status, chunk_count, self._config_version),
            )

    def record_files_batch(self, records: list[FileRecord]) -> None:
        """Batch upsert file records using executemany for better throughput."""
        if not records:
            return
        with self._tx() as cur:
            cur.executemany(
                _sql("executemany_315.sql"),
                [
                    (r.repo, "", r.path, r.repo, r.mtime, r.size, r.sha256, r.chunk_count, self._config_version)
                    for r in records
                ],
            )

    def get_file_state(
        self,
        path: str,
        *,
        repo: str | None = None,
        source_id: str | None = None,
    ) -> tuple[float, int, str, int] | None:
        """Return (mtime, size, status, chunk_count) for a manifest file."""
        source_key = self._source_id(repo, source_id)
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                _sql("execute_348.sql"),
                (source_key, path),
            ).fetchone()
        if row is None:
            return None
        return (float(row[0]), int(row[1]), str(row[2]), int(row[3]))

    def get_indexed_files(self, repo: str, source_id: str | None = None) -> set[str]:
        """Return set of file paths currently indexed for a repo."""
        source_key = self._source_id(repo, source_id)
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(_sql("execute_360.sql"), (source_key,)).fetchall()
        return {r[0] for r in rows}

    # -- Chunks --

    def record_chunks(
        self,
        chunk_ids: list[str],
        file_path: str,
        repo: str,
        chunk_hashes: list[str],
        source_id: str | None = None,
        source_type: str = "",
    ) -> None:
        """Batch insert chunk records for a file, replacing any previous chunks for that file."""
        source_key = self._source_id(repo, source_id)
        with self._tx() as cur:
            # Remove old chunks for this file
            cur.execute(_sql("execute_378.sql"), (source_key, file_path))
            cur.executemany(
                _sql("executemany_380.sql"),
                [(cid, source_key, source_type, file_path, repo, ch) for cid, ch in zip(chunk_ids, chunk_hashes)],
            )

    def mark_chunks_embedded(self, chunk_ids: list[str]) -> None:
        """Mark chunks as successfully embedded in the vector store."""
        if not chunk_ids:
            return
        with self._tx() as cur:
            placeholders = ",".join("?" for _ in chunk_ids)
            cur.execute(
                _sql("fstring_271.sql").format(placeholders),  # noqa: S608  # nosec B608
                chunk_ids,
            )

    def get_chunk_ids_for_repo(self, repo: str, source_id: str | None = None) -> set[str]:
        """Return all chunk IDs currently tracked for a repo."""
        source_key = self._source_id(repo, source_id)
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(_sql("execute_401.sql"), (source_key,)).fetchall()
        return {r[0] for r in rows}

    def get_stale_chunks(self, repo: str, valid_ids: set[str]) -> set[str]:
        """Return chunk IDs in the manifest that are NOT in valid_ids."""
        current = self.get_chunk_ids_for_repo(repo)
        return current - valid_ids

    def delete_stale_files(self, repo: str, valid_paths: set[str], source_id: str | None = None) -> list[str]:
        """Remove files no longer present in repo. Returns deleted chunk IDs."""
        source_key = self._source_id(repo, source_id)
        indexed = self.get_indexed_files(repo, source_id=source_key)
        stale = indexed - valid_paths
        if not stale:
            return []
        deleted_chunk_ids: list[str] = []
        with self._tx() as cur:
            for path in stale:
                rows = cur.execute(
                    _sql("execute_420.sql"),
                    (source_key, path),
                ).fetchall()
                deleted_chunk_ids.extend(r[0] for r in rows)
                cur.execute(_sql("execute_424.sql"), (source_key, path))
                cur.execute(_sql("execute_425.sql"), (source_key, path))
        return deleted_chunk_ids

    def detect_rename(self, repo: str, sha256: str, new_path: str, source_id: str | None = None) -> str | None:
        """Detect if a file was renamed/moved by matching content hash.

        If a file with the same sha256 exists under a different path in the
        same repo, returns the old path (candidate for rename). Returns None
        if no match found.
        """
        source_key = self._source_id(repo, source_id)
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                _sql("execute_439.sql"),
                (source_key, sha256, new_path),
            ).fetchone()
        return row[0] if row else None

    def apply_rename(self, old_path: str, new_path: str, source_id: str | None = None) -> None:
        """Update manifest records when a file is renamed/moved.

        Updates the file path and all associated chunk records.
        """
        with self._tx() as cur:
            if source_id is None:
                cur.execute(
                    _sql("execute_452.sql"),
                    (new_path, old_path),
                )
                cur.execute(
                    _sql("execute_456.sql"),
                    (new_path, old_path),
                )
            else:
                cur.execute(
                    _sql("execute_461.sql"),
                    (new_path, source_id, old_path),
                )
                cur.execute(
                    _sql("execute_465.sql"),
                    (new_path, source_id, old_path),
                )

    def delete_chunks(self, chunk_ids: list[str]) -> None:
        """Remove specific chunks from the manifest."""
        if not chunk_ids:
            return
        with self._tx() as cur:
            placeholders = ",".join("?" for _ in chunk_ids)
            cur.execute(
                _sql("fstring_355_2.sql").format(placeholders),  # noqa: S608  # nosec B608
                chunk_ids,
            )

    # -- Utilities --

    @staticmethod
    def file_sha256(path: str | Path) -> str:
        """Compute SHA256 of a file's contents (first 64KB for speed)."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            # Read first 64KB — sufficient for change detection, fast for large files
            h.update(f.read(65536))
        return h.hexdigest()[:16]

    def stats(self) -> dict[str, int]:
        """Return summary stats."""
        with self._lock:
            conn = self._get_conn()
            files = conn.execute(_sql("execute_495.sql")).fetchone()[0]
            chunks = conn.execute(_sql("execute_496.sql")).fetchone()[0]
            embedded = conn.execute(_sql("execute_497.sql")).fetchone()[0]
            runs = conn.execute(_sql("execute_498.sql")).fetchone()[0]
        return {"files": files, "chunks": chunks, "embedded": embedded, "runs": runs}
