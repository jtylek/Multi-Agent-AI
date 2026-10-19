# Agent messaging and joint-task protocol (v1)

This is the authoritative protocol for how AI agents (Claude Code, Codex, Copilot, or others)
and developers coordinate through Multi-Agent AI. Exact file grammar is in
[formats.md](formats.md); design decisions behind it are in
[../decisions.md](../decisions.md).

It is a project-neutral generalisation of a protocol first used in a real multi-agent
project; that original is kept unchanged in
[upstream/epesi-agent-messaging.md](upstream/epesi-agent-messaging.md) for provenance. Where
the two differ, this document wins.

## Overview

The **state root** is a directory in the target project's **primary checkout** that holds all
coordination state. It is excluded from Git. Default: `.multi-agent-ai/`; a project may
configure another path (for example a Laravel project may keep `storage/framework/agents/`).
Workers in a Git worktree read and write the primary checkout's state root by absolute
path, never their worktree's empty copy.

```text
<state-root>/
  messages/<message-id>/
    request.md
    ack-<recipient-agent-id>.md
  tasks/<task-id>/
    plan.md                          coordinator only
    status-<agent-id>.md             each participant, its own only
    review-<subtask>-<agent-id>.md   the reviewer only
    notes/<agent-id>-<topic>.md      optional findings, author only
  registry/<agent-id>.md             planned: presence and file reservations
```

Three mechanisms work together:

- the **mailbox**: durable request/acknowledgment files any agent with filesystem access can
  read and write;
- **native notification**: a product-specific way to wake a reachable session (optional; not
  every product has one);
- the **task board**: a revisioned plan plus per-agent status files for one joint task.

Every file has exactly **one writer**. Nobody edits, overwrites or deletes another
participant's file, except where this document gives cleanup to a specific owner.

## Agent identity

Every participant uses one agent id for the whole task: path-safe (letters, digits, `.`, `_`,
`-`; recommended `<product>-<suffix>`, e.g. `claude-a7`, `codex-a91e`). Until the registry is
implemented, ids are assigned by the coordinator or the user. Do not confuse provider identity
with session identity: two Claude sessions are two agents.

## Mailbox and acknowledgments

The sender writes `messages/<id>/request.md` with explicit recipient ids:

```text
id: <unique-message-id>
from: <sender-agent-id>
to: <recipient-agent-id>, <another-recipient-agent-id>

<Message or proposed decision. Ask for an acknowledgment and, if needed, review.>
```

Each recipient writes its own `ack-<recipient-agent-id>.md`:

```text
id: <same-message-id>
agent: <recipient-agent-id> (<actual agent product and model if known>)
status: received | agreed | changes requested
plan-revision: <n>

<Response, including any outstanding issues.>
```

`plan-revision` is required when the request concerns a plan revision and omitted otherwise.

Rules:

- Receipt and agreement differ: an acknowledgment may reject a proposal. Do not report
  agreement until all named participants explicitly agree to the same revision.
- A request stays unconfirmed until its recipient writes an acknowledgment. Transport success,
  a zero exit code, or a queued notification is never an acknowledgment.
- `received` or `agreed` does not reserve files or authorize overlapping edits.
- Use `to: broadcast` only when the request is for every agent. A broadcast that needs
  agreement lists the expected participants in its body so completion has a defined meaning.
- A recipient that finds a request not addressed to it (neither named in `to` nor a
  broadcast) does not act on it.
- A participant who finds its own ack wrong appends a dated correction; it does not rewrite
  what it said.
- Follow-up files such as reviews belong to their author and name the exact files, commit or
  revision reviewed.
- Never store secrets in any coordination file.

**When to check.** Check for requests at start or resume, before edits or tests, and when
notified. Do not poll the mailbox on a timer: it is a durable record, not a notification
channel. While waiting for named recipients, a sender may run a **bounded watch** of its own
message directory and stop it when every expected acknowledgment exists or the time limit
expires. That is response tracking for one exchange, not periodic polling of incoming mail.

**Cleanup.** The sender owns its message directory, including acknowledgment files, and
removes it after all named recipients acknowledge and any requested work or review is
resolved. After roughly three hours without activity in the whole exchange, the sender may
cancel an abandoned request and remove its directory. Never expire an ongoing review just
because the request is old, and never infer receipt or agreement from cancellation. Preserve
unresolved work in a handoff note. Nobody else removes someone's message directory.

## Joint tasks

