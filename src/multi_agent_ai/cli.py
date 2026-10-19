"""Command line: ``multi-agent-ai init | inspect | index | doctor | verify | worker run``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Sequence

from .board import check_revision_progress
from .grammar import parse_timestamp
from .notify import format_notify, notify_message
from .reachability import discover_agents
from .registry import reservation_conflicts, read_registry
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
        "agents": "discover reachable Claude sessions and Codex threads for the project root (read-only)",
        "notify": "notify recipients of a durable mailbox request without creating acknowledgments",
        "registry": "show registered agents and optionally check a file/area reservation",
    }
    for name, help_text in commands.items():
        cmd = sub.add_parser(name, help=help_text, description=help_text)
        cmd.add_argument("--project-root", default=".", help="primary checkout (default: current directory)")
        cmd.add_argument(
            "--state-root",
            default=None,
            help="state root, absolute or relative to the project root (default: .multi-agent-ai)",
        )
        if name in {"inspect", "agents", "notify", "registry"}:
            cmd.add_argument("--json", action="store_true", help="machine-readable output")
        if name == "agents":
            cmd.add_argument("--timeout", type=float, default=15, help="provider discovery timeout in seconds")
        if name == "notify":
            cmd.add_argument("message_id", help="durable mailbox message id")
            cmd.add_argument("--timeout", type=float, default=15, help="transport timeout in seconds")
            cmd.add_argument("--send", action="store_true", help="send notifications (default is dry-run)")
        if name == "inspect":
            cmd.add_argument("--now", help="evaluate staleness at this local time (YYYY-MM-DD HH:MM)")
        if name == "registry":
            cmd.add_argument("--files", action="append", default=[], help="file/area to check (repeatable)")
            cmd.add_argument("--agent-id", default=None, help="exclude this agent's own reservation")
            cmd.add_argument("--now", help="evaluate staleness at this local time (YYYY-MM-DD HH:MM)")
    doctor = sub.add_parser("doctor", help="check this machine is ready (read-only; runs only --version)",
                            description="Read-only readiness checks: Python, Git, the provider CLIs (found, version; "
                                        "sign-in is not checked because that needs the provider), the state root.")
    doctor.add_argument("--project-root", default=".", help="primary checkout (default: current directory)")
    doctor.add_argument("--state-root", default=None,
                        help="state root, absolute or relative to the project root (default: .multi-agent-ai)")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    verify = sub.add_parser("verify", help="independently verify a worktree using declared argv commands")
    verify.add_argument("--worktree", required=True, help="Git worktree to verify")
    verify.add_argument("--command", action="append", nargs="+", required=True,
                        help="one shell-free argv command; repeat for multiple commands")
    verify.add_argument("--preflight", action="append", nargs="+", default=[],
                        help="preflight argv command; failure is environment_error")
    verify.add_argument("--expected-file", action="append", default=[], help="file expected in the worktree")
    expected = verify.add_mutually_exclusive_group()
    expected.add_argument("--expected-clean", action="store_true", dest="expected_clean")
    expected.add_argument("--expected-dirty", action="store_false", dest="expected_clean")
    verify.set_defaults(expected_clean=None)
    verify.add_argument("--expected-commit", default=None)
    verify.add_argument("--timeout", type=float, default=300.0)
    verify.add_argument("--run-dir", default=None)
    verify.add_argument("--env", action="append", default=[], metavar="NAME",
                        help="environment variable name allowed through to commands")
    verify.add_argument("--project-root", default=".", help="project root for the default run directory")
    verify.add_argument("--state-root", default=None, help="state root for the default run directory")
    verify.add_argument("--json", action="store_true", help="machine-readable output")
    _add_worker_parser(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        from pathlib import Path

        from . import doctor

        checks = doctor.run_doctor(Path(args.project_root).resolve(), resolve_state_root(args.project_root, args.state_root))
        if args.json:
            print(json.dumps(doctor.to_dict(checks), indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(doctor.format_text(checks))
        return doctor.exit_code(checks)
    if args.command == "verify":
        from pathlib import Path

        from .verify import CommandSpec, VerificationSpec, verify

        spec = VerificationSpec(
            commands=tuple(CommandSpec(tuple(command)) for command in args.command),
            preflight=tuple(CommandSpec(tuple(command)) for command in args.preflight),
            expected_files=tuple(args.expected_file), expected_clean=args.expected_clean,
            expected_commit=args.expected_commit, timeout=args.timeout, environment=tuple(args.env),
        )
        root = resolve_state_root(args.project_root, args.state_root)
        result = verify(Path(args.worktree), spec, run_dir=args.run_dir or root.path / "runs" / "verify" / "cli")
        if args.json:
            print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        else:
            print(f"{result.status}: {result.run_dir} ({result.duration_seconds:.3f}s)")
            if result.error:
                print(result.error, file=sys.stderr)
        return 0 if result.ok else 1
    if args.command == "worker":  # T12 block: see _add_worker_parser / _worker_main below
        return _worker_main(args)
    project_root = resolve_state_root(args.project_root).path.parent

    if args.command == "agents":
        result = discover_agents(project_root, timeout=args.timeout)
        if args.json:
            print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        else:
            for agent in result.agents:
                status = f" status={agent.status}" if agent.status else ""
                print(f"{agent.product}\t{agent.id}\t{agent.name}\t{agent.cwd}{status}")
            for error in result.errors:
                print(f"error\t{error}", file=sys.stderr)
        return 0 if result.agents or not result.errors else 1

    root =resolve_state_root(args.project_root, args.state_root)

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

    if args.command == "registry":
        now = parse_timestamp(args.now).value if args.now else datetime.now()
        entries = read_registry(root.path)
        conflicts = reservation_conflicts(root.path, args.files, agent=args.agent_id, now=now) if args.files else []
        if args.json:
            payload = {
                "entries": [
                    {"agent": agent, "valid": parsed.ok, "errors": parsed.errors,
                     "warnings": parsed.warnings,
                     **({"started": parsed.value.started, "updated": parsed.value.updated,
                         "task": parsed.value.task, "files": parsed.value.files,
                         "status": parsed.value.status, "contact": parsed.value.contact}
                        if parsed.value else {})}
                    for agent, parsed in entries.items()
                ],
                "conflicts": [vars(item) for item in conflicts],
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            for agent, parsed in entries.items():
                if parsed.value is None:
                    print(f"{agent}\tinvalid: {'; '.join(parsed.errors)}")
                else:
                    print(f"{agent}\t{parsed.value.status}\t{parsed.value.task}\t{parsed.value.files}")
            for conflict in conflicts:
                print(f"conflict\t{conflict.agent}\t{', '.join(conflict.shared)}")
        return 1 if conflicts else 0

    if args.command == "notify":
        if not root.path.is_dir():
            print(f"state root not found: {root.path}", file=sys.stderr)
            return 2
        try:
            results = notify_message(root.path, args.message_id, project_root, timeout=args.timeout, send=args.send)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.send and not root.database_path.is_file():
            print("no index: transport results not recorded (run init)", file=sys.stderr)
        if args.json:
            print(json.dumps([item.as_dict() for item in results], indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(format_notify(results))
        if not args.send:
            return 0
        if all(item.submitted for item in results):
            return 0
        if any(not item.submitted and item.method not in {"manual", "manual (claude)"} for item in results):
            return 1
        if any(item.method in {"manual", "manual (claude)"} for item in results):
            return 3
        return 1

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
    run.add_argument("--execute", action="store_true",
                     help="really launch the provider (default is a dry run: checks and shows the command, "
                          "writes nothing, starts nothing, uses no quota)")
    run.add_argument("--project-root", default=".", help="primary checkout (default: current directory)")
    run.add_argument("--state-root", default=None,
                     help="state root, absolute or relative to the project root (default: .multi-agent-ai)")


def _worker_main(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .workers.runner import UsageError, WorkerRequest, preview_worker, run_worker

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
        if not args.execute:
            preview = preview_worker(request)
            print(json.dumps(preview, indent=2, ensure_ascii=False))
            print("dry run: nothing was written or started; add --execute to launch", file=sys.stderr)
            return 1 if preview["problems"] else 0
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
