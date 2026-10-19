# Recommendation: Next Development Milestone for Multi-Agent-AI

## Current assessment

The project has progressed beyond the documentation/bootstrap stage.

The current implementation already includes substantial M0 functionality:

- Python package structure
- authoritative file-based coordination protocol
- mailbox request/ack handling
- revisioned task board
- SQLite-derived operational index
- Windows-native process execution
- Codex and Copilot managed worker support
- reachability discovery
- notification transport
- failure classification
- Git safety checks
- attempt logging
- test coverage for the implemented components

The architecture is sound and should **not be redesigned at this point**.

In particular, the current decision that protocol files are authoritative while SQLite is only derived operational state should be preserved.

The system is now ready to move from "components exist" to "complete end-to-end orchestration works."

---

# Primary recommendation

The next milestone should be:

> **Build and validate one complete end-to-end multi-agent workflow on native Windows using real Claude, Codex, and Copilot agents.**

Do not prioritize adaptive routing or cost optimization yet.

Routing decisions will only become meaningful once execution, communication, worktree isolation, verification, and integration are reliable with the actual installed providers.

---

# Recommended implementation order

## 1. Agent registry and reservation enforcement

Implement the registry before the scheduler.

The registry should provide a canonical view of:

- agent id
- provider/product
- session identity
- working directory
- current task
- current subtask
- status
- reachability mode
- last activity
- current file/area reservations
- capabilities
- whether the agent is managed or interactive

Example conceptual record:

```text
agent: codex-a91e
provider: codex
session: <runtime-id>
cwd: C:\AI\project
task: feature-123
subtask: backend
status: working
contact: codex-queue
managed: false
updated: 2026-10-19 23:30
```

Reservation enforcement should prevent two active agents from modifying overlapping files or directories.

The orchestrator should refuse an assignment that violates an active reservation unless the coordinator explicitly revises the plan.

---

## 2. Worktree lifecycle management

Worktree creation and cleanup are now critical.

The orchestrator should be able to:

1. create a task integration branch;
2. create one branch per editing subtask;
3. create one worktree per worker;
4. validate that the worktree points to the expected base commit;
5. refuse live branches such as `main`, `master`, and configured protected branches;
6. detect dirty worktrees before execution;
7. merge reviewed commits into the integration branch in dependency order;
8. remove completed worktrees safely;
9. retain failed worktrees for inspection.

Suggested branch convention:

```text
task/<task-id>/integration
task/<task-id>/<subtask-id>
```

Worktrees should remain an isolation mechanism, not a security boundary.

---

## 3. Claude managed-worker adapter

Codex and Copilot managed workers already exist in some form.

Claude should be brought to feature parity before scheduler development.

The Claude adapter should support:

- implementation mode
- review mode
- deterministic prompt construction
- explicit tool/permission policy
- stdout/stderr capture
- timeout handling
- process-tree termination
- final-message extraction
- failure classification
- usage extraction where available
- orchestrator-controlled commit behavior
- status/review output

Provider behavior should remain behind a common worker interface.

For example:

```text
WorkerRequest
  provider
  model
  mode
  task
  subtask
  worktree
  acceptance criteria
  verification command
```

returns:

```text
WorkerResult
  status
  exit code
  failure category
  final message
  changed files
  commit
  usage
  duration
  run directory
```

---

## 4. Real-provider smoke tests

Before implementing adaptive routing, validate the runtime against the actual installed CLIs.

Fake executables are necessary for unit tests but insufficient for provider compatibility.

Create a disposable test repository and run real smoke tests for:

### Codex

Test:

- executable discovery
- prompt delivery
- `codex exec`
- model selection
- JSON/event output
- file modification
- commit creation
- timeout behavior
- usage extraction

### Copilot

Test:

- CLI invocation
- prompt quoting on Windows
- allowed tool profiles
- shell verification command
- JSONL/final-message parsing
- usage metadata if available
- modification and commit flow

### Claude

Test:

- managed invocation
- permission configuration
- file modifications
- review mode
- final result parsing
- timeout and process cleanup
- usage information

These tests should use very small tasks to avoid unnecessary token consumption.

Example:

```text
Create multiply.py containing:

def multiply(a, b):
    return a * b
```

The test is successful only if the orchestrator independently verifies the resulting file and commit.

A process exit code of zero is not sufficient.

---

## 5. Verification engine

Verification must belong to the orchestrator, not the worker.

