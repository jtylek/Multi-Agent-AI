"""End-to-end job driver (T27), stage B: run a job. Concurrent workers, orchestrator verification, independent
review, dependency-ordered integration, the full suite, and a stop at ``awaiting_approval``.

Threads, not processes: a thread only blocks on :func:`run_worker`, which starts the provider CLI as a
child process (job object, process-tree kill, timeout) - see the design note, section 3. All scheduling,
integration and Git-record changes happen on the driver's own thread; pool threads run one subtask's
implement -> verify -> review pipeline and return a :class:`SubtaskResult`. Events are appended under a
lock. The driver never merges into the user's branch, never pushes, and never edits ``plan.md``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import job as jb
from . import verify as verify_mod
from . import winproc
from . import workspace as ws
from .board import read_plan
from .document import DocumentError, atomic_write, read_document
from .state import StateRoot
from .workers import failures as F
from .workers import gitops
from .workers.runner import UsageError, WorkerOutcome, WorkerRequest, run_worker

CLARIFY = ("Your previous attempt changed no file or produced output the orchestrator could not read. Make the "
           "change the subtask asks for inside the worktree and reply in the requested shape.")
USAGE_ERROR = "usage_error"
DRIVER_ERROR = "driver_error"
REVIEW_UNPARSED = "review_unparsed"
OK, FAILED, HALT = "ok", "failed", "halt"
CANCEL_MARKER = "cancel.requested"
LOCK_FILE = "driver.lock"
HEARTBEAT_SECONDS = 30.0
STALE_LOCK_SECONDS = 600.0
STALE = "stale"


@dataclass
class Hooks:
    """Injection points so tests need no provider and no clock games."""

    worker: Callable[[WorkerRequest], WorkerOutcome] = run_worker
    verify: Callable[..., verify_mod.VerificationResult] = verify_mod.verify
    available: Callable[[str], bool] = jb.provider_available
    env: Mapping[str, str] | None = None
    poll_seconds: float = 1.0


@dataclass
class SubtaskResult:
    status: str
    """``ok`` (reviewed or waived, ready to integrate), ``failed`` (this subtask only) or ``halt`` (the job
    stops scheduling and waits for the user)."""
    failure_class: str | None = None
    reason: str = ""
    tip: str | None = None
    halt_state: str = "paused_user"


@dataclass
class ReviewResult:
    kind: str
    """``agreed``, ``changes``, ``failed``."""
    failure_class: str | None = None
    text: str = ""
    reason: str = ""


class Recorder:
    """Thread-safe event writer that keeps the in-memory job state and ``job.json`` in step."""

    def __init__(self, state: StateRoot, job: jb.JobState):
        self.state, self.job = state, job
        self.dir = jb.job_dir(state, job.job_id)
        self.lock = threading.Lock()

    def emit(self, kind: str, **data: Any) -> dict[str, Any]:
        with self.lock:
            event = jb.append_event(self.dir, kind, self.job.plan_revision, **data)
            jb.apply_event(self.job, event)
            jb.write_job_json(self.dir, self.job)
            return event

    def report(self) -> None:
        with self.lock:
            jb.write_report(self.dir, self.job)

    def count_attempts(self, subtask: str, role: str) -> int:
        with self.lock:
            return sum(1 for a in self.job.attempts if a.get("subtask") == subtask and a.get("role") == role)

    def spend(self, subtask: str, cause: str, limit: int = 1) -> bool:
        """Take one correction from the budget (stored in the events, so a resume cannot reset it)."""
        with self.lock:
            if self.job.subtasks[subtask].budget.get(cause, 0) >= limit:
                return False
        self.emit("budget_spent", subtask=subtask, cause=cause)
        return True


class Engine:
    def __init__(self, state: StateRoot, project_root: Path, job: jb.JobState, options: jb.JobOptions,
                 plan: jb.JobPlan, hooks: Hooks | None = None):
        self.state, self.project, self.options, self.plan = state, project_root.resolve(), options, plan
        self.hooks = hooks or Hooks()
        self.rec = Recorder(state, job)
        self.job = job
        self.planned = {s.id: s for s in plan.subtasks if not s.skipped}
        self.protected = (*ws.DEFAULT_PROTECTED, *options.live_branches)
        self.worktrees_root = Path(options.worktrees_root).resolve() if options.worktrees_root else None
        self.paths: dict[str, Path] = {}
        self.semaphores = {p: threading.Semaphore(options.max_per_provider) for p in options.providers}
        self.cancel = threading.Event()
        self.start_at: dict[str, str] = {}
        """Resume points: subtask -> ``verify`` or ``integrate`` (everything else starts at implement)."""
        self.lock: DriverLock | None = None
        self._beat = 0.0

    # --- the loop (driver thread) -------------------------------------------------------------

    def run(self) -> jb.JobState:
        self.rec.emit("job_state", state="preparing")
        records = ws.read_records(self.state, self.plan.task_id)
        known = records.get(ws.INTEGRATION)
        if known is None or known.state == "removed" or not Path(known.path).is_dir():  # a resumed job reuses it
            try:
                ws.create_integration(self.project, self.state, self.plan.task_id, None, self.worktrees_root,
                                      self.protected, execute=True)
            except ws.WorkspaceError as exc:
                return self._rest("paused_user", f"integration branch: {exc.code}: {exc.message}")
            records = ws.read_records(self.state, self.plan.task_id)
        for sid in self.planned:
            if self.job.subtasks[sid].state not in ("pending", "blocked_by") and sid in records:
                self.paths[sid] = Path(records[sid].path)
        self.rec.emit("job_state", state="running")
        halt: tuple[str, str] | None = None
        pending = [sid for sid in self.planned if self.job.subtasks[sid].state == "pending"]
        resumed = [sid for sid in self.planned if sid in self.start_at]
        running: dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=self.options.max_parallel, thread_name_prefix="job") as pool:
            for sid in resumed:  # reconciled subtasks continue where they stopped
                running[pool.submit(self._pipeline, sid)] = sid
            while True:
                self._heartbeat()
                halt = halt or self._cancelled() or self._plan_changed()
                if halt is None:
                    halt = self._start_ready(pending, running, pool)
                if not running:
                    break
                try:
                    done, _ = wait(running, timeout=self.hooks.poll_seconds, return_when=FIRST_COMPLETED)
                except KeyboardInterrupt:  # Ctrl+C in a foreground driver = cancel; attempts are killed, evidence kept
                    self.cancel.set()
                    continue
                for future in done:
                    sid = running.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # a pipeline bug must not take the driver down
                        result = SubtaskResult(HALT, DRIVER_ERROR, f"{type(exc).__name__}: {exc}")
                    halt = self._finish_subtask(sid, result) or halt
        return self._conclude(halt)

    def _heartbeat(self) -> None:
        if self.lock is not None and time.monotonic() - self._beat >= HEARTBEAT_SECONDS:
            self.lock.beat()
            self._beat = time.monotonic()

    def _plan_changed(self) -> tuple[str, str] | None:
        parsed = read_plan(self.state.tasks / self.plan.task_id)
        if parsed.value is None:
            return "paused_plan_changed", f"the plan is no longer readable: {'; '.join(parsed.errors)}"
        if parsed.value.revision != self.job.plan_revision:
            return "paused_plan_changed", f"plan revision {self.job.plan_revision} -> {parsed.value.revision}"
        return None

    def _cancelled(self) -> tuple[str, str] | None:
        if (self.rec.dir / CANCEL_MARKER).exists():
            self.cancel.set()
        return ("cancelled", "cancel requested") if self.cancel.is_set() else None

    def _start_ready(self, pending: list[str], running: dict[Future, str], pool: ThreadPoolExecutor) -> tuple[str, str] | None:
        for sid in list(pending):
            if len(running) >= self.options.max_parallel:
                break
            deps = [d for d in self.planned[sid].depends_on if d in self.job.subtasks]
            broken = [d for d in deps if self.job.subtasks[d].state in ("failed", "blocked_by", "retained", "cancelled")]
            if broken:
                pending.remove(sid)
                self.rec.emit("subtask_state", subtask=sid, state="blocked_by", reason=f"depends on {', '.join(broken)}")
                continue
            if not all(self.job.subtasks[d].state == "integrated" for d in deps):
                continue
            if any(jb.areas_overlap(self.planned[sid].files, self.planned[r].files) for r in running.values()):
                continue  # overlapping files/area: one after the other
            record = ws.read_records(self.state, self.plan.task_id).get(sid)
            if record is None or record.state == "removed" or not Path(record.path).is_dir():
                try:
                    record = ws.create_subtask_worktree(self.project, self.state, self.plan.task_id, sid, "auto",
                                                        self.worktrees_root, self.protected, execute=True).record
                except ws.WorkspaceError as exc:
                    pending.remove(sid)
                    self.rec.emit("subtask_state", subtask=sid, state="failed", reason=f"worktree: {exc.code}: {exc.message}")
                    return "paused_user", f"{sid}: worktree {exc.code}: {exc.message}"
            pending.remove(sid)  # (a retried subtask reuses its retained worktree and branch, with the earlier commits)
            self.paths[sid] = Path(record.path)
            self.rec.emit("subtask_state", subtask=sid, state="ready",
                          detail={"branch": record.branch, "worktree": record.path, "base": record.base,
                                  "base_from": record.base_from})
            running[pool.submit(self._pipeline, sid)] = sid
        return None

    def _finish_subtask(self, sid: str, result: SubtaskResult) -> tuple[str, str] | None:
        if result.failure_class == F.CANCELLED:
            self.rec.emit("subtask_state", subtask=sid, state="cancelled", reason="cancel requested")
            return "cancelled", "cancel requested"
        if result.status == OK:
            self.rec.emit("integration_started", subtask=sid, tip=result.tip)  # logged before the action (design 3)
            report = ws.integrate(self.project, self.state, self.plan.task_id, [sid], self.protected, execute=True)
            item = report.items[0]
            self.rec.emit("integration", subtask=sid, result=item.result, code=item.code, detail=item.detail,
                          tip=report.tip, merge_commit=item.merge_commit, commit=item.commit)
            if item.result in ("merged", "already_merged"):
                self.rec.emit("subtask_state", subtask=sid, state="integrated", commit=item.commit,
                              detail={"merge_commit": item.merge_commit})
                return None
            cls = "merge_conflict" if item.result == "conflict" else (item.code or "integration_refused")
            self.rec.emit("subtask_state", subtask=sid, state="failed", reason=f"{cls}: {item.detail}")
            self._retain(sid, f"{cls}: {item.detail}")
            return "paused_user", f"{sid}: {jb.policy_for('merge_conflict' if item.result == 'conflict' else cls).reason}"
        self.rec.emit("subtask_state", subtask=sid, state="failed",
                      reason=f"{result.failure_class}: {result.reason}"[:500])
        self._retain(sid, f"{result.failure_class}: {result.reason}")
        if result.status == HALT:
            return result.halt_state, f"{sid}: {result.failure_class}: {result.reason}"[:500]
        return None

    def _retain(self, sid: str, reason: str) -> None:
        try:
            ws.retain_failed(self.state, self.plan.task_id, sid, reason[:300], execute=True)
        except ws.WorkspaceError as exc:  # evidence stays on disk either way
            self.rec.emit("note", subtask=sid, text=f"could not mark the worktree retained: {exc.code}")

    def _conclude(self, halt: tuple[str, str] | None) -> jb.JobState:
        if halt:
            return self._rest(*halt)
        states = {sid: self.job.subtasks[sid].state for sid in self.planned}
        bad = sorted(sid for sid, st in states.items() if st != "integrated")
        if bad:
            return self._rest("failed", f"not integrated: {', '.join(bad)} (see the report for evidence)")
        self.rec.emit("job_state", state="final_check")
        integration = ws.read_records(self.state, self.plan.task_id).get(ws.INTEGRATION)
        commands = tuple(verify_mod.CommandSpec(verify_mod.parse_command(c), name="full-suite")
                         for c in self.options.verify_commands)
        run_dir = self.rec.dir / "final-check"
        result = self.hooks.verify(Path(integration.path), verify_mod.VerificationSpec(commands=commands, expected_clean=True),
                                   run_dir=run_dir)
        self.rec.emit("final_check", status=result.status, run_dir=str(run_dir), error=result.error,
                      commands=[{"argv": c.argv, "status": c.status, "exit_code": c.exit_code} for c in result.commands])
        if not result.ok:
            return self._rest("failed", f"the full suite on the integration branch did not pass ({result.status})")
        return self._rest("awaiting_approval", None)

    def _rest(self, state: str, reason: str | None) -> jb.JobState:
        state = state if state in jb.JOB_STATES else "paused_user"
        self.rec.emit("job_state", state=state, reason=reason)
        self.rec.report()
        return self.job

    # --- one subtask (pool thread) -------------------------------------------------------------

    def _pipeline(self, sid: str) -> SubtaskResult:
        plan, path = self.planned[sid], self.paths[sid]
        feedback: str | None = None
        stage = self.start_at.pop(sid, "implement")
        tip = self.job.subtasks[sid].commit if stage != "implement" else None
        while True:
            if self.cancel.is_set():
                return SubtaskResult(HALT, F.CANCELLED, "cancel requested", halt_state="cancelled")
            if stage == "implement":
                try:
                    ws.preflight_for_execution(self.project, self.state, self.plan.task_id, sid, self.protected)
                except ws.WorkspaceError as exc:
                    return SubtaskResult(HALT, exc.code, exc.message)
                self.rec.emit("subtask_state", subtask=sid, state="implementing")
                out = self._attempt(sid, "implement", plan.author_provider, plan.model, path, feedback=feedback)
                if out.failure_class:
                    cause = out.failure_class
                    policy = jb.policy_for(cause)
                    if policy.action == jb.RETRY_ONCE and self._clean(path) and self.rec.spend(sid, cause):
                        feedback = CLARIFY if cause in (F.NO_CHANGES, F.MALFORMED_OUTPUT) else None
                        continue
                    return self._terminal(cause, policy, out.detail)
                tip = out.commit or gitops.git(path, "rev-parse", "HEAD").strip()
                self.rec.emit("subtask_state", subtask=sid, state="implemented", commit=tip)
                stage = "verify"
            if stage == "verify":
                verification = self._verify(sid, path, tip)
                if verification is not None:
                    cause, text = verification
                    policy = jb.policy_for(cause)
                    if policy.action == jb.FEEDBACK_ONCE and self.rec.spend(sid, cause):
                        feedback, stage = text, "implement"
                        continue
                    return self._terminal(cause, policy, text[-300:])
                self.rec.emit("subtask_state", subtask=sid, state="verified", commit=tip)
                if plan.review == "waived":
                    self.rec.emit("subtask_state", subtask=sid, state="reviewed", commit=tip, detail={"review": "waived"})
                    return SubtaskResult(OK, tip=tip)
                stage = "review"
            if stage == "review":
                review = self._review(sid, tip)
                if review.kind == "agreed":
                    self.rec.emit("subtask_state", subtask=sid, state="reviewed", commit=tip, detail={"review": "agreed"})
                    return SubtaskResult(OK, tip=tip)
                if review.kind == "failed":
                    cause = review.failure_class or REVIEW_UNPARSED
                    return self._terminal(cause, jb.policy_for(cause), review.reason)
                self.rec.emit("subtask_state", subtask=sid, state="changes_requested",
                              reason="the reviewer asked for changes")
                if self.rec.spend(sid, "changes_requested"):
                    feedback, stage = review.text, "implement"
                    continue
                return SubtaskResult(HALT, "changes_requested", "second review also asked for changes: " + review.text[:200])
            return SubtaskResult(OK, tip=tip)  # stage == "integrate": reviewed before the driver stopped

    def _terminal(self, cause: str, policy: jb.Policy, detail: str) -> SubtaskResult:
        reason = f"{policy.reason}. {detail}".strip()
        if policy.action == jb.CANCEL:
            return SubtaskResult(HALT, F.CANCELLED, reason, halt_state="cancelled")
        if policy.action in (jb.STOP_JOB, jb.PAUSE_USER):
            return SubtaskResult(HALT, cause, reason)
        return SubtaskResult(FAILED, cause, reason)

    @staticmethod
    def _clean(path: Path) -> bool:
        try:
            return not gitops.snapshot(path).status
        except gitops.GitError:
            return False

    @staticmethod
    def _detail(out: "Attempt") -> str:
        return out.detail

    def _attempt(self, sid: str, role: str, provider: str, model: str | None, worktree: Path, *,
                 feedback: str | None = None, commit: str | None = None) -> "Attempt":
        number = self.rec.count_attempts(sid, role) + 1
        agent = jb.worker_agent_id(self.job.job_id, provider, sid, role, number)
        timeout = self.options.review_timeout if role == "review" else self.options.worker_timeout
        self.rec.emit("attempt_started", subtask=sid, attempt=agent, agent=agent, role=role, provider=provider,
                      model=model, number=number, worktree=str(worktree), commit=commit)
        request = WorkerRequest(
            provider=provider, task_id=self.plan.task_id, subtask_id=sid, agent_id=agent,
            mode="review" if role == "review" else "implement", worktree=worktree, project_root=self.project,
            state_root=self.state, model=model, timeout=timeout, commit=commit,
            live_branches=tuple(self.options.live_branches), pass_env=tuple(self.options.pass_env),
            verify_command=self.planned[sid].verify_command, feedback=feedback, cancel=self.cancel)
        outcome: WorkerOutcome | None = None
        failure, detail = None, ""
        with self.semaphores[provider]:
            try:
                outcome = self.hooks.worker(request)
                failure = outcome.failure_class
                detail = str(outcome.result.get("detail") or "")
            except UsageError as exc:
                failure, detail = USAGE_ERROR, str(exc)
            except Exception as exc:
                failure, detail = DRIVER_ERROR, f"{type(exc).__name__}: {exc}"
        result = outcome.result if outcome else {}
        self.rec.emit("attempt_finished", attempt=agent, runner_attempt=outcome.attempt_id if outcome else None,
                      exit_code=outcome.exit_code if outcome else None, failure_class=failure, detail=detail[:300],
                      run_dir=str(outcome.run_dir) if outcome else None, usage=result.get("usage", "unknown"),
                      duration_seconds=result.get("duration_seconds"), commit=outcome.commit if outcome else None,
                      review_path=str(outcome.review_path) if outcome and outcome.review_path else None,
                      files_changed=result.get("files_changed"))
        return Attempt(failure, detail, outcome)

    def _verify(self, sid: str, worktree: Path, tip: str) -> tuple[str, str] | None:
        """None when verification passed, else (failure class, feedback text)."""
        self.rec.emit("subtask_state", subtask=sid, state="verifying", commit=tip)
        command = self.planned[sid].verify_command
        index = self.rec.count_attempts(sid, "implement")
        run_dir = self.rec.dir / "verify" / f"{sid}-{index}"
        try:
            spec = verify_mod.VerificationSpec(
                commands=(verify_mod.CommandSpec(verify_mod.parse_command(command), name=sid),),
                expected_commit=tip, expected_clean=True)
            result = self.hooks.verify(worktree, spec, run_dir=run_dir)
        except (ValueError, TypeError) as exc:
            self.rec.emit("subtask_state", subtask=sid, state="verification_failed", reason=str(exc)[:300])
            return "environment_error", str(exc)
        self.rec.emit("subtask_state", subtask=sid, state="verified" if result.ok else "verification_failed",
                      commit=tip, detail={"verification": {"status": result.status, "run_dir": result.run_dir,
                                                           "error": result.error}})
        if result.ok:
            return None
        return result.status, self._failure_text(result, run_dir)

    @staticmethod
    def _failure_text(result: verify_mod.VerificationResult, run_dir: Path) -> str:
        parts = [f"verification status: {result.status}"]
        if result.error:
            parts.append(result.error)
        if result.missing_files:
            parts.append("missing files: " + ", ".join(result.missing_files))
        for item in result.commands:
            if item.status == "passed":
                continue
            parts.append("$ " + " ".join(item.argv) + f" -> exit {item.exit_code}")
            for name in (item.stdout_file, item.stderr_file):
                file = Path(name)
                file = file if file.is_absolute() else run_dir / file
                try:
                    parts.append(file.read_bytes()[-3000:].decode("utf-8", "replace"))
                except OSError:
                    pass
        return "\n".join(parts)

    # --- review ---------------------------------------------------------------------------------

    def _review(self, sid: str, tip: str) -> ReviewResult:
        author = self.planned[sid].author_provider
        tried: list[str] = []
        candidates = [self.planned[sid].reviewer_provider] if self.planned[sid].reviewer_provider else []
        while True:
            if not candidates:
                return ReviewResult("failed", "no_independent_reviewer", reason="no provider other than the author's is left to review")
            reviewer = candidates.pop(0)
            tried.append(reviewer)
            self.rec.emit("subtask_state", subtask=sid, state="reviewing", commit=tip)
            try:
                path = self._review_worktree(sid, tip, len(tried))
            except gitops.GitError as exc:
                return ReviewResult("failed", F.ENVIRONMENT, reason=f"review worktree: {exc}")
            try:
                out = self._attempt(sid, "review", reviewer, None, path, commit=tip)
            finally:
                self._remove_review_worktree(path)
            if out.failure_class == F.REVIEW_MODIFIED_FILES:
                candidates = [p for p in self.options.providers if p != author and p not in tried and self.hooks.available(p)][:1]
                continue
            if out.failure_class:
                return ReviewResult("failed", out.failure_class, reason=out.detail)
            review_path = out.outcome.review_path if out.outcome else None
            status, commit = ws.review_commit(review_path) if review_path else (None, None)
            if status == "agreed" and commit and tip.startswith(commit[:7]) or status == "agreed" and commit and commit.startswith(tip[:7]):
                return ReviewResult("agreed")
            if status == "changes requested":
                return ReviewResult("changes", text=self._review_text(review_path))
            return ReviewResult("failed", REVIEW_UNPARSED,
                                reason=f"review verdict {status or 'missing'} for commit {commit or '?'}; nothing is inferred")

    @staticmethod
    def _review_text(path: Path | None) -> str:
        try:
            return read_document(path).body.strip()
        except (OSError, DocumentError, AttributeError):
            return "The reviewer asked for changes; the review file could not be read."

    def _review_root(self) -> Path:
        return self.worktrees_root or ws.default_worktrees_root(self.project)

    def _review_worktree(self, sid: str, sha: str, number: int) -> Path:
        path = self._review_root() / f"{self.project.name}-{sid}-review{number}-{self.job.job_id[-6:]}"
        path.parent.mkdir(parents=True, exist_ok=True)
        gitops.git(self.project, "worktree", "add", "--detach", str(path), sha)
        return path

    def _remove_review_worktree(self, path: Path) -> None:
        """Throwaway detached checkout owned by this driver; ``--force`` is for this path only."""
        try:
            gitops.git(self.project, "worktree", "remove", "--force", str(path))
        except gitops.GitError:
            self.rec.emit("note", text=f"review worktree {path} could not be removed")


@dataclass
class Attempt:
    failure_class: str | None
    detail: str
    outcome: WorkerOutcome | None
    @property
    def commit(self) -> str | None:
        return self.outcome.commit if self.outcome else None


# --- one driver per job ---------------------------------------------------------------------------


class DriverLock:
    """``driver.lock``: created exclusively, holds pid and heartbeat. Stale when the process is gone or the
    heartbeat is older than ``STALE_LOCK_SECONDS``; a stale lock is taken over and the takeover is logged."""

    def __init__(self, directory: Path, clock: Callable[[], datetime] = datetime.now):
        self.path, self.clock = directory / LOCK_FILE, clock
        self.started = ""

    def _info(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def stale(self, info: dict[str, Any] | None) -> str | None:
        """Why the lock may be taken over, or None when a live driver holds it."""
        if info is None:
            return "unreadable lock file"
        pid = info.get("pid")
        if not isinstance(pid, int) or not winproc.process_alive(pid):
            return f"driver process {pid} is not running"
        try:
            beat = datetime.fromisoformat(str(info.get("heartbeat")))
        except ValueError:
            return "no valid heartbeat"
        if (self.clock() - beat).total_seconds() > STALE_LOCK_SECONDS:
            return f"no heartbeat since {info.get('heartbeat')}"
        return None

    def acquire(self) -> str | None:
        """Take the lock. Returns the reason when a stale lock was taken over."""
        taken = None
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                info = self._info()
                why = self.stale(info)
                if why is None:
                    raise jb.JobError(f"job is being driven by process {info.get('pid')} (heartbeat {info.get('heartbeat')}); "
                                      "two drivers on one job are refused")
                taken = why
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass
                continue
            self.started = self.clock().isoformat(timespec="seconds")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._text())
            return taken
        raise jb.JobError("could not take the driver lock")

    def _text(self) -> str:
        return json.dumps({"pid": os.getpid(), "started": self.started,
                           "heartbeat": self.clock().isoformat(timespec="seconds")})

    def beat(self) -> None:
        atomic_write(self.path, self._text())

    def release(self) -> None:
        info = self._info()
        if info and info.get("pid") == os.getpid():
            try:
                os.unlink(self.path)
            except OSError:
                pass


# --- resume ---------------------------------------------------------------------------------------


def find_result(state: StateRoot, agent: str) -> dict[str, Any] | None:
    """The runner's ``result.json`` for the attempt run by ``agent``, if it finished writing one."""
    runs = state.path / "runs"
    for path in sorted(runs.glob("*/result.json")) if runs.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("agent") == agent:
            return data
    return None


