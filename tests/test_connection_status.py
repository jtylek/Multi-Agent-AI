from types import SimpleNamespace

from multi_agent_ai import provider_auth


def test_missing_provider(monkeypatch):
    monkeypatch.setattr(provider_auth, "login_command", lambda provider: (_ for _ in ()).throw(FileNotFoundError()))
    assert provider_auth.check_connection("codex").status == "not installed"


def test_codex_connected(monkeypatch):
    monkeypatch.setattr(provider_auth, "login_command", lambda provider: ["codex"])
    monkeypatch.setattr(provider_auth.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr=""))
    assert provider_auth.check_connection("codex").status == "connected"


def test_codex_disconnected(monkeypatch):
    monkeypatch.setattr(provider_auth, "login_command", lambda provider: ["codex"])
    monkeypatch.setattr(provider_auth.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="Not logged in"))
    assert provider_auth.check_connection("codex").status == "not connected"


def test_claude_status(monkeypatch):
    monkeypatch.setattr(provider_auth, "login_command", lambda provider: ["claude"])
    monkeypatch.setattr(provider_auth.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{"loggedIn":true}', stderr=""))
    assert provider_auth.check_connection("claude").status == "connected"


def test_unknown_is_not_disconnected():
    assert provider_auth.check_connection("copilot").status in ("unknown", "not installed")


def test_invalid_provider():
    import pytest
    with pytest.raises(ValueError):
        provider_auth.check_connection("invalid")
