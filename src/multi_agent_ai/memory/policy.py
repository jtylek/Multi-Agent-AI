"""Conservative validation for local memory ingestion.

Not a complete secret detector or OS security boundary. Untrusted agents must not
be given direct write access to the memory database.
"""
from __future__ import annotations

import re

_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def validate_content(content: str) -> None:
    if not content.strip() or len(content) > 20000:
        raise ValueError("Memory must contain 1..20000 characters")
    if any(pattern.search(content) for pattern in _PATTERNS):
        raise ValueError("Potential secret detected; memory ingestion refused")


def validate_source(source_type: str, source_ref: str) -> None:
    if not source_type.strip() or not source_ref.strip():
        raise ValueError("Source reference required")
    if len(source_ref) > 2048:
        raise ValueError("Source reference too long")
