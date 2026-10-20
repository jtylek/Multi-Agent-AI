"""Job driver stage A (T27): events, state replay, policy, dry-run plan, report, CLI. Nothing is launched."""

import json

import pytest

from multi_agent_ai import job as jb
from multi_agent_ai.board import render_plan, render_subtask_table
from multi_agent_ai.cli import main
from multi_agent_ai.models import Plan, Subtask
from multi_agent_ai.state import init_state_root
from multi_agent_ai.workers import failures as F

TASK = "demo"
BASE = "a" * 40
ROWS = (
    Subtask("A", "multiply", "src/mul.py", (), True, "codex-1", "copilot-1", "open"),
    Subtask("B", "divide", "src/div.py", (), True, "copilot-1", "codex-1", "open"),
    Subtask("C", "tests", "tests/", ("A", "B"), True, "codex-1", "waived", "open"),
    Subtask("D", "old", "x.txt", (), True, "codex-1", "waived", "done"),
    Subtask("E", "notes", "docs/", (), False, "codex-1", "waived", "open"),
)
VERIFY = """

## Verification

verify: A: `python -m pytest tests/test_mul.py -q`
verify: B: `python -m pytest tests/test_div.py -q`
verify: C: `python -m pytest -q`
"""


def put_plan(state, rows=ROWS, revision=3, tail=VERIFY):
    body = "## Goal\n\nAdd multiply and divide.\n\n## Subtasks\n\n" + render_subtask_table(rows) + tail
    plan = Plan(TASK, "claude-a7", revision, "2026-10-20 02:00", body, base_commit=BASE,
                integration_branch=f"task/{TASK}/integration")
    path = state.tasks / TASK / "plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_plan(plan), encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    state = init_state_root(root)
    put_plan(state)
    return root, state


def ALL(name):
    return True


OPTIONS = jb.JobOptions(verify_commands=("python -m pytest -q",))


def test_events_append_and_replay_roundtrip(tmp_path):
    d = tmp_path / "jobs" / "j1"
    jb.append_event(d, "job_created", 3, job_id="j1", task_id=TASK, base_commit=BASE,
                    integration_branch="task/demo/integration", options={},
                    subtasks=[{"id": "A", "provider": "codex"}])
    jb.append_event(d, "job_state", 3, state="running")
    jb.append_event(d, "subtask_state", 3, subtask="A", state="implementing")
    jb.append_event(d, "attempt_started", 3, subtask="A", attempt="at1", role="implement", agent="codex-x-A-i1")
    jb.append_event(d, "attempt_finished", 3, attempt="at1", exit_code=0, failure_class=None)
    jb.append_event(d, "budget_spent", 3, subtask="A", cause="timeout")
    events, warnings = jb.read_events(d / jb.EVENTS_FILE)
    assert warnings == [] and [e["seq"] for e in events] == [1, 2, 3, 4, 5, 6]
    job = jb.replay(events)
    assert job.state == "running" and job.plan_revision == 3
    assert job.subtasks["A"].state == "implementing" and job.subtasks["A"].attempts == 1
    assert job.subtasks["A"].budget == {"timeout": 1}
    assert job.attempts[0]["finished"] is True and job.attempts[0]["exit_code"] == 0


def test_torn_last_line_is_ignored_and_logged_not_merged(tmp_path):
    d = tmp_path / "j"
    jb.append_event(d, "job_created", 1, job_id="j", task_id=TASK, subtasks=[])
    path = d / jb.EVENTS_FILE
    with open(path, "ab") as f:
        f.write(b'{"seq": 2, "kind": "job_st')  # power loss mid-write
    events, warnings = jb.read_events(path)
    assert len(events) == 1 and warnings and "torn last line" in warnings[0]
    jb.append_event(d, "job_state", 1, state="running")
    events, warnings = jb.read_events(path)
    assert [e["kind"] for e in events] == ["job_created", "torn_line_ignored", "job_state"]
    assert len({e["seq"] for e in events}) == 3
    assert jb.replay(events).state == "running"


def test_job_json_rebuilt_from_events_equals_original(project):
    _, state = project
    plan = jb.build_plan(state, TASK, OPTIONS, ALL)
    job = jb.create_job(state, plan, OPTIONS, "demo-20261020-020000")
    directory = jb.job_dir(state, job.job_id)
    jb.append_event(directory, "job_state", 3, state="running")
    jb.refresh(state, job.job_id)
    original = (directory / jb.JOB_FILE).read_text(encoding="utf-8")
    (directory / jb.JOB_FILE).unlink()
    jb.refresh(state, job.job_id)
    assert (directory / jb.JOB_FILE).read_text(encoding="utf-8") == original
    assert json.loads(original)["state"] == "running"


