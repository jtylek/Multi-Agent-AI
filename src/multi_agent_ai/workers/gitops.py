"""Orchestrator-side Git checks and the attributable commit (the worker never commits).

Snapshots taken before and after a run compare:

* the worktree's HEAD commit, its branch, and that branch's tip (worktree-local, reliable);
* a fingerprint of ``git status --porcelain=v1 -z --untracked-files=all`` plus a SHA-256 of
  every listed path's content, so a further edit to an already-modified file is also seen.
  Ignored files (caches, build output) are not part of the fingerprint;
* all refs of the repository. Refs are shared by every worktree of a repository and other
  agents commit concurrently, so only a **new ref pointing at this worktree's pre-run HEAD**
  is treated as the worker's; other ref changes are recorded as observations, not failures.

Git is always run with an argv list, no shell, stdin closed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


def git_executable() -> str:
    found = shutil.which("git.exe") or shutil.which("git")
    if not found or Path(found).suffix.lower() not in (".exe", ""):
        raise GitError("git.exe not found on PATH")
    return found


def git(cwd: Path, *args: str, check: bool = True, git_exe: str | None = None) -> str:
    result = subprocess.run(
        [git_exe or git_executable(), *args], cwd=str(cwd), stdin=subprocess.DEVNULL,
        capture_output=True, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = result.stdout.decode("utf-8", "replace")
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({result.returncode}): "
                       f"{result.stderr.decode('utf-8', 'replace').strip()}")
    return out


def same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    top_level: Path
    head: str
    branch: str | None
    is_linked_worktree: bool
    """False for a repository's main working tree (the primary checkout)."""


def worktree_info(path: Path) -> WorktreeInfo:
    """Raises GitError when ``path`` is not the top level of a Git working tree."""
    if not path.is_dir():
        raise GitError(f"worktree {path} does not exist")
    top = Path(git(path, "rev-parse", "--show-toplevel").strip())
    if not same_path(top, path):
        raise GitError(f"{path} is not the top level of a Git working tree (top level: {top})")
    head = git(path, "rev-parse", "--verify", "HEAD").strip()
    branch = git(path, "symbolic-ref", "--short", "-q", "HEAD", check=False).strip() or None
    git_dir = git(path, "rev-parse", "--absolute-git-dir").strip()
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return WorktreeInfo(path, top, head, branch, not same_path(git_dir, common))


def _porcelain(path: Path) -> list[str]:
    raw = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries, items, i = [], raw.split("\0"), 0
    while i < len(items):
        item = items[i]
        if not item:
            i += 1
            continue
        entries.append(item)
        if item[:1] in ("R", "C") or item[1:2] in ("R", "C"):
            i += 1  # the rename/copy source follows as its own item
            if i < len(items):
                entries.append("<- " + items[i])
        i += 1
    return entries


def _digest(file: Path) -> str:
    try:
        if file.is_file():
            return hashlib.sha256(file.read_bytes()).hexdigest()
        return "missing" if not file.exists() else "not-a-file"
    except OSError as exc:
        return f"unreadable: {exc.__class__.__name__}"


@dataclass
class Snapshot:
    head: str
    branch: str | None
    branch_tip: str | None
    status: list[str]
    contents: dict[str, str]
    refs: dict[str, str]

    def changed_paths(self) -> list[str]:
        return sorted({entry[3:] for entry in self.status if not entry.startswith("<- ")})


def snapshot(path: Path, exclude: Path | None = None) -> Snapshot:
    """``exclude``: a directory inside the worktree to ignore (the state root, if it lives there)."""
    info = worktree_info(path)
    status = _porcelain(path)
    if exclude is not None:
        try:
            rel = exclude.resolve().relative_to(path.resolve()).as_posix().rstrip("/") + "/"
            status = [e for e in status if not e[3:].startswith(rel) and e[3:] != rel.rstrip("/")]
        except ValueError:
            pass
    contents = {e[3:]: _digest(path / e[3:]) for e in status if not e.startswith("<- ")}
    refs = {}
    for line in git(path, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
        name, _, sha = line.partition(" ")
        refs[name] = sha
    tip = refs.get(f"refs/heads/{info.branch}") if info.branch else None
    return Snapshot(info.head, info.branch, tip, status, contents, refs)


@dataclass
class Comparison:
    worktree_changed: bool
    git_violations: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)


def compare(before: Snapshot, after: Snapshot) -> Comparison:
    violations: list[str] = []
    if after.head != before.head:
        violations.append(f"HEAD moved from {before.head} to {after.head}")
    if after.branch != before.branch:
        violations.append(f"branch changed from {before.branch} to {after.branch}")
    if after.branch_tip != before.branch_tip and before.branch is not None and after.branch == before.branch:
        violations.append(f"branch {before.branch} tip moved from {before.branch_tip} to {after.branch_tip}")
    observations: list[str] = []
    for name in sorted(set(before.refs) | set(after.refs)):
        old, new = before.refs.get(name), after.refs.get(name)
        if old == new or (before.branch and name == f"refs/heads/{before.branch}"):
            continue
        if old is None and new == before.head:
            violations.append(f"new ref {name} points at the worktree's HEAD {new}")
        else:
            observations.append(f"ref {name}: {old or '(none)'} -> {new or '(deleted)'} (not attributed)")
    changed = before.status != after.status or before.contents != after.contents
    paths = sorted(set(after.changed_paths()) ^ set(before.changed_paths())
                   | {p for p in after.contents if before.contents.get(p) != after.contents[p]})
    return Comparison(changed or bool(violations), violations, observations, paths)


def commit_all(path: Path, message_file: Path, expected_parent: str, exclude: Path | None = None) -> str:
    """Stage everything in the worktree and commit with the message in ``message_file``.

    ``exclude``: a directory inside the worktree that is never staged (the state root, when it
    lives there and is not git-ignored: its logs and prompts must not enter the task branch).
    Hooks run (never ``--no-verify``). Returns the new commit; raises GitError if the commit
    fails or its parent is not ``expected_parent``. Never merges or pushes.
    """
    pathspec = ["."]
    if exclude is not None:
        try:
            rel = exclude.resolve().relative_to(path.resolve()).as_posix().rstrip("/")
            if rel and rel != ".":
                pathspec.append(f":(exclude){rel}")
        except ValueError:
            pass
    git(path, "add", "-A", "--", *pathspec)
    git(path, "commit", "-q", "-F", str(message_file))
    head = git(path, "rev-parse", "--verify", "HEAD").strip()
    parent = git(path, "rev-parse", "--verify", "HEAD^").strip()
    if parent != expected_parent:
        raise GitError(f"new commit {head} has parent {parent}, expected {expected_parent}")
    return head
