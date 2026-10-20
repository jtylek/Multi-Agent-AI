"""Job driver stage B (T27): concurrent workers, verification, independent review, ordered integration, the
full suite and the stop at awaiting_approval. Real temporary Git repositories and real workspace/integration
code; the provider runs are fakes injected through Hooks (no executable of any provider is started)."""

import subprocess
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from multi_agent_ai import job as jb
from multi_agent_ai import job_run as jr
from multi_agent_ai import workspace as ws
from multi_agent_ai.board import render_plan, render_subtask_table
from multi_agent_ai.models import Plan, Subtask
from multi_agent_ai.state import init_state_root
from multi_agent_ai.verify import CommandResult, VerificationResult
from multi_agent_ai.workers import failures as F
from multi_agent_ai.workers.runner import WorkerOutcome, write_review

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")

GIT = shutil.which("git")
TASK = "demo"
ROWS = (
    Subtask("A", "multiply", "src/a.py", (), True, "codex-1", "copilot-1", "open"),
    Subtask("B", "divide", "src/b.py", (), True, "copilot-1", "codex-1", "open"),
    Subtask("C", "tests", "tests/c.py", ("A", "B"), True, "codex-1", "copilot-1", "open"),
)
VERIFY = """

## Verification

verify: A: `python -m pytest tests/test_a.py -q`
verify: B: `python -m pytest tests/test_b.py -q`
verify: C: `python -m pytest -q`
"""
SUITE = ("python -m pytest -q",)


def git(cwd, *args):
    out = subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


