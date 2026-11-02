"""CLI for the local persistent memory store; no model/provider invocation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .memory import MemoryStore, MemoryConflict


def add_parser(sub: argparse._SubParsersAction) -> None:
    memory = sub.add_parser("memory", help="project-scoped persistent agent knowledge")
    actions = memory.add_subparsers(dest="memory_command", required=True)
    for name in ("init", "add", "search", "show", "promote", "retract", "doctor"):
        p = actions.add_parser(name)
        p.add_argument("--state-root", default=".multi-agent-ai")
        if name not in ("init", "doctor"):
            p.add_argument("--project", required=True, help="stable project ID")
        if name == "add":
            p.add_argument("--kind", required=True, choices=(
                "decision", "fact", "procedure", "lesson", "task_outcome", "constraint", "hypothesis"))
            p.add_argument("--title", required=True)
            p.add_argument("--content-file", required=True)
            p.add_argument("--actor", required=True)
            p.add_argument("--source-type", required=True)
            p.add_argument("--source-ref", required=True)
        if name == "search":
            p.add_argument("query")
            p.add_argument("--limit", type=int, default=10)
        if name in ("show", "promote", "retract"):
            p.add_argument("id")
        if name in ("promote", "retract"):
            p.add_argument("--expected-revision", type=int, required=True)
            p.add_argument("--actor", required=True)
            p.add_argument("--reason", required=True)
        if name == "promote":
            p.add_argument("--evidence", required=True, help="review or test evidence reference")


def main(args: argparse.Namespace) -> int:
    root = Path(args.state_root).resolve()
    db_path = root / "memory.sqlite3"
    store = MemoryStore(db_path)
    try:
        if args.memory_command == "init":
            if not root.is_dir():
                raise ValueError("State root missing; run multi-agent-ai init first")
            store.initialize()
            payload = {"database": str(db_path), "status": "initialized"}
        elif args.memory_command == "doctor":
            if not db_path.is_file():
                raise ValueError("Memory database missing; run memory init")
            payload = store.doctor()
        else:
            if not db_path.is_file():
                raise ValueError("Memory database missing; run memory init")
            if args.memory_command == "add":
                path = Path(args.content_file).resolve()
                if path.name.lower() == ".env" or path.suffix.lower() in {".pem", ".key"}:
                    raise ValueError("Refusing a sensitive content file")
                if path.stat().st_size > 80000:
                    raise ValueError("Content file exceeds 80000 bytes")
                content = path.read_text(encoding="utf-8")
                memory_id = store.add(project_id=args.project, kind=args.kind,
                                      title=args.title, content=content, actor=args.actor,
                                      source_type=args.source_type, source_ref=args.source_ref)
                payload = {"id": memory_id, "status": "proposed"}
            elif args.memory_command == "search":
                payload = store.search(args.query, args.project, args.limit)
            elif args.memory_command == "show":
                payload = store.get(args.id, args.project)
                if payload is None:
                    raise ValueError("Memory not found in this project")
            elif args.memory_command == "promote":
                payload = {"revision": store.promote(
                    args.id, args.project, expected_revision=args.expected_revision,
                    actor=args.actor, reason=args.reason, evidence=args.evidence)}
            else:
                payload = {"revision": store.change_status(
                    args.id, args.project, expected_revision=args.expected_revision,
                    status="retracted", actor=args.actor, reason=args.reason)}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, MemoryConflict) as exc:
        print(f"memory: {exc}", file=sys.stderr)
        return 2
