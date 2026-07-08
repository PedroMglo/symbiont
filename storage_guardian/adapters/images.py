"""Image adapter placeholder for future lossless optimization."""

from __future__ import annotations

from storage_guardian.adapters.base import StoreAdapter


class ImagesAdapter(StoreAdapter):
    name = "images"