def reconcile(state: StateRoot, rec: Recorder, retry: tuple[str, ...] = ()) -> dict[str, str]:
    """Bring a job whose driver stopped back to a consistent state without launching anything.

    Every attempt with a start event and no finish event is closed from the runner's ``result.json`` (reconciled)
    or, when there is none, as ``stale``. Each subtask then gets a resume point (``verify`` / ``integrate``), is
    left as it is, or fails as stale: a stale or failed attempt is never rerun automatically (AGENTS.md 18); only
    an explicit ``retry`` puts a failed subtask back to ``pending`` (and resets its correction budget)."""
    job = rec.job
    for record in [a for a in job.attempts if not a.get("finished")]:
        found = find_result(state, record["attempt"])
        if found:
            rec.emit("attempt_finished", attempt=record["attempt"], reconciled=True, runner_attempt=found.get("attempt"),
                     exit_code=found.get("exit_code"), failure_class=found.get("failure_class"),
                     detail=str(found.get("failure_detail") or "")[:300], run_dir=str(state.path / "runs" / found["attempt"]),
                     usage=found.get("usage", "unknown"), duration_seconds=found.get("duration_seconds"),
                     commit=found.get("commit_created"), files_changed=found.get("files_changed"))
        else:
            rec.emit("attempt_finished", attempt=record["attempt"], reconciled=True, exit_code=None, failure_class=STALE,
                     detail="the driver stopped while this attempt ran; no result.json was written", run_dir=None)
    start_at: dict[str, str] = {}
    for sid in list(job.subtasks):
        sub = job.subtasks[sid]
        if sid in retry and sub.state in ("failed", "cancelled", "retained", "blocked_by", "verification_failed", "changes_requested"):
            rec.emit("retry_requested", subtask=sid)
            rec.emit("subtask_state", subtask=sid, state="pending", reason="retry requested by the user")
            sub.budget.clear()
            continue
        if sub.state in ("pending", "integrated", "failed", "blocked_by", "retained", "cancelled"):
            continue
        last = next((a for a in reversed(job.attempts) if a.get("subtask") == sid and a.get("role") == "implement"), None)
        if sub.state in ("ready", "implementing"):
            if last and last.get("finished") and not last.get("failure_class") and last.get("commit"):
                rec.emit("subtask_state", subtask=sid, state="implemented", commit=last["commit"], reason="reconciled")
                start_at[sid] = "verify"
            else:
                why = (last or {}).get("failure_class") or "no attempt"
                rec.emit("subtask_state", subtask=sid, state="failed", reason=f"stale: the implement attempt did not finish ({why})")
                _retain(state, job.task_id, sid, "stale: the driver stopped during the implement attempt")
        elif sub.state in ("implemented", "verifying", "verified"):
            start_at[sid] = "verify"  # re-verification is deterministic and costs no provider quota
        elif sub.state == "reviewing":
            if _agreed_review_exists(state, job.task_id, sid, sub.commit):
                rec.emit("subtask_state", subtask=sid, state="reviewed", commit=sub.commit, reason="reconciled",
                         detail={"review": "agreed"})
                start_at[sid] = "integrate"
            else:
                rec.emit("subtask_state", subtask=sid, state="failed", reason="stale: the review did not finish")
        elif sub.state == "reviewed":
            start_at[sid] = "integrate"
        else:  # verification_failed / changes_requested: the correction was lost with the driver
            rec.emit("subtask_state", subtask=sid, state="failed", reason=f"stale: stopped in {sub.state}")
    for sid, sub in job.subtasks.items():  # dependants of retried subtasks may run again
        if sub.state == "blocked_by" and retry:
            rec.emit("subtask_state", subtask=sid, state="pending", reason="its dependency was retried")
    return start_at


