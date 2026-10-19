"""plan.md and status-<agent-id>.md (formats.md sections 4 and 5)."""

from datetime import datetime

import pytest

from multi_agent_ai.board import (
    check_revision_progress,
    check_status,
    parse_plan,
    parse_status,
    read_plan,
    read_statuses,
    render_subtask_table,
    verification_warnings,
    write_plan,
    write_status,
)
from multi_agent_ai.document import OwnershipError, parse_text
from multi_agent_ai.models import Plan, Status, Subtask

TABLE_HEAD = (
    "| id | what | files/area | depends on | worktree | owner | reviewer | state |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
)


def plan_text(rows, headers=None, table_head=TABLE_HEAD):
    headers = headers or "task: m0-foundation\ncoordinator: claude-a7\nrevision: 2\nupdated: 2026-10-19 16:44\nbase-commit: d69eae6\n"
    return headers + "\n## Goal\n...\n\n" + table_head + "".join(f"| {' | '.join(r)} |\n" for r in rows)


def plan(rows, headers=None, directory="m0-foundation", **kw):
    return parse_plan(parse_text(plan_text(rows, headers, **kw)), directory)


ROW_T4 = ["T4", "formats", "notes/x.md", "-", "no", "claude-50", "claude-a7", "review"]


# --- formats.md section 4: valid plans -------------------------------------------------


def test_plan_valid_example():
    p = plan([ROW_T4])
    assert p.ok, p.errors
    assert (p.value.task_id, p.value.coordinator, p.value.revision, p.value.base_commit) == (
        "m0-foundation", "claude-a7", 2, "d69eae6")
    assert p.value.subtasks == (Subtask("T4", "formats", "notes/x.md", (), False, "claude-50", "claude-a7", "review"),)


def test_plan_also_valid_integration_branch_and_waived_reviewer():
    p = plan([["T1", "docs", "docs/", "-", "no", "a", "waived", "done"]],
             "task: demo\ncoordinator: a\nrevision: 1\nupdated: 2026-10-19 09:00\nbase-commit: abc1234\n"
             "integration-branch: task/demo/integration\n", directory="demo")
    assert p.ok, p.errors
    assert p.value.integration_branch == "task/demo/integration" and p.value.subtasks[0].review_waived


def test_plan_also_valid_revision_7_with_dependencies():
    p = plan([["T1", "a", "a/", "-", "no", "x", "y", "done"],
              ["T2", "b", "b/", "-", "no", "x", "y", "done"],
              ["T3", "c", "c/", "T1, T2", "yes", "y", "x", "open"]],
             "task: m0-foundation\ncoordinator: claude-a7\nrevision: 7\nupdated: 2026-10-19 16:44\n")
    assert p.ok, p.errors
    assert p.value.revision == 7 and p.value.subtask("T3").depends_on == ("T1", "T2")


# --- formats.md section 4: invalid plans -----------------------------------------------


def test_plan_invalid_1_two_coordinators():
    assert not plan([ROW_T4], "task: m0-foundation\ncoordinator: a, b\nrevision: 2\nupdated: 2026-10-19 16:44\n").ok
    p = plan([ROW_T4], "task: m0-foundation\ncoordinator: a\ncoordinator: b\nrevision: 2\nupdated: 2026-10-19 16:44\n")
    assert any("two 'coordinator' lines" in e for e in p.errors)


@pytest.mark.parametrize("revision", ["v2", "0", "2.5"])
def test_plan_invalid_2_bad_revision(revision):
    p = plan([ROW_T4], f"task: m0-foundation\ncoordinator: claude-a7\nrevision: {revision}\nupdated: 2026-10-19 16:44\n")
    assert not p.ok


def test_plan_invalid_2_revision_lower_than_already_seen():
    assert check_revision_progress(3, 4) is not None
    assert check_revision_progress(4, 4) is None and check_revision_progress(5, None) is None


@pytest.mark.parametrize("row,fragment", [
    (["T4", "x", "a", "-", "no", "o", "r", "finished"], "state must be one of"),
    (["T4", "x", "a", "-", "maybe", "o", "r", "open"], "worktree must be yes or no"),
    (["T4", "x", "a", "T9", "no", "o", "r", "open"], "unknown dependency 'T9'"),
])
def test_plan_invalid_3_bad_rows(row, fragment):
    p = plan([row])
    assert any(fragment in e for e in p.errors), p.errors


