"""Provider usage-limit detection and reset-time parsing (T15).

Positive identification only (plan.md "T15"): a generic nonzero exit, a bare HTTP 429, a missing
usage file or a timeout is never quota evidence. Detectors are pattern tables per provider; a
table lists the CLI versions it was written against (``None`` = synthetic/unversioned fixture
until a real sample exists). An unknown version falls back to ``provider_error`` with the raw
output, never to a guessed quota verdict. Nothing here starts a process or waits.

Reset times are only what the provider reports: epoch fields, or a wall-clock time plus a zone
name without a date. A time earlier than now means the next day; any parse failure (including a
zone this Python has no tz database for, which is common on Windows without ``tzdata``) means
``reset unknown``. A reset is never guessed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

QUOTA_EXHAUSTED = "quota_exhausted"
CAP_REACHED = "cap_reached"
PROVIDER_ERROR = "provider_error"

UNKNOWN_RESET_HOLD = timedelta(hours=24)
"""How long a quota block with an unknown reset suspends the stale-worker rule."""


@dataclass(frozen=True)
class LimitFinding:
    kind: str
    """``quota_exhausted``, ``cap_reached`` or ``provider_error``."""
    provider: str
    raw_message: str
    """Verbatim evidence; never rewritten."""
    reset: datetime | None = None
    """Timezone-aware instant, only when the provider reported one."""
    reset_zone: str | None = None
    """Display zone for ``reset`` (an IANA name, or ``UTC`` for epoch fields)."""
    reason: str = ""

    @property
    def reset_text(self) -> str:
        if self.reset is None:
            return "reset unknown"
        return f"resets {self.reset.strftime('%Y-%m-%d %H:%M')} {self.reset_zone or 'UTC'} (reported)"


def _one_line(text: str, limit: int = 300) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


# --- reset parsing ----------------------------------------------------------------------------

_WALL_CLOCK = re.compile(
    r"resets?(?:\s+at)?\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]m)?\s*\(?(?P<zone>[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)*|UTC)\)?",
    re.I,
)


def _default_zone(name: str) -> tzinfo | None:
    if name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError, ValueError: no tz database (Windows without tzdata)
        return None


def parse_wall_clock(
    text: str, now: datetime, zone_lookup: Callable[[str], tzinfo | None] = _default_zone,
) -> tuple[datetime, str] | None:
    """``resets 9:10pm (Europe/Budapest)`` -> (aware instant, zone name), or None.

    ``now`` must be timezone-aware. A time earlier than now in that zone means the next day.
    """
    match = _WALL_CLOCK.search(text)
    if not match:
        return None
    hour, minute = int(match["h"]), int(match["m"] or 0)
    meridiem = (match["ap"] or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    zone = zone_lookup(match["zone"])
    if zone is None:
        return None
    local_now = now.astimezone(zone)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate, match["zone"]


def from_epoch(value: Any) -> tuple[datetime, str] | None:
    """A reported epoch (seconds, or milliseconds when implausibly large) as an aware UTC instant."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = value / 1000 if value > 1e11 else value
    try:
        return datetime.fromtimestamp(seconds, timezone.utc), "UTC"
    except (OverflowError, OSError, ValueError):
        return None


# --- detectors --------------------------------------------------------------------------------


def _json_events(stdout: str) -> Iterator[dict[str, Any]]:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _codex(stdout: str, stderr: str, now: datetime, zone_lookup) -> LimitFinding | None:
    """Structured events only: ``rate_limits.rate_limit_reached_type`` or ``spend_control_reached``.

    A reset is read from the named window's ``resets_at`` epoch when the event names one.
    """
    for event in _json_events(stdout):
        for node in _walk(event):
            limits = node.get("rate_limits") if isinstance(node.get("rate_limits"), dict) else None
            if limits is None:
                continue
            reached = limits.get("rate_limit_reached_type")
            spend = limits.get("spend_control_reached")
            if not reached and not spend:
                continue
            window = limits.get(reached) if isinstance(reached, str) else None
            reset = from_epoch(window.get("resets_at")) if isinstance(window, dict) else None
            reason = f"rate_limit_reached_type={reached}" if reached else "spend_control_reached"
            return LimitFinding(
                QUOTA_EXHAUSTED, "codex", _one_line(json.dumps(limits, sort_keys=True)), *(reset or (None, None)),
                reason=reason,
            )
    return None


def _claude(stdout: str, stderr: str, now: datetime, zone_lookup) -> LimitFinding | None:
    text = f"{stderr}\n{stdout}"
    # error type rate_limit AND HTTP 429 AND a limit message with a reset: all three, or nothing.
    if re.search(r"rate_limit", text) and re.search(r"\b429\b", text) and re.search(r"limit[^\n]{0,80}resets?", text, re.I):
        parsed = parse_wall_clock(text, now, zone_lookup)
        message = re.search(r"[^\n]*limit[^\n]{0,80}resets?[^\n]*", text, re.I)
        return LimitFinding(QUOTA_EXHAUSTED, "claude", _one_line(message[0] if message else text), *(parsed or (None, None)),
                            reason="rate_limit + 429 + limit message")
    return None


