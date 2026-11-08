"""Read-only, near-real-time view of mailbox, task board and job events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .job import read_events


CHAT_EVENTS = {
    "session_started": "Chat session started",
    "message_sent": "Chat message sent",
    "provider_started": "Agent call started",
    "provider_session": "Agent session",
    "tool_use": "Agent tool use",
    "tool_result": "Agent tool result",
    "command": "Agent command started",
    "command_finished": "Agent command finished",
    "reasoning": "Agent reasoning",
    "assistant_text": "Agent message",
    "file_change": "Agent file change",
    "web_search": "Agent web search",
    "provider_error": "Agent reported error",
    "provider_finished": "Agent call finished",
    "reply_received": "Chat reply received",
    "chat_error": "Chat error",
    "cancel_requested": "Chat cancel requested",
    "session_closed": "Chat session closed",
    "provider_event": "Agent event",
    "team_task_started": "Team task started",
    "plan_received": "Coordinator plan received",
    "plan_fallback": "Coordinator plan unusable",
    "task_dispatched": "Subtask dispatched",
    "subtask_completed": "Subtask answer received",
    "subtask_failed": "Subtask failed",
    "integration_started": "Coordinator integrating",
    "result_saved": "Team result saved",
    "team_task_finished": "Team task finished",
}
"""Live Activity labels for ``<state-root>/chat/*/events.jsonl`` kinds (formats.md section 10)."""


@dataclass(frozen=True)
class Activity:
    key: str
    at: str
    source: str
    event: str
    summary: str
    detail: str


def _headers(content: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in content.lstrip("\ufeff").splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep:
            headers.setdefault(key.strip(), value.strip())
    return headers


class ActivityWatcher:
    """Return newly observed evidence; an event never implies delivery or agreement."""

    def __init__(self, state_root: Path):
        self.root = Path(state_root)
        self._files: dict[Path, tuple[int, int]] = {}
        self._log_seq: dict[Path, int] = {}

    def poll(self) -> list[Activity]:
        output: list[Activity] = []
        files = [*self.root.glob("messages/*/request.md"), *self.root.glob("messages/*/ack-*.md"),
                 *self.root.glob("tasks/*/status-*.md"), *self.root.glob("tasks/*/review-*.md")]
        for path in sorted(files):
            try:
                stat = path.stat()
                version = (stat.st_mtime_ns, stat.st_size)
                if self._files.get(path) == version:
                    continue
                content = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue  # an atomic replacement may race a poll
            self._files[path] = version
            headers = _headers(content)
            relative = path.relative_to(self.root).as_posix()
            if path.name == "request.md":
                event = "Request recorded"
                summary = f"{headers.get('from', headers.get('sender', '?'))} → {headers.get('to', '?')}"
            elif path.name.startswith("ack-"):
                event = "Acknowledgment recorded"
                summary = f"{headers.get('agent', path.stem[4:])}: {headers.get('status', '?')}"
            elif path.name.startswith("status-"):
                event = "Worker status updated"
                summary = f"{headers.get('agent', path.stem[7:])}: {headers.get('state', '?')}"
            else:
                event = "Review recorded"
                summary = f"{headers.get('agent', '?')}: {headers.get('status', '?')}"
            output.append(Activity(f"file:{relative}:{version}", datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                                   relative, event, summary, content[:20000]))

        for path, record in self._new_events("jobs/*/events.jsonl"):
            relative = path.relative_to(self.root).as_posix()
            summary = f"{record.get('subtask', record.get('subtask_id', record.get('task_id', path.parent.name)))}: {record['kind']}"
            output.append(Activity(f"job:{relative}:{record['seq']}", str(record.get("at", "")), relative,
                                   "Job event", summary, json.dumps(record, indent=2, ensure_ascii=False)))
        for path, record in self._new_events("chat/*/events.jsonl"):
            relative = path.relative_to(self.root).as_posix()
            kind = str(record["kind"])
            provider = record.get("provider")
            event = CHAT_EVENTS.get(kind, f"Chat: {kind}")
            if provider:
                event = f"{event} ({provider})"
            output.append(Activity(f"chat:{relative}:{record['seq']}", str(record.get("at", "")), relative,
                                   event, str(record.get("summary", "")),
                                   json.dumps(record, indent=2, ensure_ascii=False)))
        for path in sorted(self.root.glob("runs/*/result.json")):
            try:
                stat = path.stat()
                version = (stat.st_mtime_ns, stat.st_size)
                if self._files.get(path) == version:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            self._files[path] = version
            usage = record.get("usage_totals") or record.get("usage") or "unknown"
            relative = path.relative_to(self.root).as_posix()
            summary = f"{record.get('provider', '?')} {record.get('mode', '?')}: tokens {usage}"
            detail = {key: record.get(key) for key in ("provider", "model", "mode", "usage", "usage_totals",
                      "provider_reported_cost_usd", "duration_seconds", "failure_class", "commit_created")}
            output.append(Activity(f"result:{relative}:{version}", datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                                   relative, "Provider usage", summary, json.dumps(detail, indent=2, ensure_ascii=False)))
        return sorted(output, key=lambda item: (item.at, item.key))

    def _new_events(self, pattern: str):
        """``(path, event)`` for events with a ``seq`` above the last one seen in each JSONL log."""
        for path in sorted(self.root.glob(pattern)):
            events, _warnings = read_events(path)
            prior = self._log_seq.get(path, 0)
            if events and isinstance(events[-1].get("seq"), int) and events[-1]["seq"] < prior:
                prior = 0  # replaced log; show its current events
            for record in events:
                seq = record.get("seq")
                if isinstance(seq, int) and seq > prior:
                    yield path, record
            if events and isinstance(events[-1].get("seq"), int):
                self._log_seq[path] = events[-1]["seq"]
