"""Mailbox files: request.md and ack-<agent-id>.md (formats.md sections 2 and 3).

Only an ack file written by the recipient confirms a request. Nothing in this module (or the
package) writes an ack on another agent's behalf, and no exit code or transport result is
consulted when computing acknowledgment state (D8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
from .grammar import id_error, parse_positive_int
from .models import ACK_STATUSES, BROADCAST, Ack, Parsed, Request

REQUEST_FILE = "request.md"
ACK_PREFIX, ACK_SUFFIX = "ack-", ".md"
_AGENT_FIELD = re.compile(r"([^\s(]+)\s*(?:\((.*)\))?")
_BODY_REVISION = re.compile(r"\brevision\b[\s:]*(\d+)", re.IGNORECASE)


def ack_file_name(agent_id: str) -> str:
    return f"{ACK_PREFIX}{agent_id}{ACK_SUFFIX}"


def _missing(doc: Document, keys: tuple[str, ...]) -> list[str]:
    return [f"missing required key '{k}'" for k in keys if not doc.get(k)]


def _check_dir_id(value: str, directory: str | None, what: str, errors: list[str]) -> None:
    error = id_error(value, what)
    if error:
        errors.append(error + " (unsafe path component)")
    elif directory is not None and value != directory:
        errors.append(f"{what} '{value}' does not match its directory '{directory}'")


def _positive_int(doc: Document, key: str, errors: list[str]) -> int | None:
    raw = doc.get(key)
    if raw is None:
        return None
    value = parse_positive_int(raw)
    if value is None:
        errors.append(f"'{key}' must be a positive integer, got {raw!r}")
    return value


# --- request ---------------------------------------------------------------------------


def parse_request(doc: Document, directory: str | None = None) -> Parsed[Request]:
    errors = _missing(doc, ("id", "from", "to"))
    warnings = list(doc.warnings)
    if errors:
        return Parsed(None, errors, warnings)
    message_id, sender = doc.headers["id"], doc.headers["from"]
    _check_dir_id(message_id, directory, "message id", errors)
    if error := id_error(sender, "sender"):
        errors.append(error)

    items = [item.strip() for item in doc.headers["to"].split(",")]
    if any(not item for item in items):
        errors.append("'to' contains an empty recipient item")
    items = [item for item in items if item]
    if BROADCAST in items and len(items) > 1:
        errors.append("'to' must be exactly 'broadcast' or a list of agent ids")
    for item in items:
        if item != BROADCAST and (error := id_error(item, "recipient")):
            errors.append(error)
    if len(set(items)) != len(items):
        warnings.append("'to' names a recipient more than once")

    plan_revision = _positive_int(doc, "plan-revision", errors)
    if not doc.body.strip():
        errors.append("request body is empty")

    request = Request(
        id=message_id,
        sender=sender,
        to=tuple(dict.fromkeys(items)),
        body=doc.body,
        plan_revision=plan_revision,
        extra={k: v for k, v in doc.headers.items() if k not in ("id", "from", "to", "plan-revision")},
    )
    if request.is_broadcast and "agree" in request.body.lower():
        warnings.append(
            "broadcast asks for agreement; expected participants cannot be read from the body, "
            "so 'all agreed' is not computed"
        )
    return Parsed(request, errors, warnings)


def request_revision(request: Request) -> tuple[int | None, list[str]]:
    """The plan revision a request concerns: its header, else revisions its body names.

    When the body names several revisions the highest is used, with a warning.
    """
    if request.plan_revision is not None:
        return request.plan_revision, []
    found = sorted({int(n) for n in _BODY_REVISION.findall(request.body) if int(n) > 0})
    if not found:
        return None, []
    warnings = [f"plan revision {found[-1]} inferred from the request body"]
    if len(found) > 1:
        warnings.append(
            f"request body names revisions {', '.join(map(str, found))}; the highest is required"
        )
    return found[-1], warnings


def render_request(request: Request) -> str:
    headers = [("id", request.id), ("from", request.sender), ("to", ", ".join(request.to))]
    if request.plan_revision is not None:
        headers.append(("plan-revision", str(request.plan_revision)))
    return render(headers, request.body)


# --- ack -------------------------------------------------------------------------------


def parse_ack(
    doc: Document, directory: str | None = None, file_agent_id: str | None = None
) -> Parsed[Ack]:
    errors = _missing(doc, ("id", "agent", "status"))
    warnings = list(doc.warnings)
    if errors:
        return Parsed(None, errors, warnings)
    message_id = doc.headers["id"]
    _check_dir_id(message_id, directory, "message id", errors)

    match = _AGENT_FIELD.fullmatch(doc.headers["agent"])
    agent_id = match.group(1) if match else doc.headers["agent"]
    product = match.group(2).strip() if match and match.group(2) else None
    if not match:
        errors.append(f"'agent' must be '<agent-id> (<product>)', got {doc.headers['agent']!r}")
    elif error := id_error(agent_id, "agent id"):
        errors.append(error)
    if file_agent_id is not None and agent_id != file_agent_id:
        errors.append(
            f"file is for agent '{file_agent_id}' but 'agent' is '{agent_id}' "
            "(file/identity mismatch, possible forged ack)"
        )

    status = " ".join(doc.headers["status"].lower().split())
    if status not in ACK_STATUSES:
        errors.append(f"'status' must be one of {', '.join(ACK_STATUSES)}, got {status!r}")
    plan_revision = _positive_int(doc, "plan-revision", errors)
    if status == "changes requested" and not doc.body.strip():
        warnings.append("'changes requested' without a body describing the issues")

    ack = Ack(
        message_id=message_id,
        agent_id=agent_id,
        status=status,
        product=product,
        plan_revision=plan_revision,
        body=doc.body,
        extra={k: v for k, v in doc.headers.items() if k not in ("id", "agent", "status", "plan-revision")},
    )
    return Parsed(ack, errors, warnings)


def render_ack(ack: Ack) -> str:
    headers = [("id", ack.message_id), ("agent", ack.agent_field), ("status", ack.status)]
    if ack.plan_revision is not None:
        headers.append(("plan-revision", str(ack.plan_revision)))
    return render(headers, ack.body)


# --- agreement -------------------------------------------------------------------------


@dataclass
class RecipientState:
    agent_id: str
    state: str
    """missing | received | agreed | changes requested | invalid | stale | mismatch"""
    plan_revision: int | None = None
    counts: bool = False
    detail: str = ""


@dataclass
class Agreement:
    required_revision: int | None
    recipients: list[RecipientState]
    unaddressed: list[RecipientState] = field(default_factory=list)
    all_agreed: bool | None = False
    """None when it cannot be computed (a broadcast without a participant list)."""
    warnings: list[str] = field(default_factory=list)


def _ack_state(parsed: Parsed[Ack], agent_id: str, required: int | None) -> RecipientState:
    ack = parsed.value
    if not parsed.ok or ack is None:
        return RecipientState(agent_id, "invalid", detail="; ".join(parsed.errors))
    if required is not None:
        if ack.plan_revision is None:
            return RecipientState(
                agent_id, "invalid",
                detail=f"no plan-revision; the request concerns revision {required}",
            )
        if ack.plan_revision < required:
            return RecipientState(
                agent_id, "stale", ack.plan_revision,
                detail=f"{ack.status} for revision {ack.plan_revision}; request concerns {required}",
            )
        if ack.plan_revision > required:
            return RecipientState(
                agent_id, "mismatch", ack.plan_revision,
                detail=f"{ack.status} for revision {ack.plan_revision}; request concerns {required}",
            )
    return RecipientState(agent_id, ack.status, ack.plan_revision, counts=True)


def evaluate_agreement(request: Request, acks: dict[str, Parsed[Ack]]) -> Agreement:
    """Per-recipient acknowledgment state and whether every named recipient agreed.

    ``acks`` maps the agent id taken from each ack file name to its parse result. Only ack
    files count: nothing else (exit codes, transport results, SQLite rows) is consulted.
    """
    required, warnings = request_revision(request)
    states = {agent: _ack_state(parsed, agent, required) for agent, parsed in acks.items()}

    if request.is_broadcast:
        recipients = [states[a] for a in sorted(states)]
        return Agreement(required, recipients, [], None, warnings)

    recipients = [states.get(a, RecipientState(a, "missing")) for a in request.to]
    unaddressed = []
    for agent in sorted(set(states) - set(request.to)):
        state = states[agent]
        state.counts = False
        state.detail = (state.detail + "; " if state.detail else "") + "not named in 'to'"
        unaddressed.append(state)
    agreed = [r for r in recipients if r.counts and r.state == "agreed"]
    revisions = {r.plan_revision for r in agreed}
    all_agreed = len(agreed) == len(recipients) and len(revisions) == 1
    return Agreement(required, recipients, unaddressed, all_agreed, warnings)


# --- files -----------------------------------------------------------------------------


def _read(path: Path) -> Document | str:
    try:
        return read_document(path)
    except (OSError, DocumentError) as exc:
        return f"cannot read {path.name}: {exc}"


def read_request(message_dir: Path) -> Parsed[Request]:
    doc = _read(Path(message_dir) / REQUEST_FILE)
    if isinstance(doc, str):
        return Parsed(None, [doc])
    return parse_request(doc, Path(message_dir).name)


def read_acks(message_dir: Path) -> dict[str, Parsed[Ack]]:
    acks: dict[str, Parsed[Ack]] = {}
    for path in sorted(Path(message_dir).glob(f"{ACK_PREFIX}*{ACK_SUFFIX}")):
        agent = path.name[len(ACK_PREFIX):-len(ACK_SUFFIX)]
        doc = _read(path)
        if isinstance(doc, str):
            acks[agent] = Parsed(None, [doc])
        elif error := id_error(agent, "agent id in file name"):
            acks[agent] = Parsed(None, [error])
        else:
            acks[agent] = parse_ack(doc, Path(message_dir).name, agent)
    return acks


def write_request(state_root: Path, request: Request, author: str) -> Path:
    """Create ``messages/<id>/request.md``. Only the sender writes it; never overwrites."""
    if author != request.sender:
        raise OwnershipError(f"'{author}' cannot write a request sent by '{request.sender}'")
    text = render_request(request)
    parsed = parse_request(parse_text(text), request.id)
    if parsed.errors:
        raise ValueError("invalid request: " + "; ".join(parsed.errors))
    path = Path(state_root) / "messages" / request.id / REQUEST_FILE
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    atomic_write(path, text)
    return path


def write_ack(state_root: Path, ack: Ack, author: str) -> Path:
    """Create the author's own ``ack-<author>.md``.

    Refuses to write for any agent other than ``author``, to a request that does not address
    the author, without the plan revision the request concerns, or over an existing file
    (corrections are appended by the author, not rewritten).
    """
    if author != ack.agent_id:
        raise OwnershipError(f"'{author}' cannot write an acknowledgment for '{ack.agent_id}'")
    message_dir = Path(state_root) / "messages" / ack.message_id
    request = read_request(message_dir)
    if not request.ok or request.value is None:
        raise ValueError("cannot acknowledge an unreadable request: " + "; ".join(request.errors))
    if not request.value.is_broadcast and author not in request.value.to:
        raise OwnershipError(f"request '{ack.message_id}' is not addressed to '{author}'")
    required, _ = request_revision(request.value)
    if required is not None and ack.plan_revision != required:
        raise ValueError(f"the request concerns plan revision {required}; quote it in the ack")
    text = render_ack(ack)
    parsed = parse_ack(parse_text(text), ack.message_id, author)
    if parsed.errors:
        raise ValueError("invalid ack: " + "; ".join(parsed.errors))
    path = message_dir / ack_file_name(author)
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    atomic_write(path, text)
    return path
