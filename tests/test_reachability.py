from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path
import pytest

from multi_agent_ai.reachability import (DiscoveryResult, ReachabilityError,
                                         _query_codex_app_server, discover_agents,
                                         discover_claude_sessions, discover_codex_threads)


def test_claude_discovery_filters_to_exact_project_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr("multi_agent_ai.reachability.resolve_executable", lambda _: "claude.cmd")
    seen = {}

    def runner(command, cwd, timeout):
        seen["command"] = command
        seen["cwd"] = cwd
        return [
            {"name": "multi-agent-ai-a7", "sessionId": "s1", "cwd": str(tmp_path), "status": "busy"},
            {"name": "other", "sessionId": "s2", "cwd": str(tmp_path / "other"), "status": "idle"},
        ]

    found = discover_claude_sessions(tmp_path, runner=runner)
    assert [item.name for item in found] == ["multi-agent-ai-a7"]
    assert seen["command"] == ("claude.cmd", "agents", "--json")
    assert seen["cwd"] == tmp_path


def test_codex_discovery_filters_exact_cwd():
    root = Path("C:/AI/Multi-Agent-AI")
    found = discover_codex_threads(
        root,
        query=lambda _root, _timeout: [
            {"id": "thread-1", "name": "Review", "cwd": "c:\\ai\\multi-agent-ai", "status": {"type": "notLoaded"}},
            {"id": "thread-2", "name": "Other", "cwd": "C:\\other", "status": {"type": "notLoaded"}},
        ],
    )
    assert [(item.id, item.product, item.status) for item in found] == [("thread-1", "codex", "notLoaded")]


def test_discovery_keeps_one_provider_when_the_other_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("multi_agent_ai.reachability.discover_claude_sessions", lambda _root, **_: (_ for _ in ()).throw(ReachabilityError("bad")))
    monkeypatch.setattr("multi_agent_ai.reachability.discover_codex_threads", lambda _root, **_: ())
    result = discover_agents(tmp_path)
    assert isinstance(result, DiscoveryResult) and result.errors == ("bad",)


class _FakeProcess:
    def __init__(self, output: str):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(output)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class _DelayedStream:
    def __init__(self, lines, delay):
        self.lines = lines
        self.delay = delay

    def __iter__(self):
        for line in self.lines:
            time.sleep(self.delay)
            yield line


def test_codex_app_server_handles_notification_before_response(monkeypatch, tmp_path):
    payload = "\n".join([
        json.dumps({"jsonrpc": "2.0", "method": "status/update", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "status/update", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"data": [{"id": "t1", "name": "x", "cwd": str(tmp_path)}]}}),
    ]) + "\n"
    process = _FakeProcess(payload)
    monkeypatch.setattr("multi_agent_ai.reachability.resolve_executable", lambda _: "codex.cmd")
    monkeypatch.setattr("multi_agent_ai.reachability.subprocess.Popen", lambda *args, **kwargs: process)
    found = _query_codex_app_server(tmp_path, timeout=1)
    assert found[0]["id"] == "t1"
    assert process.terminated


def test_codex_app_server_timeout_is_bounded(monkeypatch, tmp_path):
    process = _FakeProcess("")
    monkeypatch.setattr("multi_agent_ai.reachability.resolve_executable", lambda _: "codex.cmd")
    monkeypatch.setattr("multi_agent_ai.reachability.subprocess.Popen", lambda *args, **kwargs: process)
    with pytest.raises(ReachabilityError, match="timed out"):
        _query_codex_app_server(tmp_path, timeout=0.01)


def test_codex_query_uses_one_total_deadline(monkeypatch, tmp_path):
    lines = [
        json.dumps({"id": 1, "result": {}}) + "\n",
        json.dumps({"id": 2, "result": {"data": []}}) + "\n",
    ]
    process = _FakeProcess("")
    process.stdout = _DelayedStream(lines, 0.08)
    monkeypatch.setattr("multi_agent_ai.reachability.resolve_executable", lambda _: "codex.cmd")
    monkeypatch.setattr("multi_agent_ai.reachability.subprocess.Popen", lambda *args, **kwargs: process)
    with pytest.raises(ReachabilityError, match="timed out"):
        _query_codex_app_server(tmp_path, timeout=0.12)
