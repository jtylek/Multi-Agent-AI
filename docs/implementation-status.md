# Implementation status

What exists in code today. Decisions are recorded in [decisions.md](decisions.md) and are not
repeated here; the protocol is [protocol/agent-messaging.md](protocol/agent-messaging.md)
(v1) and the file grammar [protocol/formats.md](protocol/formats.md) (format 1). Anything not
listed below is planned, not implemented.

## Implemented (milestone M0, package `multi_agent_ai`)

- Python package, `requires-python >=3.11`, standard library only at runtime, `pytest` as the
  `dev` extra (D1, D10).
- Common grammar of formats.md section 1 (`document.py`): lenient reads (BOM, CRLF/CR,
  header-termination and duplicate-key warnings, unknown keys preserved, invalid UTF-8 is an
  error) and strict atomic writes (UTF-8 without BOM, LF, temp file + rename, bounded retry
  when Windows reports the target locked). Id, revision and timestamp rules in `grammar.py`
  (D6, D7).
- Typed models (`models.py`) for request, ack, plan with subtask table, and status; plain
  operational records Job, Task, AgentSession, Reservation, Attempt and Notification.
- Mailbox (`mailbox.py`): request and ack parsing/writing, the ack `plan-revision` rules,
  per-recipient acknowledgment state and the "all agreed" computation (D5, D8). Writers refuse
  to write another agent's file, to acknowledge a request not addressed to the writer, or to
  overwrite an existing request or ack.
- Task board (`board.py`): plan and status parsing/writing, subtask-table cell and semantic
  checks (dependency DAG, reviewer vs owner, `proposed:` owners, overlapping active areas),
  strictly increasing plan revisions, stale revision, stale worker and status/plan conflicts.
- State root and derived index (`state.py`, D3, D9): configurable root (default
  `.multi-agent-ai/`), `config.json` recording it, and `state.sqlite3` holding the file index,
  the highest plan revision seen per task, and operational records. It never stores or infers
  an acknowledgment.
- CLI `multi-agent-ai` / `python -m multi_agent_ai`:
  - `init [--project-root P] [--state-root S]`: creates `messages/`, `tasks/`, `config.json`
    and the index; never touches existing coordination files or `.gitignore`.
  - `inspect [--state-root S] [--json] [--now "YYYY-MM-DD HH:MM"]`: read-only report of
    messages with per-recipient ack state, tasks with plan revision and subtask states, and
    staleness/conflict findings. It creates no file; an existing index is opened read-only.
  - `index`: rebuilds the derived index and reports plan revisions lower than one seen.
- Read-only reachability discovery (`reachability.py` and `agents`): exact-project-cwd Claude
  sessions and Codex app-server threads, with provider errors reported separately.
- Notification transport (`notify.py` and `notify`): dry-run by default; with explicit `--send`,
  sends mailbox pointers through configured Codex/Copilot contacts and reports Claude/manual relay
  targets. Transport is never an ack.
- CLI `doctor [--project-root P] [--state-root S] [--json]` (T20, `doctor.py`): read-only
  readiness report with a plain-language fix for each problem: Python >= 3.11, Git and the
  project repository, each provider CLI (Codex, Copilot, Claude Code: found on PATH, version via
  its own `--version`), the state root and index, and time zone data. It runs nothing else,
  writes nothing and never calls a provider, so sign-in is always reported as `not checked`.
  Statuses: `ok`, `warn`, `fail`, `not checked`; a missing CLI is a warning unless none is
  installed; exit 1 only when a check fails.
- CLI `worker run --provider codex|copilot --task T --subtask S --agent-id ID --mode
  review|implement --worktree ABS [--model M] [--timeout S] [--commit SHA]
  [--verify-command C] [--live-branch B]... [--pass-env NAME]... [--execute]`: one managed
  headless worker attempt (T12; see "Managed workers" in
  [protocol/agent-messaging.md](protocol/agent-messaging.md)). Exit 0 on success, 1 on a
  classified failure, 2 when rejected before an attempt exists. **Dry run by default (T17):**
  without `--execute` it runs the same plan, worktree and command checks, prints the resolved
  command and any problems as JSON, writes nothing (no runs dir, status file or SQLite attempt)
  and starts no provider; exit 0 when ready, 1 when problems are listed, 2 when rejected.
