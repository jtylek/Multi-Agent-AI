"""Agent registry parsing and conservative file/area reservation checks.

Registry files are authoritative coordination documents under ``registry/``.  A stale
entry is still useful for presence, but its reservation is released after three hours.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .document import DocumentError, read_document
from .grammar import id_error, is_stale, parse_timestamp
from .models import Parsed

REGISTRY_DIR = "registry"
REGISTRY_SUFFIX = ".md"
REQUIRED_KEYS = ("agent", "started", "updated", "task", "files", "status")
INACTIVE_STATUSES = frozenset({"idle", "done", "finished", "failed", "stopped", "offline"})


@dataclass(frozen=True)
class RegistryEntry:
    agent: str
    started: str
    updated: str
    task: str
    files: str
    status: str
    contact: str | None = None
    extra: tuple[tuple[str, str], ...] = ()

    @property
    def file_items(self) -> tuple[str, ...]:
        return tuple(_normalise_item(item) for item in re.split(r"[,;]", self.files) if _normalise_item(item))


@dataclass(frozen=True)
class ReservationConflict:
    agent: str
    files: tuple[str, ...]
    shared: tuple[str, ...]
    stale: bool = False


class ReservationError(ValueError):
    """An assignment overlaps a live agent reservation."""


def _normalise_item(value: str) -> str:
    value = re.sub(r"\([^)]*\)", "", value).strip().replace("\\", "/").lower()
    value = value.removeprefix("./").rstrip("/")
    return value if value and value != "-" else ""


def _overlap(left: str, right: str) -> bool:
    if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
        return True
    # fnmatch gives useful conservative behaviour for the globs allowed by the protocol.
    candidates = {left, right, left.rstrip("/*"), right.rstrip("/*")}
    return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates for pattern in (left, right))


def _read(path: Path) -> Parsed[RegistryEntry]:
    try:
        doc = read_document(path)
    except (OSError, DocumentError) as exc:
        return Parsed(None, [str(exc)])
    errors: list[str] = []
    for key in REQUIRED_KEYS:
        if not doc.get(key):
            errors.append(f"missing required key '{key}'")
    agent = doc.get("agent") or ""
    if agent and (error := id_error(agent, "agent id")):
        errors.append(error)
    for key in ("started", "updated"):
        value = doc.get(key)
        if value:
            try:
                parse_timestamp(value)
            except ValueError as exc:
                errors.append(str(exc))
    if errors:
        return Parsed(None, errors)
    known = set(REQUIRED_KEYS) | {"contact"}
    extra = tuple((key, value) for key, value in doc.headers.items() if key not in known)
    return Parsed(RegistryEntry(agent, doc.headers["started"], doc.headers["updated"], doc.headers["task"],
                                doc.headers["files"], doc.headers["status"], doc.get("contact"), extra),
                  warnings=list(doc.warnings))


def read_registry(state_root: Path, *, now: datetime | None = None) -> dict[str, Parsed[RegistryEntry]]:
    """Read registry entries without writing; malformed entries remain visible as invalid."""
    root = Path(state_root) / REGISTRY_DIR
    result: dict[str, Parsed[RegistryEntry]] = {}
    for path in sorted(root.glob(f"*{REGISTRY_SUFFIX}")) if root.is_dir() else ():
        agent = path.stem
        parsed = _read(path)
        if parsed.value is not None and agent != parsed.value.agent:
            parsed.errors.append(f"registry filename agent {agent!r} does not match agent header {parsed.value.agent!r}")
            parsed.value = None
        result[agent] = parsed
    return result


def _is_active(entry: RegistryEntry, now: datetime) -> tuple[bool, bool]:
    try:
        stale = is_stale(parse_timestamp(entry.updated), now) is True
    except ValueError:
        stale = False
    return not stale and entry.status.strip().lower() not in INACTIVE_STATUSES, stale


def reservation_conflicts(state_root: Path, files: str | list[str] | tuple[str, ...], *,
                          agent: str | None = None, now: datetime | None = None) -> list[ReservationConflict]:
    """Return active entries whose reservations overlap ``files``.

    ``agent`` excludes the caller's own entry, allowing a worker heartbeat to revalidate
    its reservation without conflicting with itself.
    """
    now = now or datetime.now()
    requested = tuple(_normalise_item(item) for item in (re.split(r"[,;]", files) if isinstance(files, str) else files))
    requested = tuple(item for item in requested if item)
    conflicts: list[ReservationConflict] = []
    for parsed in read_registry(state_root, now=now).values():
        entry = parsed.value
        if entry is None or entry.agent == agent:
            continue
        active, stale = _is_active(entry, now)
        if not active:
            continue
        shared = tuple(sorted({f"{left} <> {right}" for left in requested for right in entry.file_items if _overlap(left, right)}))
        if shared:
            conflicts.append(ReservationConflict(entry.agent, entry.file_items, shared, stale))
    return conflicts


def require_reservations(state_root: Path, files: str | list[str] | tuple[str, ...], *,
                         agent: str | None = None, now: datetime | None = None) -> None:
    conflicts = reservation_conflicts(state_root, files, agent=agent, now=now)
    if conflicts:
        detail = "; ".join(f"{item.agent}: {', '.join(item.shared)}" for item in conflicts)
        raise ReservationError(f"assignment refused: files/area overlaps active reservation ({detail})")