def _retain(state: StateRoot, task_id: str, sid: str, reason: str) -> None:
    try:
        ws.retain_failed(state, task_id, sid, reason, execute=True)
    except ws.WorkspaceError:  # the worktree and its evidence stay on disk either way
        pass


def _agreed_review_exists(state: StateRoot, task_id: str, sid: str, tip: str | None) -> bool:
    for path in sorted((state.tasks / task_id).glob(f"review-{sid}-*.md")):
        status, commit = ws.review_commit(path)
        if status == "agreed" and commit and tip and (tip.startswith(commit) or commit.startswith(tip[:7])):
            return True
    return False


def options_from_record(record: Mapping[str, Any]) -> jb.JobOptions:
    """Rebuild the options a job was started with (stored in its first event)."""
    known = {f for f in jb.JobOptions.__dataclass_fields__}
    values = {k: v for k, v in record.items() if k in known and k != "assign"}
    for key in ("providers", "verify_commands", "live_branches", "pass_env"):
        if key in values:
            values[key] = tuple(values[key])
    values["assign"] = {k: (v[0], v[1]) for k, v in (record.get("assign") or {}).items()}
    return jb.JobOptions(**values)


RESUMABLE = ("paused_user", "paused_plan_changed", "running", "preparing", "final_check", "integrating",
             "reviewing", "verifying", "created")