Workers may run their own tests, but the orchestrator must independently verify acceptance criteria.

The verification layer should support:

- one or more shell commands
- expected files
- expected clean/dirty state
- expected commit
- test exit codes
- configurable timeouts
- project-specific environment checks
- structured verification results

Example:

```json
{
  "commands": [
    "python -m pytest tests/test_calculator.py -q"
  ],
  "timeout": 300
}
```

Result:

```json
{
  "status": "passed",
  "commands_run": 1,
  "duration_seconds": 4.2
}
```

Verification failure should not automatically mean "retry the same model."

It should feed into failure classification.

---

# Failure classification

The retry system should classify failures before deciding what to do.

Suggested categories:

```text
configuration_error
authentication_error
unsupported_model
provider_unavailable
invalid_arguments
timeout
agent_no_changes
verification_failed
git_policy_violation
commit_failed
environment_error
permission_denied
transport_failed
unknown_agent_failure
```

The scheduler should apply different policies to each category.

Examples:

```text
unsupported_model
→ do not retry same provider/model

authentication_error
→ stop and ask user

timeout
→ retry once or escalate model/provider

agent_no_changes
→ retry with clarified task or alternate provider

verification_failed
→ send verification output back for correction

permission_denied
→ return to user, never route around denial
```

Blind retries should be avoided.

---

# End-to-end MVP

The first real MVP should complete this workflow:

```text
User
  |
  v
Coordinator
  |
  v
Create task plan
  |
  v
Create integration branch
  |
  v
Assign independent subtasks
  |
  +------> Codex worker
  |
  +------> Claude worker
  |
  +------> Copilot worker
  |
  v
Receive acknowledgments/results
  |
  v
Independent review
  |
  v
Verification
  |
  v
Merge approved subtask branches
  |
  v
Full integration test
  |
  v
Present result to user
  |
  v
User approves merge/push
```

A suitable disposable MVP job might contain three independent tasks:

```text
T1: implement multiply()
T2: implement divide()
T3: write unit tests
```

The orchestrator should decide which agents execute the tasks, create isolated workspaces, collect the results, cross-review where appropriate, and integrate them.

---

# Codex interactive-session messaging

The verified `codex queue` mechanism is important and should be treated as a first-class transport.

The design should distinguish between:

### Managed Codex worker

Launched by the orchestrator:

```text
orchestrator
    |
    +--> codex exec
```

The orchestrator controls its complete lifecycle.

### Existing interactive Codex session

Already open in VS Code:

```text
orchestrator
    |
    +--> discover thread by exact cwd
    |
    +--> codex queue
    |
    +--> mailbox pointer
```

The queued message should contain only a mailbox notification, for example:

```text
Check the agent mailbox: .multi-agent-ai/messages/abc123/request.md
```

The actual request remains in the shared mailbox.

A successful queue submission means:

```text
notification submitted
```

not:

```text
message received
```

Receipt is confirmed only when Codex writes its acknowledgment.

This separation should remain strict for every provider.

---

# Scheduler design

Once the end-to-end workflow works reliably, implement a deterministic scheduler.

The scheduler should initially use simple rules rather than AI calls.

Possible inputs:

```text
task complexity
required capabilities
estimated context size
provider reachability
provider availability
historical success rate
historical verification rate
cost
latency
current workload
```

Possible scoring model:

```text
score =
    capability_match
  + reliability_weight
  + availability_weight
  - cost_weight
  - latency_weight
  - retry_risk
```

Exact weights should remain configurable.

The scheduler should not use historical data until enough real execution evidence exists.

---

# Token and cost optimization

The optimization target should be:

> **minimum expected cost of a successfully verified task**

not:

> cheapest model per request

For example, a cheap model that needs three retries may cost more than a stronger model that succeeds once.

Track:

```text
provider
model
task type
complexity
input tokens
output tokens
estimated/actual cost
duration
outcome
verification result
review result
retry count
```

Measured values and estimated values must be clearly distinguished.

If a CLI does not report token usage, store:

```text
usage: unknown
```

rather than inventing an estimate unless an explicit estimation mechanism is enabled.

---

# Reviews

Cross-agent review was one of the most successful parts of the Epesi experiments and should remain part of the architecture.

Recommended default:

```text
low-risk trivial task
→ review may be waived

normal code task
→ independent reviewer

high-risk/shared-interface task
→ independent reviewer + integration verification
```

Prefer a different provider for review when practical.

Example:

