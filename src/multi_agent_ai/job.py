"""End-to-end job driver (T27), stage A: job state, the policy table, the dry-run plan and the
run report. No process is launched here.

The coordinator's ``plan.md`` is read, never written (D2, single writer). The driver's own state is
``<state-root>/jobs/<job-id>/``: ``events.jsonl`` (append-only, the source of truth) and
``job.json`` / ``report.json`` / ``report.md`` (derived, rewritten atomically, rebuildable by
replaying the events). Design: tasks/m1-real-execution/notes/claude-d1-t27-design.md.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import winproc
from .board import _area_items, _overlaps, read_plan
from .document import atomic_write
from .grammar import format_timestamp, id_error
from .models import Plan, Subtask
from .state import StateRoot
from .workers import failures as F
from .workers.prompt import verification_command
from .workers.runner import ADAPTERS, RESERVED_AGENT_IDS

JOBS_DIR = "jobs"
EVENTS_FILE = "events.jsonl"
JOB_FILE = "job.json"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"
REPORT_SCHEMA = 1
MAX_PARALLEL_CAP = 4
SKIPPED_PLAN_STATES = ("done", "merged")

JOB_STATES = (
    "created", "preparing", "running", "reviewing", "verifying", "integrating", "final_check",
    "awaiting_approval", "paused_plan_changed", "paused_user", "failed", "cancelled",
)
TERMINAL_JOB_STATES = ("failed", "cancelled")
RESTING_JOB_STATES = ("awaiting_approval", "paused_plan_changed", "paused_user", *TERMINAL_JOB_STATES)
SUBTASK_STATES = (
    "pending", "ready", "implementing", "implemented", "verifying", "verified", "reviewing", "reviewed",
    "integrated", "verification_failed", "changes_requested", "failed", "blocked_by", "retained", "cancelled",
)
PLAN_STATE_SUGGESTION = {
    "pending": "open", "ready": "in progress", "implementing": "in progress", "implemented": "review",
    "verifying": "review", "verified": "review", "reviewing": "review", "reviewed": "review",
    "integrated": "done",
}
"""What the coordinator would write in the plan for a driver state. Shown by ``job status``, never written."""


class JobError(Exception):
    """A request the driver refuses; nothing is written."""


# --- policy -----------------------------------------------------------------------------------

STOP_JOB = "stop_job"
PAUSE_USER = "pause_user"
RETRY_ONCE = "retry_once"
FEEDBACK_ONCE = "feedback_once"
REREVIEW_ONCE = "rereview_once"
FAIL = "fail"


@dataclass(frozen=True)
class Policy:
    action: str
    reason: str


POLICY: dict[str, Policy] = {
    F.QUOTA_EXHAUSTED: Policy(STOP_JOB, "provider quota exhausted: the user decides (wait, switch plan, other provider)"),
    F.CAP_REACHED: Policy(STOP_JOB, "provider usage cap reached: the user decides"),
    F.AUTH: Policy(STOP_JOB, "authentication failed: the user must sign in; no other provider is tried"),
    F.PERMISSION_DENIED: Policy(STOP_JOB, "a permission was denied: never routed around; back to the user"),
    F.GIT_POLICY_VIOLATION: Policy(STOP_JOB, "the worker changed Git state: back to the user"),
    F.UNSUPPORTED_MODEL: Policy(PAUSE_USER, "model not supported: change the configuration; no retry"),
    F.PROFILE_UNCONFIGURED: Policy(PAUSE_USER, "launch profile incomplete: configuration problem; no retry"),
    F.ENVIRONMENT: Policy(PAUSE_USER, "environment check failed: fix the environment; no retry"),
    F.LAUNCH_ERROR: Policy(PAUSE_USER, "executable could not be started: fix the installation; no retry"),
    F.INVALID_ARGUMENTS: Policy(PAUSE_USER, "arguments rejected: configuration problem; no retry"),
    F.TIMEOUT: Policy(RETRY_ONCE, "timeout: one bounded retry when the worktree is clean or unchanged"),
    F.NO_CHANGES: Policy(RETRY_ONCE, "no changes: one clarified retry"),
    F.MALFORMED_OUTPUT: Policy(RETRY_ONCE, "malformed output: one clarified retry"),
    F.COMMIT_FAILED: Policy(FAIL, "the orchestrator's commit failed: evidence kept, no retry"),
    F.NONZERO_EXIT: Policy(FAIL, "the worker exited nonzero: evidence kept, no retry"),
    F.PROVIDER_ERROR: Policy(FAIL, "provider error: evidence kept, no retry"),
    F.REVIEW_MODIFIED_FILES: Policy(REREVIEW_ONCE, "the reviewer changed files: discard, one re-review by another provider"),
    "verification_failed": Policy(FEEDBACK_ONCE, "verification failed: one correction round with the output tail"),
    "environment_error": Policy(PAUSE_USER, "verification environment failed: fix the environment; no retry"),
    "changes_requested": Policy(FEEDBACK_ONCE, "review requested changes: one correction round"),
    "merge_conflict": Policy(PAUSE_USER, "integration conflict: integration branch left as is; no auto-resolution"),
}
"""Failure class (workers.failures, verify statuses, review/merge outcomes) -> what the driver does.
One place; there is no blind retry for any class not marked retry/feedback."""


def policy_for(failure_class: str) -> Policy:
    """Unknown classes pause for the user rather than being guessed at."""
    return POLICY.get(failure_class, Policy(PAUSE_USER, f"unclassified failure {failure_class!r}: back to the user"))


# --- options and the dry-run plan -------------------------------------------------------------


@dataclass
class JobOptions:
    providers: tuple[str, ...] = ("codex", "copilot")
    assign: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    """subtask id -> (provider, model)"""
    max_parallel: int = 2
    max_per_provider: int = 2
    worker_timeout: float = 1800.0
    review_timeout: float = 1800.0
    job_timeout: float | None = None
    verify_commands: tuple[str, ...] = ()
    live_branches: tuple[str, ...] = ()
    pass_env: tuple[str, ...] = ()
    worktrees_root: str | None = None
    approved_provider_run: bool = False

    def validate(self) -> None:
        if not 1 <= self.max_parallel <= MAX_PARALLEL_CAP:
            raise JobError(f"--max-parallel must be 1..{MAX_PARALLEL_CAP}")
        if self.max_per_provider < 1:
            raise JobError("--max-per-provider must be at least 1")
        if not self.providers:
            raise JobError("--providers is empty")
        for name in self.providers:
            if name not in ADAPTERS:
                raise JobError(f"unknown provider {name!r} (available: {', '.join(ADAPTERS)})")
        for subtask, (provider, _model) in self.assign.items():
            if provider not in self.providers:
                raise JobError(f"--assign {subtask}={provider}: provider is not in --providers")
        for seconds in (self.worker_timeout, self.review_timeout):
            if seconds <= 0:
                raise JobError("timeouts must be positive")


def parse_assign(items: Iterable[str]) -> dict[str, tuple[str, str | None]]:
    """``SUBTASK=PROVIDER[:MODEL]`` entries."""
    result: dict[str, tuple[str, str | None]] = {}
    for item in items:
        subtask, sep, rest = item.partition("=")
        provider, _, model = rest.partition(":")
        if not sep or not subtask or not provider:
            raise JobError(f"--assign {item!r}: expected SUBTASK=PROVIDER[:MODEL]")
        if subtask in result:
            raise JobError(f"--assign {subtask}: given twice")
        result[subtask] = (provider, model or None)
    return result


def provider_of(agent: str, providers: Sequence[str]) -> str | None:
    """Map a plan owner / reviewer cell (an agent id) to a pool provider, or None.

    Only the provider name itself or ``<provider>-<anything>`` maps ("codex", "codex-w1"); anything
    else is unmappable and the caller must say so rather than guess.
    """
    lowered = agent.lower()
    for provider in providers:
        if lowered == provider or lowered.startswith(provider + "-"):
            return provider
    return None


@dataclass
class PlannedSubtask:
    id: str
    files: str
    depends_on: list[str]
    wave: int | None
    author_provider: str | None
    model: str | None
    review: str
    """``waived``, ``required`` or ``skipped``."""
    reviewer_provider: str | None
    verify_command: str | None
    plan_state: str
    skipped: str | None = None


@dataclass
class JobPlan:
    task_id: str
    plan_revision: int
    base_commit: str | None
    integration_branch: str
    waves: list[list[str]]
    subtasks: list[PlannedSubtask]
    serialised: list[tuple[str, str]]
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def provider_available(name: str, env: Mapping[str, str] | None = None) -> bool:
    command = ADAPTERS[name]().command
    try:
        winproc.resolve_command(command, env=env)
    except winproc.LaunchError:
        return False
    return True


def areas_overlap(left: str, right: str) -> bool:
    return any(_overlaps(a, b) for a in _area_items(left) for b in _area_items(right))


def _waves(subtasks: Sequence[Subtask]) -> list[list[str]]:
    """Layers of a dependency graph over the given subtasks, plan-table order inside a layer.
    Dependencies outside the set (already done) are ignored; a cycle leaves the rest unplaced."""
    ids = {s.id for s in subtasks}
    placed: set[str] = set()
    waves: list[list[str]] = []
    pending = list(subtasks)
    while pending:
        ready = [s for s in pending if all(d in placed or d not in ids for d in s.depends_on)]
        if not ready:
            break
        waves.append([s.id for s in ready])
        placed.update(s.id for s in ready)
        pending = [s for s in pending if s.id not in placed]
    return waves


def choose_reviewer(author: str, cell: str, providers: Sequence[str], available: Callable[[str], bool]) -> str | None:
    """A provider different from ``author``: the plan's reviewer cell if it maps to one, else the
    first installed pool provider that is not the author. None = no independent reviewer."""
    named = provider_of(cell, providers)
    if named and named != author and available(named):
        return named
    return next((p for p in providers if p != author and available(p)), None)


def build_plan(state: StateRoot, task_id: str, options: JobOptions,
               available: Callable[[str], bool] = provider_available) -> JobPlan:
    """Read-only: what ``job start`` would do. Problems block ``--execute``; warnings do not."""
    options.validate()
    parsed = read_plan(state.tasks / task_id)
    if parsed.value is None:
        raise JobError(f"plan for task {task_id} is not usable: {'; '.join(parsed.errors) or 'missing'}")
    plan: Plan = parsed.value
    problems: list[str] = list(parsed.errors)
    warnings: list[str] = list(parsed.warnings)
    default_branch = f"task/{task_id}/integration"
    if plan.integration_branch and plan.integration_branch != default_branch:
        problems.append(f"plan names integration branch {plan.integration_branch!r}; the driver uses {default_branch}")
    if not plan.base_commit:
        problems.append("the plan has no base-commit")

    editing = [s for s in plan.subtasks if s.worktree and s.state not in SKIPPED_PLAN_STATES]
    for sid in options.assign:
        if plan.subtask(sid) is None:
            problems.append(f"--assign {sid}: no such subtask in the plan")
    waves = _waves(editing)
    placed = {sid for wave in waves for sid in wave}
    for s in editing:
        if s.id not in placed:
            problems.append(f"subtask {s.id}: part of a dependency cycle")
    wave_of = {sid: n for n, wave in enumerate(waves) for sid in wave}

    planned: list[PlannedSubtask] = []
    for s in plan.subtasks:
        skipped = None
        if s.state in SKIPPED_PLAN_STATES:
            skipped = f"plan state is {s.state}"
        elif not s.worktree:
            skipped = "no worktree: not an editing subtask"
        elif s.state == "blocked":
            skipped = "plan state is blocked"
        if skipped:
            planned.append(PlannedSubtask(s.id, s.files, list(s.depends_on), None, None, None, "skipped", None,
                                          None, s.state, skipped))
            continue
        provider, model = options.assign.get(s.id, (None, None))
        if provider is None and s.owner_id:
            provider = provider_of(s.owner_id, options.providers)
        if provider is None:
            problems.append(f"subtask {s.id}: owner {s.owner!r} does not map to a provider in "
                            f"{', '.join(options.providers)}; use --assign {s.id}=PROVIDER")
        if provider is not None and not available(provider):
            problems.append(f"subtask {s.id}: provider {provider} is not installed or not on PATH")
        reviewer = None
        if s.review_waived:
            review = "waived"
        else:
            review = "required"
            if provider is not None:
                reviewer = choose_reviewer(provider, s.reviewer, options.providers, available)
                if reviewer is None:
                    problems.append(f"subtask {s.id}: no independent reviewer (a provider other than {provider}); "
                                    "waive review in the plan or add a provider")
        try:
            command = verification_command(plan, s.id)
        except Exception as exc:  # VerificationNotDeclared and friends
            command = None
            problems.append(f"subtask {s.id}: {exc}")
        else:
            if command is None:
                problems.append(f"subtask {s.id}: the plan declares no verification command for it")
        planned.append(PlannedSubtask(s.id, s.files, list(s.depends_on), wave_of.get(s.id), provider, model, review,
                                      reviewer, command, s.state))

    serialised = [(a.id, b.id) for i, a in enumerate(editing) for b in editing[i + 1:]
                  if wave_of.get(a.id) == wave_of.get(b.id) and areas_overlap(a.files, b.files)]
    if not options.verify_commands:
        warnings.append("--verify-command is required for --execute (the full suite on the integration branch)")
    if not any(p.skipped is None for p in planned):
        problems.append("nothing to run: no editing subtask in an open state")
    return JobPlan(task_id, plan.revision, plan.base_commit, default_branch, waves, planned, serialised, problems, warnings)


def format_plan(plan: JobPlan) -> str:
    lines = [f"job plan for task {plan.task_id} (plan revision {plan.plan_revision}, base {(plan.base_commit or '-')[:7]})",
             f"integration branch: {plan.integration_branch} (local; never pushed)"]
    for number, wave in enumerate(plan.waves, 1):
        lines.append(f"wave {number}: {', '.join(wave)}")
    for s in plan.subtasks:
        if s.skipped:
            lines.append(f"  {s.id}: skipped ({s.skipped})")
            continue
        review = f"{s.review}" + (f" by {s.reviewer_provider}" if s.reviewer_provider else "")
        lines.append(f"  {s.id}: wave {(s.wave or 0) + 1} {s.author_provider or '?'}"
                     f"{':' + s.model if s.model else ''}; review {review}; verify {s.verify_command or '-'}")
    for a, b in plan.serialised:
        lines.append(f"  {a} and {b} share files/area: they run one after the other")
    lines += [f"warning: {w}" for w in plan.warnings] + [f"PROBLEM: {p}" for p in plan.problems]
    lines.append("ready to start" if plan.ok else "not startable until the problems above are fixed")
    return "\n".join(lines)


# --- managed worker ids -----------------------------------------------------------------------


def worker_agent_id(job_id: str, provider: str, subtask_id: str, role: str, attempt: int) -> str:
    """``<provider>-<job suffix>-<subtask>-<i|r><n>``: unique per attempt, valid by the id grammar, never a
    reserved product name (an existing chat's id)."""
    suffix = re.sub(r"[^A-Za-z0-9]", "", job_id.rsplit("-", 1)[-1])[-8:] or "job"
    letter = {"implement": "i", "review": "r"}[role]
    agent = f"{provider}-{suffix}-{subtask_id}-{letter}{attempt}"
    error = id_error(agent, "agent id")
    if error or len(agent) > 128 or agent.lower() in RESERVED_AGENT_IDS:
        raise JobError(error or f"agent id {agent!r} is not usable")
    return agent


# --- events and derived state -----------------------------------------------------------------


def jobs_dir(state: StateRoot) -> Path:
    return state.path / JOBS_DIR


def job_dir(state: StateRoot, job_id: str) -> Path:
    error = id_error(job_id, "job id")
    if error:
        raise JobError(error)
    return jobs_dir(state) / job_id


def new_job_id(task_id: str, now: datetime | None = None) -> str:
    moment = now or datetime.now()
    return f"{task_id}-{moment.strftime('%Y%m%d-%H%M%S')}"


@dataclass
class SubtaskState:
    id: str
    state: str = "pending"
    provider: str | None = None
    model: str | None = None
    plan_state: str | None = None
    attempts: int = 0
    budget: dict[str, int] = field(default_factory=dict)
    """Corrections already spent, by cause (``verification_failed``, ``changes_requested``, ``timeout``, ...)."""
    commit: str | None = None
    reason: str | None = None
    attempt_ids: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobState:
    job_id: str
    task_id: str
    state: str = "created"
    reason: str | None = None
    plan_revision: int | None = None
    base_commit: str | None = None
    integration_branch: str | None = None
    started: str | None = None
    updated: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    subtasks: dict[str, SubtaskState] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    integration: dict[str, Any] = field(default_factory=dict)
    final_check: dict[str, Any] = field(default_factory=dict)
    events: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def append_event(directory: Path, kind: str, plan_revision: int | None, **data: Any) -> dict[str, Any]:
    """Append one event (flushed and fsynced) and return it. Every event carries the plan revision it
    acted on and a clock time; ``seq`` orders events within the minute. A torn last line left by a crash
    is terminated and logged first, never merged with the new event."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / EVENTS_FILE
    events, warnings = read_events(path)
    prefix = b""
    if path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n"):
        prefix = b"\n"
    event = {"seq": (events[-1]["seq"] + 1) if events else 1, "at": format_timestamp(), "kind": kind,
             "plan_revision": plan_revision, **data}
    lines = []
    if prefix:
        lines.append(json.dumps({"seq": event["seq"], "at": event["at"], "kind": "torn_line_ignored",
                                 "plan_revision": plan_revision, "warnings": warnings}) + "\n")
        event["seq"] += 1
    lines.append(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    with open(path, "ab") as handle:
        handle.write(prefix + "".join(lines).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    return event


def read_events(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Parsed events and warnings. A last line that is not valid JSON (power loss) is ignored and
    reported; a bad line earlier in the file is also skipped, with a different warning."""
    try:
        data = path.read_bytes().decode("utf-8", "replace")
    except FileNotFoundError:
        return [], []
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    lines = data.split("\n")
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict) or "kind" not in value:
                raise ValueError("not an event object")
        except ValueError:
            last = all(not rest.strip() for rest in lines[number:])
            warnings.append(f"{'torn last line' if last else 'unreadable line'} {number} ignored")
            continue
        events.append(value)
    return events, warnings


