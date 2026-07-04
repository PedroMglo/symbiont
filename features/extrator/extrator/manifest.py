"""DuckDB manifest for extrator jobs, documents, chunks, and conversions."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from extrator.config import get_config
from extrator.errors import ManifestError
from extrator.types import ChunkPayload, DocumentInfo, JobKind, JobStatus, JobStatusResponse, TableInfo

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


_SCHEMA = _sql("schema.sql")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExtratorManifest:
    """Thread-safe DuckDB wrapper for extrator state."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = None
        self._lock = threading.Lock()

    def _connect(self):
        if self._conn is None:
            try:
                import duckdb
            except Exception as exc:
                raise ManifestError("duckdb is required for extrator manifest") from exc
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = duckdb.connect(self._db_path)
            self._conn.execute(_SCHEMA)
        return self._conn

    def health(self) -> bool:
        try:
            with self._lock:
                self._connect().execute(_sql("execute_118.sql")).fetchone()
            return True
        except Exception:
            return False

    def create_job(self, kind: JobKind, payload: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._connect().execute(
                _sql("execute_127.sql"),
                [
                    job_id,
                    kind.value,
                    JobStatus.QUEUED.value,
                    json.dumps(payload, sort_keys=True, default=str),
                    now_iso(),
                    "{}",
                    "{}",
                ],
            )
        return job_id

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        error: str | None = None,
        outputs: dict[str, str] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        existing = self.get_job(job_id)
        next_status = status or existing.status
        started_at = existing.started_at
        completed_at = existing.completed_at
        if next_status == JobStatus.RUNNING and not started_at:
            started_at = now_iso()
        if next_status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            completed_at = now_iso()
        with self._lock:
            self._connect().execute(
                _sql("execute_163.sql"),
                [
                    next_status.value,
                    started_at,
                    completed_at,
                    error,
                    json.dumps(outputs if outputs is not None else existing.outputs, sort_keys=True),
                    json.dumps(summary if summary is not None else existing.summary, sort_keys=True, default=str),
                    job_id,
                ],
            )

    def get_job(self, job_id: str) -> JobStatusResponse:
        with self._lock:
            row = self._connect().execute(
                _sql("execute_183.sql"),
                [job_id],
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return JobStatusResponse(
            job_id=row[0],
            kind=JobKind(row[1]),
            status=JobStatus(row[2]),
            created_at=row[3],
            started_at=row[4],
            completed_at=row[5],
            error=row[6],
            outputs=json.loads(row[7] or "{}"),
            summary=json.loads(row[8] or "{}"),
        )

    def get_job_payload(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connect().execute(
                _sql("execute_207.sql"),
                [job_id],
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return json.loads(row[0] or "{}")

    def needs_processing(
        self,
        source_path: str,
        file_hash: str,
        config_hash: str,
        *,
        force: bool,
        source_type: str | None = None,
    ) -> bool:
        if force:
            return True
        return self.find_document_by_fingerprint(
            file_hash,
            config_hash,
            source_type=source_type,
        ) is None

    def upsert_document(self, doc: DocumentInfo) -> None:
        with self._lock:
            self._connect().execute(
                _sql("execute_231.sql"),
                [
                    doc.doc_id,
                    doc.source_path,
                    doc.source_type,
                    doc.file_hash,
                    get_config().config_hash,
                    doc.status,
                    json.dumps(doc.output_paths, sort_keys=True),
                    json.dumps(doc.metadata, sort_keys=True, default=str),
                    now_iso(),
                ],
            )

    def get_document(self, doc_id: str) -> DocumentInfo:
        with self._lock:
            row = self._connect().execute(
                _sql("execute_253.sql"),
                [doc_id],
            ).fetchone()
        if row is None:
            raise KeyError(doc_id)
        return DocumentInfo(
            doc_id=row[0],
            source_path=row[1],
            source_type=row[2],
            file_hash=row[3],
            status=row[4],
            output_paths=json.loads(row[5] or "{}"),
            metadata=json.loads(row[6] or "{}"),
        )

    def find_document_by_source(self, source_path: str) -> DocumentInfo | None:
        with self._lock:
            row = self._connect().execute(
                _sql("execute_275.sql"),
                [source_path],
            ).fetchone()
        if row is None:
            return None
        return self.get_document(row[0])

    def find_document_by_fingerprint(
        self,
        file_hash: str,
        config_hash: str,
        *,
        source_type: str | None = None,
    ) -> DocumentInfo | None:
        with self._lock:
            row = self._connect().execute(
                _sql("execute_276.sql"),
                [file_hash, config_hash, source_type, source_type],
            ).fetchone()
        if row is None:
            return None
        return self.get_document(row[0])

    def replace_chunks(self, doc_id: str, chunks: list[ChunkPayload]) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(_sql("execute_290.sql"), [doc_id])
            for chunk in chunks:
                conn.execute(
                    _sql("execute_293.sql"),
                    [
                        chunk.chunk_id,
                        chunk.doc_id,
                        chunk.content_hash,
                        chunk.text_ref,
                        chunk.token_count,
                        chunk.source_type,
                        chunk.page_start,
                        chunk.page_end,
                        json.dumps(chunk.heading_path),
                        chunk.embedding_policy.value,
                        chunk.model_dump_json(),
                        now_iso(),
                    ],
                )

    def get_chunks(self, doc_id: str) -> list[ChunkPayload]:
        with self._lock:
            rows = self._connect().execute(
                _sql("execute_319.sql"),
                [doc_id],
            ).fetchall()
        return [ChunkPayload.model_validate_json(row[0]) for row in rows]

    def replace_tables(self, doc_id: str, tables: list[TableInfo]) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(_sql("execute_327.sql"), [doc_id])
            for table in tables:
                conn.execute(
                    _sql("execute_330.sql"),
                    [
                        table.table_id,
                        table.doc_id,
                        table.name,
                        table.rows,
                        table.columns,
                        table.output_path,
                        table.summary,
                        now_iso(),
                    ],
                )

    def get_tables(self, doc_id: str) -> list[TableInfo]:
        with self._lock:
            rows = self._connect().execute(
                _sql("execute_350.sql"),
                [doc_id],
            ).fetchall()
        return [
            TableInfo(
                table_id=row[0],
                doc_id=row[1],
                name=row[2],
                rows=row[3],
                columns=row[4],
                output_path=row[5],
                summary=row[6],
            )
            for row in rows
        ]

    def record_conversion(self, job_id: str, input_path: str, output_format: str, output_path: str, status: str) -> str:
        conversion_id = uuid.uuid4().hex
        with self._lock:
            self._connect().execute(
                _sql("execute_373.sql"),
                [conversion_id, job_id, input_path, output_format, output_path, status, now_iso()],
            )
        return conversion_id

    def stats(self) -> dict[str, int]:
        with self._lock:
            conn = self._connect()
            jobs = conn.execute(_sql("execute_385.sql")).fetchone()[0]
            docs = conn.execute(_sql("execute_386.sql")).fetchone()[0]
            chunks = conn.execute(_sql("execute_387.sql")).fetchone()[0]
            tables = conn.execute(_sql("execute_388.sql")).fetchone()[0]
            conversions = conn.execute(_sql("execute_389.sql")).fetchone()[0]
        return {
            "jobs_total": int(jobs),
            "documents_total": int(docs),
            "chunks_total": int(chunks),
            "tables_total": int(tables),
            "conversions_total": int(conversions),
        }


_manifest: ExtratorManifest | None = None


def get_manifest() -> ExtratorManifest:
    global _manifest
    if _manifest is None:
        _manifest = ExtratorManifest(get_config().manifest.db_path)
    return _manifest


def reset_manifest() -> None:
    global _manifest
    _manifest = None
