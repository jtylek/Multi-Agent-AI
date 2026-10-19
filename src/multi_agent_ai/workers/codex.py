"""OpenAI Codex CLI adapter (``codex exec``, flags verified against codex-cli 0.163.0 --help).

Profiles:

* ``codex-review``:    ``codex exec -C <wt> -s read-only --ephemeral --color never --json
  -o <final-message> [-m M] -``
* ``codex-implement``: the same with ``-s workspace-write`` and without ``--ephemeral``, so a
  usage limit leaves a session that ``codex exec resume <id>`` can continue (T15).

The prompt is read from stdin (``-`` as the PROMPT argument; documented in ``codex exec
--help``), so it never passes through Windows argv quoting. ``--ephemeral`` keeps the run out
of Codex's persisted session list: a managed worker must not appear as, or be resumed like, an
interactive chat, and the orchestrator keeps its own logs. ``--skip-git-repo-check`` is not
used (worktrees are Git repositories, and outside one Codex should refuse). No approval or
config override is passed: ``codex exec`` is non-interactive, and the sandbox mode is the
least-privilege control.
"""

from __future__ import annotations

from pathlib import Path

from .base import Invocation, LaunchRequest, ParsedOutput, ProviderAdapter, read_jsonl, token_counts

SANDBOX = {"review": "read-only", "implement": "workspace-write"}
FINAL_MESSAGE_FILE = "final-message.md"


class CodexAdapter(ProviderAdapter):
    name = "codex"
    command = "codex"
    env_names = ("CODEX_HOME",)

    def build_invocation(self, request: LaunchRequest) -> Invocation:
        final = request.run_dir / FINAL_MESSAGE_FILE
        # implement runs keep their session (no --ephemeral) so a usage limit can be resumed (T15)
        args = ["exec", "-C", str(request.worktree), "-s", SANDBOX[request.mode],
                *(["--ephemeral"] if request.mode == "review" else []), "--color", "never", "--json", "-o", str(final)]
        if request.model:
            args += ["-m", request.model]
        args.append("-")
        return Invocation(tuple(args), f"codex-{request.mode}", stdin_path=request.prompt_path,
                          final_message_path=final)

    def parse_output(self, stdout_path: Path, invocation: Invocation) -> ParsedOutput:
        events, error = read_jsonl(stdout_path)
        usage: dict[str, int] = {}
        session: dict[str, str] = {}
        for event in events:
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                session["thread_id"] = event["thread_id"]
            for key, value in token_counts(event.get("usage")).items():
                usage[key] = usage.get(key, 0) + value
        message = None
        path = invocation.final_message_path
        if path is not None and path.is_file():
            message = path.read_bytes().decode("utf-8", "replace")
        if error is None and not (message or "").strip():
            error = "no final message (-o file missing or empty)"
        return ParsedOutput(message, usage or "unknown", error, session)
