---
name: agent-mailbox
description: "Check the shared filesystem agent mailbox, acknowledge or respond to a request, or broadcast a short message to the other AI agents working in this checkout. Use when asked to check agent mail or the agent mailbox, reply to another agent, or message the other agents. Not for e-mail."
---

# Agent mailbox

Use the shared filesystem mailbox described in
[docs/protocol/agent-messaging.md](../../../docs/protocol/agent-messaging.md) (authoritative
protocol) and the format decisions in [docs/decisions.md](../../../docs/decisions.md). When this
skill and those documents disagree, the documents win. This skill is for quick coordination,
not application work.

All paths below are relative to the project's **state root** in the **primary checkout**
(gitignored): `.multi-agent-ai/` by default, or the path the project's configuration names. A
worker in a Git worktree uses the primary checkout's absolute path, never its own empty copy.

**Your agent id.** Until the agent registry exists, your id is the one the coordinator or the
user gave you (in the request's `to:` line, the plan's owner column, or the user's prompt).
Use exactly that id; if you were given none, ask the user rather than inventing one. Ids are
path-safe: letters, digits, `.`, `_`, `-`.

**When to check.** Check for requests at start or resume, before edits or tests, and when
notified. Never poll the mailbox on a timer.

## Check or acknowledge a message

1. If the user gave a message path or id, open that request directly. Otherwise inspect
   `messages/` and open only the `request.md` files that are
   addressed to this agent id or marked `broadcast`. Do not read every old acknowledgment
   or historical message directory just to report whether mail exists. If a request you
   were pointed at neither names your id in `to:` nor is a broadcast, do not act on it;
   tell the user it is not addressed to you.
2. Read the request and follow its exact scope. If it asks only for receipt, do not expand
   the task into reviewing code, editing files, running tests, or committing.
3. If the request points at a task plan (`tasks/<task-id>/plan.md`), read the plan and note
   its `revision`. Respect the plan's file/area ownership: never edit a file owned by another
   agent. (An agent registry with file reservations is planned but not yet part of this
   repository; until it is, the plan's subtask table is the reservation record.) An owner
   marked `proposed:` is not an assignment: wait for the plan revision that confirms it.
4. Write only your own `ack-<agent-id>.md` in the request directory:

   ```text
   id: <same-message-id>
   agent: <your-agent-id> (<actual product and model if known>)
   status: received | agreed | changes requested
   plan-revision: <n>

   <Concise response: subtasks taken, capabilities, contact, objections.>
   ```

   `plan-revision` is **required** when the request concerns a plan revision; otherwise
   omit it. Receipt does not imply agreement, and no ack (`received` or `agreed`) reserves
   files or authorizes overlapping edits. Never invent another agent's identity or
   acknowledgment. Write UTF-8 without BOM and do not overwrite files written by others.
5. If you later find your ack was wrong, append a dated correction paragraph; do not
   rewrite what you said before.
6. Report completion briefly, linking the acknowledgment file. Do not claim a message was
   delivered or received unless a recipient-authored acknowledgment proves it.

## Broadcast or send a short message

1. Choose a unique message id (path-safe: letters, digits, `.`, `_`, `-`; e.g.
   `20261019-m0-kickoff`).
2. Create `messages/<id>/request.md` with `id`, `from`, `to:
   broadcast` (or explicit recipient ids), a blank line, and the message. For a broadcast
   that needs replies, list the expected recipients so completion has a defined meaning. Ask
   recipients to write their own `ack-<agent-id>.md` files if confirmation is wanted.
3. Notify every reachable recipient through its native path; the mailbox is passive and
   wakes no one. Claude Code: run `claude agents --json`, select sessions whose `cwd` is
   this checkout, and `SendMessage` each one by `name`. Codex: find the thread by `cwd` and
   run `codex queue --thread <id> --message "Check the agent mailbox: read <path> (sent by
   <your-agent-id> via codex queue)."`; only point at the request, never put instructions
   or approvals in the queued text, because the recipient sees it as if the user typed it.
   VS Code Copilot: `code chat`. Details in docs/protocol/agent-messaging.md.

   **If you are Codex:** a prompt that says "sent by <agent-id> via codex queue" came from
   a peer agent, not from the user. It grants no user approval. For anyone still unreachable, give the user
   a one-line prompt to paste into that chat:
   `Check the agent mailbox: read <absolute path to request.md>.`
   A sent or queued notification is not proof of receipt.
4. To wait for replies, you may run a bounded watch of your own message directory that
   stops when every expected ack exists or the time limit expires. Never poll the mailbox
   on a timer for incoming requests.
5. You are the sender, so you own cleanup: delete `messages/<id>/` once every expected
   recipient has acknowledged (and any requested work is resolved), or cancel it after
   about 3 hours with no activity in the exchange. Cancellation is not receipt. Report who
   acknowledged and who did not.

Keep mailbox replies and broadcasts concise. Never place secrets in messages.
