"""Deterministic pre-creation of a subtask's parent directories inside its worktree (T28).

Git does not track empty directories, so a fresh worktree lacks the directories a new file will
live in, and some providers' file tools cannot create parents ("Parent directory does not
exist"). Instead of giving a worker a shell for ``mkdir``, the orchestrator creates, before an
implement run starts, the directories implied by the subtask's ``files/area`` cell: the parent of
a file path, the directory itself for an area ending in ``/``, and for a glob the literal
directory prefix before the first wildcard component.

Only paths strictly inside the worktree are accepted. Absolute paths, drive letters, UNC and
``\\?\\`` prefixes, ``..`` components, ``.git`` components, reserved Windows names and any existing
component that is a symbolic link or junction are refused as a whole (nothing is created), so a
plan cannot make the orchestrator write outside the worktree. :func:`plan_directories` writes
nothing; :func:`create_directories` is the only function that does.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

_WILDCARD = re.compile(r"[*?\[\]{}]")
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
_ANNOTATION = re.compile(r"\s+\([^)]*\)\s*$")
_SPLIT = re.compile(r"[,;]")
_NOT_DECLARED = {"", "-", "tbd", "n/a", "none"}


class PathRefused(ValueError):
    """A files/area entry cannot be pre-created safely."""


@dataclass
class DirectoryPlan:
    to_create: list[str] = field(default_factory=list)
    """Worktree-relative directories that do not exist yet, parents before children."""
    existing: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    """Entries that imply no directory (bare file name in the root, undeclared)."""


def _is_link(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)  # reparse point


def _entries(files_cell: str) -> list[str]:
    return [item for item in (_ANNOTATION.sub("", part.strip()) for part in _SPLIT.split(files_cell)) if item]


def _directory_of(entry: str) -> tuple[str, ...] | None:
    """Components of the directory implied by ``entry``; raises PathRefused for unsafe entries."""
    text = entry.strip().strip("`")
    if text.lower() in _NOT_DECLARED:
        return None
    if text.startswith(("\\\\", "//")) or re.match(r"^[A-Za-z]:", text) or text.startswith(("/", "\\")):
        raise PathRefused(f"{entry!r}: absolute, drive-letter and UNC paths are not allowed")
    if "\0" in text or ":" in text:
        raise PathRefused(f"{entry!r}: contains a character that is not allowed in a path")
    is_area = text.endswith(("/", "\\"))
    parts = [p for p in re.split(r"[\\/]+", text) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathRefused(f"{entry!r}: '..' components are not allowed")
    if any(p.lower() == ".git" for p in parts):
        raise PathRefused(f"{entry!r}: .git is never touched")
    if any(_RESERVED.match(p) or p != p.rstrip(" .") for p in parts):
        raise PathRefused(f"{entry!r}: a component is a reserved Windows name or ends with a space or dot")
    literal: list[str] = []
    for index, part in enumerate(parts):
        if _WILDCARD.search(part):
            return tuple(literal)  # the literal prefix before the first wildcard is the directory
        literal.append(part)
    return tuple(literal) if is_area else tuple(literal[:-1])


def plan_directories(worktree: Path, files_cell: str) -> DirectoryPlan:
    """Which directories would be created for ``files_cell``. Writes nothing.

    Raises :class:`PathRefused` for the first unsafe entry.
    """
    root = Path(os.path.realpath(worktree))
    result = DirectoryPlan()
    seen: set[tuple[str, ...]] = set()
    for entry in _entries(files_cell):
        parts = _directory_of(entry)
        if not parts:
            result.skipped.append(entry)
            continue
        current = root
        for depth, part in enumerate(parts, 1):
            current = current / part
            key = tuple(p.lower() for p in parts[:depth])
            if _is_link(current):
                raise PathRefused(f"{entry!r}: {current} is a symbolic link or junction")
            if current.exists() and not current.is_dir():
                raise PathRefused(f"{entry!r}: {current} exists and is not a directory")
            relative = "/".join(parts[:depth])
            if key in seen:
                continue
            seen.add(key)
            (result.existing if current.is_dir() else result.to_create).append(relative)
        resolved = Path(os.path.realpath(root.joinpath(*parts)))
        try:
            resolved.relative_to(root)
        except ValueError:
            raise PathRefused(f"{entry!r}: resolves outside the worktree ({resolved})") from None
    return result


def create_directories(worktree: Path, files_cell: str) -> list[str]:
    """Create the planned directories (all or none: the whole cell is validated first).
    Returns the worktree-relative directories created."""
    plan = plan_directories(worktree, files_cell)
    root = Path(os.path.realpath(worktree))
    created: list[str] = []
    for relative in plan.to_create:
        target = root.joinpath(*relative.split("/"))
        if _is_link(target):  # re-check right before writing (a race would be a link appearing)
            raise PathRefused(f"{relative}: became a symbolic link or junction")
        target.mkdir(exist_ok=True)
        created.append(relative)
    return created
