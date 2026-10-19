"""State root, derived SQLite index, CLI, and the D8 guarantees."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multi_agent_ai.board import write_plan, write_status, render_subtask_table
from multi_agent_ai.cli import main
from multi_agent_ai.mailbox import evaluate_agreement, read_acks, read_request, write_ack, write_request
from multi_agent_ai.models import (
    Ack, AgentSession, Attempt, Job, Notification, Plan, Request, Reservation, Status, Subtask, Task,
)
from multi_agent_ai.report import scan
from multi_agent_ai.state import DEFAULT_STATE_ROOT, StateIndex, init_state_root, resolve_state_root


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


@pytest.fixture
def board(tmp_path):
    """A populated state root written only through the writers, one author per file."""
    root = tmp_path / "Target Project" / ".multi-agent-ai"
    subtasks = (Subtask("T1", "x", "src/", (), True, "w1", "w2", "in progress"),)
    write_plan(root, Plan("demo", "lead", 2, "2026-10-19 10:00", body=render_subtask_table(subtasks),
                          base_commit="abc1234", integration_branch="task/demo/integration"), author="lead")
    write_status(root, "demo", Status("w1", 1, "T1", "in progress", "none", "none run", "none", "2026-10-19 10:00"),
                 author="w1")
    write_request(root, Request("m1", "lead", ("w1", "w2"), "Plan revision 2.", plan_revision=2), author="lead")
    write_ack(root, Ack("m1", "w1", "agreed", product="Codex", plan_revision=2), author="w1")
    return root


def run(*argv):
    from io import StringIO
    from contextlib import redirect_stdout

    out = StringIO()
    with redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


def test_resolve_state_root_default_relative_and_absolute(tmp_path):
    assert resolve_state_root(tmp_path).path == (tmp_path / DEFAULT_STATE_ROOT).resolve()
    assert resolve_state_root(tmp_path, "storage/agents").path == (tmp_path / "storage" / "agents").resolve()
    assert resolve_state_root(tmp_path, tmp_path / "abs").path == (tmp_path / "abs").resolve()


def test_init_creates_layout_records_root_and_index(tmp_path):
    project = tmp_path / "Target Project"
    project.mkdir()
    code, out = run("init", "--project-root", str(project))
    assert code == 0
    root = project / ".multi-agent-ai"
    assert (root / "messages").is_dir() and (root / "tasks").is_dir() and (root / "state.sqlite3").is_file()
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    assert config["state_root"] == str(root.resolve()) and config["format"] == 1
    assert json.loads(out) == config
    assert not (project / ".gitignore").exists()


def test_init_with_custom_root_keeps_existing_files(board):
    before = (board / "tasks" / "demo" / "plan.md").read_bytes()
    project = board.parent
    init_state_root(project, ".multi-agent-ai")
    init_state_root(project, ".multi-agent-ai")  # idempotent
    assert (board / "tasks" / "demo" / "plan.md").read_bytes() == before
    custom = init_state_root(project, "storage/framework/agents")
    assert custom.path == (project / "storage" / "framework" / "agents").resolve()


def test_inspect_reports_ack_state_plan_and_staleness_without_writing(board):
    before = snapshot(board)
    code, out = run("inspect", "--state-root", str(board), "--now", "2026-10-19 15:00")
    assert code == 0
    assert "m1: from lead to w1, w2; concerns plan revision 2" in out
    assert "w1: agreed (rev 2)" in out and "w2: missing" in out and "all agreed: no" in out
    assert "demo: coordinator lead, revision 2" in out and "T1: in progress (owner w1, reviewer w2)" in out
    assert "stale revision: read revision 1, plan is at 2" in out
    assert "stale worker" in out
    assert snapshot(board) == before
    assert not (board / "state.sqlite3").exists()


def test_inspect_json(board):
    code, out = run("inspect", "--state-root", str(board), "--json", "--now", "2026-10-19 10:30")
    data = json.loads(out)
    message = data["messages"][0]
    assert code == 0 and message["all_agreed"] is False and message["required_revision"] == 2
    assert [(r["agent_id"], r["state"]) for r in message["recipients"]] == [("w1", "agreed"), ("w2", "missing")]
    assert data["tasks"][0]["revision"] == 2 and data["tasks"][0]["subtasks"][0]["state"] == "in progress"


def test_inspect_missing_root_fails_cleanly(tmp_path):
    assert run("inspect", "--state-root", str(tmp_path / "nope"))[0] == 2
    assert not (tmp_path / "nope").exists()


def test_index_is_derived_and_detects_revision_regression(board):
    init_state_root(board.parent)
    code, out = run("index", "--state-root", str(board))
    assert code == 0 and "indexed 4 file(s), 4 new or changed" in out
    assert run("index", "--state-root", str(board))[1].strip().endswith("0 new or changed")
    with StateIndex.open(board / "state.sqlite3", read_only=True) as index:
        assert index.seen_revision("demo") == 2
        kinds = sorted(f["kind"] for f in index.indexed_files())
    assert kinds == ["ack", "plan", "request", "status"]

    plan = board / "tasks" / "demo" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace("revision: 2", "revision: 1"), encoding="utf-8")
    before = snapshot(board)
    code, out = run("inspect", "--state-root", str(board), "--now", "2026-10-19 10:30")
    assert "lower than revision 2 already seen" in out
    assert snapshot(board) == before  # read-only even with an index present
    assert "lower than revision 2 already seen" in run("index", "--state-root", str(board))[1]

    (board / "state.sqlite3").unlink()  # deleting the index loses nothing authoritative
    assert scan(board).messages[0].agreement.recipients[0].state == "agreed"


def test_operational_records_round_trip(tmp_path):
    records = [
        Job("job-1", "Ship M0", "lead"),
        Task("T1", "job-1", "Build", depends_on=("T0",)),
        AgentSession("w1", "codex", capabilities=("python",)),
        Reservation("r1", "T1", "w1", "src/"),
        Attempt("a1", "T1", "w1", exit_code=0, files_changed=("src/x.py",)),
        Notification("n1", "m1", "w1", transport_result="submitted"),
    ]
    with StateIndex.open(tmp_path / "state.sqlite3") as index:
        for record in records:
            index.save(record)
        for record in records:
            assert index.get(type(record), record.id) == record
            assert index.list_records(type(record)) == [record]
        with pytest.raises(TypeError):
            index.save(Ack("m1", "w1", "agreed"))  # acks live only in files (D3)
    with pytest.raises(ValueError):
        Job("../x", "goal", "lead")


def test_open_failure_closes_the_connection(tmp_path):
    """A corrupt index raises, and the file handle is released (T14).

    On Windows an open SQLite handle blocks deleting the file, so deleting it proves the
    connection was closed. Without the fix `unlink` raises PermissionError.
    """
    import sqlite3

    broken = tmp_path / "state.sqlite3"
    broken.write_bytes(b"this is not a sqlite database at all" * 10)
    with pytest.raises(sqlite3.DatabaseError):
        StateIndex.open(broken)
    broken.unlink()
    assert not broken.exists()


def test_open_failure_closes_the_connection_with_a_failing_schema(tmp_path, monkeypatch):
    import sqlite3

    closed = []
    real_connect = sqlite3.connect

    class Spy:
        def __init__(self, connection):
            self._connection = connection

        def executescript(self, script):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            closed.append(True)
            self._connection.close()

    monkeypatch.setattr("multi_agent_ai.state.sqlite3.connect", lambda *a, **k: Spy(real_connect(*a, **k)))
    with pytest.raises(sqlite3.OperationalError):
        StateIndex.open(tmp_path / "state.sqlite3")
    assert closed == [True]
    (tmp_path / "state.sqlite3").unlink()


def test_d8_exit_code_and_transport_never_create_ack_or_completion(board):
    """A zero exit code or a successful notification is not an ack or a completion (D8)."""
    message_dir = board / "messages" / "m1"
    files_before = sorted(p.name for p in message_dir.iterdir())
    init_state_root(board.parent)
    with StateIndex.open(board / "state.sqlite3") as index:
        index.save(Attempt("a1", "T1", "w2", exit_code=0, finished="2026-10-19 10:05"))
        index.save(Notification("n1", "m1", "w2", reachability_mode="native", transport_result="delivered"))
    run("index", "--state-root", str(board))

    assert sorted(p.name for p in message_dir.iterdir()) == files_before  # no ack-w2.md appeared
    agreement = evaluate_agreement(read_request(message_dir).value, read_acks(message_dir))
    assert [(r.agent_id, r.state) for r in agreement.recipients] == [("w1", "agreed"), ("w2", "missing")]
    assert agreement.all_agreed is False
    report = scan(board)
    assert report.tasks[0].plan.value.subtask("T1").state == "in progress"  # not completed


def test_python_dash_m_entry_point(board):
    result = subprocess.run(
        [sys.executable, "-m", "multi_agent_ai", "inspect", "--state-root", str(board), "--now", "2026-10-19 10:30"],
        capture_output=True, text=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    assert "w1: agreed (rev 2)" in result.stdout