def apply_event(job: JobState | None, e: Mapping[str, Any]) -> JobState | None:
    """Apply one event to the state (``None`` until ``job_created``) and return it."""
    kind = e["kind"]
    if kind == "job_created":
        job = JobState(e["job_id"], e["task_id"], base_commit=e.get("base_commit"),
                       integration_branch=e.get("integration_branch"), started=e["at"],
                       options=dict(e.get("options", {})))
        for s in e.get("subtasks", []):
            job.subtasks[s["id"]] = SubtaskState(s["id"], provider=s.get("provider"), model=s.get("model"),
                                                 plan_state=s.get("plan_state"))
    if job is None:
        return None
    job.events += 1
    job.updated = e["at"]
    if e.get("plan_revision") is not None:
        job.plan_revision = e["plan_revision"]
    if kind == "job_state":
        job.state, job.reason = e["state"], e.get("reason")
    elif kind == "subtask_state":
        sub = job.subtasks.setdefault(e["subtask"], SubtaskState(e["subtask"]))
        sub.state, sub.reason = e["state"], e.get("reason")
        if e.get("commit"):
            sub.commit = e["commit"]
        for key, value in (e.get("detail") or {}).items():
            sub.detail[key] = value
    elif kind == "budget_spent":
        sub = job.subtasks.setdefault(e["subtask"], SubtaskState(e["subtask"]))
        sub.budget[e["cause"]] = sub.budget.get(e["cause"], 0) + 1
    elif kind == "attempt_started":
        sub = job.subtasks.setdefault(e["subtask"], SubtaskState(e["subtask"]))
        sub.attempts += 1
        sub.attempt_ids.append(e["attempt"])
        job.attempts.append({k: v for k, v in e.items() if k not in ("seq", "kind")} | {"finished": False})
    elif kind == "attempt_finished":
        for record in job.attempts:
            if record.get("attempt") == e["attempt"]:
                record.update({k: v for k, v in e.items() if k not in ("seq", "kind", "at")})
                record["finished"] = True
    elif kind == "integration":
        job.integration = {k: v for k, v in e.items() if k not in ("seq", "kind")}
    elif kind == "final_check":
        job.final_check = {k: v for k, v in e.items() if k not in ("seq", "kind")}
    return job


