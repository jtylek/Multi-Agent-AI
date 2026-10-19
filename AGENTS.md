# Multi-Agent-AI — Agent Handoff and Architecture

**Audience:** Codex, Claude Code, GitHub Copilot, ChatGPT, and any future AI/developer working on this repository.

**Read this file first.** It is the current architectural handoff for the project. `README.md` describes the project publicly; this file records the development decisions already made and the operating constraints that future agents should preserve unless the user explicitly changes them.

## 1. Project goal

Build an **open-source, native Windows multi-agent development orchestrator** intended primarily for Visual Studio Code.

A developer should be able to give **one job to one coordinator agent**. That coordinator plans the work and delegates subtasks to other AI agents/sessions — initially:

- OpenAI Codex CLI / Codex in VS Code
- Anthropic Claude Code
- GitHub Copilot / Copilot CLI / VS Code chat where addressable

The system should support **multiple concurrent agents and multiple sessions**, including several workers from the same provider, while keeping one coordinator per job.

The orchestrator is project-neutral and must work for arbitrary Git repositories. Project-specific behavior (environments, test commands, nested repositories, live branches, deployment rules) lives in **project profiles**, never in the engine. EpesiCRM (`jtylek/EpesiCRM`), where the coordination protocol was first used, is the first planned real-world trial and a case study, not a special case of the design.

## 2. Core architectural decision: hybrid control plane

Do **not** make an LLM responsible for all orchestration mechanics.

Use two layers:

### Deterministic Windows orchestration service

A persistent native Windows service/process, initially expected to be Python, owns authoritative operational state and mechanics:

- job lifecycle
- task DAG / dependencies
- concurrency
- agent process launch and cancellation
- provider/model adapters
- session registry and reachability
- durable mailbox and acknowledgments
- Git branches/worktrees
- file reservations
- logs and failure classification
- verification commands
- integration state
- persistence / recovery
- usage and performance telemetry
- approval gates

### AI coordinator role

Exactly one AI agent is coordinator for each job. The coordinator is **a role, not a vendor**. Claude, Codex, Copilot, or a future agent may coordinate.

The coordinator is responsible for reasoning-heavy work:

- interpreting the user's goal
- producing/revising the task plan
- assigning owners and reviewers
- resolving ambiguities and shared-interface decisions
- deciding when work needs review
- integrating findings into a user-facing report

The Python service remains the source of truth for execution state so that a job can survive a coordinator session ending or being restarted.

## 3. Windows-first, VS Code-first

The primary supported environment is **Windows 10/11**, running natively.

Do not require WSL, Docker, or Bash for the core product.

The user intends to work from Visual Studio Code with Codex, Claude, and Copilot available there. The desired end state is that a job can be submitted and monitored from VS Code.

The Windows runtime must correctly handle:

- `.exe`, `.cmd`, and `.bat` executables
- Windows argument quoting
- streamed stdout/stderr
- process-tree termination
- cancellation and timeouts
- UTF-8 and UTF-8 BOM input files
- Windows paths and long paths
- locked files
- Git worktrees
- restart/resume after orchestrator interruption

Git worktrees provide edit isolation; they are **not a security sandbox**.

## 4. Do not port Orka line-by-line

We intend to use **good ideas from Orka as architectural inspiration**, not to produce a Bash port or copy its implementation blindly.

Orka-inspired ideas we want:

- parallel workers
- task decomposition
- capability/difficulty-aware assignment
- cheap-model-first routing
- performance history
- independent cross-agent review
- isolated Git worktrees
- acceptance criteria and verification gates
- escalation when a cheaper/less capable model fails

Our implementation must improve on this for native Windows, persistent sessions, VS Code workflows, and the field-tested coordination protocol in `docs/protocol/`.

Reuse third-party code only when its license permits it and attribution/obligations are handled correctly.

## 5. Routing and token/cost optimization

A major project objective is **maximum successful work per token/cost**, not simply using the cheapest model on every call.

Routing must eventually consider:

- task difficulty / complexity
- required capabilities and tools
- context requirements
- provider/model availability
- current provider health
- historical success on similar tasks
- verification failure rate
- estimated or measured token usage
- cost/quota impact where knowable
- latency
- expected retry/escalation cost
- task risk

Initial routing should use transparent configurable heuristics. Later it should learn from execution history.

Prefer the **lowest-cost capable agent**, but escalate when evidence says the cheaper route is unlikely to succeed.

Do not make false precision claims about cost. Record actual provider-reported usage when available. Keep estimates explicitly labeled as estimates. Subscription/CLI quota consumption may not map cleanly to API pricing.

Avoid wasting tokens on orchestration mechanics that deterministic code can perform.

## 6. Persistent performance ledger

