"""Task board files: plan.md and status-<agent-id>.md (formats.md sections 4 and 5)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .document import (
    Document,
    DocumentError,
    OwnershipError,
    atomic_write,
    parse_text,
    read_document,
    render,
)
from .grammar import id_error, is_sha, is_stale, parse_positive_int, parse_timestamp
from .models import (
    ACTIVE_PLAN_STATES,
    PLAN_STATES,
    PROPOSED_PREFIX,
    RUNNING_TESTS_PREFIX,
    STARTED_PLAN_STATES,
    TBD,
    WAIVED,
    WAITING_FOR_USER,
    Parsed,
    Plan,
    Status,
    Subtask,
)

PLAN_FILE = "plan.md"
STATUS_PREFIX, STATUS_SUFFIX = "status-", ".md"
SUBTASK_COLUMNS = ("id", "what", "files/area", "depends on", "worktree", "owner", "reviewer", "state")
_SEPARATOR_CELL = re.compile(r":?-+:?")
_PLAN_KEYS = ("task", "coordinator", "revision", "updated", "base-commit", "integration-branch")
_STATUS_KEYS = (
    "plan-revision", "agent", "subtask", "state", "branch", "last-commit", "tests", "blockers", "updated",
)


def status_file_name(agent_id: str) -> str:
    return f"{STATUS_PREFIX}{agent_id}{STATUS_SUFFIX}"


def _timestamp(doc: Document, key: str, errors: list[str], warnings: list[str]) -> None:
    raw = doc.get(key)
    if raw is None:
        return
    try:
        if not parse_timestamp(raw).minute_precision:
            warnings.append(f"'{key}' {raw!r} has no time; staleness cannot be evaluated")
    except ValueError as exc:
        errors.append(str(exc))


# --- subtask table ---------------------------------------------------------------------


def _cells(line: str) -> list[str] | None:
    line = line.strip()
    if not line.startswith("|"):
        return None
    line = line[1:-1] if line.endswith("|") and len(line) > 1 else line[1:]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def _tables(body: str) -> list[tuple[list[str], list[list[str]]]]:
    tables, lines = [], body.split("\n")
    i = 0
    while i < len(lines):
        header = _cells(lines[i])
        separator = _cells(lines[i + 1]) if i + 1 < len(lines) else None
        if header and separator and all(_SEPARATOR_CELL.fullmatch(c) for c in separator):
            rows, i = [], i + 2
            while i < len(lines) and (row := _cells(lines[i])) is not None:
                rows.append(row)
                i += 1
            tables.append(([" ".join(c.lower().split()) for c in header], rows))
        else:
            i += 1
    return tables


def _parse_row(cells: dict[str, str], errors: list[str]) -> Subtask | None:
    sid = cells["id"]
    before = len(errors)
    if error := id_error(sid, "subtask id"):
        errors.append(error)
        return None
    depends = cells["depends on"]
    deps: tuple[str, ...] = ()
    if depends != "-":
        items = [d.strip() for d in depends.split(",")]
        if any(not d for d in items):
            errors.append(f"subtask {sid}: empty item in 'depends on'")
        deps = tuple(d for d in items if d)
        errors.extend(f"subtask {sid}: {e}" for d in deps if (e := id_error(d, "dependency")))
    worktree = cells["worktree"].lower()
    if worktree not in ("yes", "no"):
        errors.append(f"subtask {sid}: worktree must be yes or no, got {cells['worktree']!r}")
    owner = cells["owner"]
    if owner.upper() != TBD:
        owner_id = owner[len(PROPOSED_PREFIX):].strip() if owner.lower().startswith(PROPOSED_PREFIX) else owner
        if error := id_error(owner_id, f"subtask {sid}: owner"):
            errors.append(error)
    reviewer = cells["reviewer"]
    if reviewer.lower() != WAIVED and (error := id_error(reviewer, f"subtask {sid}: reviewer")):
        errors.append(error)
    state = " ".join(cells["state"].lower().split())
    if state not in PLAN_STATES:
        errors.append(f"subtask {sid}: state must be one of {', '.join(PLAN_STATES)}, got {cells['state']!r}")
    if len(errors) > before:
        return None
    return Subtask(sid, cells["what"], cells["files/area"], deps, worktree == "yes", owner, reviewer, state)


def parse_subtask_table(body: str) -> Parsed[tuple[Subtask, ...]]:
    errors: list[str] = []
    warnings: list[str] = []
    candidates = [t for t in _tables(body) if "id" in t[0] and len(set(t[0]) & set(SUBTASK_COLUMNS)) > 1]
    if not candidates:
        return Parsed(None, ["no subtask table found in the plan body"])
    if len(candidates) > 1:
        warnings.append("more than one subtask table; only the first is read")
    header, rows = candidates[0]
    missing = [c for c in SUBTASK_COLUMNS if c not in header]
    if missing:
        return Parsed(None, [f"subtask table is missing column(s): {', '.join(missing)}"], warnings)

    subtasks: list[Subtask] = []
    seen_ids: set[str] = set()
    for number, row in enumerate(rows, 1):
        if len(row) < len(header):
            errors.append(f"subtask table row {number} has {len(row)} cells, expected {len(header)}")
            continue
        cells = dict(zip(header, row))
        if cells["id"] in seen_ids:
            errors.append(f"subtask id {cells['id']!r} appears more than once")
            continue
        seen_ids.add(cells["id"])
        if (subtask := _parse_row(cells, errors)) is not None:
            subtasks.append(subtask)
    _check_semantics(subtasks, seen_ids, errors, warnings)
    return Parsed(tuple(subtasks), errors, warnings)


def _area_items(files: str) -> list[str]:
    items = []
    for item in re.sub(r"\([^)]*\)", "", files).split(","):
        item = item.strip().replace("\\", "/").lower().removeprefix("./").rstrip("/")
        if item and item != "-":
            items.append(item)
    return items


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _check_semantics(subtasks: list[Subtask], known: set[str], errors: list[str], warnings: list[str]) -> None:
    graph = {s.id: s.depends_on for s in subtasks}
    for s in subtasks:
        errors.extend(f"subtask {s.id}: unknown dependency '{d}'" for d in s.depends_on if d not in known)
        if not s.review_waived and s.owner_id is not None and s.reviewer == s.owner_id:
            errors.append(f"subtask {s.id}: reviewer equals owner '{s.owner_id}' (use 'waived')")
        if s.owner_id is None and s.state in STARTED_PLAN_STATES:
            errors.append(f"subtask {s.id}: owner '{s.owner}' is not an assignment but state is '{s.state}'")
        elif s.owner_id is None and s.state == "assigned":
            warnings.append(f"subtask {s.id}: state 'assigned' but owner '{s.owner}' is not an assignment")

    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, path: list[str]) -> None:
        if node in done or node not in graph:
            return
        if node in visiting:
            errors.append("dependency cycle: " + " -> ".join(path[path.index(node):] + [node]))
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep, path + [node])
        visiting.discard(node)
        done.add(node)

    for node in graph:
        visit(node, [])

    active = [s for s in subtasks if s.state in ACTIVE_PLAN_STATES]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            shared = sorted({
                v for x in _area_items(a.files) for y in _area_items(b.files) if _overlaps(x, y) for v in (x, y)
            })
            if shared:
                warnings.append(
                    f"active subtasks {a.id} and {b.id} overlap in files/area ({', '.join(shared)}); "
                    "allowed only if the coordinator resolved it"
                )


# --- plan ------------------------------------------------------------------------------


def parse_plan(doc: Document, directory: str | None = None) -> Parsed[Plan]:
    errors = [f"missing required key '{k}'" for k in ("task", "coordinator", "revision", "updated") if not doc.get(k)]
    warnings = list(doc.warnings)
    if errors:
        return Parsed(None, errors, warnings)
    task_id, coordinator = doc.headers["task"], doc.headers["coordinator"]
    if error := id_error(task_id, "task id"):
        errors.append(error + " (unsafe path component)")
    elif directory is not None and task_id != directory:
        errors.append(f"task '{task_id}' does not match its directory '{directory}'")
    if "," in coordinator or len(coordinator.split()) > 1:
        errors.append(f"'coordinator' must be exactly one agent id, got {coordinator!r}")
    elif error := id_error(coordinator, "coordinator"):
        errors.append(error)
    if any(v != coordinator for v in doc.repeated.get("coordinator", [])):
        errors.append("two 'coordinator' lines with different values")
    revision = parse_positive_int(doc.headers["revision"])
    if revision is None:
        errors.append(f"'revision' must be a positive integer, got {doc.headers['revision']!r}")
    _timestamp(doc, "updated", errors, warnings)
    base_commit = doc.get("base-commit")
    if base_commit is not None and not is_sha(base_commit):
        errors.append(f"'base-commit' must be a Git SHA, got {base_commit!r}")
    branch = doc.get("integration-branch")
    if branch is not None and branch != f"task/{task_id}/integration":
        errors.append(f"'integration-branch' must be 'task/{task_id}/integration', got {branch!r}")

    table = parse_subtask_table(doc.body)
    errors.extend(table.errors)
    warnings.extend(table.warnings)
    subtasks = table.value or ()
    if any(s.state in STARTED_PLAN_STATES for s in subtasks):
        warnings.extend(f"work has started but '{k}' is missing" for k in ("base-commit", "integration-branch") if not doc.get(k))

    plan = Plan(
        task_id=task_id,
        coordinator=coordinator,
        revision=revision or 0,
        updated=doc.headers["updated"],
        body=doc.body,
        base_commit=base_commit,
        integration_branch=branch,
        subtasks=subtasks,
        extra={k: v for k, v in doc.headers.items() if k not in _PLAN_KEYS},
    )
    return Parsed(plan, errors, warnings)


def check_revision_progress(revision: int, highest_seen: int | None) -> str | None:
    """A plan revision lower than one already seen is invalid (revisions strictly increase)."""
    if highest_seen is not None and revision < highest_seen:
        return f"plan revision {revision} is lower than revision {highest_seen} already seen"
    return None


def render_plan(plan: Plan) -> str:
    headers = [
        ("task", plan.task_id),
        ("coordinator", plan.coordinator),
        ("revision", str(plan.revision)),
        ("updated", plan.updated),
    ]
    if plan.base_commit:
        headers.append(("base-commit", plan.base_commit))
    if plan.integration_branch:
        headers.append(("integration-branch", plan.integration_branch))
    return render(headers, plan.body)


def render_subtask_table(subtasks: tuple[Subtask, ...] | list[Subtask]) -> str:
    lines = ["| " + " | ".join(SUBTASK_COLUMNS) + " |", "|" + " --- |" * len(SUBTASK_COLUMNS)]
    for s in subtasks:
        cells = [s.id, s.what, s.files, ", ".join(s.depends_on) or "-",
                 "yes" if s.worktree else "no", s.owner, s.reviewer, s.state]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines)


# --- status ----------------------------------------------------------------------------


def _valid_status_state(state: str) -> bool:
    if state in PLAN_STATES or state == WAITING_FOR_USER:
        return True
    return state.startswith(RUNNING_TESTS_PREFIX) and bool(state[len(RUNNING_TESTS_PREFIX):].strip())


def parse_status(doc: Document, file_agent_id: str | None = None) -> Parsed[Status]:
    required = ("plan-revision", "agent", "subtask", "state", "last-commit", "tests", "blockers", "updated")
    errors = [f"missing required key '{k}'" for k in required if not doc.get(k)]
    warnings = list(doc.warnings)
    if errors:
        return Parsed(None, errors, warnings)
    agent = doc.headers["agent"]
    if error := id_error(agent, "agent id"):
        errors.append(error)
    if file_agent_id is not None and agent != file_agent_id:
        errors.append(f"file is for agent '{file_agent_id}' but 'agent' is '{agent}'")
    revision = parse_positive_int(doc.headers["plan-revision"])
    if revision is None:
        errors.append(f"'plan-revision' must be a positive integer, got {doc.headers['plan-revision']!r}")
    subtask = doc.headers["subtask"]
    if subtask != "-" and (error := id_error(subtask, "subtask id")):
        errors.append(error)
    state = doc.headers["state"]
    if state.lower().startswith(RUNNING_TESTS_PREFIX):
        state = RUNNING_TESTS_PREFIX + " " + state[len(RUNNING_TESTS_PREFIX):].strip()
    else:
        state = " ".join(state.lower().split())
    if not _valid_status_state(state):
        errors.append(
            f"'state' must be a plan state, '{WAITING_FOR_USER}' or '{RUNNING_TESTS_PREFIX} <scope>', got {doc.headers['state']!r}"
        )
    last_commit = doc.headers["last-commit"]
    first = last_commit.split()[0]
    if not (is_sha(first) or first.lower() == "none"):
        errors.append(f"'last-commit' must be a SHA or 'none', got {last_commit!r}")
    elif first != last_commit:
        warnings.append(f"'last-commit' has text after '{first}'")
    _timestamp(doc, "updated", errors, warnings)

    status = Status(
        agent_id=agent,
        plan_revision=revision or 0,
        subtask=subtask,
        state=state,
        last_commit=last_commit,
        tests=doc.headers["tests"],
        blockers=doc.headers["blockers"],
        updated=doc.headers["updated"],
        branch=doc.get("branch"),
        body=doc.body,
        extra={k: v for k, v in doc.headers.items() if k not in _STATUS_KEYS},
    )
    return Parsed(status, errors, warnings)


def check_status(status: Status, plan: Plan | None, now: datetime) -> tuple[list[str], list[str]]:
    """Staleness and plan-consistency findings for one status: (errors, warnings).

    Stale revision and stale worker are independent checks. A status/plan disagreement is a
    conflict to report, never resolved automatically.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if plan is not None:
        if status.plan_revision > plan.revision:
            errors.append(
                f"plan-revision {status.plan_revision} is above the plan's revision {plan.revision}; "
                "trust neither file until reconciled"
            )
        elif status.plan_revision < plan.revision:
            warnings.append(f"stale revision: read revision {status.plan_revision}, plan is at {plan.revision}")
        row = plan.subtask(status.subtask) if status.subtask != "-" else None
        if status.subtask != "-" and row is None:
            warnings.append(f"subtask '{status.subtask}' is not in the plan")
        if row is not None:
            if row.owner_id != status.agent_id:
                warnings.append(f"subtask {row.id} is owned by '{row.owner}', not '{status.agent_id}'")
            if status.state in PLAN_STATES and status.state != row.state:
                warnings.append(
                    f"conflict: status says '{status.state}', plan row {row.id} says '{row.state}' "
                    "(plan is authoritative for assignment, status for progress)"
                )
    try:
        stale = is_stale(parse_timestamp(status.updated), now)
    except ValueError:
        stale = None
    if stale and status.state == "blocked":
        from .workers.quota import quota_block_active  # lazy: workers.runner imports this module

        try:
            waiting = quota_block_active(status.blockers, parse_timestamp(status.updated).value, now)
        except ValueError:
            waiting = False
        if waiting:
            warnings.append(f"quota block (updated {status.updated}): waiting for the provider limit, not stale")
            stale = False
    if stale:
        if status.state == WAITING_FOR_USER:
            warnings.append(f"stale worker (updated {status.updated}): reservation released, not failed")
        else:
            warnings.append(f"stale worker (updated {status.updated}): treat as unavailable")
    return errors, warnings


