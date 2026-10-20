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


def test_lock_with_an_old_heartbeat_is_taken_over_but_a_fresh_one_is_not(tmp_path):
    import os
    old = (datetime.now() - timedelta(seconds=jr.STALE_LOCK_SECONDS + 60)).isoformat()
    (tmp_path / jr.LOCK_FILE).write_text(json.dumps({"pid": os.getpid(), "heartbeat": old}), encoding="utf-8")
    assert "no heartbeat since" in jr.DriverLock(tmp_path).acquire()
    with pytest.raises(jb.JobError):
        jr.DriverLock(tmp_path).acquire()  # now fresh, same live pid


def test_lock_with_an_unreadable_file_is_stale(tmp_path):
    (tmp_path / jr.LOCK_FILE).write_text("{not json", encoding="utf-8")
    assert "unreadable" in jr.DriverLock(tmp_path).acquire()


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
