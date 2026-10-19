# Decisions

This file records what has been **decided**, what is **proposed**, and what is still **open**.
It complements `AGENTS.md` (architecture handoff) and does not repeat it. Nothing listed here is
implemented unless a section says so explicitly.

Status values:

- **Decided (user)**: confirmed by the project owner.
- **Decided (architecture)**: already fixed in `AGENTS.md`; listed only where a later
  decision depends on it.
- **Accepted (coordinator)**: decided by the coordinator of joint task `m0-foundation` for
  the shared file interface, as the protocol allows; reviewed by codex; the user may overturn
  it.
- **Proposed**: recommended, awaiting the user.
- **Open**: no recommendation yet, or blocked.

Sources: `AGENTS.md`, `README.md`, `docs/protocol/`, joint task `m0-foundation` (T3 conflict
review by codex, T4 file-format spec by claude-50, reviews by claude-a7, codex and copilot).

## D1. Python 3.11 minimum: Decided (user), 2026-10-19

`requires-python = ">=3.11"`. Development uses Python 3.12 (`py -3.12`) in a project-local
`.venv`; bare `python` on a developer machine may be an older version and must not be relied
on. Windows-native; no WSL/Bash dependency (architecture).

## D2. Authoritative protocol documents: Accepted (coordinator), generalised per user

`docs/protocol/agent-messaging.md` (protocol v1) and `docs/protocol/formats.md` (format 1)
are authoritative. They generalise the protocol first used in EpesiCRM, which is kept
verbatim in `docs/protocol/upstream/` for provenance only and is never edited. Protocol
changes land in the two authoritative files and are recorded here in the same change. The
earlier integration summary `docs/agent-messaging.md` was removed to avoid drift.
(T3 items 1 and 12.)

## D3. Coordination files are the source of truth; SQLite is derived: Accepted (coordinator)

Request, ack, plan, status and review files are the authoritative, interoperable record.
SQLite indexes them and stores operational data the protocol does not define (attempts,
usage, failures, verification, escalation). SQLite never creates, replaces or infers an
acknowledgment and never rewrites protocol files. Reconciliation detects changed or
malformed files. (T3 items 4 and 11.)

## D4. File grammar: Accepted (coordinator)

`docs/protocol/formats.md`, format 1: `key: value` header block, blank line, Markdown body;
lenient reads (BOM, CRLF), strict writes (UTF-8 without BOM, LF, atomic temp-file rename);
single writer per file. Request and ack fields come from the original protocol; plan,
status and registry field names are this project's definitions. (T3 items 2 and 8.)

## D5. Ack `plan-revision`: Accepted (coordinator)

Required when the request concerns a plan revision; such an ack without it is invalid.
A request concerns a plan revision only if it carries a `plan-revision:` header; the body is
never parsed for it (decided in plan revision 11, after the inferred reading rejected three
valid kickoff acks). "All agreed" counts only acks quoting the same revision. (T3 item 3.)

## D6. Agent id grammar: Accepted (coordinator)

`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, no `..`; recommended `<product>-<suffix>`. A 4-hex
suffix is allowed but not required. Until the registry exists, the coordinator or the user
assigns ids. (T3 item 5.)

## D7. Timestamps in local time: Decided (user), 2026-10-19

Writers use `YYYY-MM-DD HH:MM` local machine time from the system clock; readers also accept
date-only and ISO 8601. Staleness (about 3 hours) uses minute-precision values only and is
valid within one machine. UTC or offset-qualified times would be a format-2 change.
(T3 item 6.)

## D8. Separate delivery states: Decided (architecture), modelling Accepted (coordinator)

Written, notification submitted, received, agreed, work completed, reviewed and integrated
are distinct states/events. Neither a process exit code nor notification transport can
create an ack or a completion transition; tests must prove this. Reachability mode, transport
result and manual-relay text are reserved provider-neutral fields; provider adapters come in
the Windows-runtime milestone. (T3 items 7 and 10.)

## D9. State root is project configuration, default `.multi-agent-ai/`: Accepted (coordinator), generalised per user

The state root holds `messages/`, `tasks/`, the planned `registry/` and the SQLite database.
Default `.multi-agent-ai/` in the primary checkout, excluded from Git. A project profile may
set another path (for example `storage/framework/agents/` for a Laravel project). `init`
records it; workers read it and never infer it from their worktree. No project's paths are
hard-coded in the engine. (T3 item 9.)

## D10. Dependency policy: Decided (user), 2026-10-19

Runtime: standard library only for M0/M1 (dataclasses, `sqlite3`); revisit when the
service gains an API. Development: `pytest` allowed as a dev-only dependency; tests must run
on Windows paths.

## D11. This repository coordinates through its own protocol: Accepted (coordinator)

Agents developing Multi-Agent AI use the protocol with state root `.multi-agent-ai/` in this
checkout and the `agent-mailbox` skill (`.agents/skills/`, Claude pointer in
`.claude/skills/`).

## D12. Project-neutral documentation: Decided (user), 2026-10-19

Core documents (`AGENTS.md`, `README.md`, `docs/protocol/`, this file, the skill) are
project-neutral. Project-specific knowledge lives in project profiles and in
`docs/case-studies/`. EpesiCRM is the first planned trial, not a special case of the design.

## D13. The uncommitted bootstrap skeleton is the T6 starting point: Decided (user), 2026-10-19

A package skeleton found uncommitted in the checkout on 2026-10-19 (`pyproject.toml`,
`src/multi_agent_ai/`, `tests/test_bootstrap.py`, `docs/implementation-status.md`, and a
README section) is the starting point for T6. claude-50 wrote `pyproject.toml`,
`__init__.py` and `__main__.py`; the author of the rest is unknown (no participating agent
claims it). T6 brings it in line with D1-D10 on its own task branch; it is not on `main`.

## D14. Codex chats are notified with `codex queue`: Accepted (coordinator), verified with the user, 2026-10-19

Discovery: `thread/list` (by exact `cwd`) on a private `codex app-server --listen stdio://`.
Delivery: `codex queue --thread <id> --message <one line>`. Verified: a queued message started
a turn in an idle VS Code Codex chat with no user input (user confirmed), and Codex wrote its
ack within about 10 seconds. The recipient sees the queued text as if the user typed it, so
the text only points at the mailbox and names the sender; it never carries instructions or
approvals. Methods that start, steer or inject turns are not used. The manual user-prompt
fallback remains for when discovery or queueing fails. Details: docs/protocol/agent-messaging.md.

## Open

- **O2. Registry.** Format specified (`formats.md` section 6); implementation, reservation
  checks and its CLI are later work.
