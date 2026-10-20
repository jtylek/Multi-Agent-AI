"""Run one managed headless worker attempt end to end.

A managed worker is assigned by invocation, not by the mailbox: it gets a new agent id (for
example ``codex-w1``), is never presented as an existing interactive chat, and never writes or
causes an acknowledgment. The runner, not the worker, writes the worker's status file, the
review file (review mode) and, after checking the diff, the commit (implement mode). It never
merges, pushes or retries.

Layout: ``<state-root>/runs/<attempt-id>/`` holds ``prompt.md``, ``stdout.log``,
``stderr.log``, ``final-message.md``, ``result.json`` (and ``usage.json`` for Copilot); the
attempt is recorded in the derived SQLite index.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import winproc
from ..board import read_plan, status_file_name, write_status
from ..document import DocumentError, OwnershipError, atomic_write, read_document, render
from ..grammar import format_timestamp, id_error
from ..mailbox import ACK_PREFIX, ACK_SUFFIX, REQUEST_FILE
from ..models import Attempt, Plan, Status, Subtask
from ..registry import reservation_conflicts
from ..state import StateIndex, StateRoot
from . import failures as fc
from . import gitops, quota, scaffold, versions
from .base import MODES, Invocation, LaunchRequest, ProfileUnconfigured, ProviderAdapter, forbidden_in
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .copilot import CopilotAdapter
from .prompt import VerificationNotDeclared, build_prompt, verification_command

ADAPTERS: dict[str, Callable[[], ProviderAdapter]] = {"codex": CodexAdapter, "copilot": CopilotAdapter, "claude": ClaudeAdapter}
DEFAULT_LIVE_BRANCHES = ("main", "master")
DEFAULT_TIMEOUT = 1800.0
RESERVED_AGENT_IDS = ("codex", "copilot", "claude", "code")
"""Bare product names conventionally denote interactive chats; a managed worker never uses them."""
MANAGED_MARKER = re.compile(r"^Managed worker: provider=(?P<provider>[A-Za-z0-9._-]+)", re.M)
TAIL_BYTES = 16384


class UsageError(ValueError):
    """The request is rejected before an attempt exists; nothing is written."""


@dataclass(frozen=True)
class WorkerRequest:
    provider: str
    task_id: str
    subtask_id: str
    agent_id: str
    mode: str
    worktree: Path
    project_root: Path
    state_root: StateRoot
    model: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    commit: str | None = None
    live_branches: tuple[str, ...] = ()
    pass_env: tuple[str, ...] = ()
    verify_command: str | None = None
    max_budget_usd: float | None = None
    """Per-attempt spending cap; only the Claude adapter has one (off by default)."""
    feedback: str | None = None
    cancel: Any = None
    """Optional ``threading.Event``: when set, the provider process tree is killed and the attempt ends ``cancelled``."""
    """Correction-round text from the orchestrator (T27); capped and scrubbed by the prompt builder."""


@dataclass
class WorkerOutcome:
    attempt_id: str
    failure_class: str | None
    exit_code: int | None
    run_dir: Path
    result: dict[str, Any]
    status_path: Path | None = None
    review_path: Path | None = None
    commit: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure_class is None


# --- validation ---------------------------------------------------------------------------


def validate_request(req: WorkerRequest) -> None:
    if req.provider not in ADAPTERS:
        raise UsageError(f"unknown provider {req.provider!r} (expected {', '.join(ADAPTERS)})")
    if req.mode not in MODES:
        raise UsageError(f"unknown mode {req.mode!r} (expected {', '.join(MODES)})")
    for value, what in ((req.task_id, "task id"), (req.subtask_id, "subtask id"), (req.agent_id, "agent id")):
        if error := id_error(value, what):
            raise UsageError(error)
    if req.agent_id.lower() in RESERVED_AGENT_IDS:
        raise UsageError(f"agent id {req.agent_id!r} is a product name; a managed worker needs a new id such as "
                         f"'{req.provider}-w1'")
    if not req.worktree.is_absolute():
        raise UsageError(f"--worktree must be an absolute path, got {req.worktree}")
    if req.commit is not None and req.mode != "review":
        raise UsageError("--commit is only meaningful in review mode")
    if req.commit is not None and not re.fullmatch(r"[0-9a-fA-F]{7,40}", req.commit):
        raise UsageError(f"--commit must be a commit SHA, got {req.commit!r}")
    if not req.timeout or req.timeout <= 0:
        raise UsageError("--timeout must be positive")
    if req.max_budget_usd is not None:
        if req.provider != "claude":
            raise UsageError(f"--max-budget-usd is only supported for the claude provider, not {req.provider}")
        if not isinstance(req.max_budget_usd, (int, float)) or not 0 < req.max_budget_usd < float("inf"):
            raise UsageError("--max-budget-usd must be a positive number")
    for name in req.pass_env:
        if name.upper() in {n.upper() for n in winproc.NEVER_PASS_ENV}:
            raise UsageError(f"{name} grants blanket permissions and is never passed to a worker")


def _subdirs(path: Path) -> list[Path]:
    return sorted(p for p in path.iterdir() if p.is_dir()) if path.is_dir() else []


def agent_id_conflict(state_root: Path, req: WorkerRequest) -> str | None:
    """Why ``req.agent_id`` may not be used for this managed worker, or None.

    Rejected: an existing status file not written by a managed worker of the same provider; a
    registry entry; any mailbox participation (sender, recipient, ack author); the coordinator
    of any plan; an owner/reviewer of any plan row other than this subtask, unless the id is
    already this provider's managed worker.
    """
    agent, managed = req.agent_id, False
    for task_dir in _subdirs(state_root / "tasks"):
        status = task_dir / status_file_name(agent)
        if status.is_file():
            try:
                body = read_document(status).body
            except (OSError, DocumentError):
                return f"{status} is unreadable"
            match = MANAGED_MARKER.search(body)
            if not match or match.group("provider") != req.provider:
                return f"'{agent}' already has a status file not written by a managed {req.provider} worker ({status})"
            managed = True
    if (state_root / "registry" / f"{agent}.md").exists():
        return f"'{agent}' is a registered agent (registry/{agent}.md)"
    for message_dir in _subdirs(state_root / "messages"):
        if (message_dir / f"{ACK_PREFIX}{agent}{ACK_SUFFIX}").exists():
            return f"'{agent}' wrote an acknowledgment in messages/{message_dir.name}; it is a mailbox participant"
        try:
            doc = read_document(message_dir / REQUEST_FILE)
        except (OSError, DocumentError):
            continue
        names = [doc.get("from") or ""] + [n.strip() for n in (doc.get("to") or "").split(",")]
        if agent in names:
            return f"'{agent}' is a mailbox participant (messages/{message_dir.name})"
    for task_dir in _subdirs(state_root / "tasks"):
        plan = read_plan(task_dir).value
        if plan is None:
            continue
        if plan.coordinator == agent:
            return f"'{agent}' is the coordinator of task {plan.task_id}"
        for row in plan.subtasks:
            if agent in (row.owner_id, row.reviewer):
                this_row = task_dir.name == req.task_id and row.id == req.subtask_id
                if not (this_row or managed):
                    return f"'{agent}' is named in task {plan.task_id} subtask {row.id}"
    return None


# --- helpers ------------------------------------------------------------------------------


def _new_attempt_id(now: datetime) -> str:
    return f"att-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"


def _tail(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _one_line(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


def _review_file(task_dir: Path, subtask_id: str, agent_id: str) -> Path:
    return task_dir / f"review-{subtask_id}-{agent_id}.md"


REVIEW_VERDICT = re.compile(r"^\s*verdict:\s*(.*?)\s*$", re.I | re.M)


def review_verdict(message: str) -> str:
    """The reviewer's verdict from its final message: ``agreed``, ``changes requested`` or ``unparsed``.
    Strict: the message must contain a line that is exactly ``verdict: agreed`` or ``verdict: changes
    requested`` (case-insensitive, trimmed). No such line, a line with any other value, or two lines that
    disagree give ``unparsed``, which never lets a commit through the integration gate. Nothing is
    inferred from prose or the exit code. The verdict is the reviewer's claim, not independently verified."""
    values = {m.group(1).lower() for m in REVIEW_VERDICT.finditer(message)}
    return values.pop() if len(values) == 1 and values <= {"agreed", "changes requested"} else "unparsed"