def verification_warnings(status: Status) -> list[str]:
    """Raised by verification, not the parser (formats.md section 5)."""
    if status.state == "done" and status.tests.strip().lower().startswith("none run"):
        return [f"{status.agent_id}: state 'done' with 'tests: none run'; check whether the "
                "subtask's acceptance criteria require tests"]
    return []


def render_status(status: Status) -> str:
    headers = [("plan-revision", str(status.plan_revision)), ("agent", status.agent_id),
               ("subtask", status.subtask), ("state", status.state)]
    if status.branch:
        headers.append(("branch", status.branch))
    headers += [("last-commit", status.last_commit), ("tests", status.tests),
                ("blockers", status.blockers), ("updated", status.updated)]
    return render(headers, status.body)


# --- files -----------------------------------------------------------------------------


def _read(path: Path) -> Document | str:
    try:
        return read_document(path)
    except (OSError, DocumentError) as exc:
        return f"cannot read {path.name}: {exc}"


def read_plan(task_dir: Path) -> Parsed[Plan]:
    doc = _read(Path(task_dir) / PLAN_FILE)
    return Parsed(None, [doc]) if isinstance(doc, str) else parse_plan(doc, Path(task_dir).name)


def read_statuses(task_dir: Path) -> dict[str, Parsed[Status]]:
    result: dict[str, Parsed[Status]] = {}
    for path in sorted(Path(task_dir).glob(f"{STATUS_PREFIX}*{STATUS_SUFFIX}")):
        agent = path.name[len(STATUS_PREFIX):-len(STATUS_SUFFIX)]
        doc = _read(path)
        if isinstance(doc, str):
            result[agent] = Parsed(None, [doc])
        elif error := id_error(agent, "agent id in file name"):
            result[agent] = Parsed(None, [error])
        else:
            result[agent] = parse_status(doc, agent)
    return result


