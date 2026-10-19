# Getting started: using Multi-Agent AI today

**Status, 2026-10-19.** The orchestrator does not launch or schedule agents yet. What works
today:

- the **coordination protocol**, which agents follow by hand: mailbox, task board and reviews
  ([protocol/agent-messaging.md](protocol/agent-messaging.md));
- the **agent-mailbox skill**, which teaches Codex and Claude Code the protocol;
- the **`multi-agent-ai` command-line tool** (milestone M0, on branch
  `task/m0-foundation/T6` until it is merged), with three commands: `init` creates the state
  area, `inspect` shows messages, acks, plans and stale or invalid status (read-only), and
  `index` rebuilds the derived SQLite index.

So you are the dispatcher: you open the agent chats, a coordinator agent plans and messages,
and you relay to the agents that cannot be reached directly. Later milestones automate the
relaying, launching and verification.

## Where to open VS Code

**Open VS Code in the repository the agents are working on.** Agents use their working
directory as their checkout, Claude sessions find each other by working directory, and
`code chat` delivers to the active window.

| You want to | Open VS Code in |
| --- | --- |
| Improve Multi-Agent AI itself | `C:\AI\Multi-Agent-AI` |
| Have agents work on EpesiCRM | the Epesi **development** checkout (never one that serves a live site) |

Use one VS Code window per repository. The Multi-Agent AI repository does not need to be
open while agents work on Epesi; only its command-line tool needs to be installed.

## One-time setup

### 1. Install the tool (after T6 is merged)

```powershell
cd C:\AI\Multi-Agent-AI
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\python -m pytest -q        # all tests should pass
```

Run it as `C:\AI\Multi-Agent-AI\.venv\Scripts\multi-agent-ai.exe`, or add
`C:\AI\Multi-Agent-AI\.venv\Scripts` to your PATH.

### 2. Prepare a target project (example: Epesi)

In the target checkout:

1. Create the work branch for orchestrated work, e.g. `git switch -c ai-multiagent`.
   Agents create their task branches from it; the integration result comes back to it.
   Other branches are off-limits for agents.
2. Add `.multi-agent-ai/` to the project's `.gitignore`.
3. Run `multi-agent-ai init --project-root <checkout>`. This creates `.multi-agent-ai/`
   (messages, tasks, SQLite index).
4. Copy the skill: `.agents/skills/agent-mailbox/` and `.claude/skills/agent-mailbox/` from
   this repository into the project. Epesi has an older copy that uses
   `storage/framework/agents/`; replace it.
5. Make sure the project's `AGENTS.md` tells agents to follow the protocol and to use the
   state root `.multi-agent-ai/`.
6. Keep worktree environment files outside every Git checkout, e.g.
   `C:\AI\secrets\<project>\.env.worktree`, with test-only values. Never point agents at a
   live checkout's `.env`.

## Running a joint task

1. **Open the agent chats** in the project's VS Code window: one Claude Code session as
   coordinator, plus any of Codex, Copilot and further Claude sessions as workers.
2. **Give the job to the coordinator only**, e.g. *"You are the coordinator for this job:
   <goal>. Follow docs/protocol/agent-messaging.md. The other agents are: codex, copilot,
   claude-50."* The coordinator writes the plan and a kickoff request, then notifies:
   - other **Claude Code** sessions directly (automatic);
   - **Copilot** through `code chat` (automatic, but it reaches the last active chat);
   - **Codex** through you: the coordinator gives you a one-line message to paste.
3. **Workers acknowledge** by writing ack files; the coordinator assigns subtasks in the next
   plan revision.
4. **Watch progress** at any time:

   ```powershell
   multi-agent-ai inspect --project-root <checkout>
   ```

   It shows who acknowledged what, each subtask's state, stale status files and invalid
   files. It changes nothing.
5. **Answer decisions.** The coordinator asks you; agents cannot approve things for you.
6. **Integration.** Reviewed work is merged into a local integration branch and the full test
   suite runs there. Merging into your work branch, pushing and deploying happen only after
   you say so.

## Rules of thumb

- **Talk to the coordinator.** Telling a worker something directly bypasses the plan; ask
  the coordinator to put it in a new plan revision instead.
- **Paste exactly what the coordinator gives you** for Codex; a "Check the agent mailbox:
  ..." line is enough.
- **Agent ids are fixed per task** (`claude-a7`, `codex`, ...). Tell a new agent its id when
  it joins.
- **Don't edit `.multi-agent-ai/` files by hand**, except to read them. Each file has one
  writer.
- **Never give agents a live checkout.** Work in a development checkout on the work branch.

## Known limitations (planned work)

- Codex cannot be messaged directly yet. Being tested: `codex queue`, and launching Codex
  workers headless with `codex exec`.
- No file reservations or agent registry yet: ownership is the plan's task table.
- The tool does not launch agents, run tests, or merge branches yet; agents and you do that
  by hand.
- Project profiles (absolute tool paths, environments, test commands) are drafted, not
  implemented.
