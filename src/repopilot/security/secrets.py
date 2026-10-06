"""Bounded detection of credentials accidentally placed in task input."""

from __future__ import annotations

import re


MAX_SCAN_CHARS = 100_000

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE)),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b", re.IGNORECASE)),
)


def find_secret_kind(value: str) -> str | None:
    """Return a stable category without returning or logging the secret itself."""

    if len(value) > MAX_SCAN_CHARS:
        return "oversized_input"
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(value) is not None:
            return kind
    return None