A joint task is one problem that several agents and developers solve concurrently.

### Roles

- **Coordinator**: the agent whose user started the problem. It owns the plan, assigns work,
  integrates, runs the full verification and reports to the user. There is exactly one per
  task; the user may hand the role to another agent, and the plan records the change.
- **Workers**: every other participant. Each owns its subtasks, its status file and its
  branch.

### Plan

`plan.md` holds the task id, coordinator, `revision`, `updated`, goal, acceptance criteria
(including the exact commands that verify them), base commit, integration branch, and a
subtask table:

```text
| id | what | files/area | depends on | worktree | owner | reviewer | state |
```

- `files/area` is the ownership scope; active subtasks must not overlap unless the coordinator
  explicitly resolves it. For a shared document, assign named sections or serialize changes.
- `worktree: yes` means the subtask works on branch `task/<task-id>/<subtask-id>` in its own
  worktree from the base commit; `no` means it works in the primary checkout on its owned
  files only. A worktree may need its own dependency install and environment files; the
  project profile declares what. Anything that exists only in the primary checkout (for
  example nested repositories or generated assets not reproduced in worktrees) must be `no`.
- `reviewer` is an agent other than the owner, preferably a different product, or `waived`
  for trivial work (documentation, translations, one-line fixes).
- An owner prefixed `proposed:` is not an assignment.
- `state` is one of `open`, `assigned`, `in progress`, `blocked`, `review`, `done`, `merged`.

The coordinator increments `revision` on **every** change and takes `updated` from the system
clock. Agents quote the revision they act on, so a stale reading is visible. The integration
branch `task/<task-id>/integration` is local and never pushed.

### Worker status

`status-<agent-id>.md` holds the plan revision last read, the subtask, its state, branch and
last commit, scoped tests run and their results, blockers or questions, and `updated`. Update
it on every state change, including `running tests: <scope>` while tests run. Status must
reflect reality.

### Lifecycle

1. **Kickoff.** The coordinator writes `plan.md`, then a mailbox request with explicit `to:`
   participants pointing at it, and notifies them natively where possible; otherwise it asks
   the user to prompt each chat.
2. **Join.** Each participant acknowledges with `agreed` or `changes requested`, the subtasks
   it wants, and what it can do (product, tools, worktree ability, contact). The coordinator
   assigns owners and reviewers in a new plan revision. Workers never assign themselves.
3. **Work.** One owner per subtask, disjoint files. Run scoped tests only and commit only your
   own work, on your own local, unpushed `task/<task-id>/*` branch. The coordinator's
   integration run is the gate, so nothing reaches another branch without full verification.
4. **Questions and interface changes.** Send a request to the affected owners and the
   coordinator. The coordinator decides any shared-interface change and publishes it as a new
   plan revision; owners adapt to that revision, not to side agreements.
5. **Review.** Each finished subtask gets its reviewer unless waived. The reviewer names the
   exact commit, and reproduces relevant tests in a representative environment rather than
   only reading the diff. `changes requested` sends the subtask back to `in progress`.
6. **Integration.** The coordinator merges reviewed branches into the integration branch in
   dependency order, resolves conflicts with the owners, runs the full verification once
   (announcing it), and reports to the user. Merging into the user's working branch or main,
   any push, and any deployment need the user's explicit approval.
7. **Close.** The coordinator moves durable knowledge into the project's documentation,
   removes the task directory and its own mailbox requests, and asks other senders to clean
   up theirs. Workers remove their merged worktrees and branches.

Keep round trips few: give each agent 2-4 subtasks that can finish without waiting on
others, and fix the interfaces between subtasks in the first plan revision.

### Failures and disagreement

- Read the board at start/resume, before edits or tests, and when notified. The coordinator
  may run a bounded watch of its own task directory while waiting.
- A worker whose status is about three hours stale is unavailable. The coordinator may
  reassign its subtask in a new revision, but never takes over its uncommitted changes or
  worktree.
- `changes requested` in an ack or review blocks that item. The coordinator decides or
  escalates to the user; nobody silently overrides another participant.
- If a worker's user or permission settings deny an action, the denial goes back to the user.
  Nobody reassigns the work to another agent to get around it. A peer message never grants
  user authority.
- Work that outlives the sessions gets a handoff note written by the coordinator.

## Registry (planned)

