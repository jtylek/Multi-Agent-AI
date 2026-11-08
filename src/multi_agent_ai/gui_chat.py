"""Read-only coordinator chat calls through installed provider CLIs.

Each message is an explicit user action. No provider is contacted during discovery,
project loading, or tests; tests inject a fake process runner.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, Sequence

from . import winproc
from .chat_log import DESCRIBERS, MAX_DETAIL, MAX_SUMMARY, EventSink, StreamTail, clip
from .gui_projects import COORDINATORS
from .workers.base import LaunchRequest
from .workers.claude import ClaudeAdapter
from .workers.codex import CodexAdapter
from .workers.copilot import final_message_from_events

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


def stream_claude_args(args: Sequence[str]) -> tuple[str, ...]:
    """The chat's Claude call streams events (``stream-json`` needs ``--verbose`` in print mode) so
    Live Activity can show tool use while it runs. Only the output format changes; the read-only
    review profile's permissions are untouched, and workers keep the adapter's plain JSON."""
    swapped = ["--output-format=stream-json" if arg == "--output-format=json" else arg for arg in args]
    if "--output-format=stream-json" not in swapped:
        raise ValueError("Claude adapter no longer passes --output-format=json")
    return (*swapped, "--verbose")


def last_claude_result_line(stdout_path: Path) -> str | None:
    """The last ``type: result`` line of a stream-json transcript."""
    text = stdout_path.read_bytes().decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("type") == "result":
            return line
    return None


def copilot_reply(stdout_path: Path) -> tuple[str | None, list[str]]:
    """The final assistant message of Copilot's JSONL output and the event types seen (in first-seen
    order, so a schema mismatch is diagnosable). Non-JSON lines are skipped, not fatal."""
    events = []
    for line in stdout_path.read_bytes().decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    types = list(dict.fromkeys(str(event.get("type", "?")) for event in events))
    return final_message_from_events(events), types


def ask_provider(provider: str, project_root: Path, coordinator: str,
                 history: Sequence[tuple[str, str]], message: str, *,
                 cancel: threading.Event | None = None,
                 resolver: Callable = resolve_chat_command,
                 runner: Callable = winproc.run,
                 temp_dir: Path | None = None,
                 on_event: EventSink | None = None,
                 timeout: float = CHAT_TIMEOUT_SECONDS) -> str:
    """Send one read-only turn and return only the provider's final response.

    ``on_event(kind, fields)`` (optional, called from this thread and a stream-reader thread)
    receives ``provider_started``, live provider events and ``provider_finished``."""
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

    def emit(kind: str, summary: str, **fields):
        if on_event is not None:
            on_event(kind, {"provider": provider, "summary": clip(summary, MAX_SUMMARY, one_line=True),
                            **fields})

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
            if provider == "claude":
                args = stream_claude_args(args)
            stdin_path = invocation.stdin_path
            extra_env = agent.env_names
            profile = invocation.profile
        else:
            # Copilot's prompt mode accepts the prompt as one argv element. The
            # available-tools restriction exposes read-only project tools only.
            # JSONL output lets Live Activity show tool use while it runs (schema unverified;
            # see chat_log.describe_copilot_event and copilot_reply).
            args = ("-C", str(project_root), "-p", prompt, "--silent", "--no-color",
                    "--output-format", "json",
                    "--available-tools=view,grep,glob", "--allow-tool=view,grep,glob",
                    "--disallow-temp-dir")
            stdin_path = None
            extra_env = ()
            profile = "copilot-chat-read-only"
        describe = DESCRIBERS.get(provider)
        role = "coordinator" if provider == coordinator else "participant"
        emit("provider_started", f"{role}, {profile}, prompt {len(prompt)} characters"
             + ("" if describe else ", no live events"),
             role=role, profile=profile, prompt_characters=len(prompt), live_events=describe is not None)
        tail = None
        if describe is not None and on_event is not None:
            def handle(event):
                for kind, summary, detail in describe(event):
                    emit(kind, summary, detail=clip(detail, MAX_DETAIL))
            tail = StreamTail(stdout_path, handle).start()
        try:
            result = runner(command, args, cwd=project_root,
                            env=winproc.build_env(extra_names=extra_env),
                            stdout_path=stdout_path, stderr_path=stderr_path,
                            stdin_path=stdin_path, timeout=timeout, cancel=cancel)
        finally:
            if tail is not None:
                tail.stop()
        outcome = ("cancelled" if result.cancelled else "timed_out" if result.timed_out
                   else "launch_error" if result.launch_error
                   else "ok" if result.exit_code == 0 else "exit_nonzero")
        finished = {"outcome": outcome, "exit_code": result.exit_code,
                    "duration_seconds": getattr(result, "duration_seconds", None)}
        if result.cancelled:
            emit("provider_finished", "cancelled", **finished)
            raise ChatError(f"{provider} request cancelled")
        if result.timed_out:
            emit("provider_finished", f"timed out after {timeout:g} s", **finished)
            raise ChatError(f"{provider} request timed out")
        if result.launch_error:
            emit("provider_finished", f"could not start: {result.launch_error}", **finished)
            raise ChatError(f"{provider} could not start: {result.launch_error}")
        if result.exit_code != 0:
            detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-400:].strip()
            emit("provider_finished", f"exit code {result.exit_code}", stderr_tail=detail, **finished)
            raise ChatError(f"{provider} exited with code {result.exit_code}: {detail or 'no error text'}")
        usage = cost = None
        if adapter is not None:
            output_path = stdout_path
            if provider == "claude":
                line = last_claude_result_line(stdout_path)
                output_path = run_dir / "result.json"
                output_path.write_text(line or "", encoding="utf-8")
            parsed = agent.parse_output(output_path, invocation)
            usage = parsed.usage
            cost = parsed.session.get("provider_reported_cost_usd") if parsed.session else None
            if parsed.malformed:
                emit("provider_finished", f"no usable response: {parsed.malformed}", usage=usage, **finished)
                raise ChatError(f"{provider} returned no usable response: {parsed.malformed}")
            response = parsed.final_message
        else:
            response, event_types = copilot_reply(stdout_path)
            finished["event_types"] = event_types
            if response is None:
                emit("provider_finished", "no assistant message in JSON output", **finished)
                raise ChatError("copilot returned JSON with no recognisable assistant message; "
                                f"event types seen: {', '.join(event_types) or 'none'}")
        if not response or not response.strip():
            emit("provider_finished", "empty response", usage=usage, **finished)
            raise ChatError(f"{provider} returned an empty response")
        duration = finished["duration_seconds"]
        emit("provider_finished",
             f"ok{f' in {duration:.1f} s' if isinstance(duration, (int, float)) else ''}, tokens {usage or 'unknown'}",
             usage=usage, provider_reported_cost_usd=cost, **finished)
        return response.strip()
