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

## Current implementation

The first package (milestone M0) implements the protocol/state foundation plus read-only
reachability and mailbox-pointer notification: typed models, a parser and writer for the
coordination files in [docs/protocol/formats.md](docs/protocol/formats.md), a configurable
state root with a derived SQLite index, and a small CLI. It needs Python 3.11+ and uses only
the standard library at runtime. Agent execution, routing, the registry and the VS Code
integration are not implemented yet; see
[docs/implementation-status.md](docs/implementation-status.md).

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\python -m pytest -q

.venv\Scripts\multi-agent-ai init --project-root <target-project>
.venv\Scripts\multi-agent-ai inspect --project-root <target-project>
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