```text
Codex implements
Claude reviews
```

or:

```text
Claude implements
Copilot reviews
```

The reviewer should identify the exact commit reviewed.

---

# Environment preflight

The Epesi experience showed that a broken environment can hide real defects.

Before running workers or accepting verification, support project-specific preflight checks.

For a PHP/Laravel project, this may include:

```text
PHP executable/version
Composer executable/version
vendor directory
Node/npm version
built frontend assets
.env availability
database configuration
schema state
Premium modules
nested Git repositories
test command
```

For Python:

```text
Python version
virtual environment
dependencies
pytest availability
```

Preflight failures should be classified as:

```text
environment_error
```

and should not count against an AI worker's reliability score.

---

# Live checkout protection

This must be a hard safety feature.

The orchestrator should allow project profiles to declare:

```text
protected branches
live checkout paths
nested repositories
deployment directories
```

Workers should never modify a checkout that serves production traffic.

Before execution:

```text
if worktree == primary_live_checkout:
    refuse
```

Before deployment:

```text
require clean verified integration artifact
require explicit user approval
```

Deployment must never accidentally include another agent's uncommitted work.

---

# VS Code integration

Do not build the VS Code extension before the execution engine is reliable.

The eventual VS Code UI should be a thin interface over the Python service.

Suggested views:

```text
MULTI-AGENT AI

Current job
  Feature XYZ

Coordinator
  Claude / active

Workers
  Codex    T1    working
  Claude   T2    review
  Copilot  T3    waiting

Messages
  2 pending

Verification
  7/8 passed

Approvals
  Merge integration
```

Suggested commands:

```text
Multi-Agent AI: New Job
Multi-Agent AI: Show Task Board
Multi-Agent AI: Show Agents
Multi-Agent AI: Show Messages
Multi-Agent AI: Show Run Logs
Multi-Agent AI: Approve Integration
Multi-Agent AI: Cancel Job
```

The extension should not duplicate orchestration state.

The Python service remains authoritative.

---

# Orka influence

Continue using Orka as an architectural reference where useful, particularly for:

- parallel workers
- adaptive assignment
- performance history
- worktree isolation
- independent review
- acceptance-test verification

Do not port Orka's Bash implementation directly.

Multi-Agent-AI should remain:

```text
Windows-native
Python-based
VS Code-first
provider-neutral
filesystem-protocol compatible
```

Any reused code must be checked against its license.

---

# Architectural principle

Keep the following separation:

```text
AI Coordinator
    decides what should be done

Python Orchestrator
    decides how execution is safely carried out

AI Workers
    implement/review tasks

Verification Engine
    determines whether results actually satisfy requirements

User
    controls privileged actions
```

The coordinator must not directly bypass:

```text
Git policy
file reservations
permission rules
approval gates
verification
```

The service is the enforcement layer.

---

# What not to build yet

Delay these until the real three-provider workflow works:

- sophisticated ML-based routing
- automatic model benchmarking
- complex cost prediction
- cloud service architecture
- web dashboard
- distributed workers across machines
- long-running autonomous background agents
- deployment automation beyond explicit approval hooks

These features depend on a stable execution engine.

---

# Recommended immediate milestone

The next milestone should be called something like:

## M1 — Real Multi-Agent Execution

Acceptance criteria:

- [ ] registry implemented
- [ ] reservation checks implemented
- [ ] worktree manager implemented
- [ ] Claude worker adapter implemented
- [ ] Codex real smoke test passes
- [ ] Copilot real smoke test passes
- [ ] Claude real smoke test passes
- [ ] orchestrator independently verifies changes
- [ ] one task executes two or more workers concurrently
- [ ] cross-agent review works
- [ ] dependency-ordered integration works
- [ ] complete suite runs on integration branch
- [ ] no modification of `main` without user approval
- [ ] complete run report stored
- [ ] failed workers leave enough evidence for diagnosis

Only after M1 should development move to:

## M2 — Adaptive Routing and Cost Optimization

followed by:

## M3 — VS Code UX

---

# Summary

The project is now mature enough that the priority should shift from defining architecture to validating the complete runtime.

The most important next objective is:

> **A real Windows-native run in which one coordinator delegates work to multiple real Claude, Codex, and Copilot workers, receives and reviews their work, independently verifies it, integrates it into an isolated branch, and asks the user for approval before modifying the user's branch.**

Once that works reliably, adaptive model routing and token optimization can be implemented using real execution data rather than assumptions.
