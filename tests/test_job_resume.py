"""Job driver stage C (T27): driver lock, cancel, crash/resume reconciliation, --retry. Fake workers only."""

import json
import threading
import time
from datetime import datetime, timedelta

import pytest

from multi_agent_ai import job as jb
from multi_agent_ai import job_run as jr
from multi_agent_ai import workspace as ws
from multi_agent_ai.workers import failures as F
from multi_agent_ai.workers.runner import WorkerOutcome

from test_job_run import ROWS, TASK, World  # noqa: F401  (shared fixtures: tmp repo, plan, fake providers)

JOB = "demo-20261020-030000"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def create(world, options=None):
    options = options or world.options
    plan = jb.build_plan(world.state, TASK, options, lambda name: True)
    assert plan.ok, plan.problems
    return jb.create_job(world.state, plan, options, JOB)


def start(world):
    create(world)
    return jr.run_job(world.state, world.project, JOB, world.options, world.hooks())


def resume(world, **kw):
    return jr.resume_job(world.state, world.project, JOB, hooks=world.hooks(), **kw)


# --- the driver lock ------------------------------------------------------------------------------


def test_lock_is_exclusive_and_released(tmp_path):
    first = jr.DriverLock(tmp_path)
    assert first.acquire() is None
    with pytest.raises(jb.JobError, match="two drivers"):
        jr.DriverLock(tmp_path).acquire()
    first.release()
    assert not (tmp_path / jr.LOCK_FILE).exists()
    assert jr.DriverLock(tmp_path).acquire() is None


def test_lock_of_a_dead_process_is_taken_over(tmp_path):
    (tmp_path / jr.LOCK_FILE).write_text(json.dumps({"pid": 2_000_000_000, "heartbeat": datetime.now().isoformat()}),
                                         encoding="utf-8")
    taken = jr.DriverLock(tmp_path).acquire()
    assert taken and "not running" in taken


def test_a_live_process_with_an_old_heartbeat_needs_an_explicit_force(tmp_path):
    import os
    old = (datetime.now() - timedelta(seconds=jr.STALE_LOCK_SECONDS + 60)).isoformat()
    (tmp_path / jr.LOCK_FILE).write_text(json.dumps({"pid": os.getpid(), "heartbeat": old}), encoding="utf-8")
    with pytest.raises(jb.JobError, match="--force-takeover"):
        jr.DriverLock(tmp_path).acquire()
    assert "forced takeover" in jr.DriverLock(tmp_path).acquire(force=True)
    with pytest.raises(jb.JobError, match="two drivers"):
        jr.DriverLock(tmp_path).acquire(force=True)  # now fresh: even force does not steal a live, beating lock


def test_lock_with_an_unreadable_old_file_is_stale_but_a_brand_new_one_is_being_created(tmp_path):
    import os
    path = tmp_path / jr.LOCK_FILE
    path.write_text("", encoding="utf-8")  # between O_EXCL create and write (C4)
    with pytest.raises(jb.JobError, match="being created"):
        jr.DriverLock(tmp_path).acquire()
    os.utime(path, (time.time() - 60, time.time() - 60))
    assert "unreadable" in jr.DriverLock(tmp_path).acquire()


def test_stale_takeover_race_has_exactly_one_winner(tmp_path):
    for round_ in range(40):
        directory = tmp_path / f"r{round_}"
        directory.mkdir()
        (directory / jr.LOCK_FILE).write_text(json.dumps({"pid": 2_000_000_000, "heartbeat": datetime.now().isoformat()}),
                                              encoding="utf-8")
        barrier, results = threading.Barrier(2), []

        def contender():
            lock = jr.DriverLock(directory)
            barrier.wait()
            try:
                lock.acquire()
                results.append("got")
            except jb.JobError:
                results.append("refused")
        threads = [threading.Thread(target=contender) for _ in range(2)]
        [x.start() for x in threads]
        [x.join() for x in threads]
        assert results.count("got") == 1, (round_, results)