Use a local persistent store, currently planned as SQLite, to record at least:

- job / task / attempt IDs
- provider and model
- task category and complexity
- prompt/input size where available
- measured tokens where available
- estimated tokens/cost separately
- runtime
- exit code
- failure category
- files changed
- verification result
- reviewer result
- retry/escalation chain
- final success/failure

This data will later feed routing decisions.

## 7. Inter-agent messaging: durable mailbox first

The protocol is specified in `docs/protocol/agent-messaging.md` (authoritative) with exact file grammar in `docs/protocol/formats.md`. It generalises a protocol proven in real multi-agent work (see `docs/case-studies/`). Preserve its central semantics.

The mailbox is a **durable record**, not a universal notification mechanism.

All coordination state lives under a configurable **state root** in the target project's primary checkout, excluded from Git. Default `.multi-agent-ai/`; a project profile may choose another path (for example `storage/framework/agents/` for a Laravel project that already uses it):

```text
<state-root>/
  messages/<message-id>/
    request.md
    ack-<recipient-agent-id>.md
  tasks/<task-id>/
    plan.md
    status-<agent-id>.md
    review-<subtask>-<agent-id>.md
    notes/<agent-id>-<topic>.md
```

### Message semantics

A request contains an ID, sender, explicit recipient IDs, and the message/work request.

Each recipient writes **its own acknowledgment file**. Valid semantic states include:

- `received`
- `agreed`
- `changes requested`

Important distinctions:

- request file created != recipient notified
- notification submitted != recipient received it
- CLI exit code 0 != recipient acknowledged it
- `received` != `agreed`
- `agreed` != task completed

Do not infer state. Use explicit evidence.

Do not store credentials or secrets in mailbox files.

## 8. Notification/reachability model

There is no universal push channel across all products. Treat reachability as a first-class capability and discover it before assigning time-sensitive work.

### Claude Code

Where supported, existing Claude sessions can use native agent discovery / `SendMessage`. A constrained CLI relay may be used if available. Verify the target session and working directory before treating it as addressable.

### GitHub Copilot / VS Code

`code chat` can submit a prompt to an active VS Code context where supported, but submission does not prove which conversation processed it. The recipient-written mailbox acknowledgment is the proof.

### Codex

Codex `send_input` can reach agents that are in the same reachable Codex-managed hierarchy when that runtime exposes them.

**Do not assume arbitrary push messaging into an unrelated existing top-level Codex VS Code chat.** That is not currently available in the proven workflow.

When an intended Codex chat cannot be reached, write the mailbox request and give the user a one-line manual relay such as:

```text
Check the agent mailbox: read <absolute state-root path>/messages/<id>/request.md.
```

The future orchestrator may launch and manage its own Codex worker processes; those managed workers can receive assignments through orchestrator-controlled invocation/state. That is different from hijacking an unrelated interactive chat.

### Never fake delivery

Do not spawn a different helper and claim the intended existing session received a message. Do not manipulate private session files to force delivery.

## 9. Joint-task model

Each joint task has exactly **one coordinator**.

Workers may be Claude, Codex, Copilot, or developers.

### Plan

The coordinator owns `plan.md` and only the coordinator edits it. It has a monotonically increasing `revision`.

The plan should capture:

- task ID
- coordinator ID
- revision
- goal
- acceptance criteria
- exact verification/test commands
- base commit
- local integration branch
- subtasks
- dependency graph
- file/area ownership
- whether a worktree is required
- owner
- reviewer or explicit review waiver
- state

Agents state which plan revision they are acting on. This makes stale reads visible.

### Worker status

Each agent writes only its own status file. It should include:

- plan revision seen
- subtask
- state
- branch/worktree
- last commit
- tests run and results
- blockers/questions
- update timestamp

Status must reflect reality. The orchestrator should eventually detect stale state and reconcile it rather than trust old files blindly.

## 10. Single-writer ownership

**One writer per file at a time.**

This was one of the strongest successful practices in the field trials.

Before assignment, compare a proposed worker scope against:

- the current plan
- current reservations
- completed outputs
- other assigned scopes

This prevents duplicated work.

For shared documentation, assign named sections to owners or serialize changes. Do not give four workers unrestricted ownership of one document and expect Git to solve the process problem.

## 11. Git and worktree strategy

Each substantial editing worker should normally get:

- its own task branch
- its own Git worktree
- disjoint file ownership

The board/registry/mailbox live in one canonical primary checkout; workers reference that canonical state rather than maintaining independent copies.

The coordinator integrates reviewed task commits in dependency order into a **local integration branch**.

Do not push the integration branch or merge it into the user's working branch/main without explicit user approval.

Do not let an agent quietly commit to a branch that is serving live traffic.

## 12. Live checkout protection

