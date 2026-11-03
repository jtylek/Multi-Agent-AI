"""Login actions must not capture credentials or invoke providers during discovery."""
import pytest
from multi_agent_ai import provider_auth


def test_provider_whitelist():
    with pytest.raises(ValueError):
        provider_auth.login_command("custom; rm -rf ~")


def test_missing_cli(monkeypatch):
    monkeypatch.setattr(provider_auth.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        provider_auth.login_command("codex")


def test_login_arguments(monkeypatch):
    monkeypatch.setattr(provider_auth.shutil, "which", lambda name: "/usr/bin/" + name)
    assert provider_auth.login_command("codex")[-1] == "login"
    assert provider_auth.login_command("copilot")[-1] == "login"
    assert provider_auth.login_command("claude") == ["/usr/bin/claude"]
