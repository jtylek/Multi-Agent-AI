"""State root configuration and the derived SQLite index (D3, D9).

The coordination files under the state root are the source of truth. ``state.sqlite3`` only
indexes them and stores operational records the protocol does not define (attempts,
notifications, ...). It never creates, replaces or infers an acknowledgment and never writes a
protocol file; deleting it loses nothing authoritative.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, TypeVar

from .document import atomic_write
from .models import OPERATIONAL_RECORDS

DEFAULT_STATE_ROOT = ".multi-agent-ai"
CONFIG_FILE = "config.json"
DATABASE_FILE = "state.sqlite3"
FORMAT = 1

R = TypeVar("R")
_RECORD_TYPES = {cls.__name__: cls for cls in OPERATIONAL_RECORDS}


@dataclass(frozen=True)
class StateRoot:
    path: Path

    @property
    def messages(self) -> Path:
        return self.path / "messages"

    @property
    def tasks(self) -> Path:
        return self.path / "tasks"

    @property
    def config_path(self) -> Path:
        return self.path / CONFIG_FILE

    @property
    def database_path(self) -> Path:
        return self.path / DATABASE_FILE


def resolve_state_root(project_root: Path | str = ".", state_root: Path | str | None = None) -> StateRoot:
    """``state_root`` is absolute or relative to ``project_root``; default ``.multi-agent-ai``."""
    path = Path(state_root if state_root is not None else DEFAULT_STATE_ROOT)
    if not path.is_absolute():
        path = Path(project_root) / path
    return StateRoot(path.resolve())


def init_state_root(project_root: Path | str = ".", state_root: Path | str | None = None) -> StateRoot:
    """Create the state root layout, record its location and create the derived index.

    Existing coordination files are never touched. The project's ``.gitignore`` is not edited.
    """
    root = resolve_state_root(project_root, state_root)
    root.messages.mkdir(parents=True, exist_ok=True)
    root.tasks.mkdir(parents=True, exist_ok=True)
    config = {
        "format": FORMAT,
        "project_root": str(Path(project_root).resolve()),
        "state_root": str(root.path),
        "database": DATABASE_FILE,
    }
    text = json.dumps(config, indent=2) + "\n"
    if not root.config_path.exists() or root.config_path.read_text(encoding="utf-8") != text:
        atomic_write(root.config_path, text)
    StateIndex.open(root.database_path).close()
    return root


def read_config(root: StateRoot) -> dict[str, Any] | None:
    try:
        return json.loads(root.config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    entity_type TEXT NOT NULL,
    id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (entity_type, id)
);
CREATE TABLE IF NOT EXISTS protocol_files (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    valid INTEGER NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_revisions (
    task_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attempt_details (
    attempt_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


class StateIndex:
    """The derived SQLite index. Open read-only to guarantee nothing is written."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @classmethod
    def open(cls, path: Path, *, read_only: bool = False) -> StateIndex:
        if read_only:
            connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(path))
            connection.executescript(_SCHEMA)
        return cls(connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # operational records -----------------------------------------------------------

    def save(self, record: object) -> None:
        name = type(record).__name__
        if _RECORD_TYPES.get(name) is not type(record):
            raise TypeError(f"not an operational record: {name}")
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO records (entity_type, id, payload) VALUES (?, ?, ?)",
                (name, record.id, json.dumps(asdict(record), sort_keys=True)),  # type: ignore[attr-defined]
            )

    def get(self, record_type: type[R], record_id: str) -> R | None:
        row = self.connection.execute(
            "SELECT payload FROM records WHERE entity_type = ? AND id = ?",
            (record_type.__name__, record_id),
        ).fetchone()
        return _decode(record_type, row[0]) if row else None

    def list_records(self, record_type: type[R]) -> list[R]:
        rows = self.connection.execute(
            "SELECT payload FROM records WHERE entity_type = ? ORDER BY id", (record_type.__name__,)
        ).fetchall()
        return [_decode(record_type, row[0]) for row in rows]

    # worker attempt details (mode, subtask, usage, timings; next to the Attempt record) ----

    def save_attempt_details(self, attempt_id: str, details: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO attempt_details (attempt_id, payload) VALUES (?, ?)",
                (attempt_id, json.dumps(details, sort_keys=True)),
            )

    def attempt_details(self, attempt_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM attempt_details WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def all_attempt_details(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute("SELECT attempt_id, payload FROM attempt_details ORDER BY attempt_id")
        return {attempt_id: json.loads(payload) for attempt_id, payload in rows}

    # protocol file index -----------------------------------------------------------

    def seen_revision(self, task_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT revision FROM seen_revisions WHERE task_id = ?", (task_id,)
        ).fetchone()
        return row[0] if row else None

    def reindex(self, entries: list[tuple[Path, str, bool, dict[str, Any]]], plan_revisions: dict[str, int]) -> list[str]:
        """Replace the file index with ``entries`` (path, kind, valid, summary).

        Returns the paths whose content changed since the last index (reconciliation). The
        highest plan revision per task only ever increases.
        """
        previous = dict(self.connection.execute("SELECT path, sha256 FROM protocol_files"))
        changed: list[str] = []
        with self.connection:
            self.connection.execute("DELETE FROM protocol_files")
            for path, kind, valid, summary in entries:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if previous.get(str(path)) != digest:
                    changed.append(str(path))
                self.connection.execute(
                    "INSERT INTO protocol_files (path, kind, sha256, valid, summary) VALUES (?, ?, ?, ?, ?)",
                    (str(path), kind, digest, int(valid), json.dumps(summary, sort_keys=True)),
                )
            for task_id, revision in plan_revisions.items():
                self.connection.execute(
                    "INSERT INTO seen_revisions (task_id, revision) VALUES (?, ?) "
                    "ON CONFLICT(task_id) DO UPDATE SET revision = MAX(revision, excluded.revision)",
                    (task_id, revision),
                )
        return changed

    def indexed_files(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT path, kind, valid, summary FROM protocol_files ORDER BY path")
        return [{"path": p, "kind": k, "valid": bool(v), "summary": json.loads(s)} for p, k, v, s in rows]


def _decode(record_type: type[R], payload: str) -> R:
    values = json.loads(payload)
    for f in fields(record_type):  # type: ignore[arg-type]
        if isinstance(values.get(f.name), list):
            values[f.name] = tuple(values[f.name])
    return record_type(**values)
