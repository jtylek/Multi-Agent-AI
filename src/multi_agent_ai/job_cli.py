"""``multi-agent-ai job ...`` (T27): parser and handler, kept apart from cli.py.

``job start`` is a dry run unless ``--execute`` is given. There is deliberately no ``job approve``:
merging and pushing are the user's own Git commands (decisions.md D19).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import job as jb
from .state import resolve_state_root


def add_parser(sub: argparse._SubParsersAction) -> None:
    job = sub.add_parser("job", help="run a whole plan end to end (dry run unless --execute)")
    jsub = job.add_subparsers(dest="job_command", required=True)

    def common(cmd: argparse.ArgumentParser, *, task: bool = False, job_id: bool = False) -> None:
        if task:
            cmd.add_argument("--task", required=True, help="task id (tasks/<task-id>/plan.md)")
        if job_id:
            cmd.add_argument("--job", required=job_id == "required", default=None, help="job id")
        cmd.add_argument("--project-root", default=".", help="primary checkout (default: current directory)")
        cmd.add_argument("--state-root", default=None,
                         help="state root, absolute or relative to the project root (default: .multi-agent-ai)")
        cmd.add_argument("--json", action="store_true", help="machine-readable output")

    def options(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--providers", default="codex,copilot",
                         help="comma-separated provider pool for workers and reviewers (default: codex,copilot)")
        cmd.add_argument("--assign", action="append", default=[], metavar="SUBTASK=PROVIDER[:MODEL]",
                         help="override the provider (and model) of a subtask; repeatable")
        cmd.add_argument("--max-parallel", type=int, default=2, help=f"concurrent workers, 1..{jb.MAX_PARALLEL_CAP} (default 2)")
        cmd.add_argument("--max-per-provider", type=int, default=2, help="concurrent workers per provider (default 2)")
        cmd.add_argument("--worker-timeout", type=float, default=1800.0, help="seconds per implement attempt")
        cmd.add_argument("--review-timeout", type=float, default=1800.0, help="seconds per review attempt")
        cmd.add_argument("--job-timeout", type=float, default=None, help="seconds for the whole job (default: none)")
        cmd.add_argument("--verify-command", action="append", default=[],
                         help="the full-suite command run on the integration branch; required for --execute; repeatable")
        cmd.add_argument("--live-branch", action="append", default=[], help="extra protected branch")
        cmd.add_argument("--pass-env", action="append", default=[], help="extra environment variable name to pass to workers")
        cmd.add_argument("--worktrees-root", default=None, help="parent directory of the worktrees")

    plan = jsub.add_parser("plan", help="read-only: what the driver would do")
    common(plan, task=True)
    options(plan)
    start = jsub.add_parser("start", help="create a job and run it until a gate (dry run unless --execute)")
    common(start, task=True)
    options(start)
    start.add_argument("--execute", action="store_true", help="really run the job (default: dry run)")
    start.add_argument("--approved-provider-run", action="store_true",
                       help="you were told the user approved this real provider run (recorded in the job events; "
                            "a speed bump for agents, NOT a security control)")
    resume = jsub.add_parser("resume", help="continue a paused or interrupted job from its events (dry run unless --execute)")
    common(resume, job_id="required")
    resume.add_argument("--execute", action="store_true", help="really continue the job (default: dry run)")
    resume.add_argument("--retry", action="append", default=[], metavar="SUBTASK",
                        help="explicitly retry a failed, cancelled or stale subtask (resets its correction budget); repeatable")
    resume.add_argument("--force-takeover", action="store_true",
                        help="take the driver lock from a process that is alive but sent no heartbeat for 10 minutes "
                             "(only if you are sure it is hung)")
    resume.add_argument("--verify-command", action="append", default=[], help="override the full-suite command")
    resume.add_argument("--approved-provider-run", action="store_true",
                        help="you were told the user approved this real provider run (a speed bump for agents, NOT a security control)")
    cancel = jsub.add_parser("cancel", help="stop a job: kill its running workers, keep all evidence (dry run unless --execute)")
    common(cancel, job_id="required")
    cancel.add_argument("--execute", action="store_true", help="really cancel (default: dry run)")
    common(jsub.add_parser("status", help="read-only: one job, or all jobs without --job"), job_id="optional")
    common(jsub.add_parser("report", help="print the run report of a job"), job_id="required")


def _options(args: argparse.Namespace) -> jb.JobOptions:
    return jb.JobOptions(
        providers=tuple(p for p in args.providers.split(",") if p), assign=jb.parse_assign(args.assign),
        max_parallel=args.max_parallel, max_per_provider=args.max_per_provider, worker_timeout=args.worker_timeout,
        review_timeout=args.review_timeout, job_timeout=args.job_timeout, verify_commands=tuple(args.verify_command),
        live_branches=tuple(args.live_branch), pass_env=tuple(args.pass_env), worktrees_root=args.worktrees_root,
        approved_provider_run=getattr(args, "approved_provider_run", False))


def main(args: argparse.Namespace) -> int:
    root = resolve_state_root(args.project_root, args.state_root)
    if not root.path.is_dir():
        print(f"state root not found: {root.path} (run 'multi-agent-ai init' or pass --state-root)", file=sys.stderr)
        return 2
    command = args.job_command
    try:
        if command in ("plan", "start"):
            options = _options(args)
            plan = jb.build_plan(root, args.task, options)
            execute = command == "start" and args.execute
            if args.json:
                print(json.dumps(asdict(plan), indent=2, ensure_ascii=False))
            else:
                print(jb.format_plan(plan))
            if not execute:
                if command == "start":
                    print("dry run: nothing was changed; add --execute (and --approved-provider-run) to run it", file=sys.stderr)
                return 0 if plan.ok else 1
            if not plan.ok:
                return 1
            if not options.verify_commands:
                raise jb.JobError("--verify-command is required with --execute")
            if not options.approved_provider_run:
                raise jb.JobError("--execute launches real provider CLIs: pass --approved-provider-run once the user "
                                  "has approved the run (a speed bump for agents, not a security control)")
            from . import job_run

            job = jb.create_job(root, plan, options)
            print(f"job {job.job_id} created; running (Ctrl+C stops the driver, workers are not left behind)", file=sys.stderr)
            final = job_run.run_job(root, Path(args.project_root).resolve(), job.job_id, options)
            print(json.dumps(final.as_dict(), indent=2, ensure_ascii=False) if args.json else jb.format_status(final))
            return 0 if final.state == "awaiting_approval" else 1
        if command == "resume":
            job, warnings = jb.load_job(root, args.job)
            if not args.execute:
                print(jb.format_status(job))
                print(f"dry run: would reconcile unfinished attempts, retry {', '.join(args.retry) or 'nothing'} and continue; "
                      "add --execute --approved-provider-run to do it", file=sys.stderr)
                return 0
            if not args.approved_provider_run:
                raise jb.JobError("--execute launches real provider CLIs: pass --approved-provider-run once the user has "
                                  "approved the run (a speed bump for agents, not a security control)")
            from . import job_run

            final = job_run.resume_job(root, Path(args.project_root).resolve(), args.job, retry=tuple(args.retry),
                                       verify_commands=tuple(args.verify_command),
                                       force_takeover=args.force_takeover)
            print(json.dumps(final.as_dict(), indent=2, ensure_ascii=False) if args.json else jb.format_status(final))
            return 0 if final.state == "awaiting_approval" else 1
        if command == "cancel":
            from . import job_run

            print(job_run.request_cancel(root, args.job, execute=args.execute))
            if not args.execute:
                print("dry run: nothing was changed; add --execute to cancel", file=sys.stderr)
            return 0
        if command == "status":
            if args.job:
                job, warnings = jb.load_job(root, args.job)
                print(json.dumps(job.as_dict(), indent=2, ensure_ascii=False) if args.json else jb.format_status(job))
                for warning in warnings:
                    print(f"warning: {warning}", file=sys.stderr)
            else:
                jobs = jb.list_jobs(root)
                if args.json:
                    print(json.dumps([j.as_dict() for j in jobs], indent=2, ensure_ascii=False))
                for j in ([] if args.json else jobs):
                    print(f"{j.job_id}\t{j.task_id}\t{j.state}\t{j.updated}")
            return 0
        if command == "report":
            job, warnings = jb.load_job(root, args.job)
            report = jb.build_report(job, warnings)
            print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else jb.format_report(report))
            return 0
    except jb.JobError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
