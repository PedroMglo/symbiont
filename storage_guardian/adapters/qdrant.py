"""Qdrant adapter placeholder.

Live Qdrant storage is intentionally blocked by the safety gate. This adapter
exists to own future snapshot creation via Qdrant's snapshot API.
"""

from __future__ import annotations

from storage_guardian.adapters.base import StoreAdapter


class QdrantAdapter(StoreAdapter):
    name = "qdrant"
