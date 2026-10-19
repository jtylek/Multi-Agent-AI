# Agent messaging protocol — initial integration notes

This document captures the protocol concepts supplied for Multi-Agent AI. It is an **integration summary**, not a verbatim copy of the original `agent-messaging.md`. The complete source should be imported and versioned separately before implementing protocol compatibility.

## Durable mailbox

The source protocol stores state in the target project's primary checkout under `storage/framework/agents/`:

```text
messages/<id>/request.md
messages/<id>/ack-<recipient-agent-id>.md
tasks/<task-id>/plan.md
tasks/<task-id>/status-<agent-id>.md
tasks/<task-id>/review-<subtask>-<agent-id>.md
tasks/<task-id>/notes/<agent-id>-<topic>.md
```

Sender writes `request.md` with `id`, `from`, and explicit `to` registry agent IDs. Each recipient writes only its own acknowledgment with `id`, `agent`, and status `received`, `agreed`, or `changes requested`. Receipt and agreement are different. A mailbox record is durable but does not notify a session by itself. Keep secrets out of messages.

## Coordinator and worker responsibilities

Exactly one coordinator per joint task owns `plan.md`, assignments, revisions, integration, full-suite verification, and the final report. Workers own their status and subtask branches. Each file has a single writer. Plans contain acceptance criteria, base commit, integration branch, dependencies, owners, reviewers, and revision numbers. A recipient's acknowledgment does not grant a file reservation. A worktree worker must use the primary checkout's absolute task-board path.

Use a different agent as reviewer where warranted; the reviewer names the exact commit. Integration is dependency-ordered, and pushing or merging into the user's working branch requires approval. Do not bypass user or agent permission denials by rerouting.

## Notification channels

- **Claude:** when available, discover existing sessions with `claude agents --json`, verify matching `cwd`, and use `ListAgents`/`SendMessage` or a constrained CLI relay. Transport success is not acknowledgment.
- **VS Code Copilot:** where supported, `code chat --reuse-window --mode agent --add-file <absolute-request-path> ...` can submit a request; it does not guarantee which conversation receives it.
- **Codex:** `send_input` only reaches agents in the same reachable Codex-managed hierarchy. It cannot push to arbitrary independent top-level Codex chats. Use a managed worker invocation or mailbox plus a user prompt when necessary.
- **Fallback:** ask the user to tell an existing session: `Check the agent mailbox: read storage/framework/agents/messages/<id>/request.md.`

Do not fake delivery by opening a different chat or manipulating private session files.

## Lifecycle and failure handling

Kickoff → join/acknowledge → assignment → work → review → integration/full tests → close. Track task plan revisions and worker status updates. Use bounded watches for active outgoing exchanges rather than indiscriminate incoming polling. Sender owns mailbox cleanup; abandoned exchanges require careful timeout and handoff handling. A stale worker may be reassigned only via a revised plan, without appropriating uncommitted work.

## Implementation notes

The orchestrator should support a configurable mailbox root so Epesi's existing layout works unchanged while other repositories can choose a different path. Before claiming full compatibility, import the original protocol and test the exact request/ack formats, registry rules, session discovery, and permission behavior.
