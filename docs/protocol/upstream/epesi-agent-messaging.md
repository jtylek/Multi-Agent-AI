# Multi AI agents communication protocol

The shared file mailbox works for agents with filesystem access to the same checkout.
Native messaging adds notification where the sender can reach the recipient. The agent
registry advertises presence and file reservations; a message in a registry entry does
not notify anyone. Follow [AGENTS.md](../AGENTS.md#coordinating-with-other-agents) for
registration, reservations, and the short protocol rules. [Joint tasks](#joint-tasks)
describes how several agents divide one problem among themselves.

The `agent-mailbox` skill automates checking, acknowledging and broadcasting:
[.agents/skills/agent-mailbox/SKILL.md](../.agents/skills/agent-mailbox/SKILL.md) holds its
steps, and `.claude/skills/agent-mailbox/SKILL.md` only points Claude Code to it. Keep the
two description lines identical, and change the steps only in the `.agents` copy. This note
and AGENTS.md take precedence over the skill.

## Mailbox and acknowledgments

Choose a unique message id and use this layout under `storage/framework/agents/`:

```text
messages/<id>/request.md
messages/<id>/ack-<recipient-agent-id>.md
```

The sender writes `request.md`, with explicit registry ids in `to`:

```text
id: <unique-message-id>
from: <sender-agent-id>
to: <recipient-agent-id>, <another-recipient-agent-id>

<Message or proposed decision. Ask for an acknowledgment and, if needed, review.>
```

Each recipient writes its own `ack-<recipient-agent-id>.md`:

```text
id: <same-message-id>
agent: <recipient-agent-id> (<actual agent product>)
status: received | agreed | changes requested

<Response, including any outstanding issues.>
```

Receipt and agreement differ: an acknowledgment may reject a proposal. Do not report
agreement until all participants explicitly agree to the same revision. Follow-up files
such as `review-<agent-id>.md` belong to their author; name the exact files or revision
reviewed. Do not overwrite another participant's response. Use `to: broadcast` only
when the request is intended for every agent. For a broadcast requiring agreement,
list the expected participants explicitly so completion has a defined meaning.

Check requests at start/resume and before edits or tests. Do not poll the mailbox on a
timer: it is a durable record, not a timely notification channel, and reading these rules
installs no background watcher. Use native push to notify a reachable session of its
request. A pending request stays unconfirmed until its recipient writes an acknowledgment.
A status of `received` does not reserve files or authorize overlapping edits; registry
reservations still apply. Do not store secrets.

The sender owns cleanup of its message directory, including acknowledgment files:
remove it after all named recipients acknowledge and any requested work/review is resolved.
After roughly three hours without activity, the sender may cancel an abandoned request
and remove its directory. Use activity in the whole exchange, not just the request's
creation time. Never expire an ongoing review just because the original request is old,
or infer receipt/agreement from cancellation. Preserve unresolved work in a handoff note
when necessary. Other agents do not clean up someone else's message directory. Removing
a registry entry never removes its message directory.

While waiting for named recipients, a sender may run a bounded watch of its own active
message directory and stop it when every expected acknowledgment arrives or the time limit
expires. This is response tracking for one exchange, not periodic polling of incoming mail.

## Joint tasks

A joint task is one problem that several agents (any mix of Claude, Codex, Copilot and
developers) solve concurrently. It builds on the registry, the mailbox and the git rules in
AGENTS.md; nothing here relaxes them beyond the task-branch test exception AGENTS.md states.

### Roles

- **Coordinator**: the agent whose user started the problem. It owns the plan, assigns work,
  integrates, runs the full suite and reports to the user. There is exactly one per task; the
  user may hand the role to another agent, and the plan records the change.
- **Workers**: every other participant. Each owns its subtasks, its status file and its branch.

### Task board

Every file has a single writer, so no two agents ever edit the same file:

```text
storage/framework/agents/tasks/<task-id>/
  plan.md                          coordinator only
  status-<agent-id>.md             each participant, its own only
  review-<subtask>-<agent-id>.md   the reviewer only
  notes/<agent-id>-<topic>.md      optional findings, author only
```

The board, like the registry and the mailbox, lives only in the primary checkout:
`storage/framework/agents/` is gitignored, so a worktree has an empty copy of its own.
Workers in a worktree read and write the primary checkout's board by absolute path.

`plan.md` holds the task id, coordinator, `revision`, goal, acceptance criteria (including
the test commands that prove them), base commit, integration branch, and a subtask table.
The integration branch (`task/<task-id>/integration`) is created by the coordinator for this
task, stays local and is never pushed, so it is not a shared branch.

```text
| id | what | files/area | depends on | worktree | owner | reviewer | state |
```

`worktree` says whether the subtask needs its own worktree (`yes`) or works in the primary
checkout with reserved files (`no`). A worktree needs its own `composer install`,
`npm install` and `.env`, so use one only where isolation pays for that. Anything touching
`modules/Premium/` (absent from worktrees) is always `no`. `reviewer` may be `waived` for a
trivial subtask (documentation, translation keys, a one-line fix).

`state` is one of `open`, `assigned`, `in progress`, `blocked`, `review`, `done`, `merged`.
The coordinator increments `revision` on every change; agents quote the revision they act on,
so a stale reading is visible.

`status-<agent-id>.md` holds the plan revision seen, the subtask, its state, the branch and
last commit, the scoped tests run and their results, blockers or questions, and `updated:`.
Update it on every state change, including `running tests: <scope>` while tests run (the
primary checkout's test lock is shared by everyone working there). It replaces neither the
registry entry nor its file reservations.

### Lifecycle

1. **Kickoff.** The coordinator writes `plan.md`, then a mailbox request with explicit `to:`
   participants pointing at it, and notifies them natively where possible (see below);
   otherwise it asks the user to prompt each chat.
2. **Join.** Each participant acknowledges with `agreed` or `changes requested`, the subtasks
   it wants, and what it can do (product, tools, whether it can use a worktree). The
   coordinator assigns owners and reviewers in a new plan revision. Workers never assign
   themselves by editing `plan.md`.
3. **Work.** One owner per subtask, with disjoint files. A `worktree: yes` subtask works on
   branch `task/<task-id>/<subtask-id>` from the base commit; a `no` subtask reserves its
   exact files in the registry. Run scoped tests only, and commit only your own work.
   Commits on your own local, unpushed `task/<task-id>/*` branch need only scoped tests, an
   exception the user approved and AGENTS.md states: the coordinator's integration run is
   the gate, so nothing reaches another branch without a full run.
4. **Questions and interface changes.** Send a mailbox request to the affected owners and the
   coordinator. The coordinator decides any change to a shared interface and publishes it as
   a new plan revision; owners adapt to that revision, not to side agreements.
5. **Review.** Each finished subtask gets a reviewer other than its owner, preferably a
   different product, unless the plan marks the review `waived`. The reviewer names the exact
   commit it reviewed. `changes requested` sends the subtask back to `in progress`.
6. **Integration.** The coordinator merges reviewed branches into the local integration
   branch in dependency order (allowed without asking, since the user started the task),
   resolves conflicts with the owners, and runs the full suite once, announcing it as
   [concurrent-session-tests](concurrent-session-tests.md) describes. Then it reports to the
   user. Merging the integration branch into the user's working branch or `main`, and any
   push, needs the user's go-ahead.
7. **Close.** The coordinator moves durable knowledge into the owning AI-shared note,
   removes the task directory and its own mailbox requests, and asks the senders of any other
   resolved requests to clean up theirs. Workers remove their merged worktrees and branches
   and their registry entries.

### Failures and disagreement

- Do not poll on a timer. Read the board at start/resume, before edits or tests, and when
  notified. The coordinator may run a bounded watch of its own task directory while waiting.
- A worker whose status file is about three hours stale is unavailable. The coordinator may
  reassign its subtask in a new revision, but never takes over its uncommitted changes or
  worktree.
- `changes requested` in an acknowledgment or a review blocks that item. The coordinator
  decides or escalates to the user; nobody silently overrides another participant.
- If a worker's user or permission settings deny an action, the denial goes back to the user.
  The coordinator never reassigns the work to another agent to get around it.
- Work that outlives the sessions gets a handoff note, `AI-shared/wip/<task>.md`, written by
  the coordinator.

Native notification reaches only some products (below), so keep the round trips few: give
each agent 2-4 subtasks that can finish without waiting on others, and put the interfaces
between subtasks into the first plan revision.

## Discovery and contact addresses

Read the registry, but remember a live session may not have registered. Add an optional
`contact:` line to your own entry, describing the runtime and reachable address. A Claude
session name or Codex agent id only works for senders with access to that messaging runtime.
Otherwise state that the mailbox needs a user prompt to wake this chat. Match native
discovery's working directory to the checkout; do not guess from similar session names.

There is no universal wake-up channel across products. If the recipient cannot be reached
with a supported native mechanism, ask the user to prompt that existing chat:
`Check the agent mailbox: read storage/framework/agents/messages/<id>/request.md.`
Creating a helper or opening a new chat does not prove receipt by an existing session.
Do not resume, fork, interrupt, or modify private session data to force delivery.

## Claude Code and a CLI relay

Verified: native `ListAgents` / `SendMessage`, including a Claude CLI relay from another
agent product. This requires an installed, authenticated Claude build exposing these tools.

From the repository root, run `claude agents --json`; select existing sessions whose
`cwd` matches this checkout. Its `name` is the address for `SendMessage`. `ListAgents`
alone may omit the working directory, so use the CLI discovery to verify the target.
Write the mailbox request first. Within Claude Code, use `ListAgents`, then `SendMessage`
to point the recipient at it. Other products can invoke a short-lived relay:

```powershell
$relayPrompt = @'
Act as a read-only messaging relay. Use ListAgents, then SendMessage.
Send only to these verified session names: <names from discovery>.
Tell each recipient: Check the agent mailbox; read
storage/framework/agents/messages/<id>/request.md and write your own
ack-<your-registry-agent-id>.md in that directory. Follow AGENTS.md.
Report send results accurately. Do not edit files, run commands or tests, commit,
resume, fork, or interrupt other sessions. Do not wait for a reply to this relay.
'@
claude -p $relayPrompt --tools ListAgents SendMessage ToolSearch Read --allowedTools ListAgents SendMessage ToolSearch Read --output-format json
```

Replace placeholders before running. `--tools` restricts tool availability;
`--allowedTools` alone only grants permission. The allowlist omits file-write and command
tools. `ToolSearch` permits loading a deferred messaging tool in builds that need it;
if discovery still exposes other tools, stop and use the mailbox/user-prompt fallback.
The recipient, not the relay, writes the acknowledgment file. It survives relay exit;
an extra `SendMessage` reply is optional. Inspect the file for the matching id and identity.
Send success proves transport acceptance only; it does not prove the recipient processed
the message. The relay cannot receive replies after exit.

## VS Code Copilot

Verified: `code chat` submission followed by a recipient-written mailbox acknowledgment.
Check `code chat --help` for the installed CLI. From the repository root:

```powershell
code chat --reuse-window --mode agent --add-file <absolute-request-path> 'Read the attached inter-agent request and write your own ack-<your-agent-id>.md alongside request.md. Follow AGENTS.md. Do not edit application files, run tests or commit.'
```

This uses the last active window and can reach an existing active Copilot session, but it
does not select a particular conversation or guarantee which product handles the prompt.
Verify the checkout and the responding agent's actual identity in its acknowledgment. A zero
exit code confirms submission only. To reach a particular existing chat when no session
messenger is available, use the user-prompt fallback instead.

## Codex-managed agents

If exposed in the current runtime, `send_input` messages reachable agents by their actual
runtime ids. In particular, it reaches only agents spawned in that same Codex-managed
agent hierarchy; it does **not** make an existing top-level Codex chat a native address for
Claude, Copilot, or another independent Codex session. Point a reachable sub-agent at the
request and ask for its own mailbox acknowledgment.

Availability does not establish access to unrelated Codex windows, Claude, or Copilot. When
the intended Codex chat's registry contact says it needs a prompt (or it has no reachable
runtime id), write the addressed mailbox request and ask the user to send that chat:

```text
Check the agent mailbox: read storage/framework/agents/messages/<id>/request.md.
```

Do not spawn a new helper as a substitute for delivery to a specified existing agent.
