"""CLI version parsing and the bounded ``--version`` probe (T18). Fake runner only: no CLI is started."""

from pathlib import Path

import pytest

from multi_agent_ai import winproc
from multi_agent_ai.workers.versions import PROBE_TIMEOUT, parse_version, read_cli_version

COMMAND = winproc.ResolvedCommand("codex", Path("codex.exe"), "exe", ("codex.exe",))


@pytest.mark.parametrize("text, expected", [
    ("codex-cli 0.163.0", "0.163.0"),
    ("GitHub Copilot CLI 1.0.4.", "1.0.4"),
    ("1.2.3 (Claude Code)", "1.2.3"),
    ("v2.10.0-beta.1", "2.10.0-beta.1"),
    ("tool 3.1.4+build.7 extra", "3.1.4+build.7"),
    ("no digits here", None),
    ("version 1.2", None),
    ("", None),
])
def test_parse_version(text, expected):
    assert parse_version(text) == expected


def fake_runner(stdout="", stderr="", exit_code=0, timed_out=False, launch_error=None, seen=None):
    def runner(command, args, *, cwd, env, stdout_path, stderr_path, stdin_path, timeout):
        if seen is not None:
            seen.update(args=list(args), cwd=cwd, stdin=stdin_path, timeout=timeout, env=dict(env))
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        return winproc.RunResult(None if launch_error else exit_code, timed_out, None, None, 0.0,
                                 launch_error=launch_error)
    return runner


def test_probe_runs_only_version_with_a_closed_stdin_and_a_short_timeout():
    seen = {}
    result = read_cli_version(COMMAND, {"PATH": "x"}, runner=fake_runner("codex-cli 0.163.0\n", seen=seen))
    assert result.version == "0.163.0" and result.error is None and result.raw == "codex-cli 0.163.0"
    assert seen["args"] == ["--version"] and seen["stdin"] is None and seen["timeout"] == PROBE_TIMEOUT
    assert seen["env"] == {"PATH": "x"} and not Path(seen["cwd"]).exists()  # scratch dir is removed


def test_version_on_stderr_is_accepted():
    assert read_cli_version(COMMAND, {}, runner=fake_runner(stderr="tool 4.5.6")).version == "4.5.6"


@pytest.mark.parametrize("kwargs, message", [
    (dict(stdout="boom", exit_code=2), "exited 2"),
    (dict(timed_out=True, exit_code=None), "timed out"),
    (dict(launch_error="not found"), "could not start: not found"),
    (dict(stdout="hello world"), "no x.y.z"),
])
def test_failed_probe_gives_no_version_and_a_reason(kwargs, message):
    result = read_cli_version(COMMAND, {}, runner=fake_runner(**kwargs))
    assert result.version is None and message in result.error


def test_raw_output_is_truncated():
    assert len(read_cli_version(COMMAND, {}, runner=fake_runner("x " * 500 + "1.2.3")).raw) <= 300