def write_review(path: Path, headers: list[tuple[str, str]], message: str, agent_id: str) -> None:
    """Orchestrator provenance header, blank line, then the worker's final message verbatim."""
    if path.exists():
        try:
            existing = read_document(path).get("agent")
        except (OSError, DocumentError):
            existing = None
        if existing != agent_id:
            raise OwnershipError(f"{path} belongs to another author")
    atomic_write(path, render(headers) + message)


class _Run:
    """State of one attempt while it is being executed (kept small and explicit)."""

    def __init__(self, req: WorkerRequest, adapter: ProviderAdapter, attempt_id: str, started: datetime):
        self.req = req
        self.adapter = adapter
        self.attempt_id = attempt_id
        self.started = started
        self.run_dir = req.state_root.path / "runs" / attempt_id
        self.task_dir = req.state_root.tasks / req.task_id
        self.result: dict[str, Any] = {
            "attempt": attempt_id, "provider": req.provider, "model": req.model, "mode": req.mode,
            "task": req.task_id, "subtask": req.subtask_id, "agent": req.agent_id,
            "worktree": str(req.worktree), "profile": None, "argv": None, "prompt_sha256": None,
            "env_names": None, "started": _iso(started), "finished": None, "duration_seconds": None,
            "exit_code": None, "timed_out": False, "failure_class": None, "failure_detail": None,
            "findings": [], "usage": "unknown", "final_message_file": None, "commit_created": None,
            "files_changed": [], "git": {},
        }
        self.plan: Plan | None = None
        self.subtask: Subtask | None = None
        self.branch: str | None = None
        self.status_path: Path | None = None
        self.limit: quota.LimitFinding | None = None

    def status(self, state: str, blockers: str, last_commit: str = "none") -> None:
        if self.plan is None or self.subtask is None:
            return
        body = (f"Managed worker: provider={self.req.provider}, started by 'multi-agent-ai worker run' "
                f"(not an interactive chat).\nAttempt: {self.attempt_id} ({self.req.mode}); "
                f"logs in runs/{self.attempt_id}/.")
        branch = f"{self.branch or 'detached HEAD'} ({self.req.worktree})"
        status = Status(
            agent_id=self.req.agent_id, plan_revision=self.plan.revision, subtask=self.subtask.id,
            state=state, last_commit=last_commit,
            tests=f"none run by the orchestrator; worker report in runs/{self.attempt_id}/final-message.md",
            blockers=_one_line(blockers) or "none", updated=format_timestamp(), branch=branch, body=body,
        )
        self.status_path = write_status(self.req.state_root.path, self.req.task_id, status, author=self.req.agent_id)


