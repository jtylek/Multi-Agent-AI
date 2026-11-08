"""Native Linux provider discovery and process launch, using only local fake CLIs."""

import os
import sys

import pytest

from multi_agent_ai import winproc
from multi_agent_ai.job import provider_available

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable resolution")


def test_extensionless_cli_is_discovered_and_launched_without_a_shell(tmp_path):
    directory = tmp_path / "provider bin"
    directory.mkdir()
    cli = directory / "copilot"
    cli.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\"\n", encoding="utf-8")
    cli.chmod(0o755)
    env = winproc.build_env({**os.environ, "PATH": str(directory)})

    assert provider_available("copilot", env)
    command = winproc.resolve_command("copilot", env=env)
    assert command.kind == "exe"
    assert command.command_line(['a "quoted" argument']) == [str(cli), 'a "quoted" argument']
    result = winproc.run(command, ['a "quoted" argument'], cwd=tmp_path, env=env,
                         stdout_path=tmp_path / "out.log", stderr_path=tmp_path / "err.log", timeout=5)
    assert result.exit_code == 0
    assert (tmp_path / "out.log").read_text().strip() == 'a "quoted" argument'


def test_non_executable_path_entry_is_skipped(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "claude").write_text("not executable", encoding="utf-8")
    executable = second / "claude"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    assert winproc.resolve_command("claude", path=os.pathsep.join((str(first), str(second)))).path == executable
    with pytest.raises(winproc.LaunchError, match="executable not found"):
        winproc.resolve_command("claude", path=str(first))
