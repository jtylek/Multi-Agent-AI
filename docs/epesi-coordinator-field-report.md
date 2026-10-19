# Field report: two multi-agent joint tasks in Epesi

**Source:** Coordinator's report from two completed joint tasks involving Claude Code sessions, Codex, and VS Code Copilot in a shared Laravel/Epesi checkout. This is an experience report, not a claim that the new orchestrator already implements these controls.

## Observed setup

- Two rounds with plan, packages, review, integration, and user-approved merge/deploy.
- First round: P1–P4 (four packages). Second: approximately F1–F10.
- One coordinator owned plan, assignments, messaging, overlap resolution, some reviews and implementation, full integration suite, and reporting.
- Shared request/ack mailbox; revisioned single-writer task board; agent registry; package branches/worktrees; local integration branch.

## Practices that worked

1. Plan revisions (up to revision 7) made stale reads detectable.
2. Single-writer ownership prevented most conflicts; shared documentation still required section ownership.
3. Cross-agent reviews detected defects missed by authors' tests: an import removed with a supposedly dead method, incorrect `whereKey()` usage on a plain query builder, missing job de-duplication, and Premium-module-dependent tests.
4. Measurements were accompanied by methods and comparable baselines; reviewers corrected misleading warm-instance comparisons.
5. Review assignments were rebalanced when a reviewer became unreachable.
6. Scope messages and file ownership lists helped identify duplicate work.
7. Peer messages did not grant approval; permission denials were escalated to the user.

## Failure modes encountered

| Failure mode | Observed impact | Orchestrator requirement |
| --- | --- | --- |
| Unreachable Codex / ambiguous Copilot delivery | Human had to prompt Codex; Copilot submission did not prove receipt | Track reachability, transport acceptance, recipient acknowledgment separately; offer manual handoff |
| Duplicate assignments | Two agents wrote overlapping design notes | Compare proposed scope with current plan and completed outputs before assignment |
| Shared documentation edits | Four agents edited the same document | Assign named sections or serialize changes; coordinator handles integration conflicts |
| Incomplete worktree assets | Mail tests failed on missing build output, hiding actual errors | Preflight environment and classify missing assets separately from product failures |
| PHP/Composer mismatch | Only PHP 8.5 met version threshold; Composer failed; artisan test lost required flags | Record executable paths/versions; use exact test commands and verify flag propagation |
| Scratch DB reset | Artisan loaded schema dump into seeded database | Isolate and protect test databases; require explicit destructive-test approval |
| Live checkout commits | Commit on master immediately affected live site and crashed it | Never perform package work on a branch serving live traffic |
| Nested Premium Git repositories | Ownership checks blocked commands | Discover nested repos and use scoped safe-directory overrides, not global changes |
| Duplicate migration names | Laravel silently skipped second migration | Check migration identity collisions across modules |
| Automatic formatter | Changed a file that must retain older PHP syntax | Respect per-file formatting/runtime constraints |
| Insufficient test coverage | Enum select regression only detected by Premium tests and comparison to baseline | Include dependent module tests and fair, version-matched baselines |
| Working-tree deployment | Unfinished uncommitted changes were deployed | Deploy only from a verified isolated artifact/checkout; check clean state and provenance |
| Lost deployment output via WSL | Partial logs obscured outcome | Verify deployment independently via production status and HTTP checks |
| Stale status files | Agent remained marked blocked after resolution | Detect stale status and require reconciliation |

## Derived acceptance criteria for Multi-Agent-AI

These are **proposed implementation requirements**, inferred from the field report:

- **R1:** Every joint task has exactly one coordinator and a monotonically revisioned plan. Only the coordinator edits the plan.
- **R2:** Every package has an owner, reviewer or explicit waiver, dependency list, acceptance criteria, and file/section ownership.
- **R3:** Each editing worker uses an isolated task branch; worktrees are the default. Shared registry and board live at a canonical primary-checkout location.
- **R4:** The scheduler refuses overlapping file reservations unless the coordinator explicitly resolves them.
- **R5:** Message state distinguishes written, notification submitted, acknowledged, agreed, and blocked. Never infer acknowledgment from CLI exit status.
- **R6:** Unreachable sessions are reported with a concrete manual-relay instruction; no attempts to take over unrelated chats.
- **R7:** Preflight verifies PHP, Composer, dependencies, frontend assets, module availability, DB isolation, and test commands before claiming verification is meaningful.
- **R8:** Reviews run in a representative environment and name the exact commit and tests inspected.
- **R9:** Integration occurs in dependency order on a dedicated local branch, followed by a full-suite gate.
- **R10:** Changes to user/live branches, remote pushes, and deployments require explicit user approval.
- **R11:** Deployments use a clean, verified artifact/checkout, not an arbitrary shared working tree.
- **R12:** Benchmarks state the method and compare matched environments; unavailable usage data is reported as unknown.
- **R13:** Status freshness, blocked state, and reviewer availability are reconciled before rescheduling.
- **R14:** Cross-module migrations, formatting exceptions, nested Git repositories, and Premium-module-dependent behavior are modeled in project configuration.

## Suggested implementation order

1. Durable task board and mailbox with revision/ack validation.
2. Windows-native agent/session registry and reachability probes.
3. Workspace/worktree isolation, file reservations, and live-branch protections.
4. Preflight environment checks and test command profiles (including Epesi).
5. Dependency-aware integration, cross-agent review, and approval gates.
6. Status reconciliation, performance telemetry, and adaptive cost-based routing.

## Scope of evidence

The source describes two rounds of work, not a controlled benchmark of routing quality, token savings, or agent success rates. No measured cost or throughput improvement can be inferred from it. These requirements should be validated with automated tests and real project trials.
