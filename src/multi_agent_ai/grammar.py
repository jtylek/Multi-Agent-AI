"""Value grammar shared by all coordination files (formats.md section 1, D6, D7)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_POSITIVE_INT_RE = re.compile(r"[1-9][0-9]*")
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:(?P<sep>[ T])\d{2}:\d{2}(?::\d{2})?)?")

STALE_AFTER = timedelta(hours=3)
""""About three hours" without an update makes a worker or registry entry stale."""


def is_valid_id(value: str) -> bool:
    """Message, task, subtask and agent ids are path components (D6)."""
    return bool(_ID_RE.fullmatch(value)) and ".." not in value


def id_error(value: str, what: str) -> str | None:
    if is_valid_id(value):
        return None
    return f"{what} {value!r} is not a valid id (^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$, no '..')"


def parse_positive_int(value: str) -> int | None:
    """Return the integer for a strictly positive decimal, else None (rejects v2, 0, 2.5)."""
    return int(value) if _POSITIVE_INT_RE.fullmatch(value) else None


def is_sha(value: str) -> bool:
    return bool(_SHA_RE.fullmatch(value))


@dataclass(frozen=True)
class Timestamp:
    """A local-time timestamp (D7). ``minute_precision`` is False for date-only values."""

    value: datetime
    minute_precision: bool


def parse_timestamp(value: str) -> Timestamp:
    """Accept ``YYYY-MM-DD HH:MM``, ``YYYY-MM-DD`` and ``YYYY-MM-DDTHH:MM[:SS]``.

    Offset-qualified and UTC values are a future format-2 change and are rejected.
    """
    match = _TIMESTAMP_RE.fullmatch(value)
    if not match or (match.group("sep") == " " and value.count(":") > 1):
        raise ValueError(
            f"timestamp {value!r} is not YYYY-MM-DD HH:MM, YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS]"
        )
    return Timestamp(datetime.fromisoformat(value), match.group("sep") is not None)


def format_timestamp(moment: datetime | None = None) -> str:
    """Writers use local machine time from the system clock, minute precision (D7)."""
    return (moment or datetime.now()).strftime("%Y-%m-%d %H:%M")


def is_stale(timestamp: Timestamp, now: datetime) -> bool | None:
    """True when older than about three hours; None when not evaluable (date-only)."""
    if not timestamp.minute_precision:
        return None
    return now - timestamp.value > STALE_AFTER