_COPILOT_CAP = re.compile(r"max-ai-credits|ai credits? (limit|cap)[^\n]{0,40}(reached|exceeded)", re.I)
_COPILOT_QUOTA = re.compile(
    r"(premium requests?|monthly|usage) (quota|limit)[^\n]{0,40}(exceeded|reached|exhausted)"
    r"|you(?:'ve| have) (reached|exceeded) (your|the) [^\n]{0,40}(quota|limit)|quota (exceeded|exhausted)", re.I)


def _copilot(stdout: str, stderr: str, now: datetime, zone_lookup) -> LimitFinding | None:
    text = f"{stderr}\n{stdout}"
    cap = _COPILOT_CAP.search(text)
    if cap:
        return LimitFinding(CAP_REACHED, "copilot", _one_line(_line_of(text, cap.start())), reason="local credit cap")
    quota = _COPILOT_QUOTA.search(text)
    if quota:
        parsed = parse_wall_clock(text, now, zone_lookup)
        return LimitFinding(QUOTA_EXHAUSTED, "copilot", _one_line(_line_of(text, quota.start())),
                            *(parsed or (None, None)), reason="provider quota message")
    return None


def _line_of(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return text[start:] if end < 0 else text[start:end]


_PROVIDER_ERROR = re.compile(
    r"\b429\b|too many requests|rate[ _-]?limit(?:ed|\s+(?:exceeded|reached|hit|error))|throttl|api error|service unavailable|\b50[234]\b|overloaded", re.I)

Detector = Callable[[str, str, datetime, Callable[[str], "tzinfo | None"]], "LimitFinding | None"]

DETECTORS: dict[str, tuple[Detector, "frozenset[str] | None"]] = {
    # provider -> (detector, CLI versions it was written against; None = synthetic fixtures only)
    "codex": (_codex, None),
    "claude": (_claude, None),
    "copilot": (_copilot, None),
}
"""Add a real, versioned sample here (``frozenset({"x.y.z"})``) once one has been captured."""


def detect(
    provider: str, stdout: str, stderr: str, *, now: datetime | None = None, cli_version: str | None = None,
    zone_lookup: Callable[[str], tzinfo | None] = _default_zone,
) -> LimitFinding | None:
    """A positively identified limit, a ``provider_error`` for a throttle-like message, or None.

    A ``cli_version`` that the provider's table does not list yields ``provider_error`` carrying
    the raw output instead of a quota verdict.
    """
    now = now or datetime.now(timezone.utc)
    entry = DETECTORS.get(provider)
    if entry is not None:
        detector, versions = entry
        if versions is not None and cli_version is not None and cli_version not in versions:
            text = f"{stderr}\n{stdout}"
            if _PROVIDER_ERROR.search(text) or detector(stdout, stderr, now, zone_lookup):
                return LimitFinding(PROVIDER_ERROR, provider, _one_line(text, 500),
                                    reason=f"unknown {provider} CLI version {cli_version}")
            return None
        found = detector(stdout, stderr, now, zone_lookup)
        if found is not None:
            return found
    text = f"{stderr}\n{stdout}"
    match = _PROVIDER_ERROR.search(text)
    if match:
        return LimitFinding(PROVIDER_ERROR, provider, _one_line(_line_of(text, match.start()), 500),
                            reason="throttling or API error without a positive limit signature")
    return None


# --- status blocker text and the stale-worker suspension ----------------------------------------

_BLOCK = re.compile(r"^(?P<prefix>quota|cap):\s*(?P<provider>\S+)(?:\s*\((?P<model>[^)]*)\))?,\s*(?P<rest>.*)$")
_RESET_IN_BLOCKER = re.compile(r"resets\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})\s+(\S+)", re.I)


def blocker_text(finding: LimitFinding, model: str | None) -> str:
    """``quota: codex (gpt-5), resets 2026-10-19 21:10 UTC (reported)`` (formats.md section 5)."""
    prefix = "cap" if finding.kind == CAP_REACHED else "quota"
    return f"{prefix}: {finding.provider} ({model or 'provider default'}), {finding.reset_text}"


def quota_block_active(
    blockers: str, updated: datetime, now: datetime, zone_lookup: Callable[[str], tzinfo | None] = _default_zone,
) -> bool:
    """True while a ``quota:`` blocker still waits: the reported reset has not passed, or fewer than
    24 hours since ``updated`` when the reset is unknown. ``cap:`` blockers never suspend staleness.
    ``updated`` and ``now`` are naive local times, as in the status file."""
    match = _BLOCK.match(blockers.strip())
    if not match or match["prefix"] != "quota":
        return False
    reset = _RESET_IN_BLOCKER.search(match["rest"])
    if reset:
        zone = zone_lookup(reset[4])
        if zone is not None:
            try:
                due = datetime.fromisoformat(f"{reset[1]}T{reset[2]}:{reset[3]}").replace(tzinfo=zone)
            except ValueError:
                due = None
            if due is not None:
                return now.astimezone(zone) < due
    return now - updated < UNKNOWN_RESET_HOLD
