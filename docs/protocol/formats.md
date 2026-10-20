# Coordination file formats (format 1)

Exact grammar for the files described in [agent-messaging.md](agent-messaging.md): requests,
acknowledgments, plans, worker status and (planned) registry entries. Parsers and writers in
Multi-Agent AI implement this document. Drafted by claude-50 (joint task m0-foundation, T4),
reviewed by claude-a7 and codex; decisions in [../decisions.md](../decisions.md) (D4-D7).

Request and ack fields come from the original protocol; plan, status and registry field
names, `plan-revision` on acks, the id grammar and timestamp rules are this project's
definitions.

## 1. Common grammar

```text
file      = [BOM] header-block [blank-line body]
header    = key ":" *SP value EOL
key       = ALPHA *(ALPHA / DIGIT / "-" / "_")      ; compared case-insensitively
value     = rest of line, trailing whitespace trimmed; single line only
body      = free-form Markdown, no structure imposed by the parser
```

Reading rules (lenient, because many editors and agents write these files):

1. Decode UTF-8 and strip one leading BOM (U+FEFF). Invalid UTF-8 is an error, never guessed.
2. Normalise CRLF and lone CR to LF.
3. The header block ends at the **first blank line**. Every non-blank line before it must
   match `key: value`; a non-matching line ends the header there with a warning (the rest is
   body). A file that is only a header block needs no blank line and gets no warning (the body
   is optional). Example (no warning): `id: a` / `from: b` / `to: c` / `Note: please ack`
   without a blank line parses `note` as a header. Example (warning): `id: a` / `from: b` /
   `please acknowledge this` ends the header at line 3 and treats it as body.
4. Spaces after the colon are skipped, so column-aligned values (`agent:    codex`) parse.
5. Keys are case-insensitive; the canonical written form is lowercase.
6. A repeated key: the first wins and the parser records a warning.
7. Unknown keys are preserved and ignored (forward compatibility).
8. A value never contains a newline; multi-line content goes in the body.
9. `to:` is a comma-separated list; items are trimmed; empty items are invalid.

Writing rules: UTF-8 **without** BOM, LF newlines, header block, one blank line, body, final
newline. Write to a temporary file in the same directory, then rename, so readers never see a
partial file. Never overwrite a file written by another author.

**Ids.** Message, task, subtask and agent ids are path components and must match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and not contain `..`.

**Timestamps.** Writers use `YYYY-MM-DD HH:MM` in local machine time, taken from the system
clock. Readers also accept `YYYY-MM-DD` and ISO 8601 `YYYY-MM-DDTHH:MM[:SS]`. Staleness is
evaluated only on minute-precision values and is valid only for participants on one machine;
offset-qualified or UTC times are a future format-2 change.

## 2. request.md

Path: `messages/<id>/request.md`. Single writer: the sender.

| key | required | rule |
| --- | --- | --- |
| `id` | yes | equals the directory name |
| `from` | yes | one agent id |
| `to` | yes | comma list of agent ids, or exactly `broadcast` |
| `plan-revision` | no | the plan revision the request concerns, if any |
| body | yes (non-empty) | the message; asks for ack/review if needed |

A `to: broadcast` request cannot reach "all agreed" unless its body lists the expected
participants; a parser warns when a broadcast asks for agreement without such a list.

Valid:

```text
id: 20261019-m0-kickoff
from: claude-a7
to: claude-50, codex, copilot

Kickoff text.
```

```text
id: fix-1
from: codex-a91e
to: broadcast

FYI, test lock released.
```

```text
<BOM>id: q7
from: copilot-e19f
to: claude-5e90
plan-revision: 3

Question about plan revision 3.
```

(The third starts with a UTF-8 BOM and may use CRLF; both are accepted on read.)

Invalid:

1. `id: x` / `from: a` with no `to:` line: missing required key.
2. `to: a,, b`: empty recipient item.
3. Directory `messages/m1/` but `id: m2`: id mismatch. `id: ../x` is rejected as unsafe.

## 3. ack-&lt;agent-id&gt;.md

Path: `messages/<id>/ack-<agent-id>.md`. Single writer: that recipient. The file name's id
must equal the id in the `agent:` line.

