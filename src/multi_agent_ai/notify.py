"""Mailbox notification transports.

Notifications point recipients at a durable request. They never create acknowledgments or
task-completion state; only the recipient's mailbox file can do that (D8).
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Sequence

from .document import read_document
from .mailbox import read_request
from .reachability import resolve_executable
from .state import StateIndex
from .models import Notification


_CODEX_CONTACT = re.compile(r"^codex\s+queue\s+--thread\s+(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class NotifyResult:
    recipient: str
    method: str
    submitted: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _contact_files(state_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    candidates = list((state_root / "registry").glob("*.md")) if (state_root / "registry").is_dir() else []
    candidates.extend((state_root / "tasks").glob("*/status-*.md"))
    for path in candidates:
        try:
            document = read_document(path)
        except (OSError, ValueError):
            continue
        agent = document.get("agent")
        contact = document.get("contact")
        if agent and contact:
            result[agent] = contact
    return result


def _pointer(request_path: Path, sender: str, transport: str) -> str:
    return f"Check the agent mailbox: read {request_path} (sent by {sender} via {transport})."


def _run(command: Sequence[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False)


def _record_transport(state_root: Path, result: NotifyResult, message_id: str) -> None:
    """Persist transport telemetry only in the derived SQLite index."""
    database = state_root / "state.sqlite3"
    if not database.is_file():
        return
    try:
        with StateIndex.open(database) as index:
            index.save(Notification(
                id=f"notify-{uuid.uuid4().hex}",
                message_id=message_id,
                recipient_id=result.recipient,
                reachability_mode=result.method,
                transport_result="submitted" if result.submitted else ("manual" if result.method.startswith("manual") else "failed"),
                manual_relay_text=None if result.submitted else result.detail,
            ))
    except (OSError, sqlite3.Error):
        # Notification must not fail merely because the optional derived ledger is absent or
        # unavailable. The transport result is still printed; mailbox files remain untouched.
        return


def _send_codex(contact: str, request_path: Path, sender: str, cwd: Path, timeout: float,
                runner: Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]) -> NotifyResult:
    match = _CODEX_CONTACT.fullmatch(contact.strip())
    if not match:
        return NotifyResult("", "manual", False, f"{_pointer(request_path, sender, 'manual relay')} unsupported Codex contact: {contact}")
    thread = match.group(1).strip()
    if thread.startswith("-") or not thread:
        return NotifyResult("", "manual", False, f"{_pointer(request_path, sender, 'manual relay')} unsupported Codex contact: {contact}")
    pointer = _pointer(request_path, sender, "codex queue")
    try:
        executable = resolve_executable("codex")
        result = runner((executable, "queue", f"--thread={thread}", "--message", pointer), cwd, timeout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return NotifyResult("", "codex queue", False, str(exc))
    if result.returncode == 0:
        return NotifyResult("", "codex queue", True, (result.stdout or "").strip() or "submitted")
    return NotifyResult("", "codex queue", False, (result.stderr or result.stdout).strip() or f"exit {result.returncode}")


def _send_copilot(contact: str, request_path: Path, sender: str, cwd: Path, timeout: float,
                  runner: Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]) -> NotifyResult:
    pointer = _pointer(request_path, sender, "code chat")
    try:
        executable = resolve_executable("code")
        result = runner((executable, "chat", "--reuse-window", "--mode", "agent", "--add-file",
                         str(request_path), pointer), cwd, timeout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return NotifyResult("", "code chat", False, str(exc))
    if result.returncode == 0:
        return NotifyResult("", "code chat", True, (result.stdout or "").strip() or "submitted")
    return NotifyResult("", "code chat", False, (result.stderr or result.stdout).strip() or f"exit {result.returncode}")


def notify_message(
    state_root: Path,
    message_id: str,
    project_root: Path,
    *,
    timeout: float = 15,
    send: bool = False,
    runner: Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]] = _run,
) -> tuple[NotifyResult, ...]:
    message_dir = state_root / "messages" / message_id
    request = read_request(message_dir)
    if not request.ok or request.value is None:
        raise ValueError("cannot notify an unreadable request: " + "; ".join(request.errors))
    request_path = message_dir / "request.md"
    contacts = _contact_files(state_root)
    results: list[NotifyResult] = []
    for recipient in request.value.to:
        contact = contacts.get(recipient)
        if not contact:
            result = NotifyResult(
                recipient, "manual", False,
                _pointer(request_path, request.value.sender, "manual relay"),
            )
        elif _CODEX_CONTACT.fullmatch(contact.strip()):
            if send:
                result = _send_codex(contact, request_path, request.value.sender, project_root, timeout, runner)
            else:
                result = NotifyResult("", "dry-run", False, f"would send via {contact}: {_pointer(request_path, request.value.sender, 'codex queue')}")
            result = NotifyResult(recipient, result.method, result.submitted, result.detail)
        elif contact.strip().lower().startswith("code chat"):
            if send:
                result = _send_copilot(contact, request_path, request.value.sender, project_root, timeout, runner)
            else:
                result = NotifyResult("", "dry-run", False, f"would send via {contact}: {_pointer(request_path, request.value.sender, 'code chat')}")
            result = NotifyResult(recipient, result.method, result.submitted, result.detail)
        elif contact.strip().lower().startswith("claude "):
            target = contact.strip()[len("claude "):].strip()
            result = NotifyResult(
                recipient, "manual (claude)", False,
                f"{_pointer(request_path, request.value.sender, 'manual relay')} SendMessage target={target}",
            )
        else:
            result = NotifyResult(
                recipient, "manual", False,
                f"{_pointer(request_path, request.value.sender, 'manual relay')} unsupported contact target={contact}",
            )
        if send:
            _record_transport(state_root, result, message_id)
        results.append(result)
    return tuple(results)


def format_notify(results: Sequence[NotifyResult]) -> str:
    return "".join(f"{r.recipient}: {r.method}: {r.detail}\n" for r in results)
