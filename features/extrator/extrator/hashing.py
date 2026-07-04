"""Stable hashing helpers for extrator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path, *, block_size: int) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def hash_json(value: Any) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str))


def stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}:{hash_json(parts)[:24]}"