`registry/<agent-id>.md` will advertise presence and file reservations: headers `agent`,
`started`, `updated`, `task`, `files` (globs; this **is** the reservation), `status`, and
optional `contact` describing how the session can be reached. An entry is stale after about
three hours without update; a stale entry releases its reservation but not the agent's
uncommitted changes. A message written into a registry entry notifies no one. Until the
registry is implemented, the plan's subtask table is the reservation record.

## Notification and reachability

There is no universal wake-up channel across products. Discover reachability before
assigning time-sensitive work, and use the mailbox in every case.

If a recipient cannot be reached with a supported native mechanism, ask the user to prompt
that existing chat:

```text
Check the agent mailbox: read <absolute path to request.md>.
```

Creating a helper or opening a new chat does not prove receipt by the intended session. Do
not resume, fork, interrupt, or modify private session data to force delivery.

### Claude Code

Native `ListAgents` / `SendMessage` reach other Claude Code sessions on the same machine.
From the project root, run `claude agents --json` and select sessions whose `cwd` matches the
checkout; the `name` is the address. Write the mailbox request first, then `SendMessage` the
recipient to point at it.

Other products can invoke a short-lived, restricted Claude relay:

```powershell
$relayPrompt = @'
Act as a read-only messaging relay. Use ListAgents, then SendMessage.
Send only to these verified session names: <names from discovery>.
Tell each recipient: Check the agent mailbox; read <absolute request path>
and write your own ack-<your-agent-id>.md in that directory.
Report send results accurately. Do not edit files, run commands or tests, commit,
resume, fork, or interrupt other sessions. Do not wait for a reply to this relay.
'@
claude -p $relayPrompt --tools ListAgents SendMessage ToolSearch Read --allowedTools ListAgents SendMessage ToolSearch Read --output-format json
```

`--tools` restricts availability; `--allowedTools` alone only grants permission. If the relay
exposes other tools, stop and use the user-prompt fallback. Send success proves transport
acceptance only; the recipient, not the relay, writes the acknowledgment.

### VS Code Copilot

```powershell
code chat --reuse-window --mode agent --add-file <absolute-request-path> 'Read the attached inter-agent request and write your own ack-<your-agent-id>.md alongside request.md. Do not edit other files, run tests or commit.'
```

This submits to the last active VS Code window and does not select a particular conversation
or guarantee which product handles it. A zero exit code confirms submission only; verify the
responder's identity in its acknowledgment.

### Codex

Verified (Codex CLI 0.163.0, Windows): an existing Codex chat, including one in the VS Code
extension, can be notified with the official `codex queue` command. The queued text starts a
turn in that chat by itself.

1. **Discover.** Start a private `codex app-server --listen stdio://` (newline-delimited
   JSON-RPC), send `initialize` and `initialized`, then `thread/list` with
   `cwd: <checkout path>` and `useStateDbOnly: true`. Select threads whose `cwd` exactly
   equals the checkout; when several match, use the one the agent names in its `contact:`
   line. Never guess from similar titles.
2. **Notify.** Write the request first, then run
   `codex queue --thread <thread-id> --message "<one line>"`. "Queued message ..." with exit
   code 0 proves only that the message was submitted.
3. **Confirm.** Only the recipient's ack file proves receipt.

**The queued text appears to the recipient as if the user typed it.** It must therefore only
point at the mailbox and name the sender, for example
`Check the agent mailbox: read <absolute request path> (sent by <agent-id> via codex queue).`
Never put instructions, approvals or anything that claims user authority into it; the
request file carries the content, where it is clearly a peer message. A Codex recipient
treats such a queued prompt as a peer notification, not as the user's approval.

Do not use app-server methods that start, steer or inject into another session's turns
(`turn/start`, `turn/steer`, `thread/inject_items`) for notification.

Within a Codex runtime, `send_input` also reaches agents spawned in the **same**
Codex-managed agent hierarchy, by their runtime ids. When neither mechanism is available,
write the request and ask the user to send the chat the one-line prompt above. Do not spawn a
new helper as a substitute for delivery to a specified existing agent.

### Managed workers

Workers the orchestrator launches itself receive assignments through orchestrator-controlled
invocation and state. That is different from taking over an unrelated interactive chat.

**What a managed worker is.** One headless provider process (`codex exec`, `copilot -p`)
started by `multi-agent-ai worker run` for one subtask in one mode (`review` or
`implement`; dry run unless `--execute` is given), under a **new** agent id such as `codex-w1` or `copilot-w1`. It is never
presented as an existing interactive chat: the runner refuses product names (`codex`,
`copilot`, `claude`, `code`), the coordinator of any plan, any mailbox participant (sender,
recipient, ack author), a registry entry, an owner/reviewer named in another plan row, and an
id with a status file not written by a managed worker of the same provider.