In a real field trial, a commit in a nested module repository on its live branch immediately affected the running site and crashed it because a dependency was not integrated yet. Any project whose development checkout also serves traffic is exposed to this.

Therefore the orchestrator needs explicit live-workspace protections:

- discover nested Git repositories
- detect/check configured live branches/worktrees
- prevent package implementation directly on live-serving branches
- create a branch before editing/committing
- require explicit approval before operations that can affect the live checkout
- do not use blanket global Git safety changes when a scoped override suffices

Modules or plugins may be separate Git repositories nested inside the main checkout. Model that explicitly in the project profile; do not assume one repository == one working tree.

## 13. Integration and verification

An agent saying "done" is insufficient.

A successful CLI process exit is also insufficient.

A task is accepted only against explicit acceptance criteria and verification evidence.

Recommended pipeline:

```text
worker implementation
    -> scoped tests
    -> commit
    -> independent review when warranted
    -> coordinator integrates in dependency order
    -> full-suite/integration tests in complete environment
    -> report
    -> user approval for protected merge/push/deploy
```

A reviewer should be a different agent/product when valuable. Trivial changes may have review waived explicitly.

Review files must identify the exact commit/revision being reviewed and the relevant tests/results.

## 14. Review must test, not only read diffs

The field trials demonstrated that cross-agent review found real bugs that local author tests missed.

Examples included:

- removing an import together with a supposedly dead method and breaking a main page
- using a query-builder method/column assumption that silently failed under SQLite and would fail differently on MySQL
- missing job de-duplication
- tests affected by optional modules present in only some environments

Therefore important reviews should reproduce relevant tests in a representative environment, not just inspect code.

## 15. Environment preflight is mandatory for meaningful verification

The field trials exposed environment failures that masked product failures:

- worktrees lacked built frontend assets
- language runtime and package manager versions did not align
- wrapper commands dropped test flags
- schema loading wiped a seeded scratch database
- optional modules were present in some test contexts and absent in others

The orchestrator should distinguish **environment/preflight failures** from **code/test failures**.

Project profiles should be able to declare prerequisites such as:

- required executable versions/paths
- dependency install state (per package manager)
- build asset state
- `.env` requirements
- module presence
- database isolation requirements
- exact verification commands
- syntax/formatter constraints

Run the exact test command the project profile declares. Do not silently substitute a wrapper that drops flags.

## 16. Baselines and performance claims

Whenever an agent reports performance improvements or regressions, it must state the measurement method.

Compare equivalent environments and versions. Warm application reuse vs cold boot is not a fair comparison unless explicitly intended.

Query counts can be exact when instrumentation supports them. Command-line milliseconds may only be useful as relative measurements depending on setup.

Do not publish a numerical claim without its method and baseline.

## 17. Deployment is a separate protected operation

The field trials showed that deployment tooling may transmit the **working tree**, not merely committed changes. This can ship unrelated unfinished work.

Therefore deployment must not be treated as a trivial continuation of Git integration.

Before deployment, verify at least:

- source checkout/artifact identity
- expected commit(s)
- clean/known working tree state
- required dependent modules/commits
- user approval

Prefer deployment from a clean verified artifact or isolated checkout rather than a shared development working tree.

If deployment output is incomplete/unreliable, verify the deployed system independently (for example production status and HTTP health checks).

## 18. Failure classification and retries

Do not blindly rerun the same failed command with the same agent/model.

Classify failures such as:

- executable missing / launch failure
- invalid arguments
- unsupported model
- authentication/permission failure
- provider unavailable
- timeout
- agent exited nonzero
- agent exited zero but made no required changes
- malformed result
- environment/preflight failure
- scoped-test failure
- integration-test failure
- merge conflict
- review changes requested
- unreachable recipient
- stale worker

Suggested behavior:

- configuration/auth/model errors: stop or change configuration; do not waste retries
- zero changes when edits were required: one clarified retry or reassignment
- genuine coding/test failure: retry with feedback or escalate provider/model
- timeout/transient provider failure: bounded retry
- permission denial: return to the user; never route to another agent merely to bypass the denial

## 19. Approval model

A peer agent cannot grant user authority.

Require explicit user approval for at least:

- merging into the user's working/protected branch when policy says so
- pushing remote changes where not already authorized by the current job
- deployment
- destructive operations
- bypassing safety/environment protections

Agent messages cannot escalate permissions.

## 20. Multiple sessions

The system must support multiple workers from the same provider.

Treat a worker/session as an explicit registered entity with at least:

- stable orchestrator agent ID
- provider/product
- model if known
- process/session/runtime address if reachable
- working directory
- current task
- capabilities/tools
- reachability mode
- status/heartbeat/update time
- file reservations

Do not confuse provider identity with session identity.