def test_every_event_carries_plan_revision_and_clock(project):
    _, state = project
    job = jb.create_job(state, jb.build_plan(state, TASK, OPTIONS, ALL), OPTIONS, "demo-20261020-020001")
    events, _ = jb.read_events(jb.job_dir(state, job.job_id) / jb.EVENTS_FILE)
    assert all(e["plan_revision"] == 3 and e["at"] for e in events)


def test_policy_covers_every_class_and_stops_on_quota_auth_permission():
    for name in F.FAILURE_CLASSES:
        assert jb.policy_for(name).reason
        assert name in jb.POLICY or name in (F.REVIEW_MODIFIED_FILES,), name
    for name in (F.QUOTA_EXHAUSTED, F.CAP_REACHED, F.AUTH, F.PERMISSION_DENIED, F.GIT_POLICY_VIOLATION):
        assert jb.policy_for(name).action == jb.STOP_JOB
    for name in (F.NONZERO_EXIT, F.PROVIDER_ERROR, F.COMMIT_FAILED):
        assert jb.policy_for(name).action == jb.FAIL  # no blind retry
    assert jb.policy_for(F.TIMEOUT).action == jb.RETRY_ONCE
    assert jb.policy_for("verification_failed").action == jb.FEEDBACK_ONCE
    assert jb.policy_for("something_new").action == jb.PAUSE_USER


def test_plan_waves_review_assignment_and_skips(project):
    _, state = project
    plan = jb.build_plan(state, TASK, OPTIONS, ALL)
    assert plan.ok, plan.problems
    assert plan.waves == [["A", "B"], ["C"]]
    by = {s.id: s for s in plan.subtasks}
    assert by["A"].author_provider == "codex" and by["A"].reviewer_provider == "copilot"
    assert by["B"].author_provider == "copilot" and by["B"].reviewer_provider == "codex"
    assert by["C"].review == "waived" and by["C"].reviewer_provider is None
    assert by["D"].skipped and by["E"].skipped
    assert by["A"].verify_command == "python -m pytest tests/test_mul.py -q"


def test_reviewer_is_never_the_authors_provider(project):
    _, state = project
    opts = jb.JobOptions(assign={"B": ("codex", None)}, verify_commands=("x",))
    by = {s.id: s for s in jb.build_plan(state, TASK, opts, ALL).subtasks}
    assert by["B"].author_provider == "codex" and by["B"].reviewer_provider == "copilot"


def test_no_independent_reviewer_is_a_problem_unless_waived(project):
    _, state = project
    plan = jb.build_plan(state, TASK, jb.JobOptions(providers=("codex",), assign={"B": ("codex", None)}), ALL)
    assert any("no independent reviewer" in p for p in plan.problems)


def test_unmappable_owner_is_a_problem_not_a_guess(project):
    _, state = project
    put_plan(state, rows=(Subtask("A", "x", "a.txt", (), True, "dev-anna", "waived", "open"),),
             tail="\n\n## Verification\n\n`python -m pytest -q`\n")
    plan = jb.build_plan(state, TASK, OPTIONS, ALL)
    assert any("does not map to a provider" in p for p in plan.problems)
    fixed = jb.build_plan(state, TASK, jb.JobOptions(assign={"A": ("copilot", "m1")}), ALL)
    assert not any("does not map" in p for p in fixed.problems)
    assert fixed.subtasks[0].model == "m1"


def test_missing_verification_command_is_a_problem(project):
    _, state = project
    put_plan(state, tail="\n")
    assert any("no 'verify: A:' entry" in p for p in jb.build_plan(state, TASK, OPTIONS, ALL).problems)


def test_uninstalled_provider_overlap_and_cycle(project):
    _, state = project
    plan = jb.build_plan(state, TASK, OPTIONS, lambda name: name == "codex")
    assert any("copilot is not installed" in p for p in plan.problems)
    assert jb.areas_overlap("src/", "src/mul.py") and not jb.areas_overlap("src/a.py", "tests/")
    put_plan(state, rows=(Subtask("A", "x", "a.txt", ("B",), True, "codex-1", "waived", "open"),
                          Subtask("B", "y", "b.txt", ("A",), True, "codex-1", "waived", "open")))
    assert jb.build_plan(state, TASK, OPTIONS, ALL).problems


def test_same_wave_overlapping_files_are_serialised(project):
    _, state = project
    put_plan(state, rows=(Subtask("A", "x", "src/", (), True, "codex-1", "waived", "open"),
                          Subtask("B", "y", "src/b.py", (), True, "codex-1", "waived", "open")))
    assert jb.build_plan(state, TASK, OPTIONS, ALL).serialised == [("A", "B")]