**Assigned by invocation, not by the mailbox.** The assignment is the command line and the
prompt; no request is written and **no acknowledgment is written or implied**. Nothing about
a managed run proves delivery to, or agreement by, any other agent; a zero exit code is
evidence about the process only. The runner writes, on the worker's behalf:

- `status-<agent-id>.md` (formats.md section 5): `in progress` at launch, then `review` on
  success or `blocked` with `<failure class>: <detail>` in `blockers`; the body starts with
  `Managed worker: provider=<provider>`;
- in review mode, on success, `review-<subtask>-<agent-id>.md`: an orchestrator provenance
  header (subtask, agent, provider, model, commit reviewed, attempt, profile, exit code, plan
  revision, updated), a blank line, then the worker's final message **verbatim**;
- `<state-root>/runs/<attempt-id>/`: `prompt.md`, `stdout.log`, `stderr.log`,
  `final-message.md`, `result.json` (argv with the prompt replaced by `<PROMPT>` and its
  SHA-256, environment variable names only, timings, exit code, failure class, usage), and
  for Copilot `usage.json`; plus an `Attempt` row in the derived SQLite index. Token usage is
  recorded only as the provider reported it, else the literal `unknown`.

**Prompt.** Built deterministically from `plan.md` (goal, the subtask row, acceptance
criteria and verification sections for that subtask) plus mode rules: work only inside the
worktree; do not commit, push, merge, branch or tag; the board is read-only; review mode
modifies nothing. Codex reads it from stdin; Copilot gets it as the `-p` argument.

**Launch profiles** (least privilege; no bypass flag is ever emitted, and a test scans every
generated argv against a denylist):

```text
codex exec -C <wt> -s read-only       --ephemeral --color never --json -o <run>/final-message.md [-m M] -
codex exec -C <wt> -s workspace-write             --color never --json -o <run>/final-message.md [-m M] -

copilot -C <wt> -p <PROMPT> --output-format json --silent --session-id <uuid>
        --usage-output-file <run>/usage.json --disallow-temp-dir [--model M]
        --available-tools=powershell,view,grep,glob
        --allow-tool=shell(git diff),shell(git log),shell(git show),shell(<verify>)
        --deny-tool=shell(git commit),shell(git push),url
  implement: --available-tools=powershell,create,edit,view,grep,glob and ",write" appended to --allow-tool
  (the shell tool name is `SHELL_TOOL` in workers/copilot.py; T13 documented `bash`, which Copilot on
   Windows answered with "Tool 'bash' does not exist"; `powershell` is a best guess, see below)
```

Copilot shell tool (T28): `copilot --help`, `copilot help permissions` and the installed package do
not name the Windows shell tool (the CLI's code is compressed inside its binary), so the name is the
single constant `SHELL_TOOL` (`make_profiles(shell_tool)` for tests) and one approved real run
confirms or flips it. The `shell(...)` permission kind in `--allow-tool`/`--deny-tool` is documented
for every shell command and does not change. Before an implement run starts the orchestrator
creates, inside the worktree only, the directories implied by the subtask's `files/area` (parent of
a file, the directory of an area ending in `/`, the literal prefix of a glob), so a worker does not
need a shell for `mkdir`; absolute, drive-letter, UNC, `..`, `.git`, reserved-name and
symlink/junction paths refuse the whole run as `environment` before anything is created or started,
and a dry run lists `directories_to_create` and writes nothing. Copilot's `usage.json` totals
(`tokenDetails` input/cache_read/cache_write/output, reasoning tokens, request count, premium
request cost, model, plus the last-call values) are stored in the attempt as `usage_totals`,
`source: measured`.

`--ephemeral` (review runs only) keeps a managed Codex review out of Codex's persisted sessions.
Implement runs keep their session so a usage limit can be resumed with `codex exec resume <id>`
(never run automatically). Copilot gets no `--add-dir`, so its
file tools are limited to the worktree; `<verify>` is the verification command from the plan
(or `--verify-command`), and without one the Copilot profile is unconfigured and the worker
is not started. The exact form of `<verify>` that Copilot matches (absolute
`.venv\Scripts\python.exe` versus a bare name) is unverified until the first real run.

