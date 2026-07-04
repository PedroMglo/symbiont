"""Dask distributed engine for the ingest pipeline.

Replaces the local ProcessPoolExecutor parser stage with Dask futures,
enabling distribution across multiple machines or a local Dask cluster.

Requires: ``pip install obsidian-rag[dask]``

Architecture:
  - Reuses the same 4-stage pipeline (scan → parse → embed → write)
  - Only the parser stage changes: instead of ProcessPoolExecutor,
    file parsing is submitted as Dask futures via Client.submit()
  - The embedding and writer stages remain thread-based (I/O bound)
  - Compatible with: local threads, local processes, or remote scheduler

Usage:
  In config/rag/internal.toml:
    [pipeline]
    engine = "dask"
    dask_scheduler = ""          # empty = auto-create local cluster
    # dask_scheduler = "tcp://192.168.1.10:8786"  # remote scheduler
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _import_dask():
    """Lazy-import dask.distributed, raising a clear error if missing."""
    try:
        import dask.distributed as dd
        return dd
    except ImportError:
        raise ImportError(
            "Dask engine requires dask[distributed]. "
            "Install with: pip install obsidian-rag[dask]"
        )


class DaskParserPool:
    """Drop-in replacement for ProcessPoolExecutor that uses Dask for parsing.

    Implements the same interface used by IngestPipeline._parser_stage():
    submit(fn, *args) → future, shutdown().
    """

    def __init__(
        self,
        *,
        n_workers: int = 3,
        scheduler_address: str = "",
    ) -> None:
        dd = _import_dask()

        self._owns_cluster = False
        self._cluster: Any = None

        if scheduler_address:
            log.info("Connecting to Dask scheduler at %s", scheduler_address)
            self._client = dd.Client(scheduler_address)
        else:
            log.info("Creating local Dask cluster with %d workers", n_workers)
            self._cluster = dd.LocalCluster(
                n_workers=n_workers,
                threads_per_worker=1,
                processes=True,
                memory_limit="auto",
                silence_logs=logging.WARNING,
            )
            self._client = dd.Client(self._cluster)
            self._owns_cluster = True

        log.info("Dask dashboard: %s", self._client.dashboard_link)

    @property
    def client(self):
        """Return the Dask Client for direct access."""
        return self._client

    def submit(self, fn, /, *args, **kwargs):
        """Submit a task to the Dask cluster. Returns a Dask Future."""
        return self._client.submit(fn, *args, **kwargs, pure=False)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """Shut down the Dask client and optional local cluster."""
        if cancel_futures:
            self._client.cancel(self._client.futures)
        self._client.close()
        if self._owns_cluster and self._cluster is not None:
            self._cluster.close()
            self._cluster = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()


def create_parser_pool(
    *,
    engine: str = "local",
    n_workers: int = 3,
    scheduler_address: str = "",
):
    """Factory: create the right parser pool based on engine config.

    Args:
        engine: "local" for ProcessPoolExecutor, "dask" for Dask
        n_workers: number of parallel parser workers
        scheduler_address: Dask scheduler address (empty = local cluster)

    Returns:
        An object with submit(fn, *args) and shutdown(wait, cancel_futures) methods.
    """
    if engine == "dask":
        return DaskParserPool(
            n_workers=n_workers,
            scheduler_address=scheduler_address,
        )

    if engine == "local":
        from concurrent.futures import ProcessPoolExecutor
        from multiprocessing import get_context
        return ProcessPoolExecutor(
            max_workers=n_workers,
            mp_context=get_context("spawn"),
            max_tasks_per_child=100,
        )

    raise ValueError(f"Unknown pipeline engine: {engine!r}. Use 'local' or 'dask'.")