- Windows process runtime (`winproc.py`): executable resolution (`.exe` > `.cmd`/`.bat`, never
  `.ps1`; npm shims run as `node.exe <script>`), argv without a shell, refusal of arguments
  `cmd.exe` would alter, stdout/stderr streamed to files, timeout with process-tree kill via a
  Job Object (`taskkill /T /F` fallback), allowlisted environment.
- Managed workers (`workers/`): Codex adapter (`codex exec`, sandbox `read-only` /
  `workspace-write`, prompt on stdin) and Copilot adapter (`copilot -p`, tool profiles from T13
  as data in `workers/copilot.py`); deterministic prompt builder; table-driven failure
  classification; orchestrator-side Git checks and commit; status/review files; `runs/`
  layout; attempts in the SQLite index (`Attempt` record plus an `attempt_details` table for
  mode, usage and timings). Usage-limit handling (T15, `workers/quota.py`): failure classes
  `quota_exhausted`, `cap_reached`, `provider_error`; detector tables (Codex structured events;
  Claude and Copilot patterns are synthetic until real samples exist; **quota detection becomes
  active only once a provider's CLI version is positively known and listed in the detector
  table. The runner reads the version from the CLI's own `--version` (T18) but no table lists a
  version yet, so today every limit signature is reported as `provider_error` with the raw
  output**), reported-only reset
  parsing, `quota:`/`cap:` blocker in the worker status, suspended stale-worker rule, a `limit`
  record with the resume command. Not implemented: WIP `checkpoint:` commits, automatic
  linked resume attempts. CLI version detection (T18, `workers/versions.py`): `<cli> --version`
  is a local flag (no sign-in, no provider call, so no quota; bounded by a 15 s timeout, no shell,
  closed stdin, process-tree kill, same allowlisted environment), run once per `worker run
  --execute` after the command is resolved; the parsed `x.y.z` and the raw text are recorded in
  `result.json` (`cli_version`) and the SQLite attempt details; a failed or unparseable probe is
  recorded as unknown and never blocks the run; a dry run does not probe. The parser takes the
  first `x.y.z` and tolerates a trailing period and extra lines (real outputs: `codex-cli 0.163.0`;
  `GitHub Copilot CLI 1.0.98.` followed by `Run 'copilot update' to check for updates.`;
  `2.1.301 (Claude Code)`). That Copilot line suggests the CLI may check for updates, which is
  network traffic but not model quota. Tested only with fake executables; **no real provider run has been
  made yet** (planned after integration, with the user's OK).
- Tests in `tests/` cover every valid and invalid example in formats.md, plus the D8 rule
  that exit codes and notification transport never create an ack or a completion.

## Not implemented

Registry (`registry/`, O2) and reservation checks; review-file parsing; message cleanup and
bounded watches (formats.md section 7); Claude Code worker adapter; OS-level isolation of
workers (filesystem/network); creating worktrees; verification and routing; VS Code
integration.

Unverified until the first real run: Copilot's matching of `shell(<verification command>)`,
prompt mode without `--allow-all-tools` when tools are used, Copilot's JSONL event schema
(final message extraction is defensive) and `usage.json` fields; Codex's `--json` usage
event shape (tokens are taken from any `usage` object with integer `*token*` fields).

## Interpretations

Where formats.md left room, the implementation chose the conservative reading:

1. **Body-less files.** A file that is only a header block (the status example and the third
   ack example in formats.md) needs no blank separator and gets no warning, because the body
   is optional (plan revision 12). Non-header text directly after the header, with no blank
   line, ends the header there with a warning. Writers always emit the blank line.
2. **Header-only.** A request concerns a plan revision only if it has a `plan-revision`
   header; the body is never parsed for it (plan revision 11 replaced an earlier reading that
   inferred it from the body). Acks to a header-carrying request without `plan-revision` are
   invalid.
3. **Ack revision newer than the request's.** Reported as `mismatch` and not counted (only
   acks quoting the same revision count; formats.md only names the older case, `stale`).
4. **"All agreed" without a revision.** Agreed acks must still quote the same
   `plan-revision` value (or all none).
5. **Broadcast agreement.** formats.md defines no grammar for the expected-participant list in
   a broadcast body, so "all agreed" is never computed for a broadcast; a broadcast whose body
   mentions agreement gets a warning.
6. **Ack and request writers are create-only.** The protocol says a wrong ack is corrected by
   appending, not rewriting; M0 has no correction writer, so an existing ack or request is
   never replaced. Plan and status files may be replaced by their own author only (plan
   revision must increase).
7. **`last-commit`.** Its first word must be a SHA or `none`; trailing text is accepted with a
   warning.