**Limits of these profiles.** Say them plainly:

- "Deny network" is CLI-level only: Copilot's `url` permission and the excluded web tools,
  Codex's sandbox defaults. It is not an OS egress boundary; an allowed test command can open
  connections itself.
- Review mode still allows the verification executable, which can run arbitrary code. "Review
  is read-only" is enforced by the orchestrator comparing the worktree (status, content
  hashes of changed paths, HEAD, branch) and the primary checkout's HEAD before and after the
  run, not by the provider's permissions alone. **Writes outside the worktree by an allowed
  command are not detected** by that check. OS-level isolation (filesystem and network) is
  future work.
- The deny patterns cover direct `git commit` / `git push` shell calls only. The
  orchestrator-side checks are the real guard: after an implement run the runner verifies
  that HEAD, the branch and its tip did not move and that no new ref points at the
  worktree's HEAD, then commits the changes itself on `task/<task-id>/<subtask-id>` with an
  attributable message (agent, provider, model, attempt). It never merges or pushes. It
  refuses implement mode in the primary checkout, on `main`/`master` or a configured live
  branch, on any other branch than the subtask's, and in a dirty worktree.

**Failure classes** (primary class, first that applies; never retried automatically):
`profile_unconfigured`, `environment` (invalid/dirty worktree, refused branch, unknown commit),
`launch_error` (executable missing or not startable), `invalid_arguments` (refused before
launch, or a nonzero exit whose output says so), `review_modified_files`,
`git_policy_violation`, `timeout` (the whole process tree is killed), `unsupported_model`,
`auth_error`, `permission_denied` (a tool or command was denied; reported, the profile is never
widened), `nonzero_exit`, `malformed_output`, `no_changes` (implement run without a change),
`commit_failed` (the orchestrator's commit, including hooks, failed). A denied permission goes
back to the user; it is never routed to another agent to get around it.

**Usage limits (T15).** Three further classes, from positive identification only (a nonzero
exit, a bare 429 or a missing usage file alone is never quota evidence):
`quota_exhausted` (the provider's usage limit; Codex: `rate_limits.rate_limit_reached_type` or
`spend_control_reached` in the JSON events), `cap_reached` (a locally configured cap such as
Copilot `--max-ai-credits`) and `provider_error` (throttling or a generic API error, e.g. a bare
429). Detector tables live in `workers/quota.py`; the Claude and Copilot patterns and the Codex
event shape are synthetic until a real sample is recorded, and a CLI version a table does not
list, or an unknown one (the runner reads it from the CLI's own `--version`, a local flag that
uses no quota; a failed probe leaves it unknown), falls back to `provider_error` with the raw
output, so quota verdicts become active only once a provider's CLI version is positively known
and in the table. The version is recorded with every attempt. The reset time is only what the provider
reports (epoch field, or a wall-clock time plus zone resolved with `zoneinfo`; a time earlier
than now means next day; any failure, including a missing tz database, means `reset unknown`).
The worker's own status file becomes `state: blocked` with `blockers: quota: <provider>
(<model>), resets <time> <zone> (reported)` or `... reset unknown` (`cap:` for a cap), which
suspends the stale-worker rule until the reset, or 24 hours when unknown. `result.json` and the
SQLite `attempt_details` carry a `limit` record: provider, model, session, the exact resume
command (recorded, never run), HEAD and `git status --porcelain` at launch, `git diff --stat` at
failure, the raw message, observation time and reset; such attempts do not count toward success
rates. There is no polling and no automatic retry: at the reset the coordinator tells the user,
and a resume is a new, linked attempt after the worktree is checked. Nothing is rerouted to
another account; reassignment to another provider is a coordinator plan revision, with the
user told. Implement-mode Codex runs omit `--ephemeral` so their session can be resumed.

**Process handling (Windows).** Provider commands resolve to `.exe`, then `.cmd`/`.bat`, never
`.ps1`; npm shims run as `node.exe <script>` so the prompt never passes through `cmd.exe`. The
process tree runs in a kill-on-close Job Object; stdout/stderr stream to files; stdin is the
prompt file or closed. The environment is an allowlist (system paths, profile directories,
proxy settings, plus `CODEX_HOME` or `COPILOT_HOME`/`GH_HOST`/`COPILOT_GH_HOST`); API keys and
tokens are passed only when named with `--pass-env`, and `COPILOT_ALLOW_ALL` never.