def test_dry_run_commands_write_nothing(project):
    root, state = project
    before = sorted(p.relative_to(state.path).as_posix() for p in state.path.rglob("*"))
    base = ["--project-root", str(root)]
    main(["job", "plan", "--task", TASK, *base])
    main(["job", "start", "--task", TASK, *base, "--verify-command", "x"])
    main(["job", "status", *base])
    after = sorted(p.relative_to(state.path).as_posix() for p in state.path.rglob("*"))
    assert before == after and not (state.path / jb.JOBS_DIR).exists()


def test_start_execute_is_refused_without_verify_command_and_approval(project, monkeypatch, capsys):
    root, _ = project
    monkeypatch.setattr(jb, "provider_available", ALL)
    base = ["job", "start", "--task", TASK, "--project-root", str(root), "--execute"]
    assert main(base) == 1 and "--verify-command" in capsys.readouterr().err
    assert main([*base, "--verify-command", "x"]) == 1 and "--approved-provider-run" in capsys.readouterr().err
    assert not (root / ".multi-agent-ai" / jb.JOBS_DIR).exists()  # both refusals happened before anything was written

    from multi_agent_ai import job_run
    started = []
    monkeypatch.setattr(job_run, "run_job", lambda state, project, job_id, options, hooks=None: started.append(job_id) or
                        jb.load_job(state, job_id)[0])  # never start a real provider from a unit test
    assert main([*base, "--verify-command", "x", "--approved-provider-run"]) == 1  # job is only "created": not awaiting_approval
    assert len(started) == 1
    events = jb.read_events(next((root / ".multi-agent-ai" / jb.JOBS_DIR).iterdir()) / jb.EVENTS_FILE)[0]
    assert events[0]["approved_provider_run"] is True


def test_worker_agent_ids_are_valid_and_unique_per_attempt():
    ids = {jb.worker_agent_id("demo-20261020-020500", "codex", "A", role, n)
           for role in ("implement", "review") for n in (1, 2)}
    assert len(ids) == 4 and all(i.startswith("codex-020500-A-") for i in ids)
    with pytest.raises(jb.JobError):
        jb.worker_agent_id("j", "codex", "bad id", "implement", 1)


def test_report_status_and_user_commands(project):
    _, state = project
    job = jb.create_job(state, jb.build_plan(state, TASK, OPTIONS, ALL), OPTIONS, "demo-20261020-020002")
    d = jb.job_dir(state, job.job_id)
    jb.append_event(d, "job_state", 3, state="awaiting_approval")
    jb.refresh(state, job.job_id)
    report = json.loads((d / jb.REPORT_JSON).read_text(encoding="utf-8"))
    assert report["schema"] == 1 and report["state"] == "awaiting_approval"
    assert "the full suite on the integration branch has not run" in report["not_verified"]
    steps = " ".join(report["next_steps"])
    assert "git merge --no-ff task/demo/integration" in steps and "YOU run" in steps and "never pushes" in steps
    md = (d / jb.REPORT_MD).read_text(encoding="utf-8")
    assert "Run report" in md and "Next steps" in md and "job approve" not in md


def test_create_job_refuses_duplicate_and_bad_plan(project):
    _, state = project
    plan = jb.build_plan(state, TASK, OPTIONS, ALL)
    jb.create_job(state, plan, OPTIONS, "demo-20261020-020003")
    with pytest.raises(jb.JobError):
        jb.create_job(state, plan, OPTIONS, "demo-20261020-020003")
    plan.problems.append("boom")
    with pytest.raises(jb.JobError):
        jb.create_job(state, plan, OPTIONS)


def test_options_validation():
    with pytest.raises(jb.JobError):
        jb.JobOptions(max_parallel=9).validate()
    with pytest.raises(jb.JobError):
        jb.JobOptions(providers=("nope",)).validate()
    with pytest.raises(jb.JobError):
        jb.JobOptions(assign={"A": ("claude", None)}).validate()
    assert jb.parse_assign(["A=codex:gpt-x", "B=copilot"]) == {"A": ("codex", "gpt-x"), "B": ("copilot", None)}
    with pytest.raises(jb.JobError):
        jb.parse_assign(["A"])


def test_paths_with_spaces(tmp_path):
    root = tmp_path / "my project"
    root.mkdir()
    state = init_state_root(root)
    d = jb.job_dir(state, "j-1")
    jb.append_event(d, "job_created", 1, job_id="j-1", task_id=TASK, subtasks=[])
    assert jb.refresh(state, "j-1").job_id == "j-1"
    assert [j.job_id for j in jb.list_jobs(state)] == ["j-1"]