8. **Overlap.** `files/area` items are compared as comma-separated paths (parenthetical notes
   dropped, case-insensitive, directory prefixes overlap). There is no syntax for "resolved by
   the coordinator", so an overlap of active subtasks is a warning, never an error.
9. **Revision lower than seen.** Detected against the highest revision stored by `index`;
   `inspect` reports it only when an index exists, opened read-only.
10. **Recording the state root.** `init` writes `<state-root>/config.json`. A non-default state
    root must be passed with `--state-root`; project profiles that remember it are later work.
11. **Status timestamps.** Date-only `updated` values are accepted with a warning and are never
    evaluated for staleness; writers require `YYYY-MM-DD HH:MM`.

Managed workers (T12):

12. **Prompt delivery.** Codex reads the prompt from stdin (`-`), which `codex exec --help`
    documents; "stdin closed" therefore means stdin is the prompt file (EOF at its end) or
    `NUL`, never interactive. Copilot documents no stdin/file prompt for `-p`, so the prompt is
    an argv element; it is safe because npm shims are run as `node.exe <script>`. A prompt that
    would have to pass through a non-npm `.cmd` is refused (`invalid_arguments`), as is a
    command line over 32,000 characters.
13. **Ref checks in a shared repository.** All worktrees share refs and other agents commit
    concurrently, so only the worktree's own HEAD, branch and branch tip, and a new ref pointing
    at the worktree's pre-run HEAD, count as the worker's Git operation
    (`git_policy_violation`, a class added to the list in the plan). Other ref changes and a
    changed primary-checkout HEAD are recorded in `result.json` as unattributed observations.
    A push is only visible locally through such ref changes.
14. **Byte-for-byte review check.** Compared: `git status --porcelain=v1 -z
    --untracked-files=all`, a SHA-256 of every listed path, HEAD and branch. Ignored files
    (caches, build output) are excluded; files outside the worktree are not seen.
15. **Implement branch.** Implement mode requires the protocol's branch name
    `task/<task-id>/<subtask-id>` (besides refusing `main`, `master`, `--live-branch` values,
    the primary checkout and a dirty tree). There is no project profile yet, so extra live
    branches are passed with `--live-branch`.
16. **Commits.** The orchestrator commits with the repository's configured identity, runs
    hooks (never `--no-verify`), and verifies the new commit's parent; a hook rejection is
    `commit_failed` (a class added to the list in the plan). Changes of a failed implement run
    are left uncommitted in the worktree for inspection, so the next run is refused until
    someone cleans it.
17. **Status states.** Success writes `review` in both modes (implement: committed, awaiting
    review; review: review written). Failures write `blocked`. `tests` says the orchestrator
    ran none and points at the worker's report; `last-commit` is the orchestrator's commit or
    `none`. A managed worker's status body starts with `Managed worker: provider=<p>`; that line
    is how a re-run of the same worker is told apart from an interactive agent with the same id
    (`render_status` writes no extra header keys, so a body marker was used).
18. **Review file.** formats.md has no grammar for review files; the runner writes a header
    block (keys listed under "Managed workers"), one blank line, and the final message
    byte-for-byte (no newline normalisation, no added final newline). It is written only when
    the run succeeded; it is replaced only if its `agent` header is the same worker.
19. **Plan errors.** A plan whose header parses and whose requested row is valid is usable;
    errors in other rows (the live plan has state cells such as `merged (1f8ecbb)`) are
    recorded as findings, not fatal.
20. **Verification command.** Taken as the last inline-code span of the subtask's
    verification text (for "`a` then `b` then `c`", `c`); `--verify-command` overrides. For
    Copilot it becomes exactly `shell(<command>)` (narrowest form); a command containing `(`,
    `)`, `,` or a newline cannot be a pattern, and the profile is then unconfigured.
21. **Attempt storage.** The existing `Attempt` record is stored with `task_id` = subtask id;
    task, mode, profile, usage, duration and run directory go to a new `attempt_details`
    table in the same derived index (the `Attempt` dataclass was left unchanged).
22. **Process tree.** A Job Object with kill-on-close: descendants still running when the
    worker exits normally are killed too.
23. **Environment.** Proxy variables are passed (a proxy URL can embed credentials); API keys
    and tokens (`OPENAI_API_KEY`, `GH_TOKEN`, ...) only with `--pass-env`, which cannot pass
    `COPILOT_ALLOW_ALL`. Codex and Copilot normally authenticate from their own stored login.