def replay(events: Iterable[Mapping[str, Any]]) -> JobState | None:
    """Derive the job state from its events. The result is a pure function of the events, so a deleted or
    stale ``job.json`` is rebuilt exactly."""
    job: JobState | None = None
    for e in events:
        job = apply_event(job, e)
    return job


def write_job_json(directory: Path, job: JobState) -> None:
    atomic_write(directory / JOB_FILE, json.dumps(job.as_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def load_job(state: StateRoot, job_id: str) -> tuple[JobState, list[str]]:
    """Replay the events (the truth); ``job.json`` is never trusted over them."""
    directory = job_dir(state, job_id)
    events, warnings = read_events(directory / EVENTS_FILE)
    job = replay(events)
    if job is None:
        raise JobError(f"no job {job_id!r} under {jobs_dir(state)}")
    return job, warnings


def list_jobs(state: StateRoot) -> list[JobState]:
    root = jobs_dir(state)
    found = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        job = replay(read_events(path / EVENTS_FILE)[0])
        if job is not None:
            found.append(job)
    return found


def create_job(state: StateRoot, plan: JobPlan, options: JobOptions, job_id: str | None = None) -> JobState:
    """Write the first event and the derived files for a new job (used by ``job start --execute``).
    The caller has checked ``plan.ok``; this launches nothing."""
    if not plan.ok:
        raise JobError("the plan has problems: " + "; ".join(plan.problems))
    job_id = job_id or new_job_id(plan.task_id)
    directory = job_dir(state, job_id)
    if (directory / EVENTS_FILE).exists():
        raise JobError(f"job {job_id} already exists")
    option_record = {k: v for k, v in asdict(options).items() if k != "assign"} | {
        "assign": {k: list(v) for k, v in options.assign.items()}}
    append_event(directory, "job_created", plan.plan_revision, job_id=job_id, task_id=plan.task_id,
                 base_commit=plan.base_commit, integration_branch=plan.integration_branch, options=option_record,
                 subtasks=[{"id": s.id, "provider": s.author_provider, "model": s.model, "plan_state": s.plan_state}
                           for s in plan.subtasks if not s.skipped],
                 waves=plan.waves, approved_provider_run=options.approved_provider_run)
    return refresh(state, job_id)


def refresh(state: StateRoot, job_id: str) -> JobState:
    """Rewrite ``job.json`` and the report from the events."""
    directory = job_dir(state, job_id)
    job, warnings = load_job(state, job_id)
    write_job_json(directory, job)
    write_report(directory, job, warnings)
    return job


# --- run report -------------------------------------------------------------------------------


def build_report(job: JobState, warnings: Sequence[str] = ()) -> dict[str, Any]:
    """Machine-readable run report (schema 1). Usage is only what the runner reported; nothing is
    estimated here. Built from the job state alone, so it can be regenerated at any time."""
    subtasks = []
    for sub in job.subtasks.values():
        attempts = [a for a in job.attempts if a.get("subtask") == sub.id]
        subtasks.append({
            "id": sub.id, "state": sub.state, "reason": sub.reason, "provider": sub.provider, "model": sub.model,
            "suggested_plan_state": PLAN_STATE_SUGGESTION.get(sub.state), "commit": sub.commit,
            "attempts": attempts, "corrections_spent": dict(sub.budget), "detail": dict(sub.detail),
        })
    not_verified = []
    if not job.final_check:
        not_verified.append("the full suite on the integration branch has not run")
    if not job.integration:
        not_verified.append("nothing has been integrated yet")
    for sub in job.subtasks.values():
        if sub.detail.get("review") == "waived":
            not_verified.append(f"{sub.id}: review waived in the plan")
        elif "reviewed" not in sub.detail.get("phases", []) and sub.state in ("integrated", "reviewed"):
            not_verified.append(f"{sub.id}: no review recorded")
    return {
        "schema": REPORT_SCHEMA, "job_id": job.job_id, "task_id": job.task_id, "state": job.state, "reason": job.reason,
        "plan_revision": job.plan_revision, "base_commit": job.base_commit, "integration_branch": job.integration_branch,
        "started": job.started, "updated": job.updated, "subtasks": subtasks, "integration": job.integration,
        "final_check": job.final_check, "not_verified": not_verified, "warnings": list(warnings),
        "next_steps": next_steps(job),
    }


def next_steps(job: JobState) -> list[str]:
    """Text for the user. The driver prints Git commands and never runs them: merging and pushing are the user's."""
    if job.state == "awaiting_approval" and job.integration_branch:
        return [
            f"Review the result: git log --oneline {job.base_commit or '<base>'}..{job.integration_branch}",
            f"If you approve, YOU run: git switch <your branch> ; git merge --no-ff {job.integration_branch}",
            "Pushing is a separate decision; the driver never pushes or merges into your branch.",
        ]
    if job.state == "paused_user":
        return [f"Needs the user: {job.reason or 'see the events'}. Then: multi-agent-ai job resume --job {job.job_id} --execute"]
    if job.state == "paused_plan_changed":
        return ["The plan changed while the job ran. The coordinator decides, then: "
                f"multi-agent-ai job resume --job {job.job_id} --execute"]
    return []


def format_report(report: Mapping[str, Any]) -> str:
    lines = [f"# Run report: job {report['job_id']}", "",
             f"- task: {report['task_id']} (plan revision {report['plan_revision']}, base {(report['base_commit'] or '-')[:7]})",
             f"- state: {report['state']}" + (f" ({report['reason']})" if report["reason"] else ""),
             f"- integration branch: {report['integration_branch']} (local)",
             f"- started {report['started']}, updated {report['updated']}", "", "## Subtasks", ""]
    for s in report["subtasks"]:
        lines.append(f"### {s['id']}: {s['state']}" + (f" ({s['reason']})" if s["reason"] else ""))
        lines.append(f"- provider {s['provider'] or '?'}{':' + s['model'] if s['model'] else ''}; commit {(s['commit'] or '-')[:12]};"
                     f" suggested plan state: {s['suggested_plan_state'] or '-'}")
        for a in s["attempts"]:
            lines.append(f"- attempt {a.get('attempt')} ({a.get('role', '?')}, {a.get('agent', '?')}): "
                         f"exit {a.get('exit_code', '-')}, class {a.get('failure_class') or 'none'}, "
                         f"usage {a.get('usage', 'unknown')}, run dir {a.get('run_dir', '-')}")
        lines.append("")
    if report["integration"]:
        lines += ["## Integration", "", f"- {json.dumps(report['integration'], ensure_ascii=False)}", ""]
    if report["final_check"]:
        lines += ["## Full suite", "", f"- {json.dumps(report['final_check'], ensure_ascii=False)}", ""]
    if report["not_verified"]:
        lines += ["## Not verified", ""] + [f"- {item}" for item in report["not_verified"]] + [""]
    if report["warnings"]:
        lines += ["## Warnings", ""] + [f"- {item}" for item in report["warnings"]] + [""]
    if report["next_steps"]:
        lines += ["## Next steps (for the user)", ""] + [f"- {item}" for item in report["next_steps"]] + [""]
    return "\n".join(lines)


def write_report(directory: Path, job: JobState, warnings: Sequence[str] = ()) -> dict[str, Any]:
    report = build_report(job, warnings)
    atomic_write(directory / REPORT_JSON, json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    atomic_write(directory / REPORT_MD, format_report(report))
    return report


def format_status(job: JobState) -> str:
    lines = [f"job {job.job_id}: {job.state}" + (f" ({job.reason})" if job.reason else ""),
             f"task {job.task_id}, plan revision {job.plan_revision}, updated {job.updated}"]
    for s in job.subtasks.values():
        suggestion = PLAN_STATE_SUGGESTION.get(s.state)
        lines.append(f"  {s.id}\t{s.state}\t{s.provider or '?'}\tattempts {s.attempts}"
                     + (f"\tplan would say: {suggestion}" if suggestion else ""))
    lines += [f"next: {step}" for step in next_steps(job)]
    return "\n".join(lines)
