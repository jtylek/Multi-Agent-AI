"""Command line: ``multi-agent-ai init | inspect | index`` (also ``python -m multi_agent_ai``)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Sequence

from .board import check_revision_progress
from .grammar import parse_timestamp
from .report import format_text, index_entries, scan, to_dict
from .state import StateIndex, init_state_root, read_config, resolve_state_root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-agent-ai",
        description="Initialise and inspect a Multi-Agent AI state root (protocol v1, format 1).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "init": "create the state root layout, record it, and create the derived SQLite index",
        "inspect": "show messages, acknowledgment state, plans, subtasks and staleness (read-only)",
        "index": "rebuild the derived SQLite index from the coordination files",
    }
    for name, help_text in commands.items():
        cmd = sub.add_parser(name, help=help_text, description=help_text)
        cmd.add_argument("--project-root", default=".", help="primary checkout (default: current directory)")
        cmd.add_argument(
            "--state-root",
            default=None,
            help="state root, absolute or relative to the project root (default: .multi-agent-ai)",
        )
        if name == "inspect":
            cmd.add_argument("--json", action="store_true", help="machine-readable output")
            cmd.add_argument("--now", help="evaluate staleness at this local time (YYYY-MM-DD HH:MM)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    args = _parser().parse_args(argv)
    root = resolve_state_root(args.project_root, args.state_root)

    if args.command == "init":
        root = init_state_root(args.project_root, args.state_root)
        print(json.dumps(read_config(root), indent=2))
        return 0

    if not root.path.is_dir():
        print(f"state root not found: {root.path} (run 'multi-agent-ai init' or pass --state-root)", file=sys.stderr)
        return 2

    if args.command == "inspect":
        now = parse_timestamp(args.now).value if args.now else datetime.now()
        report = scan(root.path, now)
        if root.database_path.is_file():  # opened read-only: inspect never writes
            with StateIndex.open(root.database_path, read_only=True) as index:
                for task in report.tasks:
                    plan = task.plan.value
                    if plan and (error := check_revision_progress(plan.revision, index.seen_revision(task.task_id))):
                        task.plan.errors.append(error)
        if args.json:
            print(json.dumps(to_dict(report), indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(format_text(report))
        return 0

    # index
    if not root.database_path.is_file():
        print(f"no index at {root.database_path}; run 'multi-agent-ai init' first", file=sys.stderr)
        return 2
    report = scan(root.path)
    entries, revisions = index_entries(report)
    with StateIndex.open(root.database_path) as index:
        for task_id, revision in revisions.items():
            if error := check_revision_progress(revision, index.seen_revision(task_id)):
                print(f"{task_id}: ERROR: {error}")
        changed = index.reindex(entries, revisions)
    print(f"indexed {len(entries)} file(s), {len(changed)} new or changed")
    return 0
