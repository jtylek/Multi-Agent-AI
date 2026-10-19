"""Read-only scan of a state root for ``inspect`` and ``index``.

Scanning never writes: no protocol file, no SQLite file, nothing else in the state root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .board import PLAN_FILE, check_status, read_plan, read_statuses, verification_warnings
from .grammar import id_error
from .mailbox import REQUEST_FILE, Agreement, evaluate_agreement, read_acks, read_request
from .models import Ack, Parsed, Plan, Request, Status


@dataclass
class MessageReport:
    message_id: str
    request: Parsed[Request]
    acks: dict[str, Parsed[Ack]]
    agreement: Agreement | None
    errors: list[str] = field(default_factory=list)


@dataclass
class StatusReport:
    agent_id: str
    parsed: Parsed[Status]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TaskReport:
    task_id: str
    plan: Parsed[Plan]
    statuses: list[StatusReport]
    errors: list[str] = field(default_factory=list)


@dataclass
class StateReport:
    root: Path
    messages: list[MessageReport]
    tasks: list[TaskReport]
    errors: list[str] = field(default_factory=list)


def _subdirs(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))


def scan_message(message_dir: Path) -> MessageReport:
    errors = []
    if error := id_error(message_dir.name, "message directory"):
        errors.append(error)
    if not (message_dir / REQUEST_FILE).is_file():
        return MessageReport(message_dir.name, Parsed(None, ["no request.md"]), read_acks(message_dir), None, errors)
    request = read_request(message_dir)
    acks = read_acks(message_dir)
    agreement = evaluate_agreement(request.value, acks) if request.ok and request.value else None
    return MessageReport(message_dir.name, request, acks, agreement, errors)


def scan_task(task_dir: Path, now: datetime) -> TaskReport:
    errors = []
    if error := id_error(task_dir.name, "task directory"):
        errors.append(error)
    plan = read_plan(task_dir) if (task_dir / PLAN_FILE).is_file() else Parsed(None, ["no plan.md"])
    statuses = []
    for agent, parsed in read_statuses(task_dir).items():
        report = StatusReport(agent, parsed)
        if parsed.value is not None:
            report.errors, report.warnings = check_status(parsed.value, plan.value, now)
            report.warnings += verification_warnings(parsed.value)
        statuses.append(report)
    return TaskReport(task_dir.name, plan, statuses, errors)


def scan(state_root: Path, now: datetime | None = None) -> StateReport:
    now = now or datetime.now()
    root = Path(state_root)
    errors = [] if root.is_dir() else [f"state root {root} does not exist"]
    return StateReport(
        root,
        [scan_message(d) for d in _subdirs(root / "messages")],
        [scan_task(d, now) for d in _subdirs(root / "tasks")],
        errors,
    )


# --- output ----------------------------------------------------------------------------


def _issues(prefix: str, errors: list[str], warnings: list[str]) -> list[str]:
    return [f"{prefix}ERROR: {e}" for e in errors] + [f"{prefix}warning: {w}" for w in warnings]


def format_text(report: StateReport) -> str:
    out = [f"State root: {report.root}"]
    out += _issues("", report.errors, [])
    out.append("")
    out.append(f"Messages ({len(report.messages)})")
    for m in report.messages:
        req = m.request.value
        if req is None:
            out.append(f"  {m.message_id}: unreadable request")
        else:
            revision = m.agreement.required_revision if m.agreement else None
            out.append(
                f"  {m.message_id}: from {req.sender} to {', '.join(req.to)}"
                + (f"; concerns plan revision {revision}" if revision else "")
            )
        out += _issues("    ", m.errors + m.request.errors, m.request.warnings)
        if m.agreement:
            for r in m.agreement.recipients + m.agreement.unaddressed:
                rev = f" (rev {r.plan_revision})" if r.plan_revision else ""
                note = "" if r.counts or r.state == "missing" else " [does not count]"
                detail = f": {r.detail}" if r.detail else ""
                out.append(f"    {r.agent_id}: {r.state}{rev}{note}{detail}")
            all_agreed = {True: "yes", False: "no", None: "not computable"}[m.agreement.all_agreed]
            out.append(f"    all agreed: {all_agreed}")
            out += _issues("    ", [], m.agreement.warnings)
        for agent, ack in m.acks.items():
            out += _issues(f"    ack-{agent}: ", [], ack.warnings)
    out.append("")
    out.append(f"Tasks ({len(report.tasks)})")
    for t in report.tasks:
        plan = t.plan.value
        if plan is None:
            out.append(f"  {t.task_id}: unreadable plan")
        else:
            out.append(f"  {t.task_id}: coordinator {plan.coordinator}, revision {plan.revision}, updated {plan.updated}")
            for s in plan.subtasks:
                out.append(f"    {s.id}: {s.state} (owner {s.owner}, reviewer {s.reviewer})")
        out += _issues("    ", t.errors + t.plan.errors, t.plan.warnings)
        for s in t.statuses:
            st = s.parsed.value
            if st is None:
                out.append(f"    status-{s.agent_id}: unreadable")
            else:
                out.append(
                    f"    status-{s.agent_id}: {st.state} on {st.subtask}, plan revision {st.plan_revision}, updated {st.updated}"
                )
            out += _issues("      ", s.parsed.errors + s.errors, s.parsed.warnings + s.warnings)
    return "\n".join(out) + "\n"


def _parsed(p: Parsed[Any]) -> dict[str, Any]:
    return {"valid": p.ok, "errors": p.errors, "warnings": p.warnings}


def to_dict(report: StateReport) -> dict[str, Any]:
    messages = []
    for m in report.messages:
        req = m.request.value
        item: dict[str, Any] = {"id": m.message_id, "request": _parsed(m.request), "errors": m.errors}
        if req:
            item.update({"from": req.sender, "to": list(req.to)})
        if m.agreement:
            item["required_revision"] = m.agreement.required_revision
            item["all_agreed"] = m.agreement.all_agreed
            item["agreement_warnings"] = m.agreement.warnings
            item["recipients"] = [vars(r) for r in m.agreement.recipients]
            item["unaddressed"] = [vars(r) for r in m.agreement.unaddressed]
        messages.append(item)
    tasks = []
    for t in report.tasks:
        plan = t.plan.value
        item = {"id": t.task_id, "plan": _parsed(t.plan), "errors": t.errors}
        if plan:
            item.update({
                "coordinator": plan.coordinator,
                "revision": plan.revision,
                "updated": plan.updated,
                "subtasks": [{"id": s.id, "state": s.state, "owner": s.owner, "reviewer": s.reviewer} for s in plan.subtasks],
            })
        item["statuses"] = [
            {
                "agent": s.agent_id,
                **_parsed(s.parsed),
                "checks": {"errors": s.errors, "warnings": s.warnings},
                **({"state": s.parsed.value.state, "subtask": s.parsed.value.subtask,
                    "plan_revision": s.parsed.value.plan_revision, "updated": s.parsed.value.updated}
                   if s.parsed.value else {}),
            }
            for s in t.statuses
        ]
        tasks.append(item)
    return {"state_root": str(report.root), "errors": report.errors, "messages": messages, "tasks": tasks}


def index_entries(report: StateReport) -> tuple[list[tuple[Path, str, bool, dict[str, Any]]], dict[str, int]]:
    """Rows for the derived SQLite index, built from a scan."""
    entries: list[tuple[Path, str, bool, dict[str, Any]]] = []
    for m in report.messages:
        mdir = report.root / "messages" / m.message_id
        if (mdir / REQUEST_FILE).is_file():
            entries.append((mdir / REQUEST_FILE, "request", m.request.ok, {"message": m.message_id}))
        for agent, ack in m.acks.items():
            summary = {"message": m.message_id, "agent": agent,
                       "status": ack.value.status if ack.value else None}
            entries.append((mdir / f"ack-{agent}.md", "ack", ack.ok, summary))
    revisions: dict[str, int] = {}
    for t in report.tasks:
        tdir = report.root / "tasks" / t.task_id
        if (tdir / PLAN_FILE).is_file():
            entries.append((tdir / PLAN_FILE, "plan", t.plan.ok, {"task": t.task_id}))
            if t.plan.value and t.plan.value.revision:
                revisions[t.task_id] = t.plan.value.revision
        for s in t.statuses:
            entries.append((tdir / f"status-{s.agent_id}.md", "status", s.parsed.ok, {"task": t.task_id, "agent": s.agent_id}))
    return entries, revisions
