"""Orchestrator-owned, shell-free verification of a worker worktree.

Verification is deliberately independent of worker exit status.  It checks the declared
commands and repository/file acceptance criteria, records every command's output in a run
directory, and distinguishes launch/preflight problems from verification failures.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import time
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
    path = supplied or Path(tempfile.gettempdir()) / "multi-agent-ai" / "runs" / "verify" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _env(extra: Iterable[str], overrides: Mapping[str, str] | None) -> dict[str, str]:
    base = dict(os.environ)
    if overrides:
        base.update(overrides)
    return winproc.build_env(base, extra_names=extra)


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
        command = _resolve_command(spec.argv[0], cwd, env)
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


def _resolve_command(name: str, cwd: Path, env: Mapping[str, str]) -> winproc.ResolvedCommand:
    """Resolve relative executables from the verified worktree before PATH."""
    candidate = Path(name)
    if not candidate.is_absolute() and ("\\" in name or "/" in name or candidate.suffix == ""):
        local = cwd / candidate
        if local.is_file():
            return winproc.resolve_command(str(local), env=env)
        if not local.suffix and local.with_suffix(".exe").is_file():
            return winproc.resolve_command(str(local.with_suffix(".exe")), env=env)
    return winproc.resolve_command(name, env=env)


def parse_command(text: str) -> tuple[str, ...]:
    """Parse a plan command as Windows argv, rejecting shell syntax."""
    if not text.strip():
        raise ValueError("verification command is empty")
    if re.search(r"[|&<>;]", text) or re.search(r"%[^%\r\n]+%", text):
        raise ValueError("verification command contains shell syntax; declare argv without |, &, >, <, ;, or %VAR%")
    try:
        parts = shlex.split(text, posix=False)
    except ValueError as exc:
        raise ValueError(f"invalid Windows verification argv: {exc}") from exc
    cleaned = tuple(part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'" else part for part in parts)
    if not cleaned:
        raise ValueError("verification command is empty")
    return cleaned


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _validate_spec(cwd: Path, spec: VerificationSpec) -> None:
    if spec.expected_commit == "":
        raise ValueError("expected_commit must be a 7-40 character hexadecimal SHA")
    if spec.expected_commit is not None and not re.fullmatch(r"[0-9a-fA-F]{7,40}", spec.expected_commit):
        raise ValueError("expected_commit must be a 7-40 character hexadecimal SHA")
    for value in spec.expected_files:
        path = (cwd / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        if not _inside(path, cwd):
            raise ValueError(f"expected file must be inside the worktree: {value}")


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
    _validate_spec(cwd, spec)
    started = datetime.now()
    directory = _run_dir(cwd, Path(run_dir) if run_dir is not None else None)
    env = _env(spec.environment, environment)
    result = VerificationResult(VERIFICATION_FAILED, started.isoformat(), started.isoformat(), 0.0,
                                str(directory), expected_commit=spec.expected_commit)
    clock = time.monotonic()
    index = 0
    if not cwd.is_dir():
        result.status, result.error = ENVIRONMENT_ERROR, f"worktree does not exist: {cwd}"
    elif not (cwd / ".git").exists():
        result.status, result.error = ENVIRONMENT_ERROR, f"not a Git worktree: {cwd}"
    elif not spec.commands and result.error is None:
        result.error = "no verification commands declared"
    elif result.error is None:
        for command in spec.preflight:
            item = _execute(_command(command), cwd=cwd, env=env, run_dir=directory, index=index,
                            default_timeout=spec.timeout, preflight=True)
            index += 1
            result.commands.append(item)
            if item.status != "passed":
                result.status, result.error = ENVIRONMENT_ERROR, "preflight command failed"
                break
        if result.error is None:
            for command in spec.commands:
                item = _execute(_command(command), cwd=cwd, env=env, run_dir=directory, index=index,
                                default_timeout=spec.timeout)
                index += 1
                result.commands.append(item)
                if item.status != "passed":
                    result.status = ENVIRONMENT_ERROR if item.status == ENVIRONMENT_ERROR else VERIFICATION_FAILED
                    result.error = item.error or "verification command failed"
                    break

    # Always capture repository evidence, including after command/preflight failure.
    if cwd.is_dir() and (cwd / ".git").exists():
        status, output = _git_command(cwd, ("status", "--porcelain"), env, directory, index)
        index += 1
        result.commands.append(status)
        if status.status == ENVIRONMENT_ERROR:
            result.status, result.error = ENVIRONMENT_ERROR, result.error or "Git status check failed"
        else:
            relative_run = None
            if _inside(directory, cwd):
                relative_run = directory.relative_to(cwd).as_posix().rstrip("/")
            lines = []
            for line in output.splitlines():
                path_text = line[3:].strip().split(" -> ", 1)[-1] if len(line) > 3 else ""
                if not relative_run or not (path_text == relative_run or path_text.startswith(relative_run + "/")):
                    lines.append(line)
            result.dirty = bool(lines)
        head, commit = _git_command(cwd, ("rev-parse", "HEAD"), env, directory, index)
        result.commands.append(head)
        result.commit = commit or None
        if head.status == ENVIRONMENT_ERROR:
            result.status, result.error = ENVIRONMENT_ERROR, result.error or "Git HEAD check failed"

    if result.error is None:
        for relative in spec.expected_files:
            path = (cwd / relative).resolve() if not Path(relative).is_absolute() else Path(relative).resolve()
            if not path.is_file():
                result.missing_files.append(relative)
        if result.missing_files:
            result.error = "expected files missing: " + ", ".join(result.missing_files)
        elif spec.expected_clean is True and result.dirty:
            result.error = "expected clean state"
        elif spec.expected_clean is False and not result.dirty:
            result.error = "expected dirty state"
        elif spec.expected_commit and not result.commit.startswith(spec.expected_commit):
            result.error = f"expected commit {spec.expected_commit}, got {result.commit}"
    if result.error is None and result.commands and all(item.status == "passed" for item in result.commands):
        result.status = "passed"
    result.finished = datetime.now().isoformat()
    result.duration_seconds = round(time.monotonic() - clock, 3)
    (directory / "result.json").write_text(json.dumps(result.as_dict(), indent=2) + "\n", encoding="utf-8")
    return result


run_verification = verify