def test_the_heartbeat_thread_keeps_the_lock_fresh_during_a_long_call(world):
    seen = []
    lock_file = jb.job_dir(world.state, JOB) / jr.LOCK_FILE

    def slow_suite(cancel):
        seen.append(json.loads(lock_file.read_text(encoding="utf-8"))["heartbeat"])
        time.sleep(0.6)  # no driver-loop activity meanwhile: only the timer thread can beat
        seen.append(json.loads(lock_file.read_text(encoding="utf-8"))["heartbeat"])
    world.verify_hook["full-suite"] = slow_suite
    assert start(world).state == "awaiting_approval"
    assert seen[0] != seen[1]


def test_two_drivers_on_one_job_are_refused(world):
    create(world)
    lock = jr.DriverLock(jb.job_dir(world.state, JOB))
    lock.acquire()
    with pytest.raises(jb.JobError, match="two drivers"):
        jr.run_job(world.state, world.project, JOB, world.options, world.hooks())
    with pytest.raises(jb.JobError, match="two drivers"):
        resume(world)
    lock.release()
    assert not world.launches  # nothing was started


def test_the_lock_is_released_after_a_run(world):
    start(world)
    assert not (jb.job_dir(world.state, JOB) / jr.LOCK_FILE).exists()


# --- cancel ---------------------------------------------------------------------------------------


def test_cancel_stops_running_workers_and_rests_as_cancelled(world):
    entered, killed = threading.Event(), []
    original = world.worker

    def worker(req):
        if req.subtask_id in ("A", "B") and req.mode == "implement":
            entered.set()
            assert req.cancel is not None
            if req.cancel.wait(timeout=30):  # what winproc.run does: the tree is killed, the attempt ends cancelled
                killed.append(req.subtask_id)
                return WorkerOutcome(req.agent_id, F.CANCELLED, None, world.tmp, {})
            return WorkerOutcome(req.agent_id, F.TIMEOUT, None, world.tmp, {})
        return original(req)
    world.worker = worker
    create(world)
    result = {}
    thread = threading.Thread(target=lambda: result.update(job=jr.run_job(
        world.state, world.project, JOB, world.options, world.hooks())))
    thread.start()
    assert entered.wait(timeout=60)
    message = jr.request_cancel(world.state, JOB, execute=True)
    thread.join(timeout=60)
    assert not thread.is_alive() and "cancel requested" in message
    job = result["job"]
    assert job.state == "cancelled" and sorted(killed) == ["A", "B"]
    assert {job.subtasks[s].state for s in ("A", "B")} == {"cancelled"} and job.subtasks["C"].state == "pending"
    assert not world.launches_of("C", "implement") and not world.launches_of("A", "review")
    assert (jb.job_dir(world.state, JOB) / jr.CANCEL_MARKER).is_file()
    with pytest.raises(jb.JobError, match="cancelled job is final"):
        resume(world)


def test_cancel_with_no_live_driver_marks_the_job_cancelled(world):
    create(world)
    dry = jr.request_cancel(world.state, JOB, execute=False)
    assert "would write" in dry and jb.load_job(world.state, JOB)[0].state == "created"
    assert "no live driver" in jr.request_cancel(world.state, JOB, execute=True)
    assert jb.load_job(world.state, JOB)[0].state == "cancelled"
    with pytest.raises(jb.JobError, match="already cancelled"):
        jr.request_cancel(world.state, JOB, execute=True)


# --- resume ---------------------------------------------------------------------------------------


def test_resume_after_a_pause_runs_only_what_is_left(world):
    world.script[("B", "implement", 1)] = F.QUOTA_EXHAUSTED
    job = start(world)
    assert job.state == "paused_user" and job.subtasks["A"].state == "integrated"
    with pytest.raises(jb.JobError, match="--retry"):
        resume(world, retry=("nope",))
    world.script.clear()
    final = resume(world, retry=("B",))
    assert final.state == "awaiting_approval", (final.state, final.reason)
    assert len(world.launches_of("A", "implement")) == 1  # integrated work is never redone
    assert len(world.launches_of("B", "implement")) == 2  # the explicit retry
    assert final.subtasks["B"].budget == {}  # a user retry resets the correction budget
    events, _ = jb.read_events(jb.job_dir(world.state, JOB) / jb.EVENTS_FILE)
    assert {e["kind"] for e in events} >= {"job_resumed", "retry_requested"}