def resume_job(state: StateRoot, project_root: Path, job_id: str, *, retry: tuple[str, ...] = (),
               verify_commands: tuple[str, ...] = (), hooks: Hooks | None = None,
               accept_plan: bool = True) -> jb.JobState:
    """Continue a job from its events after a pause or a driver crash. Takes the driver lock first."""
    hooks = hooks or Hooks()
    job, _ = jb.load_job(state, job_id)
    directory = jb.job_dir(state, job_id)
    if job.state == "cancelled":
        raise jb.JobError("a cancelled job is final: start a new job")
    if job.state == "awaiting_approval":
        raise jb.JobError("the job already finished and waits for the user; see 'job status'")
    if job.state == "failed" and not retry:
        raise jb.JobError("the job failed: look at the report, then use --retry SUBTASK for an explicit retry")
    if job.state not in (*RESUMABLE, "failed"):
        raise jb.JobError(f"cannot resume a job in state {job.state}")
    unknown = [sid for sid in retry if sid not in job.subtasks]
    if unknown:
        raise jb.JobError(f"--retry: no such subtask: {', '.join(unknown)}")
    lock = DriverLock(directory)
    taken = lock.acquire()
    try:
        options = options_from_record(job.options)
        if verify_commands:
            options.verify_commands = verify_commands
        if not options.verify_commands:
            raise jb.JobError("no full-suite command: pass --verify-command")
        plan = jb.build_plan(state, job.task_id, options, hooks.available)
        engine = Engine(state, project_root, job, options, plan, hooks)
        engine.lock = lock
        if taken:
            engine.rec.emit("lock_takeover", reason=taken)
        if plan.plan_revision != job.plan_revision:
            if not accept_plan:
                return _rest_only(engine, "paused_plan_changed", f"plan revision {job.plan_revision} -> {plan.plan_revision}")
            engine.rec.emit("plan_revision_accepted", revision=plan.plan_revision, previous=job.plan_revision)
        if not plan.ok:
            return _rest_only(engine, "paused_user", "the plan has problems: " + "; ".join(plan.problems))
        engine.rec.emit("job_resumed", retry=list(retry), previous_state=job.state)
        engine.start_at = reconcile(state, engine.rec, retry)
        return engine.run()
    finally:
        lock.release()


