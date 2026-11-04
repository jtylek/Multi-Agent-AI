"""Read-only coordinator chat calls through installed provider CLIs.

Each message is an explicit user action. No provider is contacted during discovery,
project loading, or tests; tests inject a fake process runner.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, Sequence

from . import winproc
from .gui_projects import COORDINATORS
from .workers.base import LaunchRequest
from .workers.claude import ClaudeAdapter
from .workers.codex import CodexAdapter

CHAT_TIMEOUT_SECONDS = 180
MAX_HISTORY_TURNS = 8
MAX_PROMPT_CHARACTERS = 24_000
HELLO_WORLD_REQUEST = "Reply with exactly Hello World! and nothing else."


class ChatError(RuntimeError):
    """One provider call failed without changing project files."""


def resolve_chat_command(provider: str) -> winproc.ResolvedCommand:
    """Use the actual Copilot CLI on Windows, not VS Code's forwarding shim.

    The VS Code ``copilot-shim/copilot.exe`` can find an npm ``copilot.cmd`` but
    fails to start that batch file with ``UnsupportedWindowsCommandValue``.
    ``winproc`` can launch npm's shim directly as ``node.exe npm-loader.js``.
    """
    if provider == "copilot" and winproc.IS_WINDOWS:
        directories = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
        directories = [part for part in directories
                       if Path(part.strip('"')).name.casefold() != "copilot-shim"]
        return winproc.resolve_command(provider, path=os.pathsep.join(directories))
    return winproc.resolve_command(provider)


def detected_chat_providers(resolver: Callable = resolve_chat_command) -> tuple[str, ...]:
    found = []
    for provider in COORDINATORS:
        try:
            resolver(provider)
        except (OSError, ValueError):
            continue
        found.append(provider)
    return tuple(found)


def build_chat_prompt(project_root: Path, provider: str, coordinator: str,
                      history: Sequence[tuple[str, str]], message: str) -> str:
    if not message.strip():
        raise ValueError("Enter a message first")
    role = "the coordinator" if provider == coordinator else "a participating AI agent"
    prefix = (
        f"You are {role} for the project at {project_root}. "
        "Discuss the user's project and answer the latest message. "
        "You may inspect the project, but do not edit files, launch workers, or claim work was executed. "
        "If the user requests exact output, return that output exactly.\n\n"
    )
    turns = [f"{speaker}: {text}" for speaker, text in history[-MAX_HISTORY_TURNS:]]
    turns.append(f"User: {message.strip()}")
    while turns and len(prefix + "\n\n".join(turns)) > MAX_PROMPT_CHARACTERS:
        if len(turns) == 1:
            raise ValueError("Message is too long for Project AI chat")
        turns.pop(0)
    return prefix + "\n\n".join(turns) + "\nAssistant:"


def ask_provider(provider: str, project_root: Path, coordinator: str,
                 history: Sequence[tuple[str, str]], message: str, *,
                 cancel: threading.Event | None = None,
                 resolver: Callable = resolve_chat_command,
                 runner: Callable = winproc.run,
                 temp_dir: Path | None = None) -> str:
    """Send one read-only turn and return only the provider's final response."""
    if provider not in COORDINATORS or coordinator not in COORDINATORS:
        raise ValueError("Unsupported provider or coordinator")
    project_root = Path(project_root).resolve()
    if not project_root.is_dir():
        raise ValueError(f"Project folder does not exist: {project_root}")
    prompt = build_chat_prompt(project_root, provider, coordinator, history, message)
    try:
        command = resolver(provider)
    except (OSError, ValueError) as exc:
        raise ChatError(f"{provider} CLI is not available: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="multi-agent-ai-chat-", dir=temp_dir) as work:
        run_dir = Path(work)
        prompt_path = run_dir / "prompt.txt"
        stdout_path = run_dir / "stdout.txt"
        stderr_path = run_dir / "stderr.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        adapter = {"codex": CodexAdapter, "claude": ClaudeAdapter}.get(provider)
        if adapter is not None:
            agent = adapter()
            invocation = agent.build_invocation(LaunchRequest(
                project_root, "review", prompt, prompt_path, run_dir))
            args = invocation.args
            stdin_path = invocation.stdin_path
            extra_env = agent.env_names
        else:
            # Copilot's prompt mode accepts the prompt as one argv element. The
            # available-tools restriction exposes read-only project tools only.
            args = ("-C", str(project_root), "-p", prompt, "--silent", "--no-color",
                    "--available-tools=view,grep,glob", "--allow-tool=view,grep,glob",
                    "--disallow-temp-dir")
            stdin_path = None
            extra_env = ()
        result = runner(command, args, cwd=project_root,
                        env=winproc.build_env(extra_names=extra_env),
                        stdout_path=stdout_path, stderr_path=stderr_path,
                        stdin_path=stdin_path, timeout=CHAT_TIMEOUT_SECONDS, cancel=cancel)
        if result.cancelled:
            raise ChatError(f"{provider} request cancelled")
        if result.timed_out:
            raise ChatError(f"{provider} request timed out")
        if result.launch_error:
            raise ChatError(f"{provider} could not start: {result.launch_error}")
        if result.exit_code != 0:
            detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-400:].strip()
            raise ChatError(f"{provider} exited with code {result.exit_code}: {detail or 'no error text'}")
        if adapter is not None:
            parsed = agent.parse_output(stdout_path, invocation)
            if parsed.malformed:
                raise ChatError(f"{provider} returned no usable response: {parsed.malformed}")
            response = parsed.final_message
        else:
            response = stdout_path.read_text(encoding="utf-8", errors="replace")
        if not response or not response.strip():
            raise ChatError(f"{provider} returned an empty response")
        return response.strip()