| key | required | rule |
| --- | --- | --- |
| `id` | yes | equals the message id (directory) |
| `agent` | yes | `<agent-id> (<actual product>)`; the product part is recommended |
| `status` | yes | `received`, `agreed` or `changes requested` (case-insensitive on read, lowercase on write) |
| `plan-revision` | when the request concerns a plan revision | positive integer; written after `status` |
| body | expected for `changes requested` | response and outstanding issues |

A request concerns a plan revision only when it carries a `plan-revision` header; the body is
never parsed for it. An ack to such a request without `plan-revision` is **invalid** and does
not count. A request without the header concerns no revision, whatever its prose says. An ack
quoting an older revision is recorded as stale and does not count.

Semantics a parser keeps separate: `received` ≠ `agreed`; an ack may reject; "all agreed"
requires every named recipient to have written `agreed` for the same revision. Only an ack
file written by the recipient confirms a request. An ack from an agent not named in `to` is
recorded but does not count toward completion.

Valid:

```text
id: 20261019-m0-kickoff
agent: claude-50 (Claude Code, Sonnet 5.5)
status: agreed

Taking T4.
```

```text
id: 20261019-m0-rev2
agent: codex (OpenAI Codex)
status: changes requested
plan-revision: 2

Revision 2 conflicts with my owned files.
```

```text
id: m9
agent: copilot-e19f
status: RECEIVED
```

Invalid:

1. `status: ok`: not one of the three states.
2. `agent: claude-50` in file `ack-codex.md`: file/identity mismatch (possible forged ack).
3. `id: m1` in `messages/m2/`; or an ack to a revision-related request without
   `plan-revision`.

## 4. plan.md

Path: `tasks/<task-id>/plan.md`. Single writer: the coordinator.

| key | required | rule |
| --- | --- | --- |
| `task` | yes | equals the directory name |
| `coordinator` | yes | exactly one agent id |
| `revision` | yes | positive integer, strictly increasing on every change |
| `updated` | yes | timestamp (minute precision) |
| `base-commit` | before work starts | Git SHA |
| `integration-branch` | before work starts | local `task/<task-id>/integration`, never pushed |

Body: free-form sections (`## Goal`, `## Acceptance criteria`, ...) plus one **subtask
table** (GitHub-style pipes) that the parser reads:

```text
| id | what | files/area | depends on | worktree | owner | reviewer | state |
```

Cell rules: `worktree` in {`yes`, `no`}; `reviewer` an agent id or `waived`; `depends on` a
comma list of subtask ids or `-`; `state` in {`open`, `assigned`, `in progress`, `blocked`,
`review`, `done`, `merged`}; `owner` may be `TBD` or prefixed `proposed:`, which is not an
assignment. Unknown columns are ignored; a missing required column is an error.

Semantic checks: active subtasks do not overlap in `files/area` unless the coordinator marks
it resolved; `depends on` forms a DAG over known ids; `reviewer` ≠ `owner` unless `waived`; a
`proposed:` owner never starts work.

Valid:

```text
task: m0-foundation
coordinator: claude-a7
revision: 2
updated: 2026-10-19 16:44
base-commit: d69eae6

## Goal
...

| id | what | files/area | depends on | worktree | owner | reviewer | state |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T4 | formats | notes/x.md | - | no | claude-50 | claude-a7 | review |
```

Also valid: a plan with `integration-branch: task/demo/integration` and a `waived`
reviewer; a plan at `revision: 7` whose row has `depends on` = `T1, T2`.

Invalid:

1. `coordinator: a, b`, or two `coordinator` lines with different values.
2. `revision` lower than one already seen, or `v2`, `0`, `2.5`.
3. A row with `state: finished` or `worktree: maybe`, an unknown dependency id, or a cycle.

## 5. status-&lt;agent-id&gt;.md

Path: `tasks/<task-id>/status-<agent-id>.md`. Single writer: that agent; the file name's id
must equal `agent`.