def test_plan_revision_change_is_accepted_by_resuming(world):
    def bump(req):
        world.put_plan(ROWS, 4)  # the coordinator revises the plan while the first wave runs
    world.script[("A", "implement", 1)] = bump
    job = start(world)
    assert job.state == "paused_plan_changed" and job.plan_revision == 3
    world.script.clear()
    final = resume(world)
    assert final.state == "awaiting_approval" and final.plan_revision == 4
    events, _ = jb.read_events(jb.job_dir(world.state, JOB) / jb.EVENTS_FILE)
    assert any(e["kind"] == "plan_revision_accepted" and e["revision"] == 4 for e in events)
    assert len(world.launches_of("A", "implement")) == 1


def test_crashed_driver_is_reconciled_not_rerun(world):
    original = world.worker

    def worker(req):
        if req.subtask_id == "B" and req.mode == "implement":
            raise SystemExit("simulated power loss")  # not an Exception: the driver dies mid-attempt
        return original(req)
    world.worker = worker
    create(world)
    with pytest.raises(SystemExit):
        jr.run_job(world.state, world.project, JOB, world.options, world.hooks())
    # a crash leaves the lock behind: simulate it with a dead pid
    (jb.job_dir(world.state, JOB) / jr.LOCK_FILE).write_text(
        json.dumps({"pid": 2_000_000_000, "heartbeat": datetime.now().isoformat()}), encoding="utf-8")
    crashed, _ = jb.load_job(world.state, JOB)
    assert any(not a.get("finished") for a in crashed.attempts)  # B's attempt has a start and no end

    world.worker = original
    before = len(world.launches_of("B", "implement"))
    job = resume(world)  # no --retry: B must NOT be started again
    assert len(world.launches_of("B", "implement")) == before
    assert job.state == "failed" and job.subtasks["B"].state == "failed" and "stale" in (job.subtasks["B"].reason or "")
    assert job.subtasks["C"].state == "blocked_by"
    assert all(a.get("finished") for a in job.attempts)
    assert any(a.get("failure_class") == jr.STALE and a.get("reconciled") for a in job.attempts)
    events, _ = jb.read_events(jb.job_dir(world.state, JOB) / jb.EVENTS_FILE)
    assert any(e["kind"] == "lock_takeover" for e in events)
    assert ws.read_records(world.state, TASK)["B"].state == "retained"

    final = resume(world, retry=("B",))  # the explicit act
    assert final.state == "awaiting_approval", (final.state, final.reason)
    assert len(world.launches_of("B", "implement")) == before + 1
    assert final.subtasks["C"].state == "integrated"


def test_attempt_that_finished_before_the_crash_is_ingested_from_result_json(world):
    create(world)
    job, _ = jb.load_job(world.state, JOB)
    rec = jr.Recorder(world.state, job)
    rec.emit("subtask_state", subtask="A", state="implementing")
    rec.emit("attempt_started", subtask="A", attempt="codex-030000-A-i1", agent="codex-030000-A-i1", role="implement",
             provider="codex", number=1)
    run_dir = world.state.path / "runs" / "att-1"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps({
        "attempt": "att-1", "agent": "codex-030000-A-i1", "exit_code": 0, "failure_class": None, "usage": {"output_tokens": 3},
        "commit_created": "a" * 40, "files_changed": ["A.txt"]}), encoding="utf-8")
    start_at = jr.reconcile(world.state, rec)
    assert start_at == {"A": "verify"}
    assert rec.job.subtasks["A"].state == "implemented" and rec.job.subtasks["A"].commit == "a" * 40
    attempt = rec.job.attempts[0]
    assert attempt["finished"] and attempt["reconciled"] and attempt["usage"] == {"output_tokens": 3}


