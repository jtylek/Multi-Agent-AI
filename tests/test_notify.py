from __future__ import annotations

import json
import subprocess
from pathlib import Path

from multi_agent_ai.mailbox import write_request
from multi_agent_ai.models import Request, Notification
from multi_agent_ai.notify import NotifyResult, notify_message
from multi_agent_ai.state import StateIndex, init_state_root


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
    init_state_root(tmp_path, root)
    write_request(root, Request("m1", "lead", ("codex", "claude", "copilot", "unknown"), "Do work."), "lead")
    _status(root, "codex", "codex queue --thread thread-123")
    _status(root, "claude", "claude SendMessage multi-agent-ai-a7")
    _status(root, "copilot", "code chat")
    calls = []

    def runner(command, cwd, timeout):
        calls.append((list(command), cwd, timeout))
        return subprocess.CompletedProcess(command, 0, stdout="accepted", stderr="")

    monkeypatch.setattr("multi_agent_ai.notify.resolve_executable", lambda name: f"{name}.cmd")
    results = notify_message(root, "m1", tmp_path, runner=runner, send=True)
    assert [item.recipient for item in results] == ["codex", "claude", "copilot", "unknown"]
    assert results[0].submitted and results[0].method == "codex queue"
    assert results[1].method == "manual (claude)" and not results[1].submitted
    assert results[2].submitted and results[2].method == "code chat"
    assert results[3].method == "manual"
    assert len(calls) == 2
    assert calls[0][0][0:3] == ["codex.cmd", "queue", "--thread=thread-123"]
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


def test_notify_defaults_to_dry_run(tmp_path, monkeypatch):
    root = tmp_path / ".multi-agent-ai"
    write_request(root, Request("dry", "lead", ("codex",), "Do work."), "lead")
    _status(root, "codex", "codex queue --thread thread-123")

    def should_not_run(*args, **kwargs):
        raise AssertionError("dry-run invoked a transport")

    monkeypatch.setattr("multi_agent_ai.notify.resolve_executable", should_not_run)
    results = notify_message(root, "dry", tmp_path)
    assert results[0].method == "dry-run"
    assert "would send" in results[0].detail
    assert not (root / "state.sqlite3").exists()


def test_notify_records_failed_and_manual_without_creating_ack(tmp_path, monkeypatch):
    root = tmp_path / ".multi-agent-ai"
    init_state_root(tmp_path, root)
    write_request(root, Request("m2", "lead", ("codex", "bad"), "Do work."), "lead")
    _status(root, "codex", "codex queue --thread thread-123")
    _status(root, "bad", "provider unsupported")

    def failed_runner(command, cwd, timeout):
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="rejected")

    monkeypatch.setattr("multi_agent_ai.notify.resolve_executable", lambda name: f"{name}.cmd")
    results = notify_message(root, "m2", tmp_path, runner=failed_runner, send=True)
    assert [item.method for item in results] == ["codex queue", "manual"]
    assert all(not item.submitted for item in results)
    assert not (root / "messages" / "m2" / "ack-codex.md").exists()
    with StateIndex.open(root / "state.sqlite3", read_only=True) as index:
        records = index.list_records(Notification)
    assert [record.transport_result for record in records] == ["failed", "manual"]
    assert "rejected" in (records[0].manual_relay_text or "")
    assert "Check the agent mailbox" in (records[1].manual_relay_text or "")


def test_dry_run_rejects_unsupported_codex_contact(tmp_path):
    root = tmp_path / ".multi-agent-ai"
    write_request(root, Request("dry-bad", "lead", ("codex",), "Do work."), "lead")
    _status(root, "codex", "codex queue --thread --evil")
    result = notify_message(root, "dry-bad", tmp_path)[0]
    assert result.method == "manual"
    assert "would NOT send" in result.detail
    assert "Check the agent mailbox" in result.detail


def test_notify_cli_exit_codes(monkeypatch, tmp_path, capsys):
    from multi_agent_ai.cli import main

    init_state_root(tmp_path)
    seen = {}
    def mocked_notify(*args, **kwargs):
        seen.update(kwargs)
        return (NotifyResult("a", "dry-run", False, "would send"),)
    monkeypatch.setattr("multi_agent_ai.cli.notify_message", mocked_notify)
    assert main(["notify", "m", "--project-root", str(tmp_path)]) == 0
    assert seen["send"] is False
    monkeypatch.setattr("multi_agent_ai.cli.notify_message", lambda *args, **kwargs: (NotifyResult("a", "codex queue", True, "ok"),))
    assert main(["notify", "m", "--project-root", str(tmp_path), "--send"]) == 0
    monkeypatch.setattr("multi_agent_ai.cli.notify_message", lambda *args, **kwargs: (NotifyResult("a", "manual", False, "relay"),))
    assert main(["notify", "m", "--project-root", str(tmp_path), "--send"]) == 3
    monkeypatch.setattr("multi_agent_ai.cli.notify_message", lambda *args, **kwargs: (NotifyResult("a", "codex queue", False, "failed"),))
    assert main(["notify", "m", "--project-root", str(tmp_path), "--send"]) == 1
    monkeypatch.setattr("multi_agent_ai.cli.notify_message", lambda *args, **kwargs: (
        NotifyResult("a", "codex queue", False, "failed"), NotifyResult("b", "manual", False, "relay")))
    assert main(["notify", "m", "--project-root", str(tmp_path), "--send"]) == 1
    capsys.readouterr()
