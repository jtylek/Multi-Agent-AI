"""Provider-neutral interface of a managed headless worker adapter.

An adapter only turns a :class:`LaunchRequest` into an argv (:class:`Invocation`) and reads
the provider's output back (:class:`ParsedOutput`). Launching, git checks, classification and
the coordination files belong to the runner, so every provider gets the same guarantees.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

MODES = ("review", "implement")
PROMPT_PLACEHOLDER = "<PROMPT>"

FORBIDDEN_FLAGS = (
    # Codex
    "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust", "--full-auto",
    "--approve-for-me", "--ignore-rules", "danger-full-access",
    # Copilot
    "--allow-all-tools", "--allow-all-paths", "--allow-all", "--yolo", "--allow-all-urls",
    "--no-ask-user", "--autopilot", "--assisted-approval", "--allow-all-mcp-server-instructions",
    "--enable-all-github-mcp-tools", "--share-gist", "--remote", "--add-dir",
    # Claude Code (not adapted yet; listed so a future adapter is scanned too)
    "--dangerously-skip-permissions",
)
"""Flags (or flag values) that bypass approvals, sandboxing or path/tool/URL checks, or send
data off the machine. No adapter may ever emit one; tests scan every generated argv."""


class ProfileUnconfigured(RuntimeError):
    """The least-privilege launch profile is incomplete; the worker is not started."""


def forbidden_in(argv: Sequence[str]) -> list[str]:
    """The forbidden flags present in ``argv`` (exact element, ``--flag=value`` form, or a
    sandbox value such as ``danger-full-access``)."""
    found = []
    for arg in argv:
        head = arg.split("=", 1)[0]
        for flag in FORBIDDEN_FLAGS:
            if head == flag or arg == flag:
                found.append(flag)
    return found


@dataclass(frozen=True)
class LaunchRequest:
    worktree: Path
    mode: str
    prompt: str
    prompt_path: Path
    run_dir: Path
    model: str | None = None
    session_id: str | None = None
    verify_command: str | None = None
    """The project's verification command (used where a provider needs it, e.g. Copilot's
    ``shell(...)`` allowance)."""


@dataclass(frozen=True)
class Invocation:
    args: tuple[str, ...]
    """argv after the executable."""
    profile: str
    """Name of the launch profile, e.g. ``codex-review``."""
    stdin_path: Path | None = None
    final_message_path: Path | None = None
    """Where the provider itself writes its final message, if it does."""
    usage_path: Path | None = None

    def redacted_args(self, prompt: str) -> list[str]:
        return [PROMPT_PLACEHOLDER if a == prompt else a for a in self.args]


@dataclass
class ParsedOutput:
    final_message: str | None
    usage: dict[str, int] | str = "unknown"
    malformed: str | None = None
    """Why the output does not match the expected format, if it does not."""
    session: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter:
    name = ""
    command = ""
    env_names: tuple[str, ...] = ()
    """Environment variables this provider needs besides winproc.BASE_ENV_NAMES (no secrets)."""

    def build_invocation(self, request: LaunchRequest) -> Invocation:  # pragma: no cover
        raise NotImplementedError

    def parse_output(self, stdout_path: Path, invocation: Invocation) -> ParsedOutput:  # pragma: no cover
        raise NotImplementedError


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Parse a JSONL file; returns (objects, error). Blank lines are ignored."""
    events: list[dict[str, Any]] = []
    try:
        text = path.read_bytes().decode("utf-8", "replace")
    except OSError as exc:
        return [], f"cannot read stdout: {exc}"
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            return events, f"stdout line {number} is not JSON"
        if not isinstance(value, dict):
            return events, f"stdout line {number} is not a JSON object"
        events.append(value)
    return events, None


def token_counts(value: Any) -> dict[str, int]:
    """Integer fields whose name mentions tokens, from a dict and its direct sub-dicts."""
    counts: dict[str, int] = {}
    if not isinstance(value, dict):
        return counts
    for key, item in value.items():
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and "token" in key.lower():
            counts[key] = item
        elif isinstance(item, dict):
            for sub, number in item.items():
                if isinstance(number, int) and not isinstance(number, bool) and "token" in sub.lower():
                    counts[f"{key}.{sub}"] = number
    return counts
