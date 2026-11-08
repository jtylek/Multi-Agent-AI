"""Append-only event log of Project AI Chat sessions, and live provider-output streaming.

Layout and line grammar: ``docs/protocol/formats.md`` section 10; decision D20. The log lives at
``<state-root>/chat/<session-id>/events.jsonl``. It records what the chat observed, never more:
``provider_started`` means a process was asked to start, ``reply_received`` means the chat got a
final answer. Live events are parsed from the provider's own output stream while it runs; a
provider that streams no structured events (Copilot in the chat) contributes only start/finish.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

CHAT_DIR = "chat"
EVENTS_FILE = "events.jsonl"
MAX_TEXT = 20_000
"""Message and reply text stored in the log is cut to this many characters."""
MAX_SUMMARY = 200
MAX_DETAIL = 2_000
TAIL_POLL_SECONDS = 0.2

EventSink = Callable[[str, dict[str, Any]], None]
"""``sink(kind, fields)``: receives one chat event; must be thread-safe."""


def new_session_id(moment: datetime | None = None) -> str:
    return (moment or datetime.now()).strftime("chat-%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def clip(text: str, limit: int, *, one_line: bool = False) -> str:
    text = " ".join(str(text).split()) if one_line else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ChatLog:
    """Thread-safe writer for one chat session. A write failure never breaks the chat; it is kept
    in :attr:`error` so the window can report that activity is no longer recorded."""

    def __init__(self, state_root: Path, session_id: str):
        self.session_id = session_id
        self.path = Path(state_root) / CHAT_DIR / session_id / EVENTS_FILE
        self.error: str | None = None
        self._seq = 0
        self._lock = threading.Lock()

    def record(self, kind: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "kind": kind, "session": self.session_id, **fields}
            line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "ab") as handle:
                    handle.write(line.encode("utf-8"))
                    handle.flush()
            except OSError as exc:
                self.error = f"chat activity is not being recorded: {exc}"
                return None
            return event


def _tool_input_summary(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("file_path", "path", "pattern", "command", "query", "url"):
            if isinstance(value.get(key), str):
                return value[key]
    return json.dumps(value, ensure_ascii=False) if value else ""


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def describe_claude_event(event: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(kind, summary, detail)`` for one ``claude -p --output-format=stream-json`` line."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return [("provider_session", f"session {event.get('session_id', '?')}, model {event.get('model', '?')}", "")]
    message = event.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if not isinstance(blocks, list):
        return []
    found = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if kind == "assistant" and block.get("type") == "tool_use":
            target = _tool_input_summary(block.get("input"))
            found.append(("tool_use", f"{block.get('name', '?')} {target}".strip(),
                          json.dumps(block.get("input"), ensure_ascii=False, indent=2)))
        elif kind == "assistant" and block.get("type") == "text" and str(block.get("text", "")).strip():
            found.append(("assistant_text", block["text"], block["text"]))
        elif kind == "assistant" and block.get("type") == "thinking" and str(block.get("thinking", "")).strip():
            found.append(("reasoning", block["thinking"], block["thinking"]))
        elif kind == "user" and block.get("type") == "tool_result":
            text = _tool_result_text(block.get("content"))
            state = "error" if block.get("is_error") else "ok"
            found.append(("tool_result", f"{state}: {text}" if text else state, text))
    return found


def describe_codex_event(event: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(kind, summary, detail)`` for one ``codex exec --json`` line."""
    kind = event.get("type")
    if kind == "thread.started":
        return [("provider_session", f"thread {event.get('thread_id', '?')}", "")]
    if kind in ("turn.failed", "error"):
        error = event.get("error")
        text = error.get("message") if isinstance(error, dict) else event.get("message")
        return [("provider_error", str(text or kind), json.dumps(event, ensure_ascii=False, indent=2))]
    item = event.get("item")
    if not isinstance(item, dict):
        return []
    item_type = item.get("type")
    detail = json.dumps(item, ensure_ascii=False, indent=2)
    if kind == "item.started" and item_type == "command_execution":
        return [("command", str(item.get("command", "?")), detail)]
    if kind != "item.completed":
        return []
    if item_type == "command_execution":
        return [("command_finished", f"exit {item.get('exit_code', '?')}: {item.get('command', '?')}", detail)]
    if item_type == "reasoning":
        return [("reasoning", str(item.get("text", "")), str(item.get("text", "")))]
    if item_type == "agent_message":
        return [("assistant_text", str(item.get("text", "")), str(item.get("text", "")))]
    if item_type == "file_change":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        paths = ", ".join(str(c.get("path", "?")) for c in changes if isinstance(c, dict))
        return [("file_change", paths or "file change", detail)]
    if item_type == "mcp_tool_call":
        return [("tool_use", f"{item.get('server', '?')}.{item.get('tool', '?')}", detail)]
    if item_type == "web_search":
        return [("web_search", str(item.get("query", "")), detail)]
    return []