def run_worker(
    req: WorkerRequest,
    *,
    adapter: ProviderAdapter | None = None,
    env: Mapping[str, str] | None = None,
    resolver: Callable[..., winproc.ResolvedCommand] | None = None,
    cli_version: str | None = None,
    version_reader: Callable[..., versions.CliVersion] | None = None,
) -> WorkerOutcome:
    """Run one attempt. Raises :class:`UsageError` before anything is written; afterwards
    every outcome is recorded (runs dir, result.json, SQLite attempt, status file).

    The provider CLI version comes from ``cli_version`` when the caller positively knows it,
    otherwise from the CLI's own ``--version`` (T18; ``version_reader`` replaces the probe in
    tests). It is recorded in the attempt and keys the quota detector tables; an unknown version
    makes a limit signature a ``provider_error``."""
    validate_request(req)
    task_dir = req.state_root.tasks / req.task_id
    parsed = read_plan(task_dir)
    if parsed.value is None:
        raise UsageError(f"plan for task {req.task_id} is not usable: {'; '.join(parsed.errors) or 'missing'}")
    plan = parsed.value
    subtask = plan.subtask(req.subtask_id)
    if subtask is None:
        raise UsageError(f"subtask {req.subtask_id} is not a valid row in the plan of task {req.task_id}: "
                         f"{'; '.join(e for e in parsed.errors if req.subtask_id in e) or 'not found'}")
    if not plan.revision:
        raise UsageError(f"plan for task {req.task_id} has no valid revision")
    if conflict := agent_id_conflict(req.state_root.path, req):
        raise UsageError(f"agent id rejected: {conflict}")
    if conflicts := reservation_conflicts(req.state_root.path, subtask.files, agent=req.agent_id):
        detail = "; ".join(f"{item.agent}: {', '.join(item.shared)}" for item in conflicts)
        raise UsageError(f"reservation conflict: assignment for {req.subtask_id} overlaps active reservation ({detail})")

    try:
        verify = req.verify_command if req.verify_command is not None else verification_command(plan, subtask.id)
    except VerificationNotDeclared as exc:
        raise UsageError(str(exc)) from exc

    adapter = adapter or ADAPTERS[req.provider]()
    started = datetime.now()
    run = _Run(req, adapter, _new_attempt_id(started), started)
    run.plan, run.subtask = plan, subtask
    run.result["findings"].extend(f"plan: {e}" for e in parsed.errors)  # other rows; reported, not fatal
    run.run_dir.mkdir(parents=True, exist_ok=False)
    evidence: dict[str, Any] = {"mode": req.mode}
    outcome_commit: str | None = None
    message: str | None = None

    def finish(detail: str | None = None) -> WorkerOutcome:
        return _finish(run, evidence, detail, message, outcome_commit)

    # pre-launch: worktree checks ------------------------------------------------------
    try:
        info = gitops.worktree_info(req.worktree)
    except gitops.GitError as exc:
        evidence["environment_error"] = True
        return finish(f"invalid worktree: {exc}")
    run.branch = info.branch
    exclude = req.state_root.path if _inside(req.state_root.path, req.worktree) else None
    try:
        before = gitops.snapshot(req.worktree, exclude, info)
    except gitops.GitError as exc:
        evidence["environment_error"] = True
        return finish(str(exc))
    run.result["git"] = {"branch": info.branch, "head_before": info.head}
    if req.mode == "implement":
        refusal = _implement_refusal(req, info, before)
        if not refusal:
            try:
                scaffold.plan_directories(req.worktree, subtask.files)
            except scaffold.PathRefused as exc:
                refusal = f"files/area of {req.subtask_id} cannot be prepared: {exc}"
        if refusal:
            evidence["environment_error"] = True
            return finish(refusal)
    commit = None
    if req.mode == "review":
        try:
            commit = _commit_to_review(req, info)
        except gitops.GitError as exc:
            evidence["environment_error"] = True
            return finish(f"commit to review not found: {exc}")
        run.result["git"]["commit_reviewed"] = commit
    primary_before = _primary_head(req.project_root)

    if req.mode == "implement":  # deterministic, inside the worktree only; a worker needs no shell for mkdir
        run.result["created_directories"] = scaffold.create_directories(req.worktree, subtask.files)

    # prompt and invocation ----------------------------------------------------------------
    board = task_dir / "plan.md"
    prompt = build_prompt(plan=plan, subtask=subtask, agent_id=req.agent_id, provider=req.provider,
                          mode=req.mode, worktree=req.worktree, branch=info.branch, board_path=board,
                          commit=commit, feedback=req.feedback)
    prompt_path = run.run_dir / "prompt.md"
    atomic_write(prompt_path, prompt)
    run.result["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    session_id = str(uuid.uuid4())
    launch = LaunchRequest(req.worktree, req.mode, prompt, prompt_path, run.run_dir, req.model, session_id, verify,
                           req.max_budget_usd)
    try:
        invocation = adapter.build_invocation(launch)
    except ProfileUnconfigured as exc:
        evidence["profile_unconfigured"] = True
        return finish(str(exc))
    if bad := forbidden_in(invocation.args):
        evidence["profile_unconfigured"] = True
        return finish(f"refusing forbidden flag(s) {bad}")
    run.result["profile"] = invocation.profile
    run.result["verify_command"] = verify
    if req.provider == "copilot" or (req.provider == "claude" and req.mode == "implement"):
        run.result["session_id"] = session_id  # the id the provider was told to use (resumable)

    base_env = env if env is not None else os.environ
    child_env = winproc.build_env(base_env, (*adapter.env_names, *req.pass_env))
    run.result["env_names"] = sorted(child_env)
    try:
        command = (resolver or winproc.resolve_command)(adapter.command, env=child_env)
    except winproc.LaunchError as exc:
        evidence["launch_error"] = True
        run.result["argv"] = [adapter.command, *invocation.redacted_args(prompt)]
        return finish(str(exc))
    run.result["command"] = {"path": str(command.path), "kind": command.kind, "prefix": list(command.prefix)}
    run.result["argv"] = [*command.prefix, *invocation.redacted_args(prompt)]
    try:
        length = command.command_line_length(invocation.args)
    except winproc.UnsafeArgument as exc:
        evidence["arguments_rejected"] = True
        return finish(str(exc))
    if length > winproc.MAX_COMMAND_LINE:
        evidence["arguments_rejected"] = True
        return finish(f"command line is {length} characters; Windows allows {winproc.MAX_COMMAND_LINE}")

    # provider CLI version (its own --version: local, no sign-in, no quota) -------------------
    if cli_version is not None:
        known = versions.CliVersion(cli_version, f"supplied by the caller: {cli_version}")
    else:
        known = (version_reader or versions.read_cli_version)(command, child_env)
    cli_version = known.version
    run.result["cli_version"] = known.as_record()
    if known.error:
        run.result["findings"].append(f"cli version unknown: {known.error}")

    # launch -------------------------------------------------------------------------------
    run.status("in progress", "none")
    result = winproc.run(command, invocation.args, cwd=req.worktree, env=child_env,
                         stdout_path=run.run_dir / "stdout.log", stderr_path=run.run_dir / "stderr.log",
                         stdin_path=invocation.stdin_path, timeout=req.timeout, cancel=req.cancel)
    run.result.update(exit_code=result.exit_code, timed_out=result.timed_out,
                      duration_seconds=result.duration_seconds, pid=result.pid,
                      tree_control=result.tree_control, kill_method=result.kill_method)
    run.result["findings"].extend(result.notes)
    evidence.update(exit_code=result.exit_code, timed_out=result.timed_out, cancelled=result.cancelled)
    if result.launch_error:
        evidence["launch_error"] = True
        return finish(result.launch_error)
    stdout, stderr = _tail(run.run_dir / "stdout.log"), _tail(run.run_dir / "stderr.log")
    evidence.update(stdout=stdout, stderr=stderr)

    # post-run checks ----------------------------------------------------------------------
    try:
        after = gitops.snapshot(req.worktree, exclude)
    except gitops.GitError as exc:
        evidence["environment_error"] = True
        return finish(f"worktree unreadable after the run: {exc}")
    comparison = gitops.compare(before, after)
    run.result["git"].update(head_after=after.head, ref_observations=comparison.observations,
                             violations=comparison.git_violations)
    primary_after = _primary_head(req.project_root)
    if primary_before != primary_after:
        run.result["findings"].append(f"primary checkout HEAD changed during the run: {primary_before} -> "
                                      f"{primary_after} (not attributed)")
    run.result["git"].update(primary_head_before=primary_before, primary_head_after=primary_after)
    run.result["files_changed"] = comparison.changed_paths
    evidence.update(review_modified=comparison.worktree_changed, git_violation=bool(comparison.git_violations),
                    changed_files=len(after.status))

    parsed_output = adapter.parse_output(run.run_dir / "stdout.log", invocation)
    message = parsed_output.final_message
    run.result["usage"] = parsed_output.usage
    if "usage_totals" in parsed_output.session:  # measured, from the provider's own usage file
        run.result["usage_totals"] = parsed_output.session.pop("usage_totals")
    if parsed_output.session:
        run.result["provider_session"] = parsed_output.session
        if "provider_reported_cost_usd" in parsed_output.session:  # a provider figure, not API pricing
            run.result["provider_reported_cost_usd"] = parsed_output.session["provider_reported_cost_usd"]
    if result.exit_code != 0 and not result.timed_out:
        finding = quota.detect(req.provider, stdout, stderr, cli_version=cli_version)  # positive identification only
        if finding is not None:
            run.limit = finding
            evidence["limit"] = finding.kind
            run.result["limit"] = _limit_record(run, finding, before, session_id, parsed_output.session)
    if parsed_output.malformed:
        evidence["malformed"] = True
        run.result["findings"].append(f"output: {parsed_output.malformed}")
    if result.exit_code == 0 and fc.match_output(stdout, stderr) == fc.PERMISSION_DENIED:
        run.result["findings"].append("output mentions a denied permission (permission_denied); check stdout.log")

    detail = None
    if req.mode == "review" and comparison.worktree_changed:
        detail = "review run changed the worktree: " + ", ".join(comparison.changed_paths + comparison.git_violations)
    elif comparison.git_violations:
        detail = "; ".join(comparison.git_violations)
    if fc.classify(fc.Evidence(**evidence)) is None and req.mode == "implement":
        message_file = run.run_dir / "commit-message.txt"
        atomic_write(message_file, _commit_message(req, run.attempt_id, plan.revision))
        try:
            outcome_commit = gitops.commit_all(req.worktree, message_file, before.head, exclude)
        except gitops.GitError as exc:
            evidence["commit_failed"] = True
            detail = str(exc)
    return finish(detail)


def _commit_to_review(req: WorkerRequest, info: gitops.WorktreeInfo) -> str:
    """The commit a review is about: ``--commit`` resolved by Git, else the worktree's HEAD (already known)."""
    if req.commit is None:
        return info.head
    return gitops.git(req.worktree, "rev-parse", "--verify", f"{req.commit}^{{commit}}").strip()


def _implement_refusal(req: WorkerRequest, info: gitops.WorktreeInfo, before: Any) -> str | None:
    """Why implement mode must not start in this worktree (None when it may)."""
    live = {*DEFAULT_LIVE_BRANCHES, *req.live_branches}
    expected = f"task/{req.task_id}/{req.subtask_id}"
    if gitops.same_path(req.worktree, req.project_root) or not info.is_linked_worktree:
        return "implement mode refuses the primary checkout; use a linked worktree"
    if info.branch is None:
        return "implement mode needs a branch; the worktree has a detached HEAD"
    if info.branch in live:
        return f"implement mode refuses live/protected branch {info.branch!r}"
    if info.branch != expected:
        return f"implement mode expects branch {expected!r}, worktree is on {info.branch!r}"
    if before.status:
        return f"worktree is dirty before the run ({len(before.status)} entries); clean it first"
    return None


def preview_worker(
    req: WorkerRequest,
    *,
    adapter: ProviderAdapter | None = None,
    env: Mapping[str, str] | None = None,
    resolver: Callable[..., winproc.ResolvedCommand] | None = None,
) -> dict[str, Any]:
    """Dry run: the same checks as :func:`run_worker` up to the launch, but nothing is written
    (no runs dir, no status file, no SQLite attempt) and no process is started. Raises
    :class:`UsageError` for requests ``run_worker`` would reject up front; anything that would
    be a classified failure is listed under ``problems``."""
    validate_request(req)
    parsed = read_plan(req.state_root.tasks / req.task_id)
    if parsed.value is None:
        raise UsageError(f"plan for task {req.task_id} is not usable: {'; '.join(parsed.errors) or 'missing'}")
    plan = parsed.value
    subtask = plan.subtask(req.subtask_id)
    if subtask is None:
        raise UsageError(f"subtask {req.subtask_id} is not a valid row in the plan of task {req.task_id}")
    if not plan.revision:
        raise UsageError(f"plan for task {req.task_id} has no valid revision")
    if conflict := agent_id_conflict(req.state_root.path, req):
        raise UsageError(f"agent id rejected: {conflict}")
    if conflicts := reservation_conflicts(req.state_root.path, subtask.files, agent=req.agent_id):
        detail = "; ".join(f"{item.agent}: {', '.join(item.shared)}" for item in conflicts)
        raise UsageError(f"reservation conflict: assignment for {req.subtask_id} overlaps active reservation ({detail})")
    try:
        verify = req.verify_command if req.verify_command is not None else verification_command(plan, subtask.id)
    except VerificationNotDeclared as exc:
        raise UsageError(str(exc)) from exc
    adapter = adapter or ADAPTERS[req.provider]()
    problems: list[str] = []
    preview: dict[str, Any] = {
        "dry_run": True, "cli_version": "not probed in a dry run (the CLI is not started)", "provider": req.provider, "mode": req.mode, "task": req.task_id,
        "subtask": req.subtask_id, "agent": req.agent_id, "worktree": str(req.worktree),
        "plan_revision": plan.revision, "model": req.model, "timeout": req.timeout,
        "branch": None, "commit_to_review": None, "directories_to_create": [], "profile": None, "verify_command": None,
        "command": None, "argv": None, "prompt_sha256": None, "env_names": None, "problems": problems,
    }
    try:
        info = gitops.worktree_info(req.worktree)
        exclude = req.state_root.path if _inside(req.state_root.path, req.worktree) else None
        before = gitops.snapshot(req.worktree, exclude, info)
    except gitops.GitError as exc:
        problems.append(f"invalid worktree: {exc}")
        return preview
    preview["branch"] = info.branch
    commit = None
    if req.mode == "implement":
        if refusal := _implement_refusal(req, info, before):
            problems.append(refusal)
        else:
            try:
                plan_dirs = scaffold.plan_directories(req.worktree, subtask.files)
                preview["directories_to_create"] = plan_dirs.to_create
            except scaffold.PathRefused as exc:
                problems.append(f"files/area of {req.subtask_id} cannot be prepared: {exc}")
    else:
        try:
            commit = _commit_to_review(req, info)
            preview["commit_to_review"] = commit
        except gitops.GitError as exc:
            problems.append(f"commit to review not found: {exc}")
    prompt = build_prompt(plan=plan, subtask=subtask, agent_id=req.agent_id, provider=req.provider,
                          mode=req.mode, worktree=req.worktree, branch=info.branch,
                          board_path=req.state_root.tasks / req.task_id / "plan.md", commit=commit,
                          feedback=req.feedback)
    preview["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    preview["verify_command"] = verify
    # the paths below are never created in a dry run; adapters only build argv from them
    run_dir = req.state_root.path / "runs" / "dry-run"
    launch = LaunchRequest(req.worktree, req.mode, prompt, run_dir / "prompt.md", run_dir, req.model,
                           str(uuid.uuid4()), verify, req.max_budget_usd)
    try:
        invocation = adapter.build_invocation(launch)
    except ProfileUnconfigured as exc:
        problems.append(str(exc))
        return preview
    if bad := forbidden_in(invocation.args):
        problems.append(f"refusing forbidden flag(s) {bad}")
        return preview
    preview["profile"] = invocation.profile
    child_env = winproc.build_env(env if env is not None else os.environ, (*adapter.env_names, *req.pass_env))
    preview["env_names"] = sorted(child_env)
    try:
        command = (resolver or winproc.resolve_command)(adapter.command, env=child_env)
    except winproc.LaunchError as exc:
        problems.append(str(exc))
        preview["argv"] = [adapter.command, *invocation.redacted_args(prompt)]
        return preview
    preview["command"] = {"path": str(command.path), "kind": command.kind, "prefix": list(command.prefix)}
    preview["argv"] = [*command.prefix, *invocation.redacted_args(prompt)]
    try:
        length = command.command_line_length(invocation.args)
    except winproc.UnsafeArgument as exc:
        problems.append(str(exc))
        return preview
    if length > winproc.MAX_COMMAND_LINE:
        problems.append(f"command line is {length} characters; Windows allows {winproc.MAX_COMMAND_LINE}")
    return preview


_RESUME = {"codex": "codex exec resume {id}", "copilot": "copilot --resume {id}", "claude": "claude --resume {id}"}


def _limit_record(run: _Run, finding: quota.LimitFinding, before: gitops.Snapshot, session_id: str,
                  provider_session: Mapping[str, str]) -> dict[str, Any]:
    """The per-attempt limit record (plan T15 item 5). The resume command is recorded for a human or
    the coordinator; the orchestrator never runs it (a resume is a new, linked attempt)."""
    req = run.req
    own_session = req.provider == "copilot" or (req.provider == "claude" and req.mode == "implement")
    resumable = not (req.provider == "claude" and req.mode == "review")  # review runs are not persisted
    session = (provider_session.get("thread_id") or provider_session.get("session_id") or
               (session_id if own_session else None)) if resumable else None
    try:
        diff_stat = gitops.git(req.worktree, "diff", "--stat", "HEAD").strip()
    except gitops.GitError as exc:
        diff_stat = f"unavailable: {exc}"
    return {
        "kind": finding.kind, "provider": req.provider, "model": req.model, "session": session,
        "resume_command": _RESUME[req.provider].format(id=session) if session else None,
        "head_at_launch": before.head, "status_at_launch": list(before.status), "diff_stat_at_failure": diff_stat,
        "raw_message": finding.raw_message, "observed": _iso(datetime.now()), "reason": finding.reason,
        "reset": finding.reset.isoformat() if finding.reset else None, "reset_zone": finding.reset_zone,
        "reset_text": finding.reset_text, "counts_toward_success_rate": finding.kind == quota.PROVIDER_ERROR,
    }


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _primary_head(project_root: Path) -> str | None:
    try:
        return gitops.git(project_root, "rev-parse", "--verify", "HEAD").strip()
    except (gitops.GitError, OSError):
        return None


def _commit_message(req: WorkerRequest, attempt_id: str, revision: int) -> str:
    return (
        f"worker({req.task_id}/{req.subtask_id}): changes by managed worker {req.agent_id}\n\n"
        f"Committed by the Multi-Agent AI orchestrator after checking the worktree diff.\n\n"
        f"Agent: {req.agent_id}\nProvider: {req.provider}\nModel: {req.model or 'provider default (not specified)'}\n"
        f"Attempt: {attempt_id}\nMode: implement\nPlan-revision: {revision}\n"
    )


def _finish(run: _Run, evidence: dict[str, Any], detail: str | None, message: str | None,
            commit: str | None) -> WorkerOutcome:
    req = run.req
    failure = fc.classify(fc.Evidence(**evidence))
    if failure in (fc.NONZERO_EXIT, fc.UNSUPPORTED_MODEL, fc.AUTH, fc.PERMISSION_DENIED, fc.INVALID_ARGUMENTS,
                   fc.TIMEOUT) and detail is None:
        detail = _one_line(evidence.get("stderr") or "", 500) or f"exit code {evidence.get('exit_code')}"
    if failure in (fc.QUOTA_EXHAUSTED, fc.CAP_REACHED, fc.PROVIDER_ERROR) and detail is None and run.limit is not None:
        detail = run.limit.raw_message
    if failure == fc.NO_CHANGES:
        detail = "implement run exited 0 but changed no file"
    if failure == fc.MALFORMED_OUTPUT and detail is None:
        detail = next((f for f in run.result["findings"] if f.startswith("output: ")), "malformed output")
    finished = datetime.now()
    final_path = run.run_dir / "final-message.md"
    if message is not None:
        if not final_path.is_file():
            atomic_write(final_path, message)
        run.result["final_message_file"] = final_path.name
    run.result.update(finished=_iso(finished), failure_class=failure, failure_detail=detail,
                      commit_created=commit)
    if run.result["duration_seconds"] is None:
        run.result["duration_seconds"] = round((finished - run.started).total_seconds(), 3)
    atomic_write(run.run_dir / "result.json", json.dumps(run.result, indent=2, ensure_ascii=False) + "\n")

    with StateIndex.open(req.state_root.database_path) as index:
        previous = [d for d in index.all_attempt_details().values()
                    if (d.get("task"), d.get("subtask"), d.get("agent")) == (req.task_id, req.subtask_id, req.agent_id)]
        index.save(Attempt(
            id=run.attempt_id, task_id=req.subtask_id, agent_id=req.agent_id, number=len(previous) + 1,
            provider=req.provider, model=req.model, started=run.result["started"],
            finished=run.result["finished"], exit_code=run.result["exit_code"], failure_category=failure,
            files_changed=tuple(run.result["files_changed"]),
        ))
        index.save_attempt_details(run.attempt_id, {
            "task": req.task_id, "subtask": req.subtask_id, "agent": req.agent_id, "mode": req.mode,
            "profile": run.result["profile"], "usage": run.result["usage"],
            "duration_seconds": run.result["duration_seconds"], "timed_out": run.result["timed_out"],
            "commit_created": commit, "run_dir": str(run.run_dir),
            "failure_class": failure, "model": req.model, "limit": run.result.get("limit"),
            "cli_version": (run.result.get("cli_version") or {}).get("version"),
            "usage_totals": run.result.get("usage_totals"),
            "created_directories": run.result.get("created_directories"),
        })

    review_path = None
    if failure is None and req.mode == "review" and message is not None and run.plan is not None:
        review_path = _review_file(run.task_dir, req.subtask_id, req.agent_id)
        write_review(review_path, [
            ("status", review_verdict(message)),
            ("subtask", req.subtask_id), ("agent", req.agent_id), ("provider", req.provider),
            ("model", req.model or "provider default (not specified)"),
            ("commit", run.result["git"].get("commit_reviewed", "unknown")), ("attempt", run.attempt_id),
            ("profile", run.result["profile"] or "-"), ("exit-code", str(run.result["exit_code"])),
            ("plan-revision", str(run.plan.revision)), ("updated", format_timestamp()),
            ("provenance", "headers written by the orchestrator (status is parsed from the verdict line of the body); the body is the worker's final message, verbatim"),
        ], message, req.agent_id)
    state = "review" if failure is None else "blocked"
    blockers = "none" if failure is None else f"{failure}: {detail or ''}"
    if failure in (fc.QUOTA_EXHAUSTED, fc.CAP_REACHED) and run.limit is not None:
        blockers = quota.blocker_text(run.limit, req.model)  # formats.md: "quota: ..." / "cap: ..."
    run.status(state, blockers, last_commit=commit or "none")
    return WorkerOutcome(run.attempt_id, failure, run.result["exit_code"], run.run_dir, run.result,
                         run.status_path, review_path, commit)