def test_resume_refuses_finished_and_failed_jobs_without_retry(world):
    job = start(world)
    assert job.state == "awaiting_approval"
    with pytest.raises(jb.JobError, match="already finished"):
        resume(world)


def test_options_survive_the_events(world):
    job = create(world)
    rebuilt = jr.options_from_record(job.options)
    assert rebuilt.verify_commands == world.options.verify_commands and rebuilt.providers == world.options.providers
    assert rebuilt.max_parallel == world.options.max_parallel and rebuilt.worktrees_root == world.options.worktrees_root
    custom = jb.JobOptions(assign={"A": ("copilot", "m")}, verify_commands=("x",))
    job2 = jb.create_job(world.state, jb.build_plan(world.state, TASK, custom, lambda name: True), custom, "demo-20261020-040000")
    assert jr.options_from_record(job2.options).assign == {"A": ("copilot", "m")}


def test_cli_resume_and_cancel_are_dry_runs_by_default(world, capsys, monkeypatch):
    from multi_agent_ai.cli import main
    create(world)
    base = ["--project-root", str(world.project), "--job", JOB]
    before = (jb.job_dir(world.state, JOB) / jb.EVENTS_FILE).read_bytes()
    assert main(["job", "resume", *base, "--retry", "A"]) == 0
    assert main(["job", "cancel", *base]) == 0
    assert (jb.job_dir(world.state, JOB) / jb.EVENTS_FILE).read_bytes() == before
    assert not (jb.job_dir(world.state, JOB) / jr.CANCEL_MARKER).exists()
    assert main(["job", "resume", *base, "--execute"]) == 1 and "--approved-provider-run" in capsys.readouterr().err
    called = []
    monkeypatch.setattr(jr, "resume_job", lambda *a, **k: called.append(k) or jb.load_job(world.state, JOB)[0])
    main(["job", "resume", *base, "--execute", "--approved-provider-run", "--retry", "A"])
    assert called and called[0]["retry"] == ("A",)
    assert main(["job", "cancel", *base, "--execute"]) == 0
    assert jb.load_job(world.state, JOB)[0].state == "cancelled"


# --- cancel reaches verification (C3) ----------------------------------------------------------------


def test_cancel_stops_a_running_verification(world):
    from multi_agent_ai.verify import VerificationResult
    entered = threading.Event()
    original = world.verify

    def verify(worktree, spec, *, run_dir=None, environment=None, cancel=None):
        if spec.commands[0].name == "A":
            entered.set()
            assert cancel is not None and cancel.wait(timeout=30)
            run_dir.mkdir(parents=True, exist_ok=True)
            return VerificationResult("cancelled", "", "", 0.1, str(run_dir))
        return original(worktree, spec, run_dir=run_dir, environment=environment, cancel=cancel)
    world.verify = verify
    create(world)
    result = {}
    thread = threading.Thread(target=lambda: result.update(job=jr.run_job(
        world.state, world.project, JOB, world.options, world.hooks())))
    thread.start()
    assert entered.wait(timeout=60)
    jr.request_cancel(world.state, JOB, execute=True)
    thread.join(timeout=60)
    assert not thread.is_alive() and result["job"].state == "cancelled"
    assert result["job"].subtasks["A"].state == "cancelled" and not world.launches_of("A", "review")


def test_verify_kills_the_command_tree_when_cancelled(tmp_path):
    import subprocess, sys, shutil
    from multi_agent_ai import verify as verify_mod
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([shutil.which("git"), "init", "-q"], cwd=repo, check=True)
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    spec = verify_mod.VerificationSpec(commands=(verify_mod.CommandSpec((sys.executable, "-c", "import time; time.sleep(120)")),),
                                       timeout=120)
    started = time.monotonic()
    result = verify_mod.verify(repo, spec, run_dir=tmp_path / "run", cancel=cancel)
    assert result.status == "cancelled" and time.monotonic() - started < 30
    assert result.commands[0].status == "cancelled"