| key | required | rule |
| --- | --- | --- |
| `plan-revision` | yes | the plan revision the agent last read |
| `agent` | yes | agent id |
| `subtask` | yes | subtask id, or `-` |
| `state` | yes | a plan state, `waiting for user`, or `running tests: <scope>` |
| `branch` | if working in a worktree | branch and absolute worktree path |
| `last-commit` | yes | SHA or `none` |
| `tests` | yes | scoped tests and results, or `none run` |
| `blockers` | yes | text or `none` |
| `contact` | no | how other agents can notify this agent: `codex queue --thread <uuid-or-name>`, `code chat`, `claude SendMessage <session-name>`, or `user prompt`. The value must not start with `-`. Until the registry exists this is where `multi-agent-ai notify` reads it; a registry `contact:` takes precedence later. |
| `safe-to-clear` | no | `yes` or `no`. `yes` means the agent has no uncommitted or unsaved work, its state and next steps are recorded in files (this status file, its notes, its branch), and a fresh session could resume from the resume brief and this file. The user can then clear the agent's context window without loss. Absent means `no`. |
| `updated` | yes | timestamp (minute precision) |

`safe-to-clear` is the agent's own claim, not proof, and only counts while the status is current:
set it back to `no` as soon as work starts again, and ignore it when `updated` is older than the
file's own staleness limit. The coordinator tells the user which agents are safe to clear; nothing
clears a session automatically.

Staleness (two independent checks):

- **Stale revision**: `plan-revision` < the plan's `revision`; the coordinator does not treat
  the state as current and messages the agent. A `plan-revision` above the plan's revision is
  invalid; trust neither file until reconciled.
- **Stale worker**: `updated` older than about three hours; the worker is unavailable. A
  stale `waiting for user` status means "reservation released", not "failed".
- **Quota block**: a status with `state: blocked` whose `blockers` value starts with
  `quota:` records a provider usage limit, for example
  `quota: claude (claude-opus-5-5), resets 2026-10-19 21:10 Europe/Budapest (reported)` or
  `quota: codex (gpt-5), reset unknown`. While the reported reset time has not passed, or for
  24 hours after `updated` when the reset is unknown, the worker is waiting, not stale. A
  locally configured cap (for example Copilot `--max-ai-credits`) is not provider quota and
  uses a different blocker prefix, `cap:`.
- A status `state` that disagrees with the plan row is a conflict to report, not to
  auto-resolve: the plan is authoritative for assignment, the status for the worker's
  progress.

Valid:

```text
plan-revision: 3
agent: codex
subtask: T3
state: running tests: tests/test_mailbox.py
branch: task/m0-foundation/T3 (C:\work\wt\T3)
last-commit: 3fa91c2
tests: tests/test_models.py passed (12)
blockers: none
updated: 2026-10-19 18:02
```

Invalid:

1. `plan-revision` missing, or above the plan's revision.
2. `agent: codex` in `status-copilot.md`.
3. `state: finished`.

Validation warning (raised by verification, not the parser): `state: done` with
`tests: none run` for a subtask whose acceptance criteria require tests.

## 6. registry/&lt;agent-id&gt;.md

Headers `agent`, `started`, `updated`, `task`, `files` (comma/semicolon list of globs; the
reservation), `status`, optional `contact`; values may be column-aligned; no body. Stale after
about three hours without `updated`; a stale or inactive entry releases its reservation. The
registry implementation must refuse an assignment whose files/area overlaps an active
reservation unless the coordinator revises the plan. The registry filename id must match the
`agent` header. The inactive status set is `idle`, `done`, `finished`, `failed`, `stopped`, or
`offline`; unknown statuses, including `blocked`, retain their reservation. An `updated` value
without a time is visible with a warning but releases its reservation because staleness cannot be
evaluated safely.

## 7. Behaviour to test with the parser

Cleanup ownership (the sender deletes its message directory after all acks and resolution,
or cancels after about three hours of inactivity) and bounded watches are behaviour, not
format; they need their own tests.

## 8. Per-subtask verification entries

Plans may declare a verification command for a specific subtask with one entry per subtask:

```text
## Verification
verify: T1: `python -m pytest tests/test_add.py -q`
verify: T2: `python -m pytest tests/test_divide.py -q`
```

An entry may optionally be a Markdown list item (`- verify: ...`, `* verify: ...`, or
`1. verify: ...`).

The command is the inline-code span on the matching `verify: <subtask-id>:` line. The
orchestrator passes the command to shell-free verification as an argv declaration; it must not
interpret the line as a shell script. For backwards compatibility, a plan with one subtask and
no matching `verify:` entry uses the last inline-code span in that subtask's verification text.
When multiple subtasks share a verification section, each must use its own `verify:` entry;
the legacy fallback is not used to infer commands for those subtasks.
