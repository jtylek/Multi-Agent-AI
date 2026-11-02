"""SQLite-backed memory store with provenance, optimistic revisions and FTS5."""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

KINDS = {"decision", "fact", "procedure", "lesson", "task_outcome", "constraint", "hypothesis"}
CONFIDENCES = {"hypothesis", "reported", "verified"}
STATUSES = {"proposed", "active", "superseded", "retracted"}


class MemoryConflict(ValueError):
    """A record has changed since the caller read it."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS memory_records(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
                    confidence TEXT NOT NULL, status TEXT NOT NULL,
                    revision INTEGER NOT NULL, created_by TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
                    content_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memory_sources(
                    memory_id TEXT NOT NULL REFERENCES memory_records(id),
                    source_type TEXT NOT NULL, source_ref TEXT NOT NULL,
                    PRIMARY KEY(memory_id, source_type, source_ref));
                CREATE TABLE IF NOT EXISTS memory_revisions(
                    memory_id TEXT NOT NULL REFERENCES memory_records(id),
                    revision INTEGER NOT NULL, content TEXT NOT NULL,
                    status TEXT NOT NULL, changed_by TEXT NOT NULL,
                    changed_at_utc TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(memory_id, revision));
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    title, content, content='memory_records', content_rowid='rowid');
                CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_records BEGIN
                    INSERT INTO memory_fts(rowid,title,content) VALUES(new.rowid,new.title,new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_records BEGIN
                    INSERT INTO memory_fts(memory_fts,rowid,title,content)
                    VALUES('delete',old.rowid,old.title,old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_records BEGIN
                    INSERT INTO memory_fts(memory_fts,rowid,title,content)
                    VALUES('delete',old.rowid,old.title,old.content);
                    INSERT INTO memory_fts(rowid,title,content) VALUES(new.rowid,new.title,new.content);
                END;
                INSERT OR IGNORE INTO schema_migrations(version) VALUES(1);
            """)

    def add(self, *, project_id: str, kind: str, title: str, content: str,
            actor: str, source_type: str, source_ref: str) -> str:
        if kind not in KINDS or not all((project_id.strip(), title.strip(), content.strip(), actor.strip(), source_type.strip(), source_ref.strip())):
            raise ValueError("Invalid memory fields or kind")
        if len(content) > 20000:
            raise ValueError("Memory content exceeds 20000 characters")
        memory_id, now = str(uuid.uuid4()), _utc()
        with self._connect() as db:
            db.execute("""INSERT INTO memory_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, project_id, kind, title, content, "reported", "proposed",
                 1, actor, now, now, hashlib.sha256(content.encode()).hexdigest()))
            db.execute("INSERT INTO memory_sources VALUES(?,?,?)", (memory_id, source_type, source_ref))
            db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                       (memory_id, 1, content, "proposed", actor, now, "created"))
        return memory_id

    def get(self, memory_id: str, project_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM memory_records WHERE id=? AND project_id=?",
                             (memory_id, project_id)).fetchone()
            return dict(row) if row else None

    def search(self, query: str, project_id: str, limit: int = 10) -> list[dict]:
        if not query.strip() or not 1 <= limit <= 100:
            raise ValueError("Query must be nonempty and limit must be 1..100")
        # Quote as a literal FTS phrase to avoid accepting arbitrary MATCH syntax.
        phrase = '"' + query.replace('"', '""') + '"'
        with self._connect() as db:
            rows = db.execute("""SELECT m.* FROM memory_fts f
                JOIN memory_records m ON m.rowid=f.rowid
                WHERE memory_fts MATCH ? AND m.project_id=? AND m.status='active'
                ORDER BY bm25(memory_fts) LIMIT ?""", (phrase, project_id, limit)).fetchall()
            return [dict(row) for row in rows]

    def change_status(self, memory_id: str, project_id: str, *, expected_revision: int,
                      status: str, actor: str, reason: str) -> int:
        if status not in STATUSES or not actor.strip() or not reason.strip():
            raise ValueError("Invalid status, actor, or reason")
        now = _utc()
        with self._connect() as db:
            result = db.execute("""UPDATE memory_records SET status=?,revision=revision+1,
                updated_at_utc=? WHERE id=? AND project_id=? AND revision=?""",
                (status, now, memory_id, project_id, expected_revision))
            if result.rowcount != 1:
                raise MemoryConflict("Missing record, wrong project, or stale revision")
            revision = expected_revision + 1
            content = db.execute("SELECT content FROM memory_records WHERE id=?", (memory_id,)).fetchone()[0]
            db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                       (memory_id, revision, content, status, actor, now, reason))
            return revision
