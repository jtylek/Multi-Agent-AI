"""Command line: ``multi-agent-ai init | inspect | index | worker run`` (also ``python -m multi_agent_ai``)."""

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
    _add_worker_parser(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    args = _parser().parse_args(argv)
    if args.command == "worker":
        return _worker_main(args)
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


# --- worker block (T12: managed headless workers) -----------------------------------------
# Kept in one block so it can be merged with other subcommand blocks at integration.


def _add_worker_parser(sub: argparse._SubParsersAction) -> None:
    from .workers.runner import DEFAULT_TIMEOUT

    worker = sub.add_parser("worker", help="managed headless workers (codex exec, copilot -p)")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    help_text = ("run one managed worker attempt: build the prompt from plan.md, launch the provider "
                 "with a least-privilege profile, check the worktree, record the attempt (no retries)")
    run = worker_sub.add_parser("run", help=help_text, description=help_text)
    run.add_argument("--provider", required=True, choices=("codex", "copilot"))
    run.add_argument("--task", required=True, help="task id (tasks/<task-id>/plan.md)")
    run.add_argument("--subtask", required=True, help="subtask id from the plan's subtask table")
    run.add_argument("--agent-id", required=True, help="new managed-worker id, e.g. codex-w1 (never an existing chat's id)")
    run.add_argument("--mode", required=True, choices=("review", "implement"))
    run.add_argument("--worktree", required=True, help="absolute path of the worker's Git worktree")
    run.add_argument("--model", default=None, help="provider model (default: the provider's own default)")
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds before the process tree is killed")
    run.add_argument("--commit", default=None, help="review mode: SHA of the commit to review (default: worktree HEAD)")
    run.add_argument("--verify-command", default=None,
                     help="verification command (default: taken from plan.md); Copilot allows exactly shell(<command>)")
    run.add_argument("--live-branch", action="append", default=[],
                     help="extra live/protected branch implement mode refuses (main and master always are)")
    run.add_argument("--pass-env", action="append", default=[],
                     help="pass this environment variable to the worker in addition to the allowlist (e.g. an API key)")
    run.add_argument("--project-root", default=".", help="primary checkout (default: current directory)")
    run.add_argument("--state-root", default=None,
                     help="state root, absolute or relative to the project root (default: .multi-agent-ai)")


def _worker_main(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .workers.runner import UsageError, WorkerRequest, run_worker

    root = resolve_state_root(args.project_root, args.state_root)
    if not root.path.is_dir():
        print(f"state root not found: {root.path} (run 'multi-agent-ai init' or pass --state-root)", file=sys.stderr)
        return 2
    request = WorkerRequest(
        provider=args.provider, task_id=args.task, subtask_id=args.subtask, agent_id=args.agent_id,
        mode=args.mode, worktree=Path(args.worktree), project_root=Path(args.project_root).resolve(),
        state_root=root, model=args.model, timeout=args.timeout, commit=args.commit,
        live_branches=tuple(args.live_branch), pass_env=tuple(args.pass_env), verify_command=args.verify_command,
    )
    try:
        outcome = run_worker(request)
    except UsageError as exc:
        print(f"worker run rejected: {exc}", file=sys.stderr)
        return 2
    summary = {
        "attempt": outcome.attempt_id, "failure_class": outcome.failure_class,
        "failure_detail": outcome.result.get("failure_detail"), "exit_code": outcome.exit_code,
        "run_dir": str(outcome.run_dir), "commit_created": outcome.commit,
        "status_file": str(outcome.status_path) if outcome.status_path else None,
        "review_file": str(outcome.review_path) if outcome.review_path else None,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if outcome.ok else 1


# --- end of worker block ---------------------------------------------------------------------
