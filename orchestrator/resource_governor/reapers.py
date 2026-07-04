"""Background cleanup loops for leases, activities and idle models."""

from __future__ import annotations

import threading
import time
from typing import Callable


class ReaperThread:
    def __init__(self, *, interval_seconds: float, reap: Callable[[], None], name: str) -> None:
        self.interval_seconds = interval_seconds
        self.reap = reap
        self.name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.reap()
            except Exception:
                time.sleep(min(1.0, self.interval_seconds))
