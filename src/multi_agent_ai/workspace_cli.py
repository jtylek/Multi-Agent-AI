"""``multi-agent-ai workspace ...`` (T23): parser and handler, kept apart from cli.py.

Every subcommand that changes Git or the record is a dry run unless ``--execute`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import workspace as ws
from .state import resolve_state_root

MUTATING = ("create-integration", "create", "integrate", "remove", "retain")


def add_parser(sub: argparse._SubParsersAction) -> None:
    workspace = sub.add_parser("workspace", help="integration branch and per-subtask worktrees (dry run unless --execute)")
    wsub = workspace.add_subparsers(dest="workspace_command", required=True)

    def common(cmd: argparse.ArgumentParser, *, mutating: bool, subtask: bool = False, json_out: bool = True) -> None:
        cmd.add_argument("--task", required=True, help="task id (tasks/<task-id>/plan.md)")
        if subtask:
            cmd.add_argument("--subtask", required=True, help="subtask id from the plan")
        cmd.add_argument("--project-root", default=".", help="primary checkout (default: current directory)")
        cmd.add_argument("--state-root", default=None,
                         help="state root, absolute or relative to the project root (default: .multi-agent-ai)")
        cmd.add_argument("--live-branch", action="append", default=[],
                         help="extra protected branch (main and master always are)")
        if mutating:
            cmd.add_argument("--execute", action="store_true", help="really change Git and the record (default: dry run)")
        if json_out:
            cmd.add_argument("--json", action="store_true", help="machine-readable output")

    root_help = "parent directory of the worktrees (default: the 'wt' directory next to the project)"
    integration = wsub.add_parser("create-integration", help="create task/<task>/integration and its worktree")
    common(integration, mutating=True)
    integration.add_argument("--base", default=None, help="base commit (default: base-commit from the plan)")
    integration.add_argument("--worktrees-root", default=None, help=root_help)
    create = wsub.add_parser("create", help="create task/<task>/<subtask> and its worktree")
    common(create, mutating=True, subtask=True)
    create.add_argument("--from", dest="base_from", choices=("auto", "base", "integration"), default="auto",
                        help="auto: dependants from the integration tip, others from the plan's base commit")
    create.add_argument("--worktrees-root", default=None, help=root_help)
    common(wsub.add_parser("check", help="read-only: compare recorded worktrees with Git (exit 1 on a problem)"),
           mutating=False)
    common(wsub.add_parser("list", help="read-only: list recorded worktrees and orphans"), mutating=False)
    merge = wsub.add_parser("integrate", help="merge reviewed subtask tips into the integration branch")
    common(merge, mutating=True)
    merge.add_argument("--order", required=True, help="comma-separated subtask ids, dependencies first")
    remove = wsub.add_parser("remove", help="remove a completed (merged) worktree; never deletes the branch")
    common(remove, mutating=True, subtask=True)
    remove.add_argument("--discard-failed", action="store_true",
                        help="USER ONLY: also remove a retained failed worktree (needs --execute)")
    retain = wsub.add_parser("retain", help="mark a failed subtask's worktree as kept for diagnosis")
    common(retain, mutating=True, subtask=True)
    retain.add_argument("--reason", required=True, help="why it is kept (failure class, run directory)")


def main(args: argparse.Namespace) -> int:
    root = resolve_state_root(args.project_root, args.state_root)
    if not root.path.is_dir():
        print(f"state root not found: {root.path} (run 'multi-agent-ai init' or pass --state-root)", file=sys.stderr)
        return 2
    project = Path(args.project_root).resolve()
    protected = (*ws.DEFAULT_PROTECTED, *args.live_branch)
    execute = getattr(args, "execute", False)
    worktrees_root = Path(args.worktrees_root).resolve() if getattr(args, "worktrees_root", None) else None
    command = args.workspace_command
    nl = chr(10)

    def emit(value, text: str) -> None:
        if args.json:
            print(json.dumps(asdict(value) if is_dataclass(value) else value, indent=2, ensure_ascii=False, default=str))
        else:
            print(text)
        if command in MUTATING and not execute:
            print("dry run: nothing was changed; add --execute to do it", file=sys.stderr)

    def warnings(result) -> str:
        return "".join(f"{nl}warning: {w}" for w in result.warnings)

    try:
        if command == "create-integration":
            r = ws.create_integration(project, root, args.task, args.base, worktrees_root, protected, execute)
            verb = "created" if r.executed else "would create"
            emit(r, f"{verb} {r.record.branch} at {r.record.path} (base {r.record.base[:7]}"
                    f"{', reused' if r.record.reused else ''}){warnings(r)}")
        elif command == "create":
            r = ws.create_subtask_worktree(project, root, args.task, args.subtask, args.base_from, worktrees_root,
                                           protected, execute)
            verb = "created" if r.executed else "would create"
            emit(r, f"{verb} {r.record.branch} at {r.record.path} (base {r.record.base[:7]} from "
                    f"{r.record.base_from}){warnings(r)}")
        elif command in ("check", "list"):
            statuses = ws.list_worktrees(project, root, args.task)
            if args.json:
                print(json.dumps([asdict(s) for s in statuses], indent=2, ensure_ascii=False))
            else:
                for s in statuses:
                    print(f"{'ok' if not s.problems else 'PROBLEM'}\t{s.subtask}\t{s.branch}\t{s.path}\t{s.record_state or '-'}")
                    for problem in s.problems:
                        print(f"\t{problem}")
            return 1 if command == "check" and any(s.problems for s in statuses) else 0
        elif command == "integrate":
            order = [p for p in args.order.split(",") if p]
            report = ws.integrate(project, root, args.task, order, protected, execute)
            lines = [f"{i.result}\t{i.subtask}\t{(i.commit or '-')[:7]}\t{(i.code or '')} {i.detail}".rstrip()
                     for i in report.items]
            tip = f"{nl}integration tip: {report.tip[:7]}" if report.tip else ""
            emit(report, nl.join(lines) + tip)
            return 0 if report.ok else 1
        elif command == "remove":
            r = ws.remove_worktree(project, root, args.task, args.subtask, execute, args.discard_failed)
            emit(r, f"{'removed' if r.executed else 'would remove'} {r.path}")
        elif command == "retain":
            record = ws.retain_failed(root, args.task, args.subtask, args.reason, execute)
            verb = "retained" if execute else "would retain"
            emit(record, f"{verb} {record.branch} at {record.path}: {record.note}")
    except ws.WorkspaceError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        for detail in exc.details:
            print(f"  {detail}", file=sys.stderr)
        return 1
    return 0
