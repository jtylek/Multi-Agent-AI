from __future__ import annotations

import subprocess
from pathlib import Path

from multi_agent_ai.reachability import DiscoveryResult, ReachabilityError, discover_agents, discover_claude_sessions, discover_codex_threads


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
