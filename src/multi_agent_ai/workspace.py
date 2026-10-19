"""Worktree lifecycle manager (T23): integration branch, one branch and worktree per editing
subtask, ordered integration of reviewed commits, safe removal, retained failed worktrees.

Git is the source of truth; ``tasks/<task-id>/workspaces.json`` is a derived record written only
here. Conventions (decisions.md D16): branches ``task/<task-id>/integration`` and
``task/<task-id>/<subtask-id>``; worktrees at ``<worktrees-root>/<project>-<subtask-id>`` outside
the project and state root (default root: ``..\\wt`` next to the project). The module never pushes,
never touches the primary checkout, never deletes a branch (not even to undo a failed call),
and uses ``--force`` only to remove a half-made worktree of its own after a failed create.
Worktrees are edit isolation, not a security boundary.

Every mutating function takes ``execute``; with ``execute=False`` it performs every check and
returns what it would do, writing nothing.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .board import read_plan
from .document import DocumentError, atomic_write, read_document
from .grammar import format_timestamp, id_error
from .state import StateRoot
from .workers import gitops

DEFAULT_PROTECTED = ("main", "master")
INTEGRATION = "integration"
RECORD_FILE = "workspaces.json"
LOCK_FILE = "workspaces.lock"
MAX_PATH = 240
STALE_LOCK_MINUTES = 10

CODES = (
    "bad_base", "protected_branch", "branch_exists", "worktree_exists", "path_outside_policy",
    "dirty_worktree", "not_a_worktree", "unreviewed", "dependency_unmet", "merge_conflict",
    "primary_changed", "git_failed", "unsafe_remove", "plan_mismatch", "locked",
)


class WorkspaceError(Exception):
    """A refusal or failure with a code from :data:`CODES`."""

    def __init__(self, code: str, message: str, details: Sequence[str] = ()):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.details = code, message, tuple(details)


@dataclass
class WorktreeRecord:
    subtask: str
    branch: str
    path: str
    base: str
    base_from: str
    """``plan`` (the plan's base commit), ``integration`` (integration tip) or ``caller``."""
    state: str = "created"
    """``created``, ``in use``, ``retained``, ``merged`` or ``removed``."""
    created: str = ""
    reused: bool = False
    note: str = ""
    merge_commit: str | None = None


@dataclass
class CreateResult:
    record: WorktreeRecord
    executed: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class WorktreeStatus:
    subtask: str
    branch: str | None
    path: str
    head: str | None
    clean: bool
    dirty: list[str] = field(default_factory=list)
    linked: bool = True
    locked: bool = False
    prunable: bool = False
    ahead_of_base: int | None = None
    record_state: str | None = None
    problems: list[str] = field(default_factory=list)


@dataclass
class IntegrationItem:
    subtask: str
    commit: str | None = None
    result: str = "not_attempted"
    """``merged``, ``already_merged``, ``would_merge``, ``refused``, ``conflict`` or ``not_attempted``."""
    code: str | None = None
    detail: str = ""
    merge_commit: str | None = None
    conflicts: list[str] = field(default_factory=list)


@dataclass
class IntegrationReport:
    task: str
    executed: bool
    items: list[IntegrationItem]
    tip: str | None = None

    @property
    def ok(self) -> bool:
        return all(i.result in ("merged", "already_merged", "would_merge") for i in self.items)


@dataclass
class RemovalResult:
    subtask: str
    path: str
    executed: bool
    state: str


# --- paths, names, records ---------------------------------------------------------------------


def branch_name(task_id: str, subtask_id: str) -> str:
    for value, what in ((task_id, "task id"), (subtask_id, "subtask id")):
        if error := id_error(value, what):
            raise WorkspaceError("path_outside_policy", error)
    return f"task/{task_id}/{subtask_id}"


def default_worktrees_root(project_root: Path) -> Path:
    return project_root.resolve().parent / "wt"


def worktree_path(project_root: Path, worktrees_root: Path, subtask_id: str) -> Path:
    return Path(worktrees_root) / f"{project_root.resolve().name}-{subtask_id}"


def _inside(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.realpath(child)).relative_to(os.path.realpath(parent))
        return True
    except ValueError:
        return False


def _check_path(path: Path, project_root: Path, state_root: Path) -> None:
    text = str(path)
    if text.startswith("\\\\?\\"):
        raise WorkspaceError("path_outside_policy", f"{text}: extended-length paths are not accepted")
    if not path.is_absolute():
        raise WorkspaceError("path_outside_policy", f"{text}: the worktree path must be absolute")
    if len(text) >= MAX_PATH:
        raise WorkspaceError("path_outside_policy", f"{text}: {len(text)} characters; the limit is {MAX_PATH}")
    if _inside(path, project_root) or _inside(path, state_root):
        raise WorkspaceError("path_outside_policy", f"{text}: must be outside the project and the state root")
    if path.exists():
        raise WorkspaceError("worktree_exists", f"{text} already exists")


def _record_path(state: StateRoot, task_id: str) -> Path:
    return state.tasks / task_id / RECORD_FILE


def read_records(state: StateRoot, task_id: str) -> dict[str, WorktreeRecord]:
    path = _record_path(state, task_id)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: WorktreeRecord(**v) for k, v in raw.get("worktrees", {}).items()}
    except (OSError, ValueError, TypeError) as exc:
        raise WorkspaceError("git_failed", f"{path} is unreadable: {exc}") from exc


