# Problems encountered and recommendations

Written 2026-10-20 by the coordinator (claude-a7) at the end of the M1 session, when the user put the
project on hold. This records what went wrong in practice and what is recommended. Nothing here is a
decision: recommendations are **proposed**, not decided or implemented. Decisions belong in
[decisions.md](decisions.md); the implemented state is in [implementation-status.md](implementation-status.md).

## Project status: on hold

The user put the project on hold on 2026-10-20 because it takes too much of their time, and will return
to it later. State at that point:

- `main` = `origin/main` = 40b0786 (T18-T29, T23, T24, T27 job driver, T28, T32 and safe-to-clear merged).
  The last full suite on a merged tree before T32 was 564 passed; T32 was tested on its own branch at
  567 passed (Codex and claude-50, fresh venvs).
- **Not merged, still open:**
  - T30 (faster tests, claude-50, branch `task/m1-real-execution/T30`, 16d0e16): the review by Copilot was
    requested and acknowledged but never written.
  - T31 (demo job generator, Copilot, branch `task/m1-real-execution/T31`, 035032a): claude-50 reviewed it
    and asked for changes (printed `worker run` line uses the worktree as the project root; owners do not
    map to providers; hard-coded `--provider copilot`; commit inherits the user's git config; hard-coded
    `C:/AI` guard; no cleanup on failure). Copilot acknowledged the fix request but made no changes.
    The review is in the coordination board (`.multi-agent-ai/tasks/m1-real-execution/review-T31-claude-50.md`,
    gitignored).
- **Not approved or not done:** the second real Copilot run (one run was approved, 1 premium request; it
  needs the fixed T31 fixture first and a fresh approval if anything changed); real Codex and Claude runs;
  the README status update (see "README is behind the code" below).
- **Known non-blocking T27 notes:** N1 cancel does not reach the final suite; N2 `--retry` on a crash-stale
  subtask needs two resumes; N3 `Heartbeat.beat` has no ownership check after a forced takeover. Other known
  limits are in D19 and implementation-status.
- **Loose ends on disk:** old worktrees under `C:\AI\wt` (`int4`, `safeclear`, `T27-q12`, `T27-stageb`, and the
  T27, T30 and T31 branch worktrees) are idle or merged and can be removed once their owners agree. The
  machine was deliberately left awake; nothing was put to sleep.

To resume: read `.multi-agent-ai/tasks/m1-real-execution/coordinator-handoff.md` (may be out of date),
`docs/next-milestone.md`, and this file, then decide how the idle-agent problem below is handled before
assigning work to interactive sessions again.

## Problems encountered

### 1. Interactive agents fall idle and cannot be woken

The most costly problem of the session. An interactive agent (a Claude Code window, a VS Code Codex or
Copilot chat) only acts when something is typed into that window. SendMessage to an idle Claude session,
`codex queue` and `code chat` all report success (or no error) without waking a session that is not
processing. Observed:

- claude-50 and Copilot sat idle for hours with assignments acknowledged or delivered; the work only moved
  when the user typed in their windows.
- Copilot acknowledged the T30 review and the T31 fixes at about 03:00 and then produced nothing for about
  14 hours, including after a progress check and a second `code chat` pointer.
