"""Audio adapter placeholder for future FLAC lossless transforms."""

from __future__ import annotations

from storage_guardian.adapters.base import StoreAdapter


class AudioAdapter(StoreAdapter):
    name = "audio"

