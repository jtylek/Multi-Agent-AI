"""``multi-agent-ai doctor``: read-only readiness checks with plain-language fixes (T20).

Runs nothing beyond ``git --version`` / ``git rev-parse`` and each provider CLI's own
``--version`` (a local flag; see :mod:`multi_agent_ai.workers.versions`). It never calls a
provider: whether a CLI is signed in cannot be checked without one, so that is reported as
``not checked``. It writes nothing, not even to the state root. Missing provider CLIs are
warnings (you only need the ones you use) unless none is installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from . import winproc
from .state import StateRoot
from .workers import gitops
from .workers.runner import ADAPTERS
from .workers.versions import CliVersion, read_cli_version

OK, WARN, FAIL, NOT_CHECKED = "ok", "warn", "fail", "not checked"
MIN_PYTHON = (3, 11)

PROVIDERS = (
    ("codex", "Codex CLI", "Install it with 'npm install -g @openai/codex', then open a new terminal.",
     "Run 'codex login' once."),
    ("copilot", "GitHub Copilot CLI", "Install it with 'npm install -g @github/copilot', then open a new terminal.",
     "Run 'copilot login' once."),
    ("claude", "Claude Code", "Install Claude Code from https://claude.com/claude-code, then open a new terminal.",
     "Start 'claude' once and use /login."),
)
"""(command, display name, how to install, how to sign in)."""


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def check_python(version: tuple[int, ...] | None = None) -> Check:
    version = version or tuple(sys.version_info[:3])
    text = ".".join(str(p) for p in version)
    if tuple(version[:2]) >= MIN_PYTHON:
        return Check("Python", OK, f"{text} (3.11 or newer is required)")
    return Check("Python", FAIL, f"{text} is too old; 3.11 or newer is required",
                 "Install Python 3.11 or newer (3.12 recommended) from python.org and recreate the virtual environment.")


def _git_probe(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    result = subprocess.run([gitops.git_executable(), *args], cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                            capture_output=True, shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.returncode, (result.stdout or result.stderr).decode("utf-8", "replace").strip()


def check_git(project_root: Path, probe: Callable[..., tuple[int, str]] = _git_probe) -> list[Check]:
    try:
        code, text = probe("--version")
    except (gitops.GitError, OSError) as exc:
        return [Check("Git", FAIL, f"git.exe was not found ({exc})",
                      "Install Git for Windows (https://git-scm.com/download/win) and open a new terminal.")]
    if code != 0:
        return [Check("Git", FAIL, f"'git --version' failed: {text}", "Reinstall Git for Windows.")]
    checks = [Check("Git", OK, text)]
    try:
        code, inside = probe("rev-parse", "--is-inside-work-tree", cwd=project_root)
    except (gitops.GitError, OSError) as exc:
        checks.append(Check("Project repository", FAIL, f"could not run git in {project_root}: {exc}"))
        return checks
    if code == 0 and inside == "true":
        checks.append(Check("Project repository", OK, f"{project_root} is a Git working tree"))
    else:
        checks.append(Check("Project repository", FAIL, f"{project_root} is not inside a Git working tree",
                            "Run doctor from your project's main checkout, or pass --project-root <path>."))
    return checks


def check_provider(
    command: str, display: str, install: str, signin: str, env: Mapping[str, str],
    resolver: Callable[..., winproc.ResolvedCommand], version_reader: Callable[..., CliVersion],
) -> list[Check]:
    try:
        resolved = resolver(command, env=env)
    except winproc.LaunchError as exc:
        return [Check(display, WARN, f"not found ({exc})", install)]
    adapter = ADAPTERS.get(command)
    child_env = winproc.build_env(env, adapter.env_names if adapter else ())
    version = version_reader(resolved, child_env)
    checks = [Check(display, OK, f"found at {resolved.path}")]
    if version.version:
        checks.append(Check(f"{display} version", OK, version.version))
    else:
        checks.append(Check(f"{display} version", WARN, f"could not be read ({version.error})",
                            "Run '" + command + " --version' yourself; if that fails, reinstall the CLI."))
    checks.append(Check(
        f"{display} sign-in", NOT_CHECKED,
        "cannot be verified without contacting the provider, which doctor never does",
        f"{signin} If a run later reports auth_error, sign in again.",
    ))
    return checks


def check_state_root(root: StateRoot) -> list[Check]:
    if not root.path.is_dir():
        return [Check("State root", FAIL, f"{root.path} does not exist",
                      "Run 'multi-agent-ai init --project-root <your project>' to create it.")]
    checks = [Check("State root", OK, str(root.path))]
    missing = [p.name for p in (root.messages, root.tasks) if not p.is_dir()]
    if missing:
        checks.append(Check("State root layout", WARN, f"missing: {', '.join(missing)}",
                            "Run 'multi-agent-ai init' again; it never touches existing files."))
    if root.database_path.is_file():
        checks.append(Check("State index", OK, str(root.database_path)))
    else:
        checks.append(Check("State index", WARN, "no SQLite index yet",
                            "Run 'multi-agent-ai init' (or 'multi-agent-ai index') to create it."))
    return checks


def check_timezones() -> Check:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("Europe/Budapest")
    except Exception:
        return Check("Time zone data", WARN,
                     "this Python has no IANA time zone database",
                     "Run 'pip install tzdata' so provider reset times given in a named zone can be read; "
                     "without it they are shown as 'reset unknown'.")
    return Check("Time zone data", OK, "IANA time zones available")


def run_doctor(
    project_root: Path,
    root: StateRoot,
    *,
    env: Mapping[str, str] | None = None,
    resolver: Callable[..., winproc.ResolvedCommand] | None = None,
    version_reader: Callable[..., CliVersion] | None = None,
    git_probe: Callable[..., tuple[int, str]] = _git_probe,
    python_version: tuple[int, ...] | None = None,
) -> list[Check]:
    env = env if env is not None else os.environ
    resolver = resolver or winproc.resolve_command
    version_reader = version_reader or read_cli_version
    checks = [check_python(python_version), *check_git(project_root, git_probe)]
    found = 0
    for command, display, install, signin in PROVIDERS:
        provider_checks = check_provider(command, display, install, signin, env, resolver, version_reader)
        found += provider_checks[0].status == OK
        checks.extend(provider_checks)
    if not found:
        checks.append(Check("Provider CLIs", FAIL, "none of Codex, Copilot or Claude Code was found",
                            "Install at least one of the CLIs listed above."))
    checks.extend(check_state_root(root))
    checks.append(check_timezones())
    return checks


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0


def format_text(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        lines.append(f"[{c.status.upper():^11}] {c.name:<{width}}  {c.detail}")
        if c.fix and c.status != OK:
            lines.append(f"{'':15}{'':<{width}}  -> {c.fix}")
    counts = {s: sum(c.status == s for c in checks) for s in (OK, WARN, FAIL, NOT_CHECKED)}
    lines.append("")
    lines.append(f"{counts[OK]} ok, {counts[WARN]} warning(s), {counts[FAIL]} failure(s), "
                 f"{counts[NOT_CHECKED]} not checked. Nothing was changed.")
    return "\n".join(lines) + "\n"


def to_dict(checks: list[Check]) -> dict:
    return {"ok": exit_code(checks) == 0, "checks": [asdict(c) for c in checks]}
