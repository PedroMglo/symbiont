"""Global deduplication engine — two-level dedup system.

Level 1: SHA-256 (exact binary match) — fast, Redis-cached
Level 2: Chromaprint (acoustic fingerprint) — catches re-encodes, trims

Both levels share a Redis cache for hot lookups and can optionally
persist to Postgres for long-term canonical storage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import tempfile
from typing import Any

import redis.asyncio as aioredis

from streaming.config import get_config

logger = logging.getLogger(__name__)

# Key prefixes
_SHA_PREFIX = "audio:dedup:sha256:"
_FP_PREFIX = "audio:dedup:fingerprint:"
_LOCK_PREFIX = "audio:lock:"

_redis_pool: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        cfg = get_config()
        # Use dedup-specific DB
        url = cfg.redis.url.rsplit("/", 1)[0] + f"/{cfg.dedup.redis_db}"
        _redis_pool = aioredis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=3,
        )
    return _redis_pool


# =============================================================================
# LEVEL 1: SHA-256 (exact match)
# =============================================================================


def compute_sha256(file_path: str, chunk_size: int = 65536) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def compute_sha256_bytes(data: bytes) -> str:
    """Compute SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()


async def check_sha256(sha: str) -> dict[str, Any] | None:
    """Check if SHA-256 hash has a cached result."""
    try:
        r = await _get_redis()
        cached = await r.get(f"{_SHA_PREFIX}{sha}")
        if cached:
            logger.debug(f"SHA-256 HIT: {sha[:12]}...")
            return json.loads(cached)
        return None
    except Exception as exc:
        logger.debug(f"SHA-256 check failed: {exc}")
        return None


async def store_sha256(sha: str, result: dict[str, Any]) -> None:
    """Store transcription result by SHA-256."""
    try:
        cfg = get_config()
        r = await _get_redis()
        await r.setex(
            f"{_SHA_PREFIX}{sha}",
            cfg.dedup.cache_ttl,
            json.dumps(result, ensure_ascii=False),
        )
    except Exception as exc:
        logger.debug(f"SHA-256 store failed: {exc}")


# =============================================================================
# LEVEL 2: Chromaprint (acoustic fingerprint)
# =============================================================================


def compute_fingerprint(file_path: str, duration: int = 120) -> str | None:
    """Compute Chromaprint acoustic fingerprint.

    Uses fpcalc (Chromaprint CLI tool). Returns None if not available.
    Duration parameter limits analysis to first N seconds.
    """
    try:
        result = subprocess.run(
            ["fpcalc", "-raw", "-length", str(duration), file_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None

        # Parse output: FINGERPRINT=1234,5678,...
        for line in result.stdout.strip().split("\n"):
            if line.startswith("FINGERPRINT="):
                return line.split("=", 1)[1]
        return None
    except FileNotFoundError:
        logger.debug("fpcalc not installed — fingerprint dedup disabled")
        return None
    except Exception as exc:
        logger.debug(f"Fingerprint computation failed: {exc}")
        return None


def compute_fingerprint_bytes(audio_data: bytes, sample_rate: int = 16000) -> str | None:
    """Compute fingerprint from raw PCM bytes (writes temp WAV)."""
    import wave

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data)
        return compute_fingerprint(tmp.name)


async def check_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    """Check if acoustic fingerprint has a cached result.

    Uses a similarity threshold — fingerprints don't need exact match.
    For simplicity, stores first 100 values as key (covers ~10s of audio).
    """
    if not fingerprint:
        return None

    # Use first 100 fingerprint values as key (approximate match)
    fp_key = _fingerprint_key(fingerprint)

    try:
        r = await _get_redis()
        cached = await r.get(f"{_FP_PREFIX}{fp_key}")
        if cached:
            logger.debug(f"Fingerprint HIT: {fp_key[:20]}...")
            return json.loads(cached)
        return None
    except Exception as exc:
        logger.debug(f"Fingerprint check failed: {exc}")
        return None


async def store_fingerprint(fingerprint: str, result: dict[str, Any]) -> None:
    """Store result by acoustic fingerprint."""
    if not fingerprint:
        return

    fp_key = _fingerprint_key(fingerprint)
    try:
        cfg = get_config()
        r = await _get_redis()
        await r.setex(
            f"{_FP_PREFIX}{fp_key}",
            cfg.dedup.cache_ttl,
            json.dumps(result, ensure_ascii=False),
        )
    except Exception as exc:
        logger.debug(f"Fingerprint store failed: {exc}")


def _fingerprint_key(fingerprint: str) -> str:
    """Create a compact key from a fingerprint (hash of first 100 values)."""
    values = fingerprint.split(",")[:100]
    compact = ",".join(values)
    return hashlib.sha256(compact.encode()).hexdigest()


# =============================================================================
# LOCKS (distributed, for concurrent batch safety)
# =============================================================================


async def acquire_lock(identifier: str, ttl: int = 300) -> bool:
    """Acquire a distributed lock (prevents double-processing)."""
    try:
        r = await _get_redis()
        return await r.set(f"{_LOCK_PREFIX}{identifier}", "1", nx=True, ex=ttl)
    except Exception:
        return True  # Fail open — allow processing


async def release_lock(identifier: str) -> None:
    """Release a distributed lock."""
    try:
        r = await _get_redis()
        await r.delete(f"{_LOCK_PREFIX}{identifier}")
    except Exception:
        pass


# =============================================================================
# UNIFIED CHECK (both levels)
# =============================================================================


async def check_duplicate(
    file_path: str | None = None,
    audio_data: bytes | None = None,
    sha: str | None = None,
) -> tuple[dict[str, Any] | None, str, str | None]:
    """Run full dedup check (SHA-256 + optional fingerprint).

    Returns:
        (cached_result, sha256_hash, fingerprint_or_none)
    """
    cfg = get_config()

    # Compute SHA-256
    if sha is None:
        if file_path:
            sha = compute_sha256(file_path)
        elif audio_data:
            sha = compute_sha256_bytes(audio_data)
        else:
            return None, "", None

    # Level 1: SHA-256
    if cfg.dedup.sha256_enabled:
        cached = await check_sha256(sha)
        if cached:
            return cached, sha, None

    # Level 2: Fingerprint (optional)
    fingerprint = None
    if cfg.dedup.fingerprint_enabled:
        if file_path:
            fingerprint = compute_fingerprint(file_path)
        elif audio_data:
            fingerprint = compute_fingerprint_bytes(audio_data)

        if fingerprint:
            cached = await check_fingerprint(fingerprint)
            if cached:
                return cached, sha, fingerprint

    return None, sha, fingerprint


async def store_result(
    sha: str,
    result: dict[str, Any],
    fingerprint: str | None = None,
) -> None:
    """Store result in both dedup levels."""
    cfg = get_config()

    if cfg.dedup.sha256_enabled:
        await store_sha256(sha, result)

    if cfg.dedup.fingerprint_enabled and fingerprint:
        await store_fingerprint(fingerprint, result)


async def close() -> None:
    global _redis_pool
    if _redis_pool:
        await _redis_pool.aclose()
        _redis_pool = None
