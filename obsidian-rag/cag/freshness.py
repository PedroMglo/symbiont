"""Freshness validation for CAG packs.

Ensures packs are only injected when their source material
has not changed and TTL has not expired.
"""

from __future__ import annotations

import hashlib
import logging
import time

from cag.store import Pack

log = logging.getLogger(__name__)


def validate_pack(
    pack: Pack,
    *,
    current_source_hash: str = "",
    current_config_version: str = "",
) -> bool:
    """Check if a pack is still fresh and valid.

    Checks in order:
      1. TTL expired?
      2. Source hash changed? (if provided)
      3. Config version changed? (if provided)
    """
    now = time.time()

    if now >= pack.expires_at:
        log.debug("CAG: pack %s/%s expired (age=%.0fs, ttl=%ds)",
                  pack.pack_type, pack.scope,
                  now - pack.created_at, pack.ttl_seconds)
        return False

    if current_source_hash and pack.source_hash != current_source_hash:
        log.debug("CAG: pack %s/%s source hash changed (%s → %s)",
                  pack.pack_type, pack.scope,
                  pack.source_hash[:8], current_source_hash[:8])
        return False

    if current_config_version and pack.config_version != current_config_version:
        log.debug("CAG: pack %s/%s config version changed (%s → %s)",
                  pack.pack_type, pack.scope,
                  pack.config_version[:8], current_config_version[:8])
        return False

    return True


def compute_source_hash(*parts: str) -> str:
    """Hash multiple source strings into a single source fingerprint."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
    return h.hexdigest()[:16]


def compute_config_version(config_dict: dict) -> str:
    """Hash a config dict to detect config changes.

    Uses sorted JSON serialization for deterministic output.
    """
    import json
    serialized = json.dumps(config_dict, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