def _first_str(holders: tuple[Any, ...], keys: tuple[str, ...]) -> str | None:
    for holder in holders:
        if isinstance(holder, dict):
            for key in keys:
                if isinstance(holder.get(key), str) and holder[key].strip():
                    return holder[key]
    return None


def describe_copilot_event(event: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(kind, summary, detail)`` for one ``copilot -p --output-format json`` line.

    Copilot's JSONL schema is not verified in this project (workers/copilot.py), so this is generic:
    streaming deltas are skipped, an event whose type mentions a tool becomes ``tool_use`` /
    ``tool_result``, an assistant message becomes ``assistant_text`` and anything else is shown as
    ``provider_event`` with its type, so the first real run reveals the actual event names."""
    kind = str(event.get("type", "")).strip()
    lowered = kind.lower()
    if not kind or "delta" in lowered:
        return []
    holders = (event.get("data"), event)
    detail = json.dumps(event, ensure_ascii=False, indent=2)
    if "tool" in lowered:
        name = _first_str(holders, ("toolName", "tool_name", "name", "tool")) or "?"
        args = next((h.get(k) for h in holders if isinstance(h, dict)
                     for k in ("arguments", "args", "input") if h.get(k)), None)
        target = _tool_input_summary(args) if args is not None else ""
        result_like = any(word in lowered for word in ("complete", "result", "end", "finish"))
        return [("tool_result" if result_like else "tool_use", f"{kind}: {name} {target}".strip(), detail)]
    if "assistant" in lowered and "message" in lowered:
        text = _first_str(holders, ("content", "text", "message")) or ""
        return [("assistant_text", text, text)] if text else []
    return [("provider_event", kind, detail)]


DESCRIBERS: dict[str, Callable[[dict[str, Any]], list[tuple[str, str, str]]]] = {
    "claude": describe_claude_event,
    "codex": describe_codex_event,
    "copilot": describe_copilot_event,
}


class StreamTail:
    """Follow a growing JSONL stdout file on a thread and call ``handle(event)`` per complete line.

    ``stop()`` reads whatever is left (the process has exited by then) and joins the thread. A line
    that is not a JSON object is skipped: providers may print log lines between events."""

    def __init__(self, path: Path, handle: Callable[[dict[str, Any]], None]):
        self.path = Path(path)
        self.handle = handle
        self._offset = 0
        self._pending = b""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "StreamTail":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        self._read(final=True)

    def _run(self) -> None:
        while not self._stop.wait(TAIL_POLL_SECONDS):
            self._read()

    def _read(self, final: bool = False) -> None:
        try:
            with open(self.path, "rb") as source:
                source.seek(self._offset)
                data = source.read()
        except OSError:
            return  # not created yet, or briefly locked
        self._offset += len(data)
        lines = (self._pending + data).split(b"\n")
        self._pending = b"" if final else lines.pop()
        for raw in lines:
            text = raw.decode("utf-8", "replace").strip()
            if not text.startswith("{"):
                continue
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict):
                try:
                    self.handle(value)
                except Exception:  # a display problem must not stop the provider call
                    pass
