"""Claude Code adapter (``claude -p``, flags read from ``claude --help`` of 2.1.301; no model was run).

The tool names and the shell tool are data in :data:`PROFILES`; changing one is a one-place change
(the T25 smoke test is where a choice is confirmed or flipped). argv (``<S>`` = the profile's shell
tool, ``<V>`` = the verification command, both profile/request parameters)::

    claude -p --output-format=json --restricted --safe-mode --strict-mcp-config
           --disable-slash-commands --no-chrome --permission-prompts=none [--model=M]
           [--max-budget-usd=N]
           --tools=Read,Grep,Glob,<S>
           --allowedTools=Read,Grep,Glob,<S>(git diff *),<S>(git log *),<S>(git show *),<S>(git status *),<S>(<V>)
           --disallowedTools=Edit,Write,NotebookEdit,WebFetch,WebSearch,<S>(git commit *),...
           --no-session-persistence                        (review)
           --session-id=<uuid>                             (implement; resumable)

Implement mode adds ``Edit,Write`` to ``--tools``/``--allowedTools`` and removes them from the deny
list. The prompt is read from stdin (no positional prompt: ``--tools`` and friends are variadic
and would swallow one), so it never passes through Windows argv quoting.

Why: ``--restricted`` ignores the user's/project's settings files (their allow rules, hooks and
default permission mode cannot widen the worker), removes command-running tools unless ``--tools``
names them and confines file tools to the working directory; ``--safe-mode`` disables CLAUDE.md,
skills, plugins, hooks and MCP while sign-in keeps working (``--bare`` would force an API key);
``--permission-prompts=none`` denies anything not on the allow list. ``--permission-mode`` is never
passed, so neither ``bypassPermissions`` nor ``auto`` can occur.

Unverified until the first real run (docs/implementation-status.md): that the prompt is read from
stdin; that the named shell tool exists and its ``Tool(rule *)`` patterns match; that
``--restricted`` and ``--safe-mode`` combine as read; the JSON result field names (``result``,
``is_error``, ``session_id``, ``usage``, ``total_cost_usd``, ``subtype``); resume of an implement
session. A denial or a missing tool is reported (``permission_denied`` / nonzero exit) and is never
fixed by widening the profile.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import (
    Invocation,
    LaunchRequest,
    ParsedOutput,
    ProfileUnconfigured,
    ProviderAdapter,
    token_counts,
)

DEFAULT_SHELL_TOOL = "PowerShell"
"""AGENTS.md: no Bash dependency for the core product. Flip to ``Bash`` in :data:`PROFILES` (with
forward-slash verification commands) if the PowerShell tool does not work (T25)."""

COMMON_FLAGS = (
    "-p", "--output-format=json", "--restricted", "--safe-mode", "--strict-mcp-config",
    "--disable-slash-commands", "--no-chrome", "--permission-prompts=none",
)
READ_TOOLS = ("Read", "Grep", "Glob")
EDIT_TOOLS = ("Edit", "Write")
GIT_READ = ("git diff *", "git log *", "git show *", "git status *")
GIT_DENY = ("git commit *", "git push *", "git checkout *", "git switch *", "git reset *", "git merge *",
            "git rebase *", "git worktree *", "git branch *", "git tag *", "git stash *", "git clean *")
ALWAYS_DENY = ("NotebookEdit", "WebFetch", "WebSearch")

_UNSAFE_RULE = re.compile(r"[(),\r\n\0]")


@dataclass(frozen=True)
class ClaudeProfile:
    name: str
    mode: str
    shell_tool: str = DEFAULT_SHELL_TOOL
    can_edit: bool = False
    persist_session: bool = False

    def tools(self) -> tuple[str, ...]:
        return (*READ_TOOLS, *(EDIT_TOOLS if self.can_edit else ()), self.shell_tool)

    def allowed(self, verify_rule: str | None) -> tuple[str, ...]:
        rules = [*READ_TOOLS, *(EDIT_TOOLS if self.can_edit else ())]
        rules += [f"{self.shell_tool}({g})" for g in GIT_READ]
        if verify_rule:
            rules.append(f"{self.shell_tool}({verify_rule})")
        return tuple(rules)

    def denied(self) -> tuple[str, ...]:
        return (*(() if self.can_edit else EDIT_TOOLS), *ALWAYS_DENY,
                *(f"{self.shell_tool}({g})" for g in GIT_DENY))


PROFILES: dict[str, ClaudeProfile] = {
    "review": ClaudeProfile("claude-review", "review"),
    "implement": ClaudeProfile("claude-implement", "implement", can_edit=True, persist_session=True),
}
"""The single place where Claude permissions are configured."""


def verification_rule(command: str | None) -> str | None:
    """The verification command as a permission-rule argument, or None when it cannot be one
    (missing, multi-line, or containing ``(``, ``)`` or ``,``, which would break the rule or the
    comma-separated list). Without it the profile simply has no test-running rule."""
    if command is None or _UNSAFE_RULE.search(command):
        return None
    command = " ".join(command.split())
    return command if command and not command.startswith("-") else None


def parse_result(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """The result object of ``--output-format=json`` output, tolerating leading log lines and a
    list of events (the last ``type: result`` object wins). Returns (object, error)."""
    candidates = [text.strip()]
    candidates += [line.strip() for line in reversed(text.splitlines()) if line.strip().startswith(("{", "["))]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value, None
        if isinstance(value, list):
            results = [v for v in value if isinstance(v, dict) and v.get("type") == "result"]
            if results:
                return results[-1], None
    return None, "stdout holds no JSON result object"


class ClaudeAdapter(ProviderAdapter):
    name = "claude"
    command = "claude"
    env_names = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH")

    def __init__(self, profiles: dict[str, ClaudeProfile] | None = None) -> None:
        self.profiles = profiles if profiles is not None else PROFILES

    def build_invocation(self, request: LaunchRequest) -> Invocation:
        profile = self.profiles.get(request.mode)
        if profile is None:
            raise ProfileUnconfigured(f"no Claude profile for mode {request.mode!r}")
        args = list(COMMON_FLAGS)
        if request.model:
            args.append(f"--model={request.model}")
        if request.max_budget_usd is not None:
            args.append(f"--max-budget-usd={request.max_budget_usd:g}")
        args.append("--tools=" + ",".join(profile.tools()))
        args.append("--allowedTools=" + ",".join(profile.allowed(verification_rule(request.verify_command))))
        args.append("--disallowedTools=" + ",".join(profile.denied()))
        if profile.persist_session:
            if not request.session_id:
                raise ProfileUnconfigured(f"{profile.name}: a persisted session needs a session id")
            args.append(f"--session-id={request.session_id}")
        else:
            args.append("--no-session-persistence")
        return Invocation(tuple(args), profile.name, stdin_path=request.prompt_path)

    def parse_output(self, stdout_path: Path, invocation: Invocation) -> ParsedOutput:
        try:
            text = stdout_path.read_bytes().decode("utf-8", "replace")
        except OSError as exc:
            return ParsedOutput(None, "unknown", f"cannot read stdout: {exc}")
        result, error = parse_result(text)
        if result is None:
            return ParsedOutput(None, "unknown", error)
        message = result.get("result") if isinstance(result.get("result"), str) else None
        usage = token_counts(result.get("usage")) or "unknown"
        session: dict[str, Any] = {}
        if isinstance(result.get("session_id"), str):
            session["session_id"] = result["session_id"]
        cost = result.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            session["provider_reported_cost_usd"] = cost
        problem = None
        if result.get("is_error") is True:
            problem = f"the result reports is_error ({result.get('subtype') or 'no subtype'})"
        elif message is None or not message.strip():
            problem = "no result text in the JSON result"
        return ParsedOutput(message, usage, problem, session)
