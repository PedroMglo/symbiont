"""Shared secret detection patterns — used by both the active scanner and the persistence redactor."""

from __future__ import annotations

import re

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key_assignment", re.compile(r"(?i)(api[_-]?key|apikey|token|secret|password|auth|bearer)\s*[:=]\s*['\"]?\S{8,}")),
    ("openai_key", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("github_pat", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("github_server", re.compile(r"ghs_[a-zA-Z0-9]{36}")),
    ("github_oauth", re.compile(r"gho_[a-zA-Z0-9]{36}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("jwt", re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+")),
    ("pem_private_key", re.compile(r"-----BEGIN\s+(?:RSA|EC|DSA|OPENSSH)?\s*PRIVATE\s+KEY-----")),
    ("hex_token_long", re.compile(r"\b[a-f0-9]{40,64}\b")),
]
