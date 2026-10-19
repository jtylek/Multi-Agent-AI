from __future__ import annotations

import json
import subprocess
from pathlib import Path

from multi_agent_ai.mailbox import write_request
from multi_agent_ai.models import Request, Notification
from multi_agent_ai.notify import notify_message
from multi_agent_ai.state import StateIndex


def _status(root: Path, agent: str, contact: str) -> None:
    path = root / "tasks" / "demo" / f"status-{agent}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"plan-revision: 1\nagent: {agent}\nsubtask: -\nstate: waiting for user\n"
        f"last-commit: none\ntests: none run\nblockers: none\ncontact: {contact}\n"
        "updated: 2026-10-19 18:00\n",
        encoding="utf-8",
    )


def test_notify_sends_only_pointer_and_never_ack(tmp_path, monkeypatch):
    root = tmp_path / ".multi-agent-ai"
    write_request(root, Request("m1", "lead", ("codex", "claude", "copilot", "unknown"), "Do work."), "lead")
    _status(root, "codex", "codex queue --thread thread-123")
    _status(root, "claude", "claude SendMessage multi-agent-ai-a7")
    _status(root, "copilot", "code chat")
    calls = []

    def runner(command, cwd, timeout):
        calls.append((list(command), cwd, timeout))
        return subprocess.CompletedProcess(command, 0, stdout="accepted", stderr="")

    monkeypatch.setattr("multi_agent_ai.notify.resolve_executable", lambda name: f"{name}.cmd")
    results = notify_message(root, "m1", tmp_path, runner=runner)
    assert [item.recipient for item in results] == ["codex", "claude", "copilot", "unknown"]
    assert results[0].submitted and results[0].method == "codex queue"
    assert results[1].method == "claude target" and not results[1].submitted
    assert results[2].submitted and results[2].method == "code chat"
    assert results[3].method == "manual"
    assert len(calls) == 2
    assert calls[0][0][0:4] == ["codex.cmd", "queue", "--thread", "thread-123"]
    assert "Check the agent mailbox" in calls[0][0][-1]
    assert calls[1][0][0:3] == ["code.cmd", "chat", "--reuse-window"]
    assert not (root / "messages" / "m1" / "ack-codex.md").exists()

    with StateIndex.open(root / "state.sqlite3", read_only=True) as index:
        records = index.list_records(Notification)
    assert len(records) == 4
    assert all(record.message_id == "m1" for record in records)


def test_notify_rejects_invalid_request(tmp_path):
    root = tmp_path / ".multi-agent-ai"
    (root / "messages" / "bad").mkdir(parents=True)
    (root / "messages" / "bad" / "request.md").write_text("id: bad\nfrom: lead\n", encoding="utf-8")
    try:
        notify_message(root, "bad", tmp_path)
    except ValueError as exc:
        assert "unreadable request" in str(exc)
    else:
        raise AssertionError("invalid request was accepted")