def _strict_timestamp(value: str, key: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", value):
        raise ValueError(f"'{key}' must be written as YYYY-MM-DD HH:MM (local time), got {value!r}")


def write_plan(state_root: Path, plan: Plan, author: str) -> Path:
    """Write ``tasks/<task>/plan.md``. Only the coordinator writes; revision must increase."""
    if author != plan.coordinator:
        raise OwnershipError(f"'{author}' is not the coordinator '{plan.coordinator}'")
    _strict_timestamp(plan.updated, "updated")
    text = render_plan(plan)
    parsed = parse_plan(parse_text(text), plan.task_id)
    if parsed.errors:
        raise ValueError("invalid plan: " + "; ".join(parsed.errors))
    path = Path(state_root) / "tasks" / plan.task_id / PLAN_FILE
    if path.exists():
        existing = read_plan(path.parent)
        if existing.value is None or existing.value.coordinator != author:
            raise OwnershipError(f"{path} belongs to another coordinator or is unreadable")
        if plan.revision <= existing.value.revision:
            raise ValueError(f"revision must exceed the current revision {existing.value.revision}")
    atomic_write(path, text)
    return path


def write_status(state_root: Path, task_id: str, status: Status, author: str) -> Path:
    """Write the author's own ``tasks/<task>/status-<author>.md``."""
    if author != status.agent_id:
        raise OwnershipError(f"'{author}' cannot write the status of '{status.agent_id}'")
    if error := id_error(task_id, "task id"):
        raise ValueError(error)
    _strict_timestamp(status.updated, "updated")
    text = render_status(status)
    parsed = parse_status(parse_text(text), author)
    if parsed.errors:
        raise ValueError("invalid status: " + "; ".join(parsed.errors))
    path = Path(state_root) / "tasks" / task_id / status_file_name(author)
    if path.exists():
        existing = _read(path)
        if isinstance(existing, str) or existing.get("agent") != author:
            raise OwnershipError(f"{path} belongs to another agent or is unreadable")
    atomic_write(path, text)
    return path
