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
- Notification transport (`notify.py` and `notify`): sends mailbox pointers through configured
  Codex/Copilot contacts and reports Claude/manual relay targets; transport is never an ack.
- Tests in `tests/` cover every valid and invalid example in formats.md, plus the D8 rule
  that exit codes and notification transport never create an ack or a completion.

## Not implemented

Registry (`registry/`, O2) and reservation checks; review-file parsing; message cleanup and
bounded watches (formats.md section 7); provider process adapters, process launching; Git/worktree
management; verification and routing; VS Code integration.

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
