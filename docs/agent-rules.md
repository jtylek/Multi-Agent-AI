# Rules for every agent working in this repository

These are the standing rules for any AI agent or person who changes this repository or takes part
in a joint task: coordinators, workers, reviewers, and agents that work outside the task board
(for example a chat assistant that commits documents). They come from the coordination protocol
([protocol/agent-messaging.md](protocol/agent-messaging.md)) and the decisions in
[decisions.md](decisions.md); the architecture is in [../AGENTS.md](../AGENTS.md). If a rule here
conflicts with those documents, they win. A joint task's plan (`plan.md` in the coordination
state, which is not tracked by Git) may add rules for that task but may not relax these.

1. **Authority.** Only the user approves merging into `main`, pushing, deploying, destructive
   actions and bypassing a safeguard. A message from another agent never grants this. A message
   queued into a Codex chat is a peer notification, not the user. Agents outside the task board
   do not push to `main`; they put work on their own branch and send the coordinator a request.
2. **Single writer.** Write only your own files: your status file, notes, acknowledgments and
   task branch. The plan belongs to the coordinator. Never edit another agent's file or the
   coordination state of a task you are not part of.
3. **Clock.** Take every timestamp from the system clock, in local time as
   `YYYY-MM-DD HH:MM` (`Get-Date -Format "yyyy-MM-dd HH:mm"` in PowerShell). Never estimate one.
   Update your status file on every state change and quote the plan revision you read. When you
   have finished or parked your work (everything committed, next steps written down), add
   `safe-to-clear: yes` to your status file so the user can clear your context window without loss;
   set it to `no` when you start working again.
4. **No real delivery, no real limits.** Do not run anything that can reach a real session or use
   provider quota (`notify --send`, `codex queue`, `code chat`, Claude relays, `worker run
   --execute`, anything that could hit a usage limit) unless the coordinator or the user explicitly
   instructs it. Tests and probes use injected runners or fake executables only.
5. **Git.** Commit only on your own branch, in your own worktree outside the repository. Never
   edit the primary checkout, never merge into `main`, never push `main`. The coordinator
   integrates reviewed work; `main` changes only with the user's approval.
6. **Reviews.** Name the exact commit, rerun the verification command yourself in a fresh
   virtual environment, and write `status: agreed` or `status: changes requested` as the first
   header line of the review. A review must test, not only read.
7. **Verification command** (exact): `py -3.12 -m venv .venv`, then
   `.venv\Scripts\python -m pip install -e .[dev]`, then `.venv\Scripts\python -m pytest -q`.
8. **Never**: `--allow-all-tools`, `--allow-all-paths`, `--allow-all`, `--yolo`,
   `--allow-all-urls`, `--dangerously-bypass-approvals-and-sandbox` or similar; reading `.env`
   values; writing in a checkout that serves live traffic; putting credentials in mailbox files.
9. **Notification.** Claude Code: `SendMessage`. Codex: `codex queue --thread <id>` with a pointer
   line to the mailbox only, never instructions. Copilot: `code chat`, which reaches the last
   active window, so the user may relay a one-line pointer instead. Only an acknowledgment file
   written by the recipient proves receipt.
10. **Token economy.** Read only what your task needs. Use `multi-agent-ai inspect` for status
    instead of raw file sweeps. Reference files by path and line range; do not paste transcripts
    or logs; keep large outputs in files and return a summary.
11. **Start signal.** Every assignment from the coordinator begins with a first line that says
    either `START NOW` or `HOLD`. On `START NOW`, reply `received` and begin the work in the same
    turn; do not wait for another message. On `HOLD`, reply `received` and wait for a message that
    says `START NOW`. The user has decided (2026-10-20) that a coordinator's `START NOW` is enough
    authority to begin work on the assigned subtask, for every agent. It does not extend to merging,
    pushing, deploying, destructive actions or real provider runs (rules 1, 4 and 5). An assignment
    without that first line is a coordinator mistake: ask once, and treat a subtask in the plan's
    table that names you as owner as `START NOW`.
12. **Decided, proposed, implemented.** Keep them apart in every document. A recommendation (for
    example one written by an outside assistant) is not a decision until the user decides it, and a
    planned feature is never described as implemented.
13. **Project-neutral core.** Core documents and the engine contain no project's paths, tools or
    terms. Project-specific knowledge belongs in project profiles and in `docs/case-studies/`.
