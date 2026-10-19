"""Typed records.

Protocol records (Request, Ack, Plan, Subtask, Status) mirror docs/protocol/formats.md
(format 1). Job, Task, AgentSession, Reservation, Attempt and Notification are plain
operational records for the derived SQLite store, where formats.md is silent (D3); none of
them can stand in for an acknowledgment or a completion (D8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from .grammar import format_timestamp, id_error

BROADCAST = "broadcast"
WAIVED = "waived"
TBD = "TBD"
PROPOSED_PREFIX = "proposed:"

ACK_STATUSES = ("received", "agreed", "changes requested")
PLAN_STATES = ("open", "assigned", "in progress", "blocked", "review", "done", "merged")
ACTIVE_PLAN_STATES = ("assigned", "in progress", "blocked", "review")
STARTED_PLAN_STATES = ("in progress", "review", "done", "merged")
WAITING_FOR_USER = "waiting for user"
RUNNING_TESTS_PREFIX = "running tests:"

T = TypeVar("T")


@dataclass
class Parsed(Generic[T]):
    """Result of reading one file: a value (possibly partial), errors and warnings.

    A file with errors is invalid and does not count (for example toward "all agreed").
    """

    value: T | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.errors


# --- protocol records (formats.md) -------------------------------------------------------


@dataclass
class Request:
    id: str
    sender: str
    to: tuple[str, ...]
    body: str
    plan_revision: int | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_broadcast(self) -> bool:
        return self.to == (BROADCAST,)


@dataclass
class Ack:
    message_id: str
    agent_id: str
    status: str
    product: str | None = None
    plan_revision: int | None = None
    body: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def agent_field(self) -> str:
        return f"{self.agent_id} ({self.product})" if self.product else self.agent_id


@dataclass
class Subtask:
    id: str
    what: str
    files: str
    depends_on: tuple[str, ...]
    worktree: bool
    owner: str
    reviewer: str
    state: str

    @property
    def owner_id(self) -> str | None:
        """The assigned owner, or None for ``TBD`` and ``proposed:`` owners."""
        if self.owner.upper() == TBD or self.owner.lower().startswith(PROPOSED_PREFIX):
            return None
        return self.owner

    @property
    def review_waived(self) -> bool:
        return self.reviewer.lower() == WAIVED


@dataclass
class Plan:
    task_id: str
    coordinator: str
    revision: int
    updated: str
    body: str = ""
    base_commit: str | None = None
    integration_branch: str | None = None
    subtasks: tuple[Subtask, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)

    def subtask(self, subtask_id: str) -> Subtask | None:
        return next((s for s in self.subtasks if s.id == subtask_id), None)


@dataclass
class Status:
    agent_id: str
    plan_revision: int
    subtask: str
    state: str
    last_commit: str
    tests: str
    blockers: str
    updated: str
    branch: str | None = None
    body: str = ""
    extra: dict[str, str] = field(default_factory=dict)


# --- operational records (SQLite only; formats.md is silent) ----------------------------


def _check_ids(**ids: str | None) -> None:
    for what, value in ids.items():
        if value is not None:
            error = id_error(value, what.replace("_", " "))
            if error:
                raise ValueError(error)


@dataclass(frozen=True)
class Job:
    id: str
    goal: str
    coordinator_id: str
    state: str = "planning"
    created: str = field(default_factory=format_timestamp)

    def __post_init__(self) -> None:
        _check_ids(job_id=self.id, coordinator_id=self.coordinator_id)


@dataclass(frozen=True)
class Task:
    """An orchestrator-side view of one plan subtask; the plan file stays authoritative."""

    id: str
    job_id: str
    title: str
    state: str = "open"
    plan_revision: int = 1
    owner_id: str | None = None
    reviewer_id: str | None = None
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_ids(task_id=self.id, job_id=self.job_id, owner_id=self.owner_id)
        if self.state not in PLAN_STATES:
            raise ValueError(f"task state {self.state!r} is not a plan state")


@dataclass(frozen=True)
class AgentSession:
    id: str
    provider: str
    model: str | None = None
    working_directory: str | None = None
    reachability_mode: str = "unknown"
    """Reserved provider-neutral field (D8); adapters fill it in the Windows-runtime milestone."""
    capabilities: tuple[str, ...] = ()
    current_task_id: str | None = None
    updated: str = field(default_factory=format_timestamp)

    def __post_init__(self) -> None:
        _check_ids(agent_id=self.id, current_task_id=self.current_task_id)


@dataclass(frozen=True)
class Reservation:
    id: str
    task_id: str
    agent_id: str
    files: str
    state: str = "active"
    created: str = field(default_factory=format_timestamp)

    def __post_init__(self) -> None:
        _check_ids(reservation_id=self.id, task_id=self.task_id, agent_id=self.agent_id)


@dataclass(frozen=True)
class Attempt:
    """One agent run. ``exit_code`` is evidence about the process only (D8)."""

    id: str
    task_id: str
    agent_id: str
    number: int = 1
    provider: str | None = None
    model: str | None = None
    started: str = field(default_factory=format_timestamp)
    finished: str | None = None
    exit_code: int | None = None
    failure_category: str | None = None
    files_changed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_ids(attempt_id=self.id, task_id=self.task_id, agent_id=self.agent_id)
        if self.number < 1:
            raise ValueError("attempt number must be at least 1")


@dataclass(frozen=True)
class Notification:
    """A notification attempt for a mailbox request. Transport success is not receipt (D8)."""

    id: str
    message_id: str
    recipient_id: str
    reachability_mode: str = "unknown"
    transport_result: str | None = None
    manual_relay_text: str | None = None
    submitted: str = field(default_factory=format_timestamp)

    def __post_init__(self) -> None:
        _check_ids(
            notification_id=self.id, message_id=self.message_id, recipient_id=self.recipient_id
        )


OPERATIONAL_RECORDS = (Job, Task, AgentSession, Reservation, Attempt, Notification)
