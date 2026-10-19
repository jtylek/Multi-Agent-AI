# Multi-Agent AI

**Native Windows multi-agent development orchestration for Visual Studio Code.**

Multi-Agent AI is an open-source project to coordinate **OpenAI Codex CLI, Anthropic Claude Code, and GitHub Copilot CLI** as a team of coding agents. A developer gives a job to one **coordinator agent**; the coordinator breaks it into subtasks, delegates work to suitable agents and sessions, tracks progress, reviews results, and proposes an integrated change.

The primary development environment is **Windows 10/11 with Visual Studio Code**. The orchestrator is intended to work with existing CLI authentication and local repositories, without requiring WSL or Docker. It is project-neutral: any Git repository can be a target, with project-specific settings kept in a project profile.

> **Project status:** Early design / bootstrap. This README describes the target architecture, not a finished or tested product. Agent execution, routing, messaging bridges, and the VS Code integration remain to be implemented.

## Goals

1. **One job, many agents.** Assign a goal to a coordinator, which plans, delegates, monitors, verifies, and reports on work.
2. **Multiple providers and sessions.** Support Claude Code, Codex CLI, and Copilot CLI, with several concurrent managed sessions per provider (similar to Claude's multi-agent workflows). Do not assume unrelated pre-existing chat sessions are remotely addressable.
3. **Difficulty-aware routing.** Classify tasks by complexity, required tools, risk, and context; select a suitable provider/model rather than using the strongest model for everything.
4. **Token optimization (maximum value per token).** Prefer the lowest-cost *capable* agent for each task, while factoring in reliability, latency, review overhead, and expected retries. Record measured usage when available and clearly label estimates. Optimize total successful-job cost, not just price per request.
5. **Inter-agent messaging.** Provide durable messages, acknowledgments, reviews, and handoffs between agents, with optional immediate notifications when a supported recipient is reachable.
6. **Safe parallelism.** Track dependencies, file ownership, and reservations; use separate Git worktrees for conflicting or substantial tasks.
7. **Verified integration.** Run scoped tests, independent reviews when warranted, and full integration checks. Never treat a zero exit code alone as proof of task completion.
8. **VS Code-first experience.** Submit jobs, see the task board and agent/session status, read messages, and approve integration without leaving VS Code.
9. **Reusable across projects.** Keep the orchestrator separate from the repositories it works on, with project-specific settings and policies.

## Intended workflow

```text
Developer in VS Code
        |
        v
Coordinator agent (Claude / Codex / Copilot)
        |
        v
Task plan + dependencies + acceptance criteria
        |
        v
Deterministic Python orchestration service
        |
        +--> Codex worker/session ----> isolated task branch/worktree
        +--> Claude worker/session ---> isolated task branch/worktree
        +--> Copilot worker/session --> isolated task branch/worktree
        |
        v
Mailbox + acknowledgments + status + review
        |
        v
Verification + dependency-ordered integration
        |
        v
Developer approval before changing main / pushing
```

The coordinator is a **role**, not a hard-coded vendor. Exactly one coordinator owns each job. A user can transfer that role explicitly. The Python service handles process management, scheduling, state, and Git; AI handles planning, implementation, and reviews where reasoning adds value.

## When multi-agent work pays off

Multi-agent work is not automatically more efficient than a single agent. It costs more tokens in total (every agent is briefed cold, and plans, mailbox files, status files and reviews are overhead). What it buys is wall-clock time through parallel work, independent review that finds bugs the author's tests missed, smaller per-agent contexts, and cheaper routing of mechanical work. These are observations from the first field trials, not measurements: the performance ledger that will quantify them is still planned.

Best suited:

- Work that splits into three or more subtasks with disjoint files, so each file has one writer.
- Work with explicit acceptance criteria and exact verification commands, so "done" can be checked.
- Medium to large jobs (hours rather than minutes), where the briefing overhead is small compared with the work.
- Risky changes that benefit from a second, different reviewer who runs the tests: shared infrastructure, security, migrations, ports of legacy behavior.
- Mixed workloads with a few hard tasks and many mechanical ones, where cheap-model-first routing saves quota.

Poorly suited (use a single agent):

- Small or ordinary changes that one agent finishes in a few tool calls.
- Tightly coupled work in one file or one design, where parallel writers conflict.
- Exploratory work whose design is still changing, so the plan goes stale between agents.
- Quick bug fixes where one agent already holds the context.

Rule of thumb: use several agents when a job has several independent parts and a missed bug is expensive. Do not make performance claims without a stated method and baseline; once the ledger exists, compare single-agent and multi-agent runs of the same job.

## Routing and token efficiency

A planned routing policy evaluates:

- Task complexity: low, medium, high, with configurable overrides.
- Required capabilities, tools, context size, and permitted filesystem access.
- Provider/model availability and health.
- Historical success and verification rates on similar tasks.
- Estimated tokens, cost, latency, and retry risk.
- Whether a second-model review is justified by the task's risk.

Start with configurable heuristics. Introduce evidence-based routing as execution history accumulates. **Do not claim a cost saving without measurement.** CLI subscription quotas and token reports may differ between providers; unavailable data must remain marked unknown.

## Inter-agent messaging

The protocol is specified in [docs/protocol/agent-messaging.md](docs/protocol/agent-messaging.md), with exact file grammar in [docs/protocol/formats.md](docs/protocol/formats.md). It generalises a protocol proven in real multi-agent work and separates **durable delivery** from **live notification**.

### Shared mailbox

Coordination state lives under a **state root** in the *target project's primary checkout* (default `.multi-agent-ai/`, configurable per project):

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

A project that already uses another location (for example `storage/framework/agents/` in a Laravel project) configures it in its profile. The shared state is local and excluded from Git. Worktree workers reference the primary checkout's board using an absolute path.

A request names its sender, intended recipient IDs, and message ID. Each recipient writes **its own** acknowledgment with `received`, `agreed`, or `changes requested`. Receipt does not imply agreement or permission to edit overlapping files. Messages remain unconfirmed until the actual recipient acknowledges. Never put credentials in mailbox files.

### Delivery: push where supported, mailbox everywhere

| Recipient | Intended delivery approach | Important limitation |
| --- | --- | --- |
| Claude Code | Native session messaging when available; otherwise CLI relay or mailbox | Verify the actual session and working directory |
| VS Code Copilot | `code chat` submission where supported, followed by recipient acknowledgment | Submission does not prove which chat received or processed it |
| Codex | Native `send_input` only within a reachable Codex-managed agent hierarchy; otherwise managed CLI invocation or mailbox handoff | **No general push channel to arbitrary existing top-level Codex chats** |
| Any managed worker | Orchestrator-owned subprocess/session plus durable mailbox | The service can manage its own workers, not take over unrelated sessions |

A successful notification is **not** a successful acknowledgment. An unreachable existing interactive session may require the developer to prompt it manually. The orchestrator must never manipulate private session files to simulate delivery.

### Joint tasks and ownership

- One coordinator owns the plan, assigns workers, tracks revisions, integrates, and reports.
- Each worker owns its subtask, branch, and status file; each file has one writer.
- Plans record task IDs, acceptance criteria, dependencies, base commit, integration branch, owners, reviewers, and revision numbers.
- Reviews should be performed by another agent where useful; trivial work may have review waived.
- Integrate reviewed commits in dependency order and run the complete test suite before proposing the result.
- Changes to the user's working branch, `main`, or remote repositories require explicit approval.
- Handle stale sessions, disagreements, and permission denials without silently bypassing a denied action.

The source protocol also defines bounded response watches, cleanup ownership, and handoff notes. These rules should be implemented and tested rather than replaced by uncontrolled mailbox polling.

## Native Windows architecture (planned)

| Component | Responsibility |
| --- | --- |
| Python service | Job lifecycle, DAG scheduler, concurrency, cancellation, recovery |
| Provider adapters | Codex, Claude, Copilot CLI launch and structured result collection |
| Session registry | Managed session identities, capabilities, availability, and contact channels |
| Message broker | Durable mailbox, acknowledgments, notification adapters |
| Workspace manager | Git branches, worktrees, reservations, file-lock awareness |
| Verification engine | Tests, diff inspection, risk-based review, integration gates |
| SQLite ledger | Attempts, failure categories, usage, latency, task outcomes |
| VS Code integration | Job submission, task board, logs, message and approval UI |

Windows implementation must correctly resolve `.exe`, `.cmd`, and `.bat` entrypoints, manage process trees, stream logs, tolerate UTF-8 BOMs, and classify configuration/authentication failures without blindly retrying. Git worktrees provide editing isolation **but are not a security boundary**.

## Quick start

There is no installer or UI yet; everything is a command-line tool. This walkthrough uses a
throwaway Git repository and starts no AI agent and uses no provider quota.

**You need:** Windows 10/11, Python 3.11 or newer (`py -3.12 --version`), and Git. The `codex`
and `copilot` command-line tools are not needed for the dry run, but the dry run reports a
problem if the one you pick is missing. Install them and sign in separately before any real run.

**1. Install** (PowerShell, in this repository):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
```

**2. Create a practice project** (any Git repository with at least one commit works):

```powershell
git init demo
cd demo
"hello" | Out-File README.md -Encoding ascii
git add .; git commit -m init
cd ..
```

**3. Initialise the coordination state** in that project:

```powershell
.venv\Scripts\multi-agent-ai init --project-root demo
```

This creates `demo\.multi-agent-ai\` (messages, tasks and a SQLite index). Keep that folder out
of Git, for example by adding it to `demo\.gitignore`.

**4. Write a tiny plan.** Save the following as `demo\.multi-agent-ai\tasks\demo\plan.md`
(create the `tasks\demo` folders). The table is what the tool reads; the file formats are in
[docs/protocol/formats.md](docs/protocol/formats.md).

```text
task: demo
coordinator: me
revision: 1
updated: 2026-10-19 12:00

## Goal

Review the README.

## Verification

`git status`

| id | what | files/area | depends on | worktree | owner | reviewer | state |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | review README | README.md | - | no | codex-w1 | waived | assigned |
```

Check that it parses:

```powershell
.venv\Scripts\multi-agent-ai inspect --project-root demo
```

**5. Run a first job as a dry run.** `worker run` only checks and prints what it would do unless
you add `--execute`:

```powershell
.venv\Scripts\multi-agent-ai worker run --project-root demo --provider codex `
    --task demo --subtask R1 --agent-id codex-w1 --mode review --worktree (Resolve-Path demo)
```

You should see `dry run: nothing was written or started` and a JSON description with the
provider, the commit to review and the exact command line. An empty `"problems": []` means a real
run would be accepted; otherwise each problem is listed (for example `codex` not found on `PATH`)
and the exit code is 1. A real run needs `--execute`, uses provider quota, and should only be
done after you read the printed command.

Next: the [protocol](docs/protocol/agent-messaging.md) explains messages and joint tasks, and
[docs/implementation-status.md](docs/implementation-status.md) lists what works today.

## Current implementation

The first package (milestone M0) implements the protocol/state foundation plus read-only
reachability, dry-run-by-default mailbox-pointer notification and managed headless workers:
typed models, a parser and writer for the coordination files in
[docs/protocol/formats.md](docs/protocol/formats.md), a configurable state root with a derived
SQLite index, and a small CLI. It needs Python 3.11+ and uses only the standard library at
runtime. `notify` only sends when explicitly invoked with `--send`; without it, the command
prints the intended transport and performs no external action. It can also launch one **managed
headless worker** (`codex exec` or `copilot -p`) for a plan subtask with least-privilege
profiles, classify the outcome, and record status, review and attempt data; this has been tested
with fake executables only. Routing, the registry and the VS Code integration are not
implemented yet; see [docs/implementation-status.md](docs/implementation-status.md).

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\python -m pytest -q

.venv\Scripts\multi-agent-ai init --project-root <target-project>
.venv\Scripts\multi-agent-ai inspect --project-root <target-project>

# readiness check: Python, Git, provider CLIs (found, version), state root; read-only, no provider call
.venv\Scripts\multi-agent-ai doctor --project-root <target-project>

# one read-only review by a managed Codex worker; without --execute this is a dry run
# (checks and prints the command, starts nothing); --execute launches it and uses provider quota
.venv\Scripts\multi-agent-ai worker run --project-root <target-project> --provider codex `
    --task <task-id> --subtask <id> --agent-id codex-w1 --mode review --worktree <abs worktree> --execute
```

The state root defaults to `.multi-agent-ai/` in the target project's primary checkout; pass
`--state-root <path>` (absolute, or relative to the project root) to use another location.
SQLite lives at `<state-root>/state.sqlite3` and is only an index of the coordination files,
which remain the source of truth. `inspect` is read-only. `init` does not edit the target
project's `.gitignore`; exclude the state root from version control yourself.

## Proposed milestones

- **M0 — Bootstrap:** README, protocol documentation, architecture decisions, initial tests and project layout.
- **M1 — Windows runtime:** reliable CLI discovery, managed agent execution, logs, cancellation, and failure classification.
- **M2 — Messaging and sessions:** registry, mailbox/acknowledgments, notification bridges, multiple managed sessions, restart handoffs.
- **M3 — Coordinator and parallel tasks:** planning, dependency graph, file reservations, worktrees, reviews, and integration.
- **M4 — Adaptive routing:** usage ledger, cost/reliability scoring, escalation policies, provider health.
- **M5 — VS Code UX:** task board, chat/job handoff, progress, review, and approval controls.

## Example use cases

- **Web application feature:** Ask a coordinator to implement a feature; delegate backend, frontend, tests, and independent review to different agents, respecting the project profile's policies (the first planned trial is [EpesiCRM](https://github.com/jtylek/EpesiCRM); see the [case study](docs/case-studies/epesi-field-report.md)).
- **Any Git project:** Ask for a bug fix or refactor, parallelize independent tasks, verify the resulting commits, and approve integration.
- **Multi-session work:** Keep several managed Claude, Codex, or Copilot workers active across distinct subtasks, with durable status and handoffs.

## Security and approval principles

- Honor the user's and each agent's permission settings; never reassign denied operations merely to bypass a refusal.
- Require explicit approval for pushing changes, merging into protected/user branches, or destructive operations.
- Use least-privilege CLI settings by default; do not assume blanket `--allow-all-tools` is appropriate.
- Keep secrets, tokens, and private session data out of logs and mailbox files.
- Report partial success honestly and retain enough evidence to diagnose failures.

## Contributing

This repository is being bootstrapped. Initial contributions should focus on reliable Windows execution, protocol compatibility, and reproducible tests. Architectural ideas may be inspired by projects such as Orka, but implementation should be original or reuse code only in accordance with its license.

## License

See [LICENSE](LICENSE).