def test_plan_invalid_3_dependency_cycle():
    p = plan([["T1", "a", "a", "T2", "no", "x", "y", "open"], ["T2", "b", "b", "T1", "no", "x", "y", "open"]])
    assert any("dependency cycle" in e for e in p.errors)


# --- other plan rules ------------------------------------------------------------------


def test_missing_required_column_is_an_error_unknown_column_ignored():
    head = "| id | what | files/area | depends on | worktree | owner | state |\n| --- | --- | --- | --- | --- | --- | --- |\n"
    p = plan([["T1", "a", "a", "-", "no", "x", "open"]], table_head=head)
    assert any("missing column(s): reviewer" in e for e in p.errors)
    head = TABLE_HEAD.replace("| state |", "| state | notes |").replace("| --- |\n", "| --- | --- |\n")
    assert plan([ROW_T4 + ["anything"]], table_head=head).ok


def test_no_subtask_table_is_an_error():
    assert any("no subtask table" in e for e in parse_plan(parse_text(
        "task: t\ncoordinator: a\nrevision: 1\nupdated: 2026-10-19 10:00\n\nNo table.\n"), "t").errors)


def test_reviewer_must_differ_from_owner_unless_waived():
    assert any("reviewer equals owner" in e for e in plan([["T1", "a", "a", "-", "no", "x", "x", "open"]]).errors)


def test_proposed_owner_is_not_an_assignment():
    p = plan([["T1", "a", "a", "-", "no", "proposed: x", "y", "in progress"]])
    assert any("not an assignment" in e for e in p.errors)
    ok = plan([["T1", "a", "a", "-", "no", "proposed: x", "y", "open"], ["T2", "b", "b", "-", "no", "TBD", "y", "open"]])
    assert ok.ok and ok.value.subtasks[0].owner_id is None and ok.value.subtasks[1].owner_id is None


def test_overlapping_active_subtasks_warn():
    p = plan([["T1", "a", "src/", "-", "no", "x", "y", "in progress"],
              ["T2", "b", "src/pkg/a.py, docs/", "-", "no", "y", "x", "assigned"],
              ["T3", "c", "src/", "-", "no", "y", "x", "done"]])
    assert p.ok and [w for w in p.warnings if "overlap" in w] == [
        "active subtasks T1 and T2 overlap in files/area (src, src/pkg/a.py); allowed only if the coordinator resolved it"]


def test_task_and_integration_branch_must_match_directory():
    assert not plan([ROW_T4], directory="other").ok
    assert not plan([ROW_T4], "task: m0-foundation\ncoordinator: a\nrevision: 1\nupdated: 2026-10-19 10:00\n"
                              "integration-branch: main\n").ok


# --- formats.md section 5: status ------------------------------------------------------

STATUS_VALID = (
    "plan-revision: 3\nagent: codex\nsubtask: T3\nstate: running tests: tests/test_mailbox.py\n"
    "branch: task/m0-foundation/T3 (C:\\work\\wt\\T3)\nlast-commit: 3fa91c2\n"
    "tests: tests/test_models.py passed (12)\nblockers: none\nupdated: 2026-10-19 18:02\n"
)
NOW = datetime(2026, 10, 19, 18, 30)


def status(text=STATUS_VALID, agent="codex"):
    return parse_status(parse_text(text), agent)


def board_plan(revision=3, state="in progress", owner="codex"):
    return Plan("m0-foundation", "claude-a7", revision, "2026-10-19 18:00",
                subtasks=(Subtask("T3", "x", "src/", (), True, owner, "claude-a7", state),))


def test_status_valid_example():
    p = status()
    assert p.ok, p.errors
    assert p.value.state == "running tests: tests/test_mailbox.py"
    assert p.value.branch == "task/m0-foundation/T3 (C:\\work\\wt\\T3)"
    assert check_status(p.value, board_plan(), NOW) == ([], [])


def test_status_invalid_1_missing_or_above_plan_revision():
    assert not status(STATUS_VALID.replace("plan-revision: 3\n", "")).ok
    errors, _ = check_status(status(STATUS_VALID.replace("plan-revision: 3", "plan-revision: 4")).value, board_plan(), NOW)
    assert errors and "trust neither file" in errors[0]


def test_status_invalid_2_file_identity_mismatch():
    assert not status(agent="copilot").ok


