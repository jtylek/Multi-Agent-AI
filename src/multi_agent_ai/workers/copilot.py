"""GitHub Copilot CLI adapter (``copilot -p``, flags verified against Copilot CLI 1.0.98 --help).

The tool names and permission patterns come from T13
(``tasks/m0-foundation/notes/copilot-cli-permissions.md``, accepted in review-T13-claude-a7.md).
They are data in :data:`PROFILES`; changing a profile is a one-place change there.

argv (``<V>`` = the verification-command pattern, a profile parameter)::

    copilot -C <wt> -p <PROMPT> --output-format json --silent --session-id <uuid>
            --usage-output-file <run>/usage.json --disallow-temp-dir [--model M]
            --available-tools=<S>,view,grep,glob          (<S> = SHELL_TOOL, "powershell"; see below)
            --allow-tool=shell(git diff),shell(git log),shell(git show),shell(<V>)
            --deny-tool=shell(git commit),shell(git push),url

Implement mode exposes ``create``/``edit`` and appends ``write`` to ``--allow-tool``. Every
comma list is one argv element (``--flag=value``). No ``--add-dir``: Copilot's path check
then limits file tools to the worktree (``-C``), and ``--disallow-temp-dir`` removes the temp
directory. Without a usable verification pattern the profile is unconfigured and the worker is
not started (failure class ``profile_unconfigured``).

Unverified until the first real run: whether ``shell(<V>)`` with a full command prefix (for
example ``.venv\\Scripts\\python -m pytest -q``) matches the shell call the model makes, and
that prompt mode works without ``--allow-all-tools`` when tools are used (T13 saw it work for
a prompt that used no tool). Neither is ever "fixed" by widening the profile; a denial is
reported (``permission_denied``). The JSONL event schema is also unverified, so the final
message is extracted defensively (see :func:`final_message_from_events`).

The prompt is passed as an argv element (``-p``); there is no documented stdin/file prompt
for prompt mode. It reaches node unaltered because npm shims are run as ``node.exe <script>``
(see winproc), never through ``cmd.exe``.
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
    forbidden_in,
    read_jsonl,
    token_counts,
)

USAGE_FILE = "usage.json"


@dataclass(frozen=True)
class CopilotProfile:
    name: str
    available_tools: tuple[str, ...]
    allow_tools: tuple[str, ...]
    """Allowed before the verification pattern."""
    allow_tools_after: tuple[str, ...]
    """Allowed after the verification pattern (``write`` for implement)."""
    deny_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        items = (*self.available_tools, *self.allow_tools, *self.allow_tools_after, *self.deny_tools)
        if any("," in i or not i.strip() for i in items):
            raise ValueError(f"{self.name}: tool items must be non-empty and contain no comma")
        if forbidden_in(items):
            raise ValueError(f"{self.name}: forbidden item {forbidden_in(items)}")
        if self.name.endswith("-review") and (
            {"create", "edit"} & set(self.available_tools)
            or any(i == "write" or i.startswith("write(") for i in (*self.allow_tools, *self.allow_tools_after))
        ):
            raise ValueError(f"{self.name}: a review profile may not expose or allow file writes")

    def argv(self, verification_pattern: str) -> list[str]:
        allow = (*self.allow_tools, verification_pattern, *self.allow_tools_after)
        return [
            "--available-tools=" + ",".join(self.available_tools),
            "--allow-tool=" + ",".join(allow),
            "--deny-tool=" + ",".join(self.deny_tools),
        ]


_GIT_READ = ("shell(git diff)", "shell(git log)", "shell(git show)")
_DENY = ("shell(git commit)", "shell(git push)", "url")

SHELL_TOOL = "powershell"
"""Name of the shell tool for ``--available-tools``. T13 documented ``bash``, but the first real
run on Windows (T25 P1) was answered ``Tool 'bash' does not exist.``. Neither ``copilot --help``,
``copilot help permissions`` nor the installed package names the Windows tool (the CLI's code is
compressed inside the binary), so ``powershell`` is a best guess that one approved real run
confirms or flips: change this one constant (or pass ``shell_tool`` to :func:`make_profiles`).
The ``shell(...)`` permission kind in ``--allow-tool``/``--deny-tool`` is documented by
``copilot help permissions`` as the kind for every shell command and does not depend on it."""


def make_profiles(shell_tool: str = SHELL_TOOL) -> dict[str, CopilotProfile]:
    return {
        "review": CopilotProfile("copilot-review", (shell_tool, "view", "grep", "glob"), _GIT_READ, (), _DENY),
        "implement": CopilotProfile(
            "copilot-implement", (shell_tool, "create", "edit", "view", "grep", "glob"), _GIT_READ, ("write",), _DENY
        ),
    }


PROFILES = make_profiles()
"""The single place where Copilot permissions are configured (from T13, shell tool from T28)."""

_UNSAFE_PATTERN = re.compile(r"[(),\r\n\0]")


def verification_pattern(command: str | None) -> str | None:
    """``shell(<command>)`` for a verification command, or None when it cannot be expressed
    as one pattern (missing, multi-line, or containing ``(``, ``)`` or ``,``)."""
    if command is None or _UNSAFE_PATTERN.search(command):
        return None
    command = " ".join(command.split())
    if not command or _UNSAFE_PATTERN.search(command) or command.startswith("-"):
        return None
    return f"shell({command})"


def final_message_from_events(events: list[dict[str, Any]]) -> str | None:
    """The last assistant message text in Copilot's JSONL (schema unverified).

    Accepts events whose ``type`` mentions both "assistant" and "message" and whose text is in
    ``content``, ``text`` or ``message`` directly or under ``data``. Deltas are ignored.
    """
    message = None
    for event in events:
        kind = str(event.get("type", "")).lower()
        if "assistant" not in kind or "message" not in kind or "delta" in kind:
            continue
        for holder in (event.get("data"), event):
            if not isinstance(holder, dict):
                continue
            text = next((holder[k] for k in ("content", "text", "message") if isinstance(holder.get(k), str)), None)
            if text and text.strip():
                message = text
                break
    return message


def _count(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def usage_totals(document: Any) -> dict[str, Any] | None:
    """Full run totals from the usage file written by ``--usage-output-file`` (field names from a
    real run, Copilot CLI 1.0.98): ``tokenDetails`` (input, cache_read, cache_write, output),
    reasoning tokens and request count from ``modelMetrics``, ``totalPremiumRequestCost`` and the
    model. Everything is provider-reported, hence ``source: measured``; absent fields are left out,
    never invented. Returns None when nothing usable is present."""
    if not isinstance(document, dict):
        return None
    totals: dict[str, Any] = {}
    details = document.get("tokenDetails")
    if isinstance(details, dict):
        for key in ("input", "cache_read", "cache_write", "output"):
            entry = details.get(key)
            count = _count(entry.get("tokenCount")) if isinstance(entry, dict) else None
            if count is not None:
                totals[f"{key}_tokens"] = count
    models = document.get("modelMetrics")
    if isinstance(models, dict) and models:
        reasoning = requests = 0
        found_reasoning = found_requests = False
        for metrics in models.values():
            if not isinstance(metrics, dict):
                continue
            used = _count((metrics.get("usage") or {}).get("reasoningTokens")) if isinstance(metrics.get("usage"), dict) else None
            if used is not None:
                reasoning, found_reasoning = reasoning + used, True
            count = _count((metrics.get("requests") or {}).get("count")) if isinstance(metrics.get("requests"), dict) else None
            if count is not None:
                requests, found_requests = requests + count, True
        if found_reasoning:
            totals["reasoning_tokens"] = reasoning
        if found_requests:
            totals["request_count"] = requests
        totals["models"] = sorted(str(name) for name in models)
    for source, target in (("totalPremiumRequestCost", "premium_request_cost"), ("totalUserRequests", "user_requests"),
                           ("totalApiDurationMs", "api_duration_ms"), ("totalNanoAiu", "nano_aiu")):
        value = _count(document.get(source))
        if value is not None:
            totals[target] = value
    if isinstance(document.get("currentModel"), str):
        totals["model"] = document["currentModel"]
    for source, target in (("lastCallInputTokens", "last_call_input_tokens"),
                          ("lastCallOutputTokens", "last_call_output_tokens")):
        value = _count(document.get(source))
        if value is not None:
            totals[target] = value
    if not totals:
        return None
    totals["source"] = "measured"
    return totals


class CopilotAdapter(ProviderAdapter):
    name = "copilot"
    command = "copilot"
    env_names = ("COPILOT_HOME", "GH_HOST", "COPILOT_GH_HOST")

    def __init__(self, profiles: dict[str, CopilotProfile] | None = None) -> None:
        self.profiles = profiles if profiles is not None else PROFILES

    def build_invocation(self, request: LaunchRequest) -> Invocation:
        profile = self.profiles.get(request.mode)
        if profile is None:
            raise ProfileUnconfigured(f"no Copilot profile for mode {request.mode!r}")
        pattern = verification_pattern(request.verify_command)
        if pattern is None:
            raise ProfileUnconfigured(
                f"{profile.name}: no verification command usable as a shell(...) pattern "
                f"(got {request.verify_command!r}); pass --verify-command"
            )
        if not request.session_id:
            raise ProfileUnconfigured("a Copilot run needs a session id")
        usage = request.run_dir / USAGE_FILE
        args = ["-C", str(request.worktree), "-p", request.prompt, "--output-format", "json", "--silent",
                "--session-id", request.session_id, "--usage-output-file", str(usage), "--disallow-temp-dir"]
        if request.model:
            args += ["--model", request.model]
        args += profile.argv(pattern)
        return Invocation(tuple(args), profile.name, usage_path=usage)

    def parse_output(self, stdout_path: Path, invocation: Invocation) -> ParsedOutput:
        events, error = read_jsonl(stdout_path)
        message = final_message_from_events(events)
        if error is None and message is None:
            error = "no assistant message found in the JSONL output"
        usage: dict[str, int] | str = "unknown"
        session: dict[str, Any] = {}
        if invocation.usage_path is not None and invocation.usage_path.is_file():
            try:
                document = json.loads(invocation.usage_path.read_text(encoding="utf-8"))
                usage = token_counts(document) or "unknown"  # includes the lastCall* values
                if totals := usage_totals(document):
                    session["usage_totals"] = totals
            except (OSError, ValueError):
                usage = "unknown"
        return ParsedOutput(message, usage, error, session)
