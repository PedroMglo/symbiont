"""Compressor registry."""

from __future__ import annotations

from storage_guardian.compressors.base import Compressor
from storage_guardian.compressors.passthrough import PassthroughCompressor
from storage_guardian.compressors.sevenzip import SevenZipCompressor
from storage_guardian.compressors.zstd import ZstdCompressor


def get_compressor(backend: str) -> Compressor:
    if backend == "sevenzip":
        return SevenZipCompressor()
    if backend == "passthrough":
        return PassthroughCompressor()
    return ZstdCompressor()