def test_status_invalid_3_unknown_state():
    assert not status(STATUS_VALID.replace("state: running tests: tests/test_mailbox.py", "state: finished")).ok
    assert not status(STATUS_VALID.replace("tests/test_mailbox.py\nbranch", "\nbranch")).ok  # empty scope


def test_status_verification_warning_done_without_tests():
    s = status(STATUS_VALID.replace("state: running tests: tests/test_mailbox.py", "state: done")
               .replace("tests: tests/test_models.py passed (12)", "tests: none run")).value
    assert verification_warnings(s) and not verification_warnings(status().value)


def test_stale_revision_and_stale_worker_are_independent():
    s = status().value
    _, warnings = check_status(s, board_plan(revision=4), NOW)
    assert warnings == ["stale revision: read revision 3, plan is at 4"]
    _, warnings = check_status(s, board_plan(), datetime(2026, 10, 19, 21, 3))
    assert len(warnings) == 1 and "stale worker" in warnings[0] and "unavailable" in warnings[0]
    _, warnings = check_status(s, board_plan(), datetime(2026, 10, 19, 21, 2))
    assert warnings == []


def test_stale_waiting_for_user_releases_reservation():
    s = status(STATUS_VALID.replace("state: running tests: tests/test_mailbox.py", "state: waiting for user")).value
    _, warnings = check_status(s, board_plan(), datetime(2026, 10, 20, 9, 0))
    assert "reservation released, not failed" in warnings[0]


def test_date_only_updated_is_not_evaluated_for_staleness():
    p = status(STATUS_VALID.replace("2026-10-19 18:02", "2026-10-01"))
    assert p.ok and any("staleness cannot be evaluated" in w for w in p.warnings)
    assert check_status(p.value, board_plan(), NOW) == ([], [])


def test_status_plan_disagreement_is_reported_not_resolved():
    s = status(STATUS_VALID.replace("state: running tests: tests/test_mailbox.py", "state: review")).value
    plan_ = board_plan(state="in progress")
    _, warnings = check_status(s, plan_, NOW)
    assert any(w.startswith("conflict:") for w in warnings)
    assert plan_.subtask("T3").state == "in progress"


def test_last_commit_must_be_sha_or_none():
    assert not status(STATUS_VALID.replace("last-commit: 3fa91c2", "last-commit: latest")).ok
    lenient = status(STATUS_VALID.replace("last-commit: 3fa91c2", "last-commit: none (nothing yet)"))
    assert lenient.ok and lenient.warnings


# --- writers ---------------------------------------------------------------------------


def _plan(revision, coordinator="lead"):
    subtasks = (Subtask("T1", "x", "src/", (), True, "w1", "w2", "assigned"),)
    return Plan("demo", coordinator, revision, "2026-10-19 10:00", body="## Goal\n\nx\n\n" + render_subtask_table(subtasks),
                base_commit="abc1234", integration_branch="task/demo/integration")


def test_plan_writer_owner_and_strictly_increasing_revision(tmp_path):
    write_plan(tmp_path, _plan(1), author="lead")
    read = read_plan(tmp_path / "tasks" / "demo")
    assert read.ok, read.errors
    assert read.value.subtasks[0].owner == "w1"
    with pytest.raises(OwnershipError):
        write_plan(tmp_path, _plan(2), author="w1")
    with pytest.raises(OwnershipError):
        write_plan(tmp_path, _plan(2, coordinator="other"), author="other")
    with pytest.raises(ValueError):
        write_plan(tmp_path, _plan(1), author="lead")
    write_plan(tmp_path, _plan(2), author="lead")
    with pytest.raises(ValueError):  # writers use minute precision
        bad = _plan(3)
        bad.updated = "2026-10-19"
        write_plan(tmp_path, bad, author="lead")


def test_status_writer_only_writes_own_file(tmp_path):
    s = Status("w1", 1, "T1", "in progress", "none", "none run", "none", "2026-10-19 10:00")
    with pytest.raises(OwnershipError):
        write_status(tmp_path, "demo", s, author="lead")
    path = write_status(tmp_path, "demo", s, author="w1")
    assert path.name == "status-w1.md"
    assert read_statuses(path.parent)["w1"].ok
    path.write_text("plan-revision: 1\nagent: intruder\n", encoding="utf-8")
    with pytest.raises(OwnershipError):
        write_status(tmp_path, "demo", s, author="w1")