def _write_records(state: StateRoot, task_id: str, records: dict[str, WorktreeRecord]) -> None:
    payload = {"task": task_id, "format": 1, "worktrees": {k: asdict(v) for k, v in sorted(records.items())}}
    atomic_write(_record_path(state, task_id), json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


class _Lock:
    """``workspaces.lock`` created with O_EXCL; a stale lock is reported, never deleted."""

    def __init__(self, state: StateRoot, task_id: str, enabled: bool):
        self.path = state.tasks / task_id / LOCK_FILE
        self.enabled = enabled
        self.held = False

    def __enter__(self) -> "_Lock":
        if not self.enabled:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = (datetime.now().timestamp() - self.path.stat().st_mtime) / 60
            stale = f" (stale: {age:.0f} min old; delete it yourself if no workspace command is running)" \
                if age > STALE_LOCK_MINUTES else ""
            raise WorkspaceError("locked", f"another workspace operation is running: {self.path}{stale}") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid {os.getpid()} {format_timestamp()}\n")
        self.held = True
        return self

    def __exit__(self, *exc) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


# --- Git helpers ---------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    try:
        return gitops.git(cwd, *args)
    except gitops.GitError as exc:
        raise WorkspaceError("git_failed", str(exc)) from exc


def _branch_tip(project_root: Path, branch: str) -> str | None:
    out = gitops.git(project_root, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}").strip()
    return out or None


def _contains(project_root: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant``."""
    return gitops.git(project_root, "rev-list", "--count", f"{descendant}..{ancestor}").strip() == "0"


def _valid_branch(project_root: Path, name: str) -> None:
    try:
        gitops.git(project_root, "check-ref-format", "--branch", name)
    except gitops.GitError as exc:
        raise WorkspaceError("path_outside_policy", f"{name!r} is not a valid branch name") from exc


def _worktree_list(project_root: Path) -> list[dict[str, str]]:
    blocks, current = [], {}
    for line in _git(project_root, "worktree", "list", "--porcelain").splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        blocks.append(current)
    return blocks


def _primary_state(project_root: Path) -> tuple[str, str | None]:
    info = gitops.worktree_info(project_root)
    return info.head, info.branch


def validate_base(project_root: Path, base_commit: str, protected: Sequence[str] = DEFAULT_PROTECTED) -> tuple[str, list[str]]:
    """Full SHA of ``base_commit`` plus warnings. A base not reachable from a protected branch is
    allowed (a task may start from an unmerged branch) but warned about."""
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", base_commit or ""):
        raise WorkspaceError("bad_base", f"{base_commit!r} is not a commit SHA")
    try:
        sha = gitops.git(project_root, "rev-parse", "--verify", f"{base_commit}^{{commit}}").strip()
    except gitops.GitError as exc:
        raise WorkspaceError("bad_base", f"{base_commit} is not a commit in {project_root}") from exc
    warnings = []
    reachable = [b for b in protected if _branch_tip(project_root, b) and _contains(project_root, sha, b)]
    if not reachable:
        warnings.append(f"base {sha[:7]} is not reachable from {', '.join(protected)} (task starts from unmerged work)")
    return sha, warnings


def _check_name_not_protected(name: str, protected: Sequence[str], project_root: Path) -> None:
    _, primary_branch = _primary_state(project_root)
    if name in protected or name == primary_branch:
        raise WorkspaceError("protected_branch", f"branch {name!r} is protected or checked out in the primary checkout")


def _plan(state: StateRoot, task_id: str):
    parsed = read_plan(state.tasks / task_id)
    if parsed.value is None:
        raise WorkspaceError("plan_mismatch", f"plan for task {task_id} is not usable: {'; '.join(parsed.errors) or 'missing'}")
    plan = parsed.value
    if plan.integration_branch and plan.integration_branch != f"task/{task_id}/{INTEGRATION}":
        raise WorkspaceError("plan_mismatch", f"plan names integration branch {plan.integration_branch!r}; "
                                              f"this manager uses task/{task_id}/{INTEGRATION}")
    return plan


# --- creation ----------------------------------------------------------------------------------


def _create(project_root: Path, state: StateRoot, task_id: str, name: str, branch: str, path: Path, base: str,
            base_from: str, protected: Sequence[str], execute: bool, allow_reuse: bool) -> CreateResult:
    project_root = project_root.resolve()
    _valid_branch(project_root, branch)
    _check_name_not_protected(branch, protected, project_root)
    sha, warnings = validate_base(project_root, base, protected)
    existing = _branch_tip(project_root, branch)
    reused = False
    if existing:
        listed = [b for b in _worktree_list(project_root) if b.get("branch") == f"refs/heads/{branch}"]
        if allow_reuse and existing == sha and not listed:
            reused = True  # leftover of an earlier failed attempt, or the branch was made by hand at the base
        else:
            where = "has a worktree" if listed else f"is at {existing[:7]}, not the expected base {sha[:7]}"
            raise WorkspaceError(
                "branch_exists", f"branch {branch} already exists and {where}. The manager never deletes branches; "
                f"if it is a leftover you do not need, run this yourself and retry: git -C \"{project_root}\" "
                f"branch -D {branch}")
    _check_path(path, project_root, state.path)
    record = WorktreeRecord(name, branch, str(path), sha, base_from, "created", format_timestamp(), reused)
    if not execute:
        return CreateResult(record, False, warnings)
    with _Lock(state, task_id, True):
        primary_before = _primary_state(project_root)
        records = read_records(state, task_id)
        args = ["worktree", "add", str(path), branch] if reused else ["worktree", "add", "-b", branch, str(path), sha]
        try:
            gitops.git(project_root, *args)
            info = gitops.worktree_info(path)
            problems = []
            if not info.is_linked_worktree:
                problems.append("not a linked worktree")
            if info.branch != branch:
                problems.append(f"on branch {info.branch!r}, expected {branch!r}")
            if info.head != sha:
                problems.append(f"HEAD {info.head[:7]} is not the base {sha[:7]}")
            if gitops.snapshot(path, state.path if _inside(state.path, path) else None).status:
                problems.append("worktree is not clean")
            if problems:
                raise gitops.GitError("; ".join(problems))
        except gitops.GitError as exc:
            _rollback(project_root, path)
            raise WorkspaceError("git_failed", f"could not create {path}: {exc}") from exc
        if _primary_state(project_root) != primary_before:
            raise WorkspaceError("primary_changed", "the primary checkout changed during the operation")
        records[name] = record
        _write_records(state, task_id, records)
    return CreateResult(record, True, warnings)


def _rollback(project_root: Path, path: Path) -> None:
    """Remove only the half-made worktree of a failed create. The branch is left alone (D18): a
    retry accepts an existing branch at the expected base that has no worktree."""
    try:
        if any(gitops.same_path(b.get("worktree", ""), path) for b in _worktree_list(project_root)):
            gitops.git(project_root, "worktree", "remove", "--force", str(path))
        gitops.git(project_root, "worktree", "prune")
    except gitops.GitError:
        pass


def create_integration(project_root: Path, state: StateRoot, task_id: str, base_commit: str | None = None,
                       worktrees_root: Path | None = None, protected: Sequence[str] = DEFAULT_PROTECTED,
                       execute: bool = False) -> CreateResult:
    plan = _plan(state, task_id)
    base = base_commit or plan.base_commit
    if not base:
        raise WorkspaceError("bad_base", "no base commit: pass --base or set base-commit in the plan")
    root = worktrees_root or default_worktrees_root(project_root)
    return _create(project_root, state, task_id, INTEGRATION, branch_name(task_id, INTEGRATION),
                   worktree_path(project_root, root, INTEGRATION), base,
                   "caller" if base_commit else "plan", protected, execute, allow_reuse=True)


def create_subtask_worktree(project_root: Path, state: StateRoot, task_id: str, subtask_id: str,
                            base_from: str = "auto", worktrees_root: Path | None = None,
                            protected: Sequence[str] = DEFAULT_PROTECTED, execute: bool = False) -> CreateResult:
    """``base_from``: ``auto`` (dependants from the integration tip, others from the plan's base),
    ``base`` or ``integration``."""
    if subtask_id == INTEGRATION:
        raise WorkspaceError("protected_branch", f"'{INTEGRATION}' is reserved for the integration branch")
    plan = _plan(state, task_id)
    subtask = plan.subtask(subtask_id)
    if subtask is None:
        raise WorkspaceError("plan_mismatch", f"subtask {subtask_id} is not a valid row in the plan of {task_id}")
    records = read_records(state, task_id)
    dependants = bool(subtask.depends_on)
    source = base_from if base_from != "auto" else ("integration" if dependants else "base")
    if dependants:
        unmet = [d for d in subtask.depends_on if records.get(d, WorktreeRecord(d, "", "", "", "")).state != "merged"]
        if unmet:
            raise WorkspaceError("dependency_unmet", f"{subtask_id} depends on {', '.join(unmet)}, not merged into the "
                                                     "integration branch yet")
    project_root = project_root.resolve()
    if source == "integration":
        tip = _branch_tip(project_root, branch_name(task_id, INTEGRATION))
        if not tip:
            raise WorkspaceError("bad_base", "the integration branch does not exist; create it first")
        base, label = tip, "integration"
    else:
        if not plan.base_commit:
            raise WorkspaceError("bad_base", "the plan has no base-commit")
        base, label = plan.base_commit, "plan"
    root = worktrees_root or default_worktrees_root(project_root)
    return _create(project_root, state, task_id, subtask_id, branch_name(task_id, subtask_id),
                   worktree_path(project_root, root, subtask_id), base, label, protected, execute, allow_reuse=True)


# --- inspection ----------------------------------------------------------------------------------


def check_worktree(project_root: Path, state: StateRoot, path: Path, subtask: str, expect_branch: str,
                   expect_base: str | None = None, record_state: str | None = None) -> WorktreeStatus:
    status = WorktreeStatus(subtask, None, str(path), None, False, record_state=record_state)
    listed = next((b for b in _worktree_list(project_root) if gitops.same_path(b.get("worktree", ""), path)), None)
    if listed is not None:
        status.locked, status.prunable = "locked" in listed, "prunable" in listed
    try:
        info = gitops.worktree_info(path)
        snap = gitops.snapshot(path, state.path if _inside(state.path, path) else None)
    except gitops.GitError as exc:
        status.problems.append(f"not_a_worktree: {exc}")
        return status
    status.branch, status.head, status.linked = info.branch, info.head, info.is_linked_worktree
    status.dirty = snap.status[:20]
    status.clean = not snap.status
    if not info.is_linked_worktree:
        status.problems.append("not_a_worktree: this is the primary checkout")
    if info.branch != expect_branch:
        status.problems.append(f"branch is {info.branch!r}, expected {expect_branch!r}")
    if status.dirty:
        status.problems.append(f"dirty_worktree: {len(snap.status)} entr{'y' if len(snap.status) == 1 else 'ies'}")
    if expect_base:
        try:
            status.ahead_of_base = int(gitops.git(path, "rev-list", "--count", f"{expect_base}..HEAD").strip())
        except gitops.GitError:
            status.problems.append(f"base {expect_base[:7]} is not reachable from HEAD")
        else:
            if not _contains(path, expect_base, "HEAD"):
                status.problems.append(f"base {expect_base[:7]} is not an ancestor of HEAD")
    return status


def list_worktrees(project_root: Path, state: StateRoot, task_id: str) -> list[WorktreeStatus]:
    """Read-only join of the record file and Git's worktree list; orphans are reported."""
    records = read_records(state, task_id)
    result = []
    for name, rec in sorted(records.items()):
        if rec.state == "removed":
            continue
        result.append(check_worktree(project_root, state, Path(rec.path), name, rec.branch, rec.base, rec.state))
    prefix = f"refs/heads/task/{task_id}/"
    known = {gitops.same_path(r.path, r.path) and os.path.normcase(os.path.realpath(r.path)) for r in records.values()}
    for block in _worktree_list(project_root):
        if block.get("branch", "").startswith(prefix) and os.path.normcase(os.path.realpath(block["worktree"])) not in known:
            orphan = WorktreeStatus(block["branch"][len(prefix):], block["branch"][len("refs/heads/"):], block["worktree"],
                                    block.get("HEAD"), True)
            orphan.problems.append("orphan: in Git but not in the workspace record")
            result.append(orphan)
    return result


def preflight_for_execution(project_root: Path, state: StateRoot, task_id: str, subtask_id: str,
                            protected: Sequence[str] = DEFAULT_PROTECTED) -> WorktreeStatus:
    """The gate before a worker runs: recorded worktree exists, branch is the subtask's and not
    protected, clean, base still an ancestor of HEAD, primary checkout is on a different branch."""
    rec = read_records(state, task_id).get(subtask_id)
    if rec is None:
        raise WorkspaceError("not_a_worktree", f"no workspace record for {task_id}/{subtask_id}")
    if rec.branch in protected:
        raise WorkspaceError("protected_branch", f"recorded branch {rec.branch} is protected")
    status = check_worktree(project_root, state, Path(rec.path), subtask_id, rec.branch, rec.base, rec.state)
    if status.locked:
        status.problems.append("worktree is locked")
    return status


# --- integration --------------------------------------------------------------------------------


_SHA = re.compile(r"\b([0-9a-f]{7,40})\b")


def _review_commit(path: Path) -> tuple[str | None, str | None]:
    """(status, commit sha or prefix) from a review file's headers; ``commit`` or ``reviewed``."""
    try:
        doc = read_document(path)
    except (OSError, DocumentError):
        return None, None
    text = doc.get("commit") or doc.get("reviewed") or ""
    match = re.search(r"commit\s+(\S+)", text) if doc.get("reviewed") and not doc.get("commit") else None
    token = match.group(1) if match else text
    found = _SHA.search(token.lower())
    status = (doc.get("status") or "").strip().lower()
    return status, found.group(1) if found else None


def _reviewed(state: StateRoot, task_id: str, subtask, sha: str) -> str | None:
    """None when the commit is covered by an agreeing review (or review is waived), else why not."""
    if subtask.review_waived:
        return None
    files = sorted((state.tasks / task_id).glob(f"review-{subtask.id}-*.md"))
    if not files:
        return f"no review file review-{subtask.id}-*.md"
    seen = []
    for path in files:
        status, commit = _review_commit(path)
        if status == "agreed" and commit and sha.startswith(commit):
            return None
        seen.append(f"{path.name}: status={status or '?'} commit={commit or '?'}")
    return f"no agreeing review of {sha[:7]} ({'; '.join(seen)})"


def integrate(project_root: Path, state: StateRoot, task_id: str, order: Sequence[str],
              protected: Sequence[str] = DEFAULT_PROTECTED, execute: bool = False) -> IntegrationReport:
    """Merge reviewed subtask tips into the integration branch, one by one, in ``order``.

    Stops at the first refusal or conflict; later items stay ``not_attempted``.
    """
    project_root = project_root.resolve()
    plan = _plan(state, task_id)
    records = read_records(state, task_id)
    integration = branch_name(task_id, INTEGRATION)
    if integration in protected:
        raise WorkspaceError("protected_branch", f"{integration} is protected")
    irec = records.get(INTEGRATION)
    if irec is None or irec.state == "removed":
        raise WorkspaceError("not_a_worktree", "no integration worktree recorded; run create-integration first")
    ipath = Path(irec.path)
    report = IntegrationReport(task_id, execute, [IntegrationItem(s) for s in order])
    with _Lock(state, task_id, execute):
        primary_before = _primary_state(project_root)
        merged_here: set[str] = {k for k, v in records.items() if v.state == "merged"}
        for item in report.items:
            sub = plan.subtask(item.subtask)
            tip = _branch_tip(project_root, branch_name(task_id, item.subtask)) if sub else None
            item.commit = tip
            problem = None
            if sub is None:
                problem = ("plan_mismatch", f"{item.subtask} is not a subtask of the plan")
            elif tip is None:
                problem = ("not_a_worktree", f"branch task/{task_id}/{item.subtask} does not exist")
            else:
                unmet = [d for d in sub.depends_on if d not in merged_here]
                if unmet:
                    problem = ("dependency_unmet", f"depends on {', '.join(unmet)}, not merged yet")
                elif (why := _reviewed(state, task_id, sub, tip)):
                    problem = ("unreviewed", why)
            if problem is None:
                status = check_worktree(project_root, state, ipath, INTEGRATION, integration)
                bad = [p for p in status.problems]
                if bad:
                    problem = ("dirty_worktree" if any("dirty" in p for p in bad) else "not_a_worktree", "; ".join(bad))
            if problem:
                item.result, item.code, item.detail = "refused", problem[0], problem[1]
                break
            if _contains(project_root, tip, integration):
                item.result, item.detail = "already_merged", "the commit is already in the integration branch"
                merged_here.add(item.subtask)
                continue
            if not execute:
                item.result = "would_merge"
                merged_here.add(item.subtask)
                continue
            message = f"merge: {task_id}/{item.subtask} ({tip[:7]})"
            try:
                gitops.git(ipath, "merge", "--no-ff", "-m", message, tip)
            except gitops.GitError as exc:
                item.conflicts = [p for p in gitops.git(ipath, "diff", "--name-only", "--diff-filter=U").splitlines() if p]
                try:
                    gitops.git(ipath, "merge", "--abort")
                except gitops.GitError:
                    pass
                clean = not gitops.snapshot(ipath, state.path if _inside(state.path, ipath) else None).status
                item.result, item.code = "conflict", "merge_conflict"
                item.detail = (f"{exc}" if not item.conflicts else "conflicts in " + ", ".join(item.conflicts)) + \
                    ("" if clean else "; WARNING: integration worktree is not clean after abort")
                break
            item.result = "merged"
            item.merge_commit = gitops.git(ipath, "rev-parse", "HEAD").strip()
            merged_here.add(item.subtask)
            rec = records.get(item.subtask)
            if rec is not None:
                rec.state, rec.merge_commit = "merged", item.merge_commit
            else:
                records[item.subtask] = WorktreeRecord(item.subtask, branch_name(task_id, item.subtask), "", tip,
                                                       "caller", "merged", format_timestamp(), merge_commit=item.merge_commit)
            _write_records(state, task_id, records)
        if _primary_state(project_root) != primary_before:
            raise WorkspaceError("primary_changed", "the primary checkout changed during integration")
        report.tip = _branch_tip(project_root, integration)
    return report


# --- removal and retention ----------------------------------------------------------------------


def remove_worktree(project_root: Path, state: StateRoot, task_id: str, subtask_id: str, execute: bool = False,
                    discard_failed: bool = False) -> RemovalResult:
    """Remove a completed worktree (never the branch). Completed = recorded ``merged`` and its tip is
    contained in the integration branch. A ``retained`` (failed) worktree needs ``discard_failed``."""
    project_root = project_root.resolve()
    records = read_records(state, task_id)
    rec = records.get(subtask_id)
    if rec is None or not rec.path:
        raise WorkspaceError("unsafe_remove", f"{subtask_id} is not in the workspace record; nothing is removed")
    path = Path(rec.path)
    if rec.state == "removed":
        raise WorkspaceError("unsafe_remove", f"{subtask_id} is already removed")
    if rec.state == "retained" and not discard_failed:
        raise WorkspaceError("unsafe_remove", f"{subtask_id} is a retained failed worktree; it is kept for diagnosis "
                                              "(only the user passes --discard-failed)")
    if rec.state not in ("merged", "retained"):
        raise WorkspaceError("unsafe_remove", f"{subtask_id} is '{rec.state}', not completed (merged)")
    if rec.state == "merged":
        tip = _branch_tip(project_root, rec.branch)
        itip = _branch_tip(project_root, branch_name(task_id, INTEGRATION))
        if tip is None or itip is None or not _contains(project_root, tip, itip):
            raise WorkspaceError("unsafe_remove", f"{rec.branch} is not contained in the integration branch")
    status = check_worktree(project_root, state, path, subtask_id, rec.branch)
    if status.locked:
        raise WorkspaceError("unsafe_remove", f"{path} is locked")
    if status.dirty and rec.state == "merged":
        raise WorkspaceError("unsafe_remove", f"{path} has uncommitted changes", status.dirty)
    if rec.state == "retained" and status.dirty:
        raise WorkspaceError("unsafe_remove", f"{path} has uncommitted changes; clean or commit them first", status.dirty)
    if not execute:
        return RemovalResult(subtask_id, str(path), False, rec.state)
    with _Lock(state, task_id, True):
        _git(project_root, "worktree", "remove", str(path))  # no --force
        rec.state = "removed"
        _write_records(state, task_id, records)
    return RemovalResult(subtask_id, str(path), True, "removed")


def retain_failed(state: StateRoot, task_id: str, subtask_id: str, reason: str, execute: bool = False) -> WorktreeRecord:
    """Mark a subtask's worktree as kept for diagnosis. Worktree and branch are not touched.
    Without ``execute`` the record is not changed; the returned record shows what it would become."""
    records = read_records(state, task_id)
    rec = records.get(subtask_id)
    if rec is None:
        raise WorkspaceError("not_a_worktree", f"{subtask_id} is not in the workspace record")
    rec = WorktreeRecord(**asdict(rec))
    rec.state, rec.note = "retained", " ".join(reason.split())[:300]
    if execute:
        with _Lock(state, task_id, True):
            records = read_records(state, task_id)
            records[subtask_id] = rec
            _write_records(state, task_id, records)
    return rec