# --- a crashed review is retained and its worktree cleaned (C5) ------------------------------------


def test_crash_during_a_review_is_retained_and_the_leftover_worktree_is_removed(world, monkeypatch):
    original = world.worker

    def worker(req):
        if req.subtask_id == "A" and req.mode == "review":
            raise SystemExit("power loss during the review")
        return original(req)
    world.worker = worker
    monkeypatch.setattr(jr.Engine, "_remove_review_worktree", lambda self, path: None)  # a crash leaves it behind
    create(world)
    with pytest.raises(SystemExit):
        jr.run_job(world.state, world.project, JOB, world.options, world.hooks())
    leftovers = list(world.wt_root.glob("My Project-A-review*"))
    assert leftovers
    (jb.job_dir(world.state, JOB) / jr.LOCK_FILE).write_text(
        json.dumps({"pid": 2_000_000_000, "heartbeat": datetime.now().isoformat()}), encoding="utf-8")
    monkeypatch.undo()
    world.worker = original
    job = resume(world)
    assert job.subtasks["A"].state == "failed" and "stale" in job.subtasks["A"].reason
    assert ws.read_records(world.state, TASK)["A"].state == "retained"
    assert not list(world.wt_root.glob("My Project-A-review*"))
    final = resume(world, retry=("A",))
    assert final.state == "awaiting_approval", (final.state, final.reason)


def test_retry_reopens_only_the_dependants_of_the_retried_subtask(tmp_path):
    from multi_agent_ai.models import Subtask
    rows = (Subtask("A", "chain 1 start", "a/", (), True, "codex-1", "waived", "open"),
            Subtask("A2", "chain 1 next", "a2/", ("A",), True, "codex-1", "waived", "open"),
            Subtask("A3", "chain 1 end", "a3/", ("A2",), True, "codex-1", "waived", "open"),
            Subtask("B", "chain 2 start", "b/", (), True, "copilot-1", "waived", "open"),
            Subtask("B2", "chain 2 next", "b2/", ("B",), True, "copilot-1", "waived", "open"))
    w = World(tmp_path, rows=rows)
    w.put_plan(rows, 3)
    import re
    text = (w.state.tasks / TASK / "plan.md").read_text(encoding="utf-8")
    verify = "".join(f"verify: {sid}: `python -m pytest -q`\n" for sid in ("A", "A2", "A3", "B", "B2"))
    (w.state.tasks / TASK / "plan.md").write_text(re.sub(r"verify: .*\n", "", text) + "\n" + verify, encoding="utf-8")
    w.script[("A", "implement", 1)] = F.NO_CHANGES
    w.script[("A", "implement", 2)] = F.NO_CHANGES  # chain 1 fails at its start
    w.script[("B", "implement", 1)] = F.NO_CHANGES
    w.script[("B", "implement", 2)] = F.NO_CHANGES  # chain 2 fails at its start
    job = start(w)
    assert {s: job.subtasks[s].state for s in rows_ids(rows)} == {
        "A": "failed", "A2": "blocked_by", "A3": "blocked_by", "B": "failed", "B2": "blocked_by"}
    w.script.clear()
    events_before, _ = jb.read_events(jb.job_dir(w.state, JOB) / jb.EVENTS_FILE)
    final = resume(w, retry=("A",))
    assert final.subtasks["A"].state == "integrated" and final.subtasks["A2"].state == "integrated"
    assert final.subtasks["A3"].state == "integrated"  # transitive dependant reopened and run
    assert final.subtasks["B"].state == "failed" and final.subtasks["B2"].state == "blocked_by"
    events, _ = jb.read_events(jb.job_dir(w.state, JOB) / jb.EVENTS_FILE)
    new = events[len(events_before):]
    assert not [e for e in new if e["kind"] == "subtask_state" and e["subtask"] in ("B", "B2")]  # no transition at all
    assert not w.launches_of("B", "implement")[2:]  # chain 2 was not run again


def rows_ids(rows):
    return [r.id for r in rows]
