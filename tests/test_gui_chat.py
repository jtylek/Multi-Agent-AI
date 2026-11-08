"""Project chat uses fake provider processes and read-only invocations."""

import json
import os
from types import SimpleNamespace

import pytest

from multi_agent_ai.gui_chat import (
    HELLO_WORLD_REQUEST, ChatError, ask_provider, build_chat_prompt, detected_chat_providers,
    resolve_chat_command,
)
from multi_agent_ai import winproc


def test_detected_providers_include_only_available_clis():
    def resolve(name):
        if name == "copilot":
            raise OSError("missing")
        return name

    assert detected_chat_providers(resolve) == ("codex", "claude")


def test_copilot_skips_vscode_forwarder_and_runs_npm_shim(monkeypatch, tmp_path):
    vscode = tmp_path / "VS Code" / "bin" / "copilot-shim"
    npm = tmp_path / "npm"
    node = tmp_path / "node"
    vscode.mkdir(parents=True)
    npm.mkdir()
    node.mkdir()
    (vscode / "copilot.exe").write_bytes(b"forwarder")
    (npm / "copilot.cmd").write_text(
        '"%_prog%" "%dp0%\\node_modules\\@github\\copilot\\npm-loader.js" %*',
        encoding="utf-8")
    loader = npm / "node_modules" / "@github" / "copilot" / "npm-loader.js"
    loader.parent.mkdir(parents=True)
    loader.write_text("", encoding="utf-8")
    (node / "node.exe").write_bytes(b"node")
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    monkeypatch.setenv("PATH", os.pathsep.join((str(vscode), str(npm), str(node))))

    resolved = resolve_chat_command("copilot")
    assert resolved.kind == "npm-shim"
    assert resolved.path == npm / "copilot.cmd"
    assert resolved.prefix == (str(node / "node.exe"), str(loader))
    assert detected_chat_providers() == ("copilot",)


@pytest.mark.parametrize("provider", ["codex", "claude", "copilot"])
def test_hello_world_uses_read_only_provider_call(tmp_path, provider):
    calls = []

    def fake_runner(command, args, **kwargs):
        calls.append((command, list(args), kwargs))
        prompt = kwargs["stdin_path"].read_text(encoding="utf-8") if kwargs["stdin_path"] else args[args.index("-p") + 1]
        assert HELLO_WORLD_REQUEST in prompt
        kwargs["stderr_path"].write_text("", encoding="utf-8")
        if provider == "codex":
            output = args[args.index("-o") + 1]
            from pathlib import Path
            Path(output).write_text("Hello World!", encoding="utf-8")
            kwargs["stdout_path"].write_text('{"type":"thread.started","thread_id":"fake"}\n', encoding="utf-8")
        elif provider == "claude":
            kwargs["stdout_path"].write_text(json.dumps({"type": "result", "result": "Hello World!"}), encoding="utf-8")
        else:
            kwargs["stdout_path"].write_text(
                json.dumps({"type": "assistant.message", "data": {"content": "Hello World!"}}) + "\n",
                encoding="utf-8")
        return SimpleNamespace(exit_code=0, timed_out=False, cancelled=False, launch_error=None)

    answer = ask_provider(provider, tmp_path, "codex", [], HELLO_WORLD_REQUEST,
                          resolver=lambda name: name, runner=fake_runner, temp_dir=tmp_path)
    assert answer == "Hello World!"
    _, args, kwargs = calls[0]
    assert kwargs["cwd"] == tmp_path.resolve()
    assert kwargs["timeout"] == 180
    if provider == "codex":
        assert args[args.index("-s") + 1] == "read-only"
        assert "--ephemeral" in args
    elif provider == "claude":
        assert "--no-session-persistence" in args
        assert any(arg.startswith("--disallowedTools=Edit,Write") for arg in args)
    else:
        assert "--available-tools=view,grep,glob" in args
        assert all("allow-all" not in arg for arg in args)
        assert args[args.index("--output-format") + 1] == "json"


def test_prompt_keeps_recent_context_and_bounds_size(tmp_path):
    history = [("User", "first"), ("Assistant", "second")]
    prompt = build_chat_prompt(tmp_path, "claude", "codex", history, "third")
    assert "a participating AI agent" in prompt
    assert "User: first" in prompt and "Assistant: second" in prompt
    assert prompt.endswith("User: third\nAssistant:")
    with pytest.raises(ValueError, match="too long"):
        build_chat_prompt(tmp_path, "codex", "codex", [], "x" * 25000)


def test_failed_provider_call_is_reported(tmp_path):
    def fake_runner(command, args, **kwargs):
        kwargs["stderr_path"].write_text("authentication required", encoding="utf-8")
        return SimpleNamespace(exit_code=1, timed_out=False, cancelled=False, launch_error=None)

    with pytest.raises(ChatError, match="authentication required"):
        ask_provider("codex", tmp_path, "codex", [], "hello", resolver=lambda name: name,
                     runner=fake_runner, temp_dir=tmp_path)