- Codex was reachable only intermittently: `codex queue` failed once ("cannot queue through an embedded app
  server while a local app-server daemon is running") and worked later; the user had to paste a pointer into
  the Codex chat by hand.

Delivery, acknowledgement and work are separate states (AGENTS.md section 7); this session confirmed they
really do diverge. An acknowledgement proves receipt, not that work will happen.

### 2. No stall detection

Nothing flagged an assignment that had been acknowledged but then gone quiet. The coordinator noticed only by
comparing file timestamps by hand, hours late. The board watch (`tools/watch_board.py`) reports changes; it
cannot report the absence of change, and its monitor expires every 30 minutes and must be re-armed.

### 3. Status files go stale

`status-claude-d1.md` kept reporting `last-commit: 847b024` while the branch tip moved to 5a309ee and 2af3c20,
and `status-claude-50.md` kept an old T28 SHA. Agents did not update their own status file after new commits.
The coordinator had to check git directly. This repeats the field-trial lesson that status must reflect reality.

### 4. Reviews are slow and queue behind each other

Fresh-venv full-suite runs took 17-28 minutes each, longer under load (Windows Defender scanning worktrees,
several suites running at once). A single change went through up to four reviewer suites plus the
integration suite. A Defender exclusion for `C:\AI\wt` and the T25 temp directory was added during the
session, but its effect was not measured.

### 5. Review rounds were repeated because of incomplete first passes and scope drift

T27 needed several rounds: Codex's `integration_started` finding, claude-50's B1-B8 and C1-C5 findings, then
Codex's Stage C blocker about retry resetting unrelated blocked subtasks. An early reading of Codex's
"agreed at 5a309ee" entry as clearing Stage C was wrong; its later Stage C section still had an open blocker
that was still present in the code. Lesson: read every section of a review file, not only the entry for the
newest commit, and confirm each blocker against the code at the tip being merged.

### 6. Copilot CLI on Windows

- The tool name `bash` does not exist on Windows (the first real run failed on it; T28 made the shell tool a
  profile field, default `powershell`, which is still a guess).
- The installed Copilot CLI resolves to an npm `.cmd` shim, which the runner refuses for prompts with quotes
  or newlines (`invalid_arguments`, a T12 rule). Any job that picks Copilot as author or reviewer halts at its
  first launch until that is solved. Documented as a known limit (T27 review B6); not fixed.

### 7. Coordination mechanics are fragile on Windows

- `git push` writes progress to stderr, so PowerShell shows NativeCommandError even on success; check the
  `main -> main` line.
- PowerShell `-like` treats backtick as an escape; `[regex]::Replace` with a script block silently did
  nothing once and conflict markers were committed to a local branch (reset before anything was shared).
- Review worktrees and venvs pile up and slow Defender; owners must remove their own, and the coordinator
  cannot safely remove another agent's.
- Process-count and timing tests are load-sensitive.

### 8. README is behind the code

`README.md` still says "Early design / bootstrap" and that agent execution remains to be implemented. Managed
workers, the workspace manager, the verification engine, the provider adapters and the job driver exist on
`main`. The design sections are still accurate; the status banner, "Current implementation" and the quick start
need a targeted update (not a full rewrite) once real runs have been shown to work.

## Recommendations (proposed, not decided)

1. **Prefer managed workers over idle interactive sessions for anything time-sensitive.** The job driver
   launches workers as processes it owns, so nobody has to wake a window. Real provider runs still need the
   user's approval each time, which is the trade-off. This is the main structural answer to problem 1.
2. **Add stall detection** to the job driver and the board watch: an assignment with an acknowledgement but no
   status update, commit or review within a configured time (suggest 20 minutes) is reported as `stale worker`
   (already a failure class in AGENTS.md section 18) with the one-line manual relay for the user. Include a
   watch that also reports silence, not only changes, and that does not need re-arming every 30 minutes.
3. **Check reachability before assigning time-sensitive work**: `ListAgents` busy/idle for Claude sessions, an
   acknowledgement within a few minutes for Codex and Copilot. If an agent is unreachable, say so at once
   instead of queueing work behind it.
4. **Coordinator rule:** when an agent has not acknowledged or moved within a stated window, tell the user
   immediately with the paste-ready line; do not keep waiting silently.
5. **Require agents to update their status file on every commit** (and have the board watch print the real git
   tip next to the reported one), so stale status is visible.
6. **Reduce review cost**: keep T30's faster test suite (once reviewed), measure the effect of the Defender
   exclusion, run reviewer suites one at a time where load matters, and keep one full suite on the merged tree
   as the gate rather than repeating the same suite per reviewer where the tree is identical.
7. **Resolve the Copilot Windows launch problem** (npm `.cmd` shim, shell tool name) before relying on Copilot
   as a managed worker; until then use Copilot as an interactive reviewer only.
8. **Update the README status** in a small, single-owner change once a real end-to-end run has worked.
9. **Review checklist for the coordinator:** read every section of each review file; confirm each blocker
   against the code at the exact tip; merge only a tree whose full suite ran after merging with current `main`;
   merge and push only with the user's approval for that task.