class World:
    """A tmp project, a plan, and fake provider behaviour."""

    def __init__(self, tmp_path, rows=ROWS, **option_kw):
        self.tmp = tmp_path
        self.project = tmp_path / "My Project"
        self.project.mkdir()
        git(self.project, "init", "-q", "-b", "main")
        hooks_dir = tmp_path / "no hooks"
        hooks_dir.mkdir()
        for key, value in (("user.name", "Test User"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false"),
                           ("core.hooksPath", str(hooks_dir)), ("core.autocrlf", "false")):
            git(self.project, "config", key, value)
        (self.project / ".gitignore").write_text(".multi-agent-ai/\n", encoding="utf-8")
        (self.project / "shared.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "base")
        self.base = git(self.project, "rev-parse", "HEAD")
        self.state = init_state_root(self.project)
        self.put_plan(rows, 3)
        self.wt_root = tmp_path / "wt dir"
        self.options = jb.JobOptions(verify_commands=SUITE, worktrees_root=str(self.wt_root), **option_kw)
        self.lock = threading.Lock()
        self.launches: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.script: dict[tuple, object] = {}
        """(subtask, role, n) -> behaviour: a failure class string, ('verdict', text), ('write', {file: text}), callable"""
        self.barrier: threading.Barrier | None = None
        self.verify_calls: list[tuple[str, Path]] = []
        self.verify_fail: dict[str, int] = {}

    def put_plan(self, rows, revision):
        body = "## Goal\n\nAdd multiply and divide.\n\n## Subtasks\n\n" + render_subtask_table(rows) + VERIFY
        plan = Plan(TASK, "lead", revision, "2026-10-20 10:00", body, base_commit=self.base,
                    integration_branch=f"task/{TASK}/integration")
        path = self.state.tasks / TASK / "plan.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_plan(plan), encoding="utf-8")

    # --- fakes ---------------------------------------------------------------------------------

    def worker(self, req):
        mode = "review" if req.mode == "review" else "implement"
        with self.lock:
            number = 1 + sum(1 for l in self.launches if l["subtask"] == req.subtask_id and l["mode"] == mode)
            self.launches.append({"subtask": req.subtask_id, "mode": mode, "provider": req.provider, "agent": req.agent_id,
                                  "feedback": req.feedback, "n": number, "t": time.monotonic(), "commit": req.commit,
                                  "worktree": req.worktree})
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            behaviour = self.script.get((req.subtask_id, mode, number))
            if self.barrier is not None and mode == "implement" and number == 1 and req.subtask_id in ("A", "B"):
                self.barrier.wait(timeout=20)  # both first-wave workers must be running at the same time
            run_dir = self.tmp / "runs" / req.agent_id
            run_dir.mkdir(parents=True, exist_ok=True)
            if callable(behaviour):
                behaviour(req)
                behaviour = None
            if isinstance(behaviour, str):  # a failure class
                return WorkerOutcome(req.agent_id, behaviour, 1, run_dir, {"detail": f"fake {behaviour}"})
            if mode == "implement":
                files = {f"{req.subtask_id}.txt": f"{req.subtask_id} attempt {number}\n"}
                if isinstance(behaviour, tuple) and behaviour[0] == "write":
                    files = behaviour[1]
                if isinstance(behaviour, tuple) and behaviour[0] == "nothing":
                    return WorkerOutcome(req.agent_id, F.NO_CHANGES, 0, run_dir, {})
                for name, text in files.items():
                    (req.worktree / name).write_text(text, encoding="utf-8")
                git(req.worktree, "add", "-A")
                git(req.worktree, "commit", "-q", "-m", f"{req.subtask_id} by {req.agent_id}")
                sha = git(req.worktree, "rev-parse", "HEAD")
                return WorkerOutcome(req.agent_id, None, 0, run_dir, {"usage": {"output_tokens": 7}, "duration_seconds": 0.1},
                                     commit=sha)
            if isinstance(behaviour, tuple) and behaviour[0] == "modify":
                return WorkerOutcome(req.agent_id, F.REVIEW_MODIFIED_FILES, 0, run_dir, {})
            verdict = behaviour[1] if isinstance(behaviour, tuple) and behaviour[0] == "verdict" else "agreed"
            sha = git(req.worktree, "rev-parse", "HEAD")
            path = self.state.tasks / TASK / f"review-{req.subtask_id}-{req.agent_id}.md"
            status = verdict if verdict in ("agreed", "changes requested") else "unparsed"
            write_review(path, [("status", status), ("subtask", req.subtask_id), ("agent", req.agent_id),
                                ("commit", sha)], f"verdict: {verdict}\nfindings: fake\n", req.agent_id)
            return WorkerOutcome(req.agent_id, None, 0, run_dir, {"usage": "unknown"}, review_path=path)
        finally:
            with self.lock:
                self.active -= 1

    def verify(self, worktree, spec, *, run_dir=None, environment=None):
        name = spec.commands[0].name
        with self.lock:
            self.verify_calls.append((name, Path(worktree)))
            left = self.verify_fail.get(name, 0)
            if left:
                self.verify_fail[name] = left - 1
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        if left:
            out = run_dir / "stdout.log"
            out.write_text("FAILED tests/test_a.py::test_mul - assert 6 == 7\n", encoding="utf-8")
            (run_dir / "stderr.log").write_text("", encoding="utf-8")
            cmd = CommandResult(list(spec.commands[0].argv), "failed", 1, 0.1, str(out), str(run_dir / "stderr.log"))
            return VerificationResult("verification_failed", "", "", 0.1, str(run_dir), [cmd])
        return VerificationResult("passed", "", "", 0.1, str(run_dir))

    def hooks(self):
        return jr.Hooks(worker=self.worker, verify=self.verify, available=lambda name: True, poll_seconds=0.05)

    def run(self, options=None):
        options = options or self.options
        plan = jb.build_plan(self.state, TASK, options, lambda name: True)
        assert plan.ok, plan.problems
        job = jb.create_job(self.state, plan, options, "demo-20261020-030000")
        final = jr.run_job(self.state, self.project, job.job_id, options, self.hooks())
        return final

    def launches_of(self, subtask, mode):
        return [l for l in self.launches if l["subtask"] == subtask and l["mode"] == mode]

    def integration_tree(self):
        path = Path(ws.read_records(self.state, TASK)["integration"].path)
        return sorted(p.name for p in path.iterdir() if p.is_file())


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_happy_path_concurrent_cross_reviewed_ordered_integration(world):
    world.barrier = threading.Barrier(2)
    job = world.run()
    assert job.state == "awaiting_approval", (job.state, job.reason)
    assert {s.id: s.state for s in job.subtasks.values()} == {"A": "integrated", "B": "integrated", "C": "integrated"}
    assert world.max_active >= 2  # A and B were running at the same time (the barrier would have broken otherwise)

    # review is by a provider other than the author's
    for sid, author in (("A", "codex"), ("B", "copilot"), ("C", "codex")):
        assert [l["provider"] for l in world.launches_of(sid, "implement")] == [author]
        reviewers = [l["provider"] for l in world.launches_of(sid, "review")]
        assert reviewers and all(r != author for r in reviewers)
    # the reviewer never reviewed in the author's own worktree
    assert all(l["worktree"].name != f"My Project-{l['subtask']}" for l in world.launches if l["mode"] == "review")

    # dependency order: C starts only after A and B are integrated
    c_start = world.launches_of("C", "implement")[0]["t"]
    events, _ = jb.read_events(jb.job_dir(world.state, job.job_id) / jb.EVENTS_FILE)
    integrated = [e for e in events if e["kind"] == "integration" and e["result"] == "merged"]
    assert [e["subtask"] for e in integrated][-1] == "C" and len(integrated) == 3
    assert world.launches_of("C", "implement")[0]["worktree"].is_dir()
    assert {"A.txt", "B.txt", "C.txt", "shared.txt"} <= set(world.integration_tree())
    assert c_start >= max(l["t"] for l in world.launches_of("A", "review") + world.launches_of("B", "review"))

    # the full suite ran once, on the integration worktree, and the user's branch is untouched
    assert world.verify_calls[-1][0] == "full-suite"
    assert world.verify_calls[-1][1].name == "My Project-integration"
    assert git(world.project, "rev-parse", "main") == world.base and git(world.project, "branch", "--show-current") == "main"
    assert "task/demo/integration" in git(world.project, "branch", "--list", "task/demo/*")

    # report and state files
    report = jb.build_report(job)
    assert report["state"] == "awaiting_approval" and "git merge --no-ff task/demo/integration" in " ".join(report["next_steps"])
    assert not any(a.get("failure_class") for a in report["subtasks"][0]["attempts"])
    assert (jb.job_dir(world.state, job.job_id) / jb.REPORT_MD).is_file()
    again, _ = jb.load_job(world.state, job.job_id)
    assert again.as_dict() == job.as_dict()  # replay of the events reproduces the live state


def test_max_parallel_one_runs_one_worker_at_a_time(tmp_path):
    w = World(tmp_path, max_parallel=1)
    assert w.run().state == "awaiting_approval" and w.max_active == 1


def test_overlapping_files_in_one_wave_are_serialised(tmp_path):
    rows = (Subtask("A", "x", "src/", (), True, "codex-1", "waived", "open"),
            Subtask("B", "y", "src/b.py", (), True, "copilot-1", "waived", "open"),
            Subtask("C", "z", "tests/c.py", ("A", "B"), True, "codex-1", "waived", "open"))
    w = World(tmp_path, rows=rows)
    assert w.run().state == "awaiting_approval" and w.max_active == 1


def test_quota_stops_the_job_without_retry_or_other_provider(world):
    world.script[("B", "implement", 1)] = F.QUOTA_EXHAUSTED
    job = world.run()
    assert job.state == "paused_user" and "B" in job.reason and F.QUOTA_EXHAUSTED in job.reason
    assert len(world.launches_of("B", "implement")) == 1  # no retry, no other provider
    assert job.subtasks["B"].state == "failed" and job.subtasks["C"].state == "pending"
    assert not world.launches_of("C", "implement")
    assert ws.read_records(world.state, TASK)["B"].state == "retained"  # evidence kept
    assert "Needs the user" in " ".join(jb.build_report(job)["next_steps"])


@pytest.mark.parametrize("cls", [F.AUTH, F.PERMISSION_DENIED, F.GIT_POLICY_VIOLATION, F.LAUNCH_ERROR, F.UNSUPPORTED_MODEL])
def test_stop_and_configuration_classes_pause_with_one_launch(world, cls):
    world.script[("A", "implement", 1)] = cls
    job = world.run()
    assert job.state == "paused_user" and len(world.launches_of("A", "implement")) == 1


def test_no_changes_gets_one_clarified_retry_then_fails_and_blocks_dependants(world):
    world.script[("A", "implement", 1)] = F.NO_CHANGES
    world.script[("A", "implement", 2)] = F.NO_CHANGES
    job = world.run()
    runs = world.launches_of("A", "implement")
    assert len(runs) == 2 and runs[1]["feedback"] == jr.CLARIFY
    assert job.state == "failed" and job.subtasks["A"].state == "failed"
    assert job.subtasks["C"].state == "blocked_by" and job.subtasks["B"].state == "integrated"
    assert job.subtasks["A"].budget == {F.NO_CHANGES: 1}


def test_timeout_is_retried_once_when_the_worktree_is_clean(world):
    world.script[("B", "implement", 1)] = F.TIMEOUT
    job = world.run()
    assert job.state == "awaiting_approval" and len(world.launches_of("B", "implement")) == 2


def test_timeout_with_a_dirty_worktree_is_not_retried(world):
    original = world.worker

    def worker(req):
        if req.subtask_id == "B" and req.mode == "implement":
            (req.worktree / "half.txt").write_text("partial", encoding="utf-8")
            with world.lock:
                world.launches.append({"subtask": "B", "mode": "implement", "provider": req.provider, "agent": req.agent_id,
                                       "feedback": None})
            return WorkerOutcome(req.agent_id, F.TIMEOUT, None, world.tmp, {})
        return original(req)
    world.worker = worker
    job = world.run()
    assert job.subtasks["B"].state == "failed" and len(world.launches_of("B", "implement")) == 1  # evidence kept, no retry
    assert (Path(ws.read_records(world.state, TASK)["B"].path) / "half.txt").is_file()


def test_verification_failure_gets_one_feedback_round_with_the_output_tail(world):
    world.verify_fail["A"] = 1
    job = world.run()
    assert job.state == "awaiting_approval"
    runs = world.launches_of("A", "implement")
    assert len(runs) == 2 and "assert 6 == 7" in runs[1]["feedback"] and runs[0]["feedback"] is None
    assert job.subtasks["A"].budget == {"verification_failed": 1}


def test_verification_failing_twice_fails_the_subtask(world):
    world.verify_fail["A"] = 2
    job = world.run()
    assert job.subtasks["A"].state == "failed" and job.state == "failed" and len(world.launches_of("A", "implement")) == 2


def test_changes_requested_is_one_feedback_round_then_pause(world):
    world.script[("A", "review", 1)] = ("verdict", "changes requested")
    job = world.run()
    assert job.state == "awaiting_approval"
    assert "findings: fake" in world.launches_of("A", "implement")[1]["feedback"]
    assert len(world.launches_of("A", "review")) == 2


def test_second_changes_requested_pauses_for_the_user(world):
    world.script[("A", "review", 1)] = ("verdict", "changes requested")
    world.script[("A", "review", 2)] = ("verdict", "changes requested")
    job = world.run()
    assert job.state == "paused_user" and "changes_requested" in job.reason
    assert job.subtasks["A"].state == "failed" and not world.launches_of("C", "implement")


def test_unparsed_verdict_is_never_inferred_as_agreed(world):
    world.script[("A", "review", 1)] = ("verdict", "i guess it is fine")
    job = world.run()
    assert job.state == "paused_user" and jr.REVIEW_UNPARSED in job.reason
    assert job.subtasks["A"].state == "failed"
    assert ws.read_records(world.state, TASK).get("A").state == "retained"


def test_reviewer_that_modified_files_cannot_be_overridden_by_an_agreed_review(world):
    world.script[("A", "review", 1)] = ("modify",)
    job = world.run()
    # only one other provider exists, and it already failed: the job stops instead of downgrading
    assert job.state == "paused_user" and "no_independent_reviewer" in job.reason
    assert job.subtasks["A"].state == "failed"


def test_review_waived_by_the_plan_only(tmp_path):
    rows = (Subtask("A", "x", "src/a.py", (), True, "codex-1", "waived", "open"),)
    w = World(tmp_path, rows=rows)
    w.body = None
    (w.state.tasks / TASK / "plan.md").write_text(
        (w.state.tasks / TASK / "plan.md").read_text(encoding="utf-8"), encoding="utf-8")
    job = w.run()
    assert job.state == "awaiting_approval" and not w.launches_of("A", "review")
    assert jb.build_report(job)["not_verified"].count("A: review waived in the plan") == 1


def test_plan_revision_change_pauses_scheduling(world):
    def bump(req):
        world.put_plan(ROWS, 4)
    world.script[("A", "implement", 1)] = bump
    world.script[("B", "implement", 1)] = bump
    job = world.run()
    assert job.state == "paused_plan_changed" and "3 -> 4" in job.reason
    assert not world.launches_of("C", "implement")
    assert job.subtasks["C"].state == "pending"


def test_merge_conflict_pauses_and_leaves_the_integration_branch(world):
    world.script[("A", "implement", 1)] = ("write", {"shared.txt": "A changed\ntwo\nthree\n"})
    world.script[("B", "implement", 1)] = ("write", {"shared.txt": "B changed\ntwo\nthree\n"})
    job = world.run()
    states = {job.subtasks["A"].state, job.subtasks["B"].state}
    assert job.state == "paused_user" and states == {"integrated", "failed"}
    assert "conflict" in job.reason.lower() or "merge" in job.reason.lower()
    status = git(Path(ws.read_records(world.state, TASK)["integration"].path), "status", "--porcelain")
    assert status == ""  # the failed merge was aborted, the branch is clean
    assert git(world.project, "rev-parse", "main") == world.base


def test_job_files_are_the_only_new_state_and_plan_is_untouched(world):
    plan_path = world.state.tasks / TASK / "plan.md"
    before = plan_path.read_bytes()
    world.run()
    assert plan_path.read_bytes() == before
    assert sorted(p.name for p in (world.state.path / "jobs").iterdir()) == ["demo-20261020-030000"]


def test_every_worker_id_is_unique_and_never_a_product_name(world):
    world.run()
    ids = [l["agent"] for l in world.launches]
    assert len(ids) == len(set(ids)) and not {i.lower() for i in ids} & {"codex", "copilot", "claude", "code"}


def test_integration_is_logged_before_it_happens(world, monkeypatch):
    real = ws.integrate
    seen = []

    def integrate(project, state, task, order, *a, **kw):
        events, _ = jb.read_events(jb.job_dir(state, "demo-20261020-030000") / jb.EVENTS_FILE)
        seen.append([(e["kind"], e.get("subtask")) for e in events if e["kind"].startswith("integration")])
        return real(project, state, task, order, *a, **kw)
    monkeypatch.setattr(ws, "integrate", integrate)
    job = world.run()
    assert job.state == "awaiting_approval" and len(seen) == 3
    for number, (sid, before) in enumerate(zip(("A", "B", "C"), seen)):
        assert ("integration_started", sid) in before  # the start event exists when the merge begins
        assert sum(1 for kind, _ in before if kind == "integration") == number  # earlier results only


def test_a_crash_inside_integration_leaves_a_start_event_and_no_result(world, monkeypatch):
    def boom(*a, **kw):
        raise SystemExit("power loss during the merge")
    monkeypatch.setattr(ws, "integrate", boom)
    with pytest.raises(SystemExit):
        world.run()
    events, _ = jb.read_events(jb.job_dir(world.state, "demo-20261020-030000") / jb.EVENTS_FILE)
    kinds = [e["kind"] for e in events]
    assert "integration_started" in kinds and "integration" not in kinds
