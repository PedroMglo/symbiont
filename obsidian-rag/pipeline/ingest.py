"""Bounded parallel ingest pipeline — parse → embed → store with backpressure.

Architecture:
  1. File scanner thread — discovers changed files, feeds files_queue
  2. Parser pool (ProcessPoolExecutor) — parses files into chunks, feeds chunks_queue
  3. Embedding batcher thread — collects micro-batches, calls Ollama, feeds write_queue
  4. Writer thread — upserts to vector store, updates manifest

Backpressure: bounded queues between every stage. When the embedder is slow,
parsers block on chunks_queue.put(). When the writer is slow, the embedder
blocks on write_queue.put(). This prevents unbounded memory growth.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Callable, NamedTuple

from chunking.markdown import Chunk
from metadata import stable_source_id

log = logging.getLogger(__name__)

# Sentinel value to signal end of stream
_DONE = object()
_LANE_LOCK = threading.Lock()
_EMBEDDING_LANE_STATE: tuple[int, threading.BoundedSemaphore] | None = None
_VECTOR_WRITE_LANE = threading.BoundedSemaphore(1)

_PDF_SUFFIXES = {".pdf"}
_AUDIO_VIDEO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mkv", ".webm", ".mov", ".avi"}


def _embedding_lane(perf) -> threading.BoundedSemaphore:
    global _EMBEDDING_LANE_STATE
    concurrency = max(1, int(getattr(perf, "embedding_lane_concurrency", 1)))
    with _LANE_LOCK:
        if _EMBEDDING_LANE_STATE is None or _EMBEDDING_LANE_STATE[0] != concurrency:
            _EMBEDDING_LANE_STATE = (concurrency, threading.BoundedSemaphore(concurrency))
        return _EMBEDDING_LANE_STATE[1]


class FileJob(NamedTuple):
    """A file to be parsed."""
    path: str          # absolute path as string (must be picklable for ProcessPoolExecutor)
    repo_name: str
    repo_dir: str      # absolute repo root as string
    source_type: str   # "code", "document", or "vault"


class EmbeddedBatch(NamedTuple):
    """A batch of chunks with pre-computed embeddings, ready for vector store upsert."""
    chunks: list[Chunk]
    embeddings: list[list[float]]


@dataclass
class IngestResult:
    """Summary of an ingest pipeline run."""
    files_scanned: int = 0
    files_parsed: int = 0
    files_skipped: int = 0
    chunks_produced: int = 0
    chunks_embedded: int = 0
    chunks_stored: int = 0
    stale_deleted: int = 0
    errors: list[str] = field(default_factory=list)
    resource_pressure: dict[str, Any] | None = None
    elapsed_seconds: float = 0.0
    # Per-stage wall-clock timing (stages are pipelined, so these overlap)
    scan_ms: float = 0.0
    parse_ms: float = 0.0
    embed_ms: float = 0.0
    write_ms: float = 0.0


@dataclass
class IngestSource:
    """A source to ingest — a repo or vault directory."""
    source_type: str   # "code", "document", or "vault"
    path: Path
    name: str          # display name (repo name or "vault")
    exclude_patterns: tuple[str, ...] = ()


def _apply_worker_rlimits() -> None:
    """Apply resource limits to the current worker process (Unix only).

    Sets RLIMIT_AS (virtual address space) to prevent a single parser
    from consuming unbounded memory. If the limit is hit, the worker
    gets a MemoryError — which is caught by the pool — instead of
    causing system-wide OOM.
    """
    try:
        import resource

        import psutil

        total_ram = psutil.virtual_memory().total
        # Allow each worker up to 25% of total RAM
        worker_limit = total_ram // 4
        # Set soft = worker_limit, hard = worker_limit (can't be raised)
        try:
            resource.setrlimit(resource.RLIMIT_AS, (worker_limit, worker_limit))
        except (ValueError, OSError):
            # RLIMIT_AS not supported or already lower — ignore
            pass
    except Exception:
        # Non-Unix or psutil unavailable — skip silently
        pass


def _parse_file_worker(job_path: str, job_repo_dir: str, job_source_type: str) -> list[Chunk]:
    """Worker function for ProcessPoolExecutor — parses a single file into chunks.

    This runs in a separate process for memory isolation. Imports are local
    to avoid pickling issues with module-level singletons.

    RLIMIT_AS is set on first call to cap memory per worker process.
    """
    from pathlib import Path as _Path

    # Apply memory limits on first invocation in this process
    if not getattr(_parse_file_worker, "_rlimits_applied", False):
        _apply_worker_rlimits()
        _parse_file_worker._rlimits_applied = True  # type: ignore[attr-defined]

    path = _Path(job_path)
    repo_dir = _Path(job_repo_dir)

    if job_source_type == "code":
        from chunking.code import chunk_file
        from rag_config import settings
        chunks = chunk_file(path, repo_dir, settings.repos.chunking)
        return _enforce_chunk_count_limit(chunks, path)
    if job_source_type == "document":
        from chunking.document import chunk_document_file
        from rag_config import settings
        chunks = chunk_document_file(path, repo_dir, settings.chunking)
        return _enforce_chunk_count_limit(chunks, path)
    else:
        from chunking.markdown import chunk_note
        chunks = chunk_note(path, repo_dir)
        return _enforce_chunk_count_limit(chunks, path)


def _enforce_chunk_count_limit(chunks: list[Chunk], path: Path) -> list[Chunk]:
    """Cap per-file chunks so one large source cannot monopolise ingest."""
    if not chunks:
        return chunks
    try:
        from rag_config import settings

        max_chunks = int(settings.sync.limits.max_chunks_per_file)
    except Exception:
        max_chunks = 2000
    if max_chunks <= 0 or len(chunks) <= max_chunks:
        return chunks

    original_count = len(chunks)
    capped = chunks[:max_chunks]
    for chunk in capped:
        chunk.metadata["truncated_after_max_chunks"] = True
        chunk.metadata["original_chunk_count"] = original_count
    log.warning(
        "Chunk limit reached for %s: keeping %d/%d chunks",
        path,
        len(capped),
        original_count,
    )
    return capped


def _file_size_limit_bytes(path: Path, source_type: str) -> int | None:
    """Return hard size limit for files that must be read as text/documents."""
    suffix = path.suffix.lower()
    if suffix in _AUDIO_VIDEO_SUFFIXES:
        return None

    try:
        from rag_config import settings

        limits = settings.sync.limits
        if suffix in _PDF_SUFFIXES:
            return int(limits.max_file_size_mb_pdf) * 1024 * 1024
        return int(limits.max_file_size_mb_text) * 1024 * 1024
    except Exception:
        if suffix in _PDF_SUFFIXES:
            return 200 * 1024 * 1024
        return 50 * 1024 * 1024


def _is_external_service_pending(exc: BaseException) -> bool:
    try:
        from integrations.external_services import ExternalServicePending

        return isinstance(exc, ExternalServicePending)
    except Exception:
        return exc.__class__.__name__ == "ExternalServicePending"


def _parser_future_timeout_seconds() -> int:
    try:
        from rag_config import settings

        return max(
            60,
            int(settings.sync.lifecycle_start_timeout_seconds)
            + max(int(settings.sync.extrator_timeout_seconds), int(settings.sync.audio_transcribe_timeout_seconds))
            + 15,
        )
    except Exception:
        return 180


class IngestPipeline:
    """Bounded parallel ingest pipeline with backpressure between stages.

    Usage:
        pipeline = IngestPipeline(manifest, settings)
        result = pipeline.run(sources)
    """

    def __init__(
        self,
        manifest,  # IngestManifest
        perf,      # PerformanceConfig
        store,     # VectorStore (backend-agnostic)
        *,
        collection_name: str = "code_repos",
        embed_fn=None,  # optional: callable(list[str]) -> list[list[float]] for testing
        governor=None,  # optional: ResourceGovernor (created automatically if None)
        pipeline_config=None,  # optional: PipelineConfig (engine, dask_scheduler)
        max_run_seconds: float = 1800,  # global timeout (default 30 min)
        force: bool = False,  # if True, skip manifest checks and reindex everything
        mtime_shortcircuit: bool = False,  # if True, skip sha256 when mtime+size unchanged
        reset_source_types: tuple[str, ...] | None = None,
        cleanup_stale_global: bool = True,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        progress_child_id: str | None = None,
        progress_phase: str | None = None,
        progress_attempt: int = 1,
    ) -> None:
        from pipeline.manifest import IngestManifest
        self._manifest: IngestManifest = manifest
        self._perf = perf
        self._store = store
        self._collection_name = collection_name
        self._embed_fn = embed_fn
        self._governor = governor       # set in run() if None
        self._owns_governor = False      # True when we created the governor
        self._pipeline_config = pipeline_config
        self._max_run_seconds = max_run_seconds
        self._force = force
        self._mtime_shortcircuit = mtime_shortcircuit
        self._reset_source_types = reset_source_types
        self._cleanup_stale_global_enabled = cleanup_stale_global
        self._cancel_event = cancel_event
        self._cancel_recorded = False
        self._progress_callback = progress_callback
        self._progress_child_id = progress_child_id
        self._progress_phase = progress_phase or "ingest"
        self._progress_attempt = max(1, int(progress_attempt or 1))

        # Queues with bounded sizes for backpressure
        self._files_queue: Queue = Queue(maxsize=perf.files_queue_max)
        self._chunks_queue: Queue = Queue(maxsize=perf.chunks_queue_max)
        self._write_queue: Queue = Queue(maxsize=4)

        # Coordination
        self._abort = threading.Event()
        self._result = IngestResult()
        self._result_lock = threading.Lock()

    def run(self, sources: list[IngestSource]) -> IngestResult:
        """Execute the full ingest pipeline. Blocks until complete."""
        start = time.monotonic()
        run_id = self._manifest.start_run()

        if self._cancel_requested():
            self._record_cancel()

        if self._force and not self._abort.is_set():
            self._reset_for_forced_rebuild(sources)

        # --- Governor lifecycle ---
        if self._governor is None:
            from pipeline.governor import ResourceGovernor
            data_dir = None
            try:
                from rag_config import settings
                data_dir = str(settings.paths.data_dir)
            except Exception:
                pass
            self._governor = ResourceGovernor(self._perf, data_dir=data_dir)
            self._owns_governor = True
        self._governor.start()

        # --- Global timeout watchdog ---
        def _watchdog() -> None:
            deadline = start + self._max_run_seconds
            while not self._abort.is_set():
                if self._cancel_requested():
                    self._record_cancel()
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.error(
                        "Pipeline watchdog: global timeout (%.0f s) exceeded — aborting",
                        self._max_run_seconds,
                    )
                    print(
                        f"✗ Pipeline timeout ({self._max_run_seconds:.0f}s) — a abortar de forma segura"
                    )
                    self._abort.set()
                    with self._result_lock:
                        self._result.errors.append(
                            f"watchdog: global timeout {self._max_run_seconds:.0f}s exceeded"
                        )
                    return
                # Check every 5 s
                self._abort.wait(min(5.0, remaining))

        watchdog_thread = threading.Thread(
            target=_watchdog,
            name="ingest-watchdog",
            daemon=True,
        )
        watchdog_thread.start()

        # Start stages as daemon threads (except parser pool)
        scanner_thread = threading.Thread(
            target=self._timed_stage,
            args=("scan_ms", self._scanner_stage, sources),
            name="ingest-scanner",
            daemon=True,
        )

        # Parallel embedding: start N embedder threads (configurable)
        embedding_concurrency = getattr(self._perf, "embedding_concurrency", 1)
        embedding_concurrency = max(1, min(embedding_concurrency, 4))
        embedder_threads: list[threading.Thread] = []
        for i in range(embedding_concurrency):
            t = threading.Thread(
                target=self._timed_stage,
                args=("embed_ms", self._embedder_stage),
                name=f"ingest-embedder-{i}",
                daemon=True,
            )
            embedder_threads.append(t)

        writer_thread = threading.Thread(
            target=self._timed_stage,
            args=("write_ms", self._writer_stage),
            name="ingest-writer",
            daemon=True,
        )

        # Parser stage runs in this method (manages ProcessPoolExecutor lifecycle)
        scanner_thread.start()
        for t in embedder_threads:
            t.start()
        writer_thread.start()

        _parse_start = time.monotonic()
        try:
            self._parser_stage()
        except Exception as e:
            log.error("Parser stage fatal error: %s", e)
            self._abort.set()
            with self._result_lock:
                self._result.errors.append(f"parser_fatal: {e}")
        finally:
            with self._result_lock:
                self._result.parse_ms = round((time.monotonic() - _parse_start) * 1000, 1)

        # Wait for downstream stages to drain
        for t in embedder_threads:
            t.join(timeout=300)
        writer_thread.join(timeout=300)
        scanner_thread.join(timeout=10)

        # Stale cleanup: collect ALL manifest IDs across ALL sources first,
        # then delete store entries not present in any source — once.
        # Per-source cleanup is wrong: existing_in_store − one_repo_ids
        # removes chunks belonging to every other repo each iteration.
        if not self._abort.is_set() and self._cleanup_stale_global_enabled:
            try:
                all_manifest_ids: set[str] = set()
                for source in sources:
                    source_id = stable_source_id(source.name, source.path)
                    all_manifest_ids |= self._manifest.get_chunk_ids_for_repo(source.name, source_id=source_id)
                self._cleanup_stale_global(all_manifest_ids)
            except Exception as e:
                log.warning("Stale cleanup error: %s", e)

        self._result.elapsed_seconds = time.monotonic() - start

        # --- Finalize HNSW index (restore graph degree after deferred bulk build) ---
        if not self._abort.is_set() and self._result.chunks_stored > 0:
            finalize = getattr(self._store, "finalize_collection_index", None)
            if callable(finalize):
                try:
                    finalize(self._collection_name)
                except Exception as e:
                    log.warning("HNSW finalize error: %s", e)

        # --- BM25 sparse index rebuild (async, non-blocking) ---
        if not self._abort.is_set() and self._result.chunks_stored > 0:
            bm25_thread = threading.Thread(
                target=self._rebuild_bm25_index_safe,
                daemon=True,
                name="bm25-rebuild",
            )
            bm25_thread.start()

        status = "completed" if not self._abort.is_set() else "aborted"
        error_msg = "; ".join(self._result.errors) if self._result.errors else None
        self._manifest.finish_run(run_id, status=status, error=error_msg)

        # --- Governor cleanup ---
        if self._owns_governor and self._governor is not None:
            self._governor.stop()

        from pipeline.governor import release_process_memory

        release_process_memory(perf=self._perf, label=f"ingest:{self._collection_name}")

        # Emit observability event
        from observability import emit, is_enabled
        if is_enabled():
            from observability import EventName, RAGEvent
            emit(RAGEvent(
                event=EventName.INGEST_RUN_COMPLETED if status == "completed" else EventName.INGEST_RUN_STARTED,
                run_id=run_id,
                latency_ms=self._result.elapsed_seconds * 1000,
                files_scanned=self._result.files_scanned,
                files_parsed=self._result.files_parsed,
                files_skipped=self._result.files_skipped,
                chunks_produced=self._result.chunks_produced,
                chunks_embedded=self._result.chunks_embedded,
                chunks_stored=self._result.chunks_stored,
                stale_deleted=self._result.stale_deleted,
                success=status == "completed",
                error_count=len(self._result.errors),
            ))

        return self._result

    def _cancel_requested(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    def _record_cancel(self) -> None:
        if self._cancel_recorded:
            self._abort.set()
            return
        self._cancel_recorded = True
        self._abort.set()
        with self._result_lock:
            self._result.errors.append("canceled")
        log.info("Ingest pipeline canceled for collection %s", self._collection_name)
        print(f"✗ Pipeline cancelado para '{self._collection_name}'")

    def _emit_progress(self, event: str, **payload: Any) -> None:
        if self._progress_callback is None or not self._progress_child_id:
            return
        try:
            self._progress_callback(
                {
                    "event": event,
                    "child_id": self._progress_child_id,
                    "phase": self._progress_phase,
                    "attempt": self._progress_attempt,
                    **payload,
                }
            )
        except Exception:
            pass

    def _record_resource_pressure(self, exc: BaseException, *, stage: str) -> None:
        payload_fn = getattr(exc, "payload", None)
        payload = dict(payload_fn()) if callable(payload_fn) else {}
        status = str(payload.get("resource_state") or getattr(exc, "status", "failed_resource_pressure"))
        payload.setdefault("resource_state", status)
        payload.setdefault("status", status)
        payload.setdefault("attempt", self._progress_attempt)
        payload.setdefault("error", str(exc)[:1000])
        if status == "deferred_resource_pressure" and payload.get("retry_at") is None:
            retry_after = payload.get("retry_after_seconds")
            try:
                payload["retry_at"] = time.time() + max(1, int(retry_after))
            except (TypeError, ValueError):
                pass
        payload["stage"] = stage
        if status == "cancelled":
            self._record_cancel()
        else:
            self._abort.set()
        with self._result_lock:
            self._result.errors.append(f"{stage}:{status}: {str(exc)[:500]}")
            self._result.resource_pressure = dict(payload)
        event = "child_deferred" if status == "deferred_resource_pressure" else "child_failed"
        self._emit_progress(event, **payload)

    def _lease_limit_int(self, lease: Any, *keys: str) -> int | None:
        decision = getattr(lease, "decision", None)
        limits = getattr(decision, "limits", None)
        if not isinstance(limits, dict):
            return None
        for key in keys:
            value = limits.get(key)
            if value is None:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return None

    def _put_done_sentinel(self, queue: Queue, *, label: str) -> None:
        while True:
            try:
                queue.put(_DONE, timeout=0.5)
                return
            except Full:
                if self._abort.is_set() or self._cancel_requested():
                    log.debug("Skipping %s DONE sentinel after abort/cancel with a full queue", label)
                    return

    def _acquire_lane(self, semaphore: threading.BoundedSemaphore) -> bool:
        while not self._abort.is_set():
            if self._cancel_requested():
                self._record_cancel()
                return False
            if semaphore.acquire(timeout=0.5):
                return True
        return False

    def _reset_for_forced_rebuild(self, sources: list[IngestSource]) -> None:
        """Discard collection and manifest state before a forced replacement run."""
        source_types = self._reset_source_types
        if source_types is None:
            source_types = tuple(sorted({source.source_type for source in sources}))
        removed_manifest = self._manifest.delete_source_types(source_types)

        reset_collection = getattr(self._store, "reset_collection", None)
        if callable(reset_collection):
            deleted_vectors = reset_collection(collection=self._collection_name)
        else:
            existing_ids = self._store.get_existing_ids(collection=self._collection_name)
            deleted_vectors = self._store.delete_ids(list(existing_ids), collection=self._collection_name)
        self._reset_bm25_state()
        if deleted_vectors:
            with self._result_lock:
                self._result.stale_deleted += deleted_vectors
        log.info(
            "Forced rebuild reset for %s: deleted %d vectors, %d manifest files, %d manifest chunks",
            self._collection_name,
            deleted_vectors,
            removed_manifest["files"],
            removed_manifest["chunks"],
        )

    def _reset_bm25_state(self) -> None:
        try:
            from rag_config import settings
            model_path = Path(settings.paths.data_dir) / "bm25" / f"{self._collection_name}.json"
        except Exception:
            model_path = Path("data/qdrant/bm25") / f"{self._collection_name}.json"
        try:
            model_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("BM25 reset: could not remove %s: %s", model_path, exc)
        try:
            from retrieval import rag as retrieval_rag

            retrieval_rag._bm25_cache.pop(self._collection_name, None)  # noqa: SLF001
        except Exception:
            pass

    def _timed_stage(self, field_name: str, fn, *args) -> None:
        """Run a pipeline stage, recording its wall-clock duration on the result."""
        t0 = time.monotonic()
        try:
            fn(*args)
        finally:
            with self._result_lock:
                setattr(self._result, field_name, round((time.monotonic() - t0) * 1000, 1))

    # -- Stage 1: Scanner --

    def _scanner_stage(self, sources: list[IngestSource]) -> None:
        """Discover files that need reindexing and feed them to the files queue."""
        try:
            for source in sources:
                if self._abort.is_set():
                    break
                self._scan_source(source)
        except Exception as e:
            log.error("Scanner error: %s", e)
            with self._result_lock:
                self._result.errors.append(f"scanner: {e}")
        finally:
            # Signal end of files
            self._put_done_sentinel(self._files_queue, label="files")

    def _scan_source(self, source: IngestSource) -> None:
        """Scan a single source (repo or vault) for changed files."""
        if source.source_type == "code":
            from chunking.code import iter_repo_files
            file_iter = iter_repo_files(source.path)
        elif source.source_type == "document":
            from chunking.document import iter_document_files
            file_iter = iter_document_files(source.path, exclude_patterns=source.exclude_patterns)
        else:
            from chunking.markdown import iter_note_files
            file_iter = iter_note_files(source.path)

        print(f"  [scan] {source.name} ({source.path})")
        source_scanned = 0
        source_queued = 0

        for file_path in file_iter:
            if self._abort.is_set():
                return

            with self._result_lock:
                self._result.files_scanned += 1
            source_scanned += 1

            # Check if file needs reindexing
            try:
                stat = file_path.stat()
                rel_path = str(file_path.relative_to(source.path))
                source_id = stable_source_id(source.name, source.path)

                size_limit = _file_size_limit_bytes(file_path, source.source_type)
                if size_limit is not None and stat.st_size > size_limit:
                    existing = self._manifest.get_file_state(
                        rel_path,
                        repo=source.name,
                        source_id=source_id,
                    )
                    if (
                        not self._force
                        and existing is not None
                        and existing[0] == stat.st_mtime
                        and existing[1] == stat.st_size
                        and existing[2] == "skipped_too_large"
                    ):
                        with self._result_lock:
                            self._result.files_skipped += 1
                        continue

                    pseudo_sha = f"too-large:{stat.st_size}:{getattr(stat, 'st_mtime_ns', int(stat.st_mtime))}"
                    self._manifest.record_file(
                        path=rel_path,
                        repo=source.name,
                        mtime=stat.st_mtime,
                        size=stat.st_size,
                        sha256=pseudo_sha,
                        chunk_count=0,
                        source_id=source_id,
                        source_type=source.source_type,
                        status="skipped_too_large",
                    )
                    self._manifest.record_chunks(
                        chunk_ids=[],
                        file_path=rel_path,
                        repo=source.name,
                        chunk_hashes=[],
                        source_id=source_id,
                        source_type=source.source_type,
                    )
                    log.warning(
                        "[scan] skipping %s/%s: %.1f MB exceeds %.1f MB limit",
                        source.name,
                        rel_path,
                        stat.st_size / (1024 * 1024),
                        size_limit / (1024 * 1024),
                    )
                    with self._result_lock:
                        self._result.files_skipped += 1
                    continue

                # mtime short-circuit: check mtime+size first to avoid disk read
                mtime_sc = self._mtime_shortcircuit
                if not self._force and mtime_sc:
                    if not self._manifest.needs_reindex(
                        rel_path,
                        stat.st_mtime,
                        stat.st_size,
                        "",  # sha not computed yet
                        repo=source.name,
                        source_id=source_id,
                        mtime_shortcircuit=True,
                    ):
                        with self._result_lock:
                            self._result.files_skipped += 1
                        continue

                sha = self._manifest.file_sha256(file_path)

                if not self._force and not self._manifest.needs_reindex(
                    rel_path,
                    stat.st_mtime,
                    stat.st_size,
                    sha,
                    repo=source.name,
                    source_id=source_id,
                ):
                    with self._result_lock:
                        self._result.files_skipped += 1
                    continue
            except OSError as e:
                log.warning("Cannot stat %s: %s", file_path, e)
                continue

            source_queued += 1
            log.info("[scan] queuing %s/%s", source.name, rel_path)

            job = FileJob(
                path=str(file_path),
                repo_name=source.name,
                repo_dir=str(source.path),
                source_type=source.source_type,
            )

            # Block if queue is full — this is backpressure from parsers
            while not self._abort.is_set():
                try:
                    self._files_queue.put(job, timeout=1)
                    break
                except Full:
                    continue

        print(f"  [scan] {source.name}: {source_scanned} ficheiros, {source_queued} para processar")

        # Generate repo overview chunks for code sources (Repomix-style)
        if source.source_type == "code" and not self._abort.is_set():
            try:
                from chunking.repo_overview import generate_repo_overview
                overview_chunks = generate_repo_overview(source.path)
                for chunk in overview_chunks:
                    while not self._abort.is_set():
                        try:
                            self._chunks_queue.put(chunk, timeout=1)
                            break
                        except Full:
                            continue
                if overview_chunks:
                    log.info("[scan] %s: generated %d overview chunks", source.name, len(overview_chunks))
            except Exception as e:
                log.debug("Repo overview generation failed for %s: %s", source.name, e)

    def _record_parsed_file(self, job: FileJob, chunks: list[Chunk]) -> None:
        """Persist manifest state for a parsed file, including zero-chunk files."""
        file_path_rel = str(Path(job.path).relative_to(Path(job.repo_dir)))
        source_id = stable_source_id(job.repo_name, job.repo_dir)
        stat = Path(job.path).stat()
        sha = self._manifest.file_sha256(job.path)
        self._manifest.record_file(
            path=file_path_rel,
            repo=job.repo_name,
            mtime=stat.st_mtime,
            size=stat.st_size,
            sha256=sha,
            chunk_count=len(chunks),
            source_id=source_id,
            source_type=job.source_type,
        )
        self._manifest.record_chunks(
            chunk_ids=[c.id for c in chunks],
            file_path=file_path_rel,
            repo=job.repo_name,
            chunk_hashes=[c.metadata.get("content_hash", c.id) for c in chunks],
            source_id=source_id,
            source_type=job.source_type,
        )

    def _record_pending_file(self, job: FileJob, reason: str) -> None:
        """Persist a retryable pending state without embedding placeholder text."""
        file_path_rel = str(Path(job.path).relative_to(Path(job.repo_dir)))
        source_id = stable_source_id(job.repo_name, job.repo_dir)
        stat = Path(job.path).stat()
        sha = self._manifest.file_sha256(job.path)
        self._manifest.record_file(
            path=file_path_rel,
            repo=job.repo_name,
            mtime=stat.st_mtime,
            size=stat.st_size,
            sha256=sha,
            chunk_count=0,
            source_id=source_id,
            source_type=job.source_type,
            status="pending_external_service",
        )
        self._manifest.record_chunks(
            chunk_ids=[],
            file_path=file_path_rel,
            repo=job.repo_name,
            chunk_hashes=[],
            source_id=source_id,
            source_type=job.source_type,
        )
        log.info("External service pending for %s/%s: %s", job.repo_name, file_path_rel, reason)

    # -- Stage 2: Parser Pool --

    def _parser_stage(self) -> None:
        """Consume FileJobs from files_queue, parse in parallel, feed chunks_queue.

        When parser_workers <= 1, parsing runs in-process (no fork) to avoid
        the memory spike caused by ProcessPoolExecutor's fork().
        """
        use_pool = self._perf.parser_workers > 1

        if use_pool:
            from pipeline.dask_engine import create_parser_pool

            engine = "local"
            scheduler = ""
            if self._pipeline_config is not None:
                engine = self._pipeline_config.engine
                scheduler = self._pipeline_config.dask_scheduler

            executor = create_parser_pool(
                engine=engine,
                n_workers=self._perf.parser_workers,
                scheduler_address=scheduler,
            )

        try:
            pending_futures = []

            while not self._abort.is_set():
                try:
                    job = self._files_queue.get(timeout=1)
                except Empty:
                    continue

                if job is _DONE:
                    break

                if use_pool:
                    future = executor.submit(
                        _parse_file_worker,
                        job.path,
                        job.repo_dir,
                        job.source_type,
                    )
                    pending_futures.append((future, job))

                    # Harvest completed futures to avoid unbounded list growth
                    self._harvest_futures(pending_futures)
                else:
                    # In-process parsing — no fork overhead
                    rel = str(Path(job.path).relative_to(Path(job.repo_dir)))
                    print(f"  [parse] {job.repo_name}/{rel}")
                    try:
                        chunks = _parse_file_worker(job.path, job.repo_dir, job.source_type)
                    except Exception as e:
                        if _is_external_service_pending(e):
                            print(f"  [parse] PENDENTE {job.repo_name}/{rel}: {e}")
                            try:
                                self._record_pending_file(job, str(e))
                            except Exception as record_exc:
                                log.warning("Manifest pending record error for %s: %s", job.path, record_exc)
                            with self._result_lock:
                                self._result.files_skipped += 1
                            continue
                        import traceback as _tb
                        log.warning(
                            "Parse error for %s: %r\n%s",
                            job.path, e, _tb.format_exc(),
                        )
                        print(f"  [parse] ERRO {job.repo_name}/{rel}: {e!r}")
                        with self._result_lock:
                            self._result.errors.append(f"parse:{Path(job.path).name}: {e!r}")
                        continue

                    with self._result_lock:
                        self._result.files_parsed += 1
                        self._result.chunks_produced += len(chunks)
                    if chunks:
                        print(f"  [parse] {job.repo_name}/{rel} → {len(chunks)} chunks")

                        # Inject source_name for multi-vault filtering
                        source_id = stable_source_id(job.repo_name, job.repo_dir)
                        for chunk in chunks:
                            chunk.metadata.setdefault("source_name", job.repo_name)
                            chunk.metadata.setdefault("source_id", source_id)

                    try:
                        self._record_parsed_file(job, chunks)
                    except Exception as e:
                        log.warning("Manifest record error for %s: %s", job.path, e)

                    if chunks:
                        for chunk in chunks:
                            while not self._abort.is_set():
                                try:
                                    self._chunks_queue.put(chunk, timeout=1)
                                    break
                                except Full:
                                    continue

            # Drain remaining futures (only relevant when using pool)
            if use_pool:
                self._harvest_futures(pending_futures, drain=True)

        finally:
            if use_pool:
                executor.shutdown(wait=True, cancel_futures=True)
            # Signal end of chunks — one DONE per embedder thread
            embedding_concurrency = getattr(self._perf, "embedding_concurrency", 1)
            for _ in range(max(1, min(embedding_concurrency, 4))):
                self._put_done_sentinel(self._chunks_queue, label="chunks")

    def _harvest_futures(self, pending: list, drain: bool = False) -> None:
        """Collect results from completed parser futures and push chunks to queue."""
        still_pending = []

        for future, job in pending:
            if drain:
                # Wait for completion
                try:
                    chunks = future.result(timeout=_parser_future_timeout_seconds())
                except Exception as e:
                    if _is_external_service_pending(e):
                        try:
                            self._record_pending_file(job, str(e))
                        except Exception as record_exc:
                            log.warning("Manifest pending record error for %s: %s", job.path, record_exc)
                        with self._result_lock:
                            self._result.files_skipped += 1
                        continue
                    import traceback as _tb
                    log.warning(
                        "Parse error for %s: %r\n%s",
                        job.path, e, _tb.format_exc(),
                    )
                    with self._result_lock:
                        self._result.errors.append(f"parse:{Path(job.path).name}: {e!r}")
                    continue
            elif future.done():
                try:
                    chunks = future.result()
                except Exception as e:
                    if _is_external_service_pending(e):
                        try:
                            self._record_pending_file(job, str(e))
                        except Exception as record_exc:
                            log.warning("Manifest pending record error for %s: %s", job.path, record_exc)
                        with self._result_lock:
                            self._result.files_skipped += 1
                        continue
                    import traceback as _tb
                    log.warning(
                        "Parse error for %s: %r\n%s",
                        job.path, e, _tb.format_exc(),
                    )
                    with self._result_lock:
                        self._result.errors.append(f"parse:{Path(job.path).name}: {e!r}")
                    continue
            else:
                still_pending.append((future, job))
                continue

            with self._result_lock:
                self._result.files_parsed += 1
                self._result.chunks_produced += len(chunks)

            # Push chunks to queue with backpressure
            if chunks:
                # Inject source_name for multi-vault filtering
                source_id = stable_source_id(job.repo_name, job.repo_dir)
                for chunk in chunks:
                    chunk.metadata.setdefault("source_name", job.repo_name)
                    chunk.metadata.setdefault("source_id", source_id)

            # Record in manifest, including zero-chunk files that parsed cleanly.
            try:
                self._record_parsed_file(job, chunks)
            except Exception as e:
                log.warning("Manifest record error for %s: %s", job.path, e)

            if chunks:
                # Put each chunk into the queue — blocks when embedder is slow
                for chunk in chunks:
                    while not self._abort.is_set():
                        try:
                            self._chunks_queue.put(chunk, timeout=1)
                            break
                        except Full:
                            continue

        pending.clear()
        pending.extend(still_pending)

    # -- Stage 3: Embedding Batcher --

    def _embedder_stage(self) -> None:
        """Collect chunks into micro-batches, embed, and feed write_queue."""
        if self._embed_fn is not None:
            _embed = self._embed_fn
        else:
            from embeddings import get_embedder
            embedder = get_embedder()
            # Prefer the persistent-cache path so unchanged chunk text is never
            # re-embedded across runs (huge win at 300GB+ scale).
            _embed = getattr(embedder, "embed_texts_cached", None) or embedder.embed_texts

        from pipeline.governor import GovernorAction, ResourcePressureError, wait_for_resource_budget

        batch: list[Chunk] = []
        batch_chars = 0
        batch_start: float | None = None

        max_batch = self._perf.embedding_batch_size
        max_chars = self._perf.embedding_batch_max_chars

        def flush_batch() -> None:
            nonlocal batch, batch_chars, batch_start, max_batch
            if not batch:
                return
            deferred_chunks: list[Chunk] = []

            def apply_batch_limit(limit: int, reason: str) -> None:
                nonlocal batch, batch_chars, max_batch, deferred_chunks
                limit = max(1, int(limit))
                if len(batch) <= limit:
                    return
                deferred_chunks = batch[limit:] + deferred_chunks
                batch = batch[:limit]
                batch_chars = sum(len(chunk.text) for chunk in batch)
                max_batch = min(max_batch, limit)
                log.info("%s — batch limitado para %d chunk(s)", reason, limit)

            # Resource check via governor before embedding.
            if self._governor is not None:
                try:
                    action = wait_for_resource_budget(
                        self._governor,
                        perf=self._perf,
                        label=f"{self._collection_name}:embed",
                        cancel_event=self._cancel_event,
                        progress_callback=self._progress_callback,
                        child_id=self._progress_child_id,
                        phase=self._progress_phase,
                        attempt=self._progress_attempt,
                    )
                except ResourcePressureError as exc:
                    log.warning("Governor deferred embedding batch: %s", exc)
                    self._record_resource_pressure(exc, stage="embed")
                    batch.clear()
                    batch_chars = 0
                    batch_start = None
                    return
                if action in (GovernorAction.THROTTLE, GovernorAction.REDUCE):
                    # Reduce batch size dynamically
                    max_batch = max(5, max_batch // 2)
                    log.info("Governor: %s — batch reduzido para %d", action.name, max_batch)
                    apply_batch_limit(max_batch, f"Governor {action.name}")

            try:
                lease_context = None
                try:
                    from integrations.resource_governor_client import lease_context as _lease_context

                    lease_context = _lease_context
                except Exception as exc:
                    log.debug("Resource Governor embedding lease skipped: %s", exc)

                def _embed_current_batch() -> list[list[float]]:
                    texts = [c.text for c in batch]
                    repos_in_batch = {c.metadata.get("repo_name", "?") for c in batch if hasattr(c, "metadata")}
                    repo_tag = ",".join(sorted(repos_in_batch)) if repos_in_batch else "?"
                    print(f"  [embed] {len(batch)} chunks ({repo_tag})")
                    lane = _embedding_lane(self._perf)
                    acquired = self._acquire_lane(lane)
                    if not acquired:
                        return []
                    try:
                        return _embed(texts)
                    finally:
                        lane.release()

                if lease_context is None:
                    embeddings = _embed_current_batch()
                else:
                    try:
                        with lease_context(
                            component="embedding_batcher",
                            lane="background",
                            lease_scope="batch",
                            resource_class="vram",
                            capability="embedding_gpu_batch",
                            estimated_duration_seconds=60,
                            estimated_ram_mb=max(128, batch_chars // 1024),
                            estimated_vram_mb=1024,
                            preemptible=True,
                            quality_policy="preserve",
                            estimated_quality_impact="high",
                            idempotency_suffix="-".join(c.id for c in batch[:3]),
                        ) as lease:
                            if not lease.granted:
                                retry = int(lease.decision.retry_after_seconds or 30)
                                raise ResourcePressureError(
                                    "deferred_resource_pressure",
                                    str(lease.decision.reason or "embedding lease denied by Resource Governor"),
                                    action=GovernorAction.PAUSE,
                                    retry_after_seconds=retry,
                                    attempt=self._progress_attempt,
                                )
                            limit = self._lease_limit_int(lease, "batch_size", "embedding_batch_size")
                            if limit is not None:
                                apply_batch_limit(limit, "Resource Governor lease limit")
                            embeddings = _embed_current_batch()
                    except ResourcePressureError:
                        raise
                    except Exception as exc:
                        log.debug("Resource Governor embedding lease unavailable; using local lane only: %s", exc)
                        embeddings = _embed_current_batch()
                if not embeddings:
                    return

                embedded = EmbeddedBatch(chunks=list(batch), embeddings=embeddings)

                with self._result_lock:
                    self._result.chunks_embedded += len(batch)
                print(f"  [embed] OK — {self._result.chunks_embedded} embedded total")

                # Block if writer is slow
                while not self._abort.is_set():
                    try:
                        self._write_queue.put(embedded, timeout=1)
                        break
                    except Full:
                        continue

            except ResourcePressureError as exc:
                log.warning("Embedding deferred by Resource Governor: %s", exc)
                self._record_resource_pressure(exc, stage="embed")
            except Exception as e:
                log.error("Embedding error (batch of %d): %s", len(batch), e)
                print(f"Embedding error (batch of {len(batch)}): {e}")
                with self._result_lock:
                    self._result.errors.append(f"embed: {e}")

            if self._abort.is_set():
                batch = []
                batch_chars = 0
                batch_start = None
            else:
                batch = deferred_chunks
                batch_chars = sum(len(chunk.text) for chunk in batch)
                batch_start = time.monotonic() if batch else None

        try:
            while not self._abort.is_set():
                try:
                    item = self._chunks_queue.get(timeout=0.5)
                except Empty:
                    # Check if batch should be flushed on timeout
                    if batch and batch_start and (time.monotonic() - batch_start) >= 1.0:
                        flush_batch()
                    continue

                if item is _DONE:
                    while batch and not self._abort.is_set():
                        flush_batch()
                    break

                chunk: Chunk = item
                if batch_start is None:
                    batch_start = time.monotonic()

                batch.append(chunk)
                batch_chars += len(chunk.text)

                # Flush conditions: count, chars, or time
                should_flush = (
                    len(batch) >= max_batch
                    or batch_chars >= max_chars
                    or (time.monotonic() - batch_start) >= 1.0
                )
                if should_flush:
                    flush_batch()

        except Exception as e:
            log.error("Embedder stage error: %s", e)
            with self._result_lock:
                self._result.errors.append(f"embedder_fatal: {e}")
        finally:
            # Signal end of embedded batches
            self._put_done_sentinel(self._write_queue, label="write")

    # -- Stage 4: Writer --

    def _writer_stage(self) -> None:
        """Consume EmbeddedBatch items and upsert to the vector store."""
        from pipeline.governor import GovernorAction, ResourcePressureError, wait_for_resource_budget

        embedding_concurrency = getattr(self._perf, "embedding_concurrency", 1)
        done_count = 0
        expected_done = max(1, min(embedding_concurrency, 4))
        try:
            while not self._abort.is_set():
                try:
                    item = self._write_queue.get(timeout=1)
                except Empty:
                    continue

                if item is _DONE:
                    done_count += 1
                    if done_count >= expected_done:
                        break
                    continue

                batch: EmbeddedBatch = item

                try:
                    if self._governor is not None:
                        action = wait_for_resource_budget(
                            self._governor,
                            perf=self._perf,
                            label=f"{self._collection_name}:write",
                            cancel_event=self._cancel_event,
                            progress_callback=self._progress_callback,
                            child_id=self._progress_child_id,
                            phase=self._progress_phase,
                            attempt=self._progress_attempt,
                        )
                        if action in (GovernorAction.THROTTLE, GovernorAction.REDUCE):
                            log.info("Governor: %s before vector write — lane remains serialized", action.name)

                    ids = [c.id for c in batch.chunks]
                    texts = [c.text for c in batch.chunks]
                    metadatas = [c.metadata for c in batch.chunks]
                    repos_in_batch = {m.get("repo_name", "?") for m in metadatas}
                    repo_tag = ",".join(sorted(repos_in_batch))

                    def _write_current_batch() -> bool:
                        acquired = self._acquire_lane(_VECTOR_WRITE_LANE)
                        if not acquired:
                            return False
                        try:
                            self._store.upsert_batch(
                                ids=ids,
                                embeddings=batch.embeddings,
                                documents=texts,
                                metadatas=metadatas,
                                collection=self._collection_name,
                            )
                            self._manifest.mark_chunks_embedded(ids)
                            return True
                        finally:
                            _VECTOR_WRITE_LANE.release()

                    lease_context = None
                    try:
                        from integrations.resource_governor_client import lease_context as _lease_context

                        lease_context = _lease_context
                    except Exception as exc:
                        log.debug("Resource Governor Qdrant write lease skipped: %s", exc)

                    if lease_context is None:
                        wrote = _write_current_batch()
                    else:
                        try:
                            with lease_context(
                                component="qdrant_writer",
                                lane="background",
                                lease_scope="batch",
                                resource_class="qdrant_write",
                                capability="rag_query",
                                estimated_duration_seconds=30,
                                estimated_io_mb=max(
                                    1,
                                    math.ceil(
                                        sum(len(c.text.encode("utf-8")) for c in batch.chunks) / (1024 * 1024)
                                    ),
                                ),
                                preemptible=True,
                                quality_policy="preserve",
                                estimated_quality_impact="high",
                                idempotency_suffix="-".join(c.id for c in batch.chunks[:3]),
                            ) as lease:
                                if not lease.granted:
                                    retry = int(lease.decision.retry_after_seconds or 30)
                                    raise ResourcePressureError(
                                        "deferred_resource_pressure",
                                        str(lease.decision.reason or "vector write lease denied by Resource Governor"),
                                        action=GovernorAction.PAUSE,
                                        retry_after_seconds=retry,
                                        attempt=self._progress_attempt,
                                    )
                                wrote = _write_current_batch()
                        except ResourcePressureError:
                            raise
                        except Exception as exc:
                            log.debug("Resource Governor Qdrant write lease unavailable; using local lane only: %s", exc)
                            wrote = _write_current_batch()
                    if not wrote:
                        continue

                    with self._result_lock:
                        self._result.chunks_stored += len(batch.chunks)
                    print(f"  [write] {len(batch.chunks)} chunks → store ({repo_tag}) | total: {self._result.chunks_stored}")

                except ResourcePressureError as e:
                    log.warning("Vector write deferred by Resource Governor: %s", e)
                    self._record_resource_pressure(e, stage="write")
                except Exception as e:
                    log.error("Writer error (batch of %d): %s", len(batch.chunks), e)
                    with self._result_lock:
                        self._result.errors.append(f"write: {e}")

        except Exception as e:
            log.error("Writer stage error: %s", e)
            with self._result_lock:
                self._result.errors.append(f"writer_fatal: {e}")

    # -- Stale cleanup --

    def _cleanup_stale_global(self, all_manifest_ids: set[str]) -> None:
        """Remove from vector store any chunk not present in ANY source manifest.

        Called once after all sources are processed with the union of all
        manifest IDs.  The per-source variant (_cleanup_stale) must NOT be used
        for multi-source runs: existing_in_store − one_repo_ids deletes every
        chunk that belongs to the other repos.
        """
        if not all_manifest_ids:
            return

        existing_in_store = self._store.get_existing_ids(collection=self._collection_name)
        stale_in_store = existing_in_store - all_manifest_ids
        if not stale_in_store:
            return

        deleted = self._store.delete_ids(list(stale_in_store), collection=self._collection_name)
        with self._result_lock:
            self._result.stale_deleted += deleted
        log.info("Deleted %d globally-stale chunks", deleted)

    def _cleanup_stale(self, source: IngestSource) -> None:
        """Remove chunks from vector store that no longer exist in the source."""
        # Get all current chunk IDs for this source from the manifest
        source_id = stable_source_id(source.name, source.path)
        current_ids = self._manifest.get_chunk_ids_for_repo(source.name, source_id=source_id)
        if not current_ids:
            return

        # Get existing IDs in the vector store
        existing_in_store = self._store.get_existing_ids(collection=self._collection_name)

        # Find IDs that are in the store but not in the manifest
        # (they were from files that no longer exist or changed)
        stale_in_store = existing_in_store - current_ids
        if not stale_in_store:
            return

        # Delete via VectorStore protocol
        deleted = self._store.delete_ids(list(stale_in_store), collection=self._collection_name)

        with self._result_lock:
            self._result.stale_deleted += deleted

        log.info("Deleted %d stale chunks from %s", deleted, source.name)

    # -- BM25 sparse index --

    def _rebuild_bm25_index_safe(self) -> None:
        """Thread-safe wrapper for BM25 rebuild (runs in background daemon thread)."""
        try:
            self._rebuild_bm25_index()
        except Exception as e:
            log.warning("BM25 index rebuild failed (background): %s", e)

    def _rebuild_bm25_index(self) -> None:
        """Scroll all docs from collection, fit BM25, upsert sparse vectors."""
        from retrieval.sparse import BM25Vectorizer, tokenize

        store = self._store
        collection = self._collection_name
        lease = None
        try:
            from integrations.resource_governor_client import request_lease

            lease = request_lease(
                component="bm25_rebuild",
                lane="background",
                lease_scope="batch",
                resource_class="cpu",
                capability="bm25_rebuild",
                estimated_duration_seconds=300,
                preemptible=True,
                quality_policy="degrade_allowed",
                estimated_quality_impact="low",
                idempotency_suffix=collection,
            )
            if not lease.granted:
                log.info("BM25 rebuild deferred by Resource Governor: %s", lease.decision.reason)
                return
        except Exception as exc:
            log.debug("Resource Governor BM25 lease skipped: %s", exc)

        def _release_bm25_lease() -> None:
            if lease is not None:
                lease.release()

        # 1. Scroll all documents
        all_docs: list[tuple[str, str]] = []  # (id, text)
        try:
            from store.qdrant_store import QdrantVectorStore
            if not isinstance(store, QdrantVectorStore):
                log.debug("BM25 rebuild: store is not QdrantVectorStore, skipping")
                _release_bm25_lease()
                return
        except ImportError:
            _release_bm25_lease()
            return

        offset = None
        while True:
            scroll_kwargs: dict = {
                "collection_name": collection,
                "limit": 500,
                "with_payload": ["_id", "_document"],
                "with_vectors": False,
            }
            if offset is not None:
                scroll_kwargs["offset"] = offset
            points, next_offset = store._client.scroll(**scroll_kwargs)
            for p in points:
                if p.payload:
                    rid = p.payload.get("_id", "")
                    doc = p.payload.get("_document", "")
                    if rid and doc:
                        all_docs.append((rid, doc))
            if next_offset is None:
                break
            offset = next_offset

        if not all_docs:
            _release_bm25_lease()
            return

        # 2. Tokenize corpus and fit BM25
        corpus_tokens = [tokenize(doc) for _, doc in all_docs]
        bm25 = BM25Vectorizer()
        bm25.fit(corpus_tokens)

        # 3. Save model
        try:
            from rag_config import settings
            model_path = Path(settings.paths.data_dir) / "bm25" / f"{collection}.json"
        except Exception:
            model_path = Path("data/qdrant/bm25") / f"{collection}.json"
        bm25.save(model_path)
        log.info("BM25: fitted on %d docs, vocab=%d → %s", len(all_docs), bm25.vocab_size, model_path)

        # 4. Generate sparse vectors and update (not upsert!) to preserve dense vectors
        from store.qdrant_store import _str_to_uint

        models = store._models
        batch_size = 100
        for i in range(0, len(all_docs), batch_size):
            if lease is not None:
                lease.heartbeat()
            batch = all_docs[i : i + batch_size]
            points_update = []
            for (rid, _doc), tokens in zip(batch, corpus_tokens[i : i + batch_size]):
                sv = bm25.transform(tokens, doc_len=len(tokens))
                if sv["indices"]:
                    points_update.append(
                        models.PointVectors(
                            id=_str_to_uint(rid),
                            vector={
                                "bm25": models.SparseVector(
                                    indices=sv["indices"],
                                    values=sv["values"],
                                ),
                            },
                        )
                    )
            if points_update:
                store._client.update_vectors(
                    collection_name=collection,
                    points=points_update,
                )
        _release_bm25_lease()

        print(f"  [bm25] Indexed {len(all_docs)} docs (vocab={bm25.vocab_size})")