def _rest_only(engine: Engine, state: str, reason: str) -> jb.JobState:
    return engine._rest(state, reason)


def run_job(state: StateRoot, project_root: Path, job_id: str, options: jb.JobOptions, hooks: Hooks | None = None) -> jb.JobState:
    """Run a freshly created job in this process until it rests (holds the driver lock meanwhile)."""
    job, _ = jb.load_job(state, job_id)
    hooks = hooks or Hooks()
    plan = jb.build_plan(state, job.task_id, options, hooks.available)
    engine = Engine(state, project_root, job, options, plan, hooks)
    lock = DriverLock(jb.job_dir(state, job_id))
    lock.acquire()
    engine.lock = lock
    try:
        if plan.plan_revision != job.plan_revision:
            return _rest_only(engine, "paused_plan_changed", f"plan revision {job.plan_revision} -> {plan.plan_revision}")
        return engine.run()
    finally:
        lock.release()


def request_cancel(state: StateRoot, job_id: str, execute: bool = False) -> str:
    """Ask the driver of ``job_id`` to stop: writes ``cancel.requested`` (the driver polls it and kills the running
    attempts). Returns what happened. With no live driver the job is finalised here instead."""
    directory = jb.job_dir(state, job_id)
    job, _ = jb.load_job(state, job_id)
    if job.state in ("cancelled", "failed", "awaiting_approval"):
        raise jb.JobError(f"the job is already {job.state}")
    lock = DriverLock(directory)
    info = lock._info() if lock.path.exists() else None
    alive = info is not None and lock.stale(info) is None
    if not execute:
        return f"would write {CANCEL_MARKER} for job {job_id}" + ("" if alive else " and mark it cancelled (no live driver)")
    atomic_write(directory / CANCEL_MARKER, json.dumps({"requested": datetime.now().isoformat(timespec="seconds")}) + "\n")
    if alive:
        return f"cancel requested; the driver (pid {info['pid']}) stops its workers and rests the job as cancelled"
    rec = Recorder(state, job)
    rec.emit("job_state", state="cancelled", reason="cancelled with no live driver")
    rec.report()
    return "no live driver: the job is marked cancelled; worktrees and evidence are kept"