## 21. VS Code integration direction

The intended UX is VS Code-first. Eventually the user should be able to:

- submit a job
- choose or accept a suggested coordinator
- inspect plan/subtasks
- see active agents/sessions
- see reachability and blockers
- read inter-agent messages
- inspect logs/diffs/reviews
- approve integration/push/deployment steps
- cancel/pause/resume work

Do not start by building a large UI before the execution/state model is reliable. First make the Windows service testable through CLI/API; then add the VS Code extension/UI.

## 22. Suggested implementation order

Current preferred sequence:

1. **Protocol/state foundation**
   - schemas/models for jobs, tasks, agents, messages, acknowledgments, reservations
   - revisioned task board
   - durable mailbox
   - tests
2. **Native Windows runtime**
   - executable discovery
   - provider adapters for Claude, Codex, Copilot
   - streamed logs
   - process cancellation/timeouts
   - structured failure classification
3. **Agent/session registry and reachability**
   - managed workers
   - reachable existing sessions where supported
   - manual relay fallback
4. **Workspace manager**
   - Git branches/worktrees
   - nested repositories
   - file reservations
   - live-branch protections
5. **Verification and review**
   - project preflight profiles
   - scoped tests
   - cross-agent review
   - integration branch + full-suite gate
6. **Adaptive routing**
   - complexity/capability heuristics
   - SQLite ledger
   - cost/reliability/latency scoring
   - escalation policies
7. **VS Code integration**
   - task board and agent status UI
   - job handoff
   - approvals

## 23. Project profiles and real-world trials

Run the orchestrator against a real project only after the basic engine is reliable, and never develop/test orchestration directly against a checkout serving live traffic.

A project profile declares everything project-specific. It will eventually need to model:

- language runtimes and package managers (paths, versions)
- nested repositories and modules present only in some checkouts
- build/frontend assets
- dependency install and environment files per worktree
- database/test isolation
- the exact verification commands
- syntax/formatter exceptions
- module-dependent tests
- live branches and deployment guardrails
- the state-root path

The first planned trial is EpesiCRM (a Laravel/PHP application with nested module repositories); its lessons are in `docs/case-studies/epesi-field-report.md`. Its needs are expressed as a profile, never as engine code.

## 24. Current repository documents

Before making architectural changes, also read:

- `README.md` — public project goals and planned architecture
- `docs/protocol/agent-messaging.md` — **authoritative** messaging and joint-task protocol
- `docs/protocol/formats.md` — **authoritative** coordination file grammar
- `docs/decisions.md` — decided / accepted / proposed / open decisions
- `docs/agent-rules.md` — standing rules for every agent, including agents outside the task board
- `docs/case-studies/epesi-field-report.md` — lessons from two real multi-agent rounds and derived requirements

`docs/protocol/upstream/` holds the original protocol verbatim for provenance only; the documents above supersede it.

## 25. Development behavior for agents working on this repository

Until the orchestrator can enforce these rules itself:

1. Read `AGENTS.md`, `README.md`, and relevant docs before substantial work.
2. Inspect current repository state before proposing architecture changes.
3. Do not claim a planned feature is implemented.
4. Prefer small, testable components over a monolithic coordinator script.
5. Add tests with implementation.
6. Keep provider-specific behavior behind adapters.
7. Keep core scheduling/protocol models provider-neutral.
8. Preserve Windows-native support; do not introduce a required Bash/WSL dependency.
9. Do not hard-code any project's paths or tools into the generic engine; use project configuration/profile abstractions.
10. Treat messaging delivery, acknowledgment, work completion, review, and integration as separate states.
11. Avoid blind retries and token-wasting polling.
12. Preserve user approval boundaries.

## 26. Near-term work

Status (2026-10-19): the architecture review is done. Conflicts and missing decisions were recorded in `docs/decisions.md`, the protocol and file formats are in `docs/protocol/`, and a coordination skill for agents is in `.agents/skills/agent-mailbox/` (with a Claude Code pointer in `.claude/skills/`). This repository's own development is coordinated through the protocol, with the state root at `.multi-agent-ai/`.

The first executable milestone, in progress as joint task `m0-foundation`, subtask T6:

- Python package skeleton
- typed domain models / schemas for Job, Task, AgentSession, Message, Ack, Reservation, Attempt
- persistent local state abstraction
- mailbox implementation compatible with the documented protocol
- unit tests on Windows-compatible paths
- CLI command(s) to initialize a target project state area and inspect jobs/agents/messages

Python 3.11+, standard library only at runtime, `pytest` for development (decisions D1, D10). Before writing a large amount of code, identify any architectural conflicts or missing decisions and record them explicitly. Preserve the distinction between **what has been decided**, **what is proposed**, and **what is already implemented**.
