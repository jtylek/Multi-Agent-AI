"""Orchestrator-owned, shell-free verification of a worker worktree.

Verification is deliberately independent of worker exit status.  It checks the declared
commands and repository/file acceptance criteria, records every command's output in a run
directory, and distinguishes launch/preflight problems from verification failures.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import winproc

VERIFICATION_FAILED = "verification_failed"
ENVIRONMENT_ERROR = "environment_error"


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    timeout: float | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0]:
            raise ValueError("verification command must contain an executable")
        if any("\0" in arg for arg in self.argv):
            raise ValueError("verification command contains NUL")


@dataclass(frozen=True)
class VerificationSpec:
    commands: tuple[CommandSpec, ...] = ()
    expected_files: tuple[str, ...] = ()
    expected_clean: bool | None = None
    expected_commit: str | None = None
    timeout: float = 300.0
    preflight: tuple[CommandSpec, ...] = ()
    environment: tuple[str, ...] = ()


@dataclass
class CommandResult:
    argv: list[str]
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout_file: str
    stderr_file: str
    timed_out: bool = False
    error: str | None = None
    preflight: bool = False


@dataclass
class VerificationResult:
    status: str
    started: str
    finished: str
    duration_seconds: float
    run_dir: str
    commands: list[CommandResult] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    dirty: bool | None = None
    commit: str | None = None
    expected_commit: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _command(value: CommandSpec | Sequence[str]) -> CommandSpec:
    return value if isinstance(value, CommandSpec) else CommandSpec(tuple(str(part) for part in value))


def _run_dir(root: Path, supplied: Path | None) -> Path:
    path = supplied or root / ".multi-agent-ai" / "runs" / "verify" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _env(extra: Iterable[str], overrides: Mapping[str, str] | None) -> dict[str, str]:
    return winproc.build_env(extra_names=extra, overrides=overrides)


def _execute(
    spec: CommandSpec,
    *,
    cwd: Path,
    env: Mapping[str, str],
    run_dir: Path,
    index: int,
    default_timeout: float,
    preflight: bool = False,
) -> CommandResult:
    stdout = run_dir / f"{index:03d}-stdout.log"
    stderr = run_dir / f"{index:03d}-stderr.log"
    try:
        command = winproc.resolve_command(spec.argv[0], env=env)
    except (OSError, ValueError) as exc:
        stdout.touch()
        stderr.touch()
        return CommandResult(list(spec.argv), "environment_error", None, 0.0, str(stdout), str(stderr), error=str(exc), preflight=preflight)
    result = winproc.run(
        command, spec.argv[1:], cwd=cwd, env=env, stdout_path=stdout, stderr_path=stderr,
        timeout=spec.timeout if spec.timeout is not None else default_timeout,
    )
    if result.launch_error:
        status = "environment_error"
    elif result.timed_out or result.exit_code != 0:
        status = "failed"
    else:
        status = "passed"
    return CommandResult(
        list(spec.argv), status, result.exit_code, result.duration_seconds, str(stdout), str(stderr),
        result.timed_out, result.launch_error, preflight,
    )


def _git_command(cwd: Path, args: Sequence[str], env: Mapping[str, str], run_dir: Path, index: int) -> tuple[CommandResult, str]:
    result = _execute(CommandSpec(("git", *args)), cwd=cwd, env=env, run_dir=run_dir, index=index, default_timeout=30, preflight=True)
    output = Path(result.stdout_file).read_text(encoding="utf-8", errors="replace") if Path(result.stdout_file).is_file() else ""
    return result, output.strip()


def verify(
    worktree: Path | str,
    spec: VerificationSpec,
    *,
    run_dir: Path | str | None = None,
    environment: Mapping[str, str] | None = None,
) -> VerificationResult:
    """Run *spec* in *worktree* and return durable structured evidence.

    Commands are argv sequences and are never passed through a shell.  Preflight and launch
    failures produce ``environment_error``; failed commands or acceptance checks produce
    ``verification_failed``.  An empty command list is not a successful verification.
    """
    cwd = Path(worktree).resolve()
    started = datetime.now()
    directory = _run_dir(cwd, Path(run_dir) if run_dir is not None else None)
    env = _env(spec.environment, environment)
    result = VerificationResult("verification_failed", started.isoformat(), started.isoformat(), 0.0, str(directory), expected_commit=spec.expected_commit)
    clock = __import__("time").monotonic()
    if not cwd.is_dir():
        result.status, result.error = ENVIRONMENT_ERROR, f"worktree does not exist: {cwd}"
    elif not (cwd / ".git").exists():
        result.status, result.error = ENVIRONMENT_ERROR, f"not a Git worktree: {cwd}"
    elif not spec.commands:
        result.error = "no verification commands declared"
    else:
        index = 0
        for command in spec.preflight:
            item = _execute(_command(command), cwd=cwd, env=env, run_dir=directory, index=index, default_timeout=spec.timeout, preflight=True)
            index += 1
            result.commands.append(item)
            if item.status != "passed":
                result.status, result.error = ENVIRONMENT_ERROR, "preflight command failed"
                break
        if result.status != "environment_error":
            for command in spec.commands:
                item = _execute(_command(command), cwd=cwd, env=env, run_dir=directory, index=index, default_timeout=spec.timeout)
                index += 1
                result.commands.append(item)
                if item.status != "passed":
                    result.status = ENVIRONMENT_ERROR if item.status == ENVIRONMENT_ERROR else VERIFICATION_FAILED
                    result.error = item.error or "verification command failed"
                    break
            if result.commands and all(item.status == "passed" for item in result.commands) and not result.error:
                for relative in spec.expected_files:
                    if not (cwd / relative).is_file():
                        result.missing_files.append(relative)
                status, output = _git_command(cwd, ("status", "--porcelain"), env, directory, index)
                index += 1
                result.commands.append(status)
                if status.status == "environment_error":
                    result.status, result.error = ENVIRONMENT_ERROR, "Git preflight failed"
                else:
                    result.dirty = bool(output)
                    if spec.expected_clean is True and result.dirty:
                        result.error = "expected clean state" if spec.expected_clean else "expected dirty state"
                    elif spec.expected_clean is False and not result.dirty:
                        result.error = "expected dirty state"
                    head, commit = _git_command(cwd, ("rev-parse", "HEAD"), env, directory, index)
                    result.commands.append(head)
                    result.commit = commit or None
                    if head.status == "environment_error":
                        result.status, result.error = ENVIRONMENT_ERROR, "Git commit check failed"
                    elif spec.expected_commit and not (commit == spec.expected_commit or commit.startswith(spec.expected_commit)):
                        result.error = f"expected commit {spec.expected_commit}, got {commit}"
                if result.missing_files:
                    result.error = "expected files missing"
                if not result.error:
                    result.status = "passed"
    result.finished = datetime.now().isoformat()
    result.duration_seconds = round(__import__("time").monotonic() - clock, 3)
    (directory / "result.json").write_text(json.dumps(result.as_dict(), indent=2) + "\n", encoding="utf-8")
    return result


run_verification = verify
