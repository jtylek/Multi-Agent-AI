"""winproc: command resolution, quoting, streaming, timeout and process-tree kill.

Only fake programs in tmp_path and the test interpreter itself are run; nothing here can
reach a real provider CLI.
"""

import json
import sys
import time
from pathlib import Path

import pytest

from multi_agent_ai import winproc
from multi_agent_ai.winproc import LaunchError, ResolvedCommand, UnsafeArgument, build_env, resolve_command

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime")

NPM_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@github\copilot\npm-loader.js" %*
"""

ECHO = r'''
import json, sys
out = sys.argv[1]
data = {"argv": sys.argv[2:], "stdin": sys.stdin.read()}
open(out, "w", encoding="utf-8").write(json.dumps(data))
print("stdout-line")
print("stderr-line", file=sys.stderr)
'''

AWKWARD = ['say "hi"', "100% done", "a^b", "x & y | z", "line1\nline2", "zażółć ✓", "", "trailing\\",
           'back\\"slash', "(paren) <angle>", "!bang!"]


def py(tmp_path: Path) -> ResolvedCommand:
    return ResolvedCommand("python", Path(sys.executable), "exe", (sys.executable,))


def touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_resolution_prefers_exe_then_cmd_and_never_ps1(tmp_path):
    a, b = tmp_path / "a dir", tmp_path / "b"
    touch(a / "tool.ps1")
    touch(a / "tool")  # extensionless sh shim: never used
    with pytest.raises(LaunchError):
        resolve_command("tool", path=str(a))
    touch(a / "tool.cmd", "@echo off\r\n")
    assert resolve_command("tool", path=str(a)).path == a / "tool.cmd"
    assert resolve_command("tool", path=str(a)).kind == "batch"
    touch(a / "tool.exe")
    assert resolve_command("tool", path=str(a)).path == a / "tool.exe"
    # PATH order wins across directories
    touch(b / "tool.cmd", "@echo off\r\n")
    assert resolve_command("tool", path=f"{b};{a}").path == b / "tool.cmd"


def test_absolute_ps1_and_missing_are_launch_errors(tmp_path):
    with pytest.raises(LaunchError, match="never .ps1"):
        resolve_command(str(touch(tmp_path / "x.ps1")))
    with pytest.raises(LaunchError):
        resolve_command("definitely-not-here", path=str(tmp_path))


def test_npm_shim_resolves_to_node_and_script(tmp_path):
    shim_dir = tmp_path / "npm dir"
    touch(shim_dir / "copilot.cmd", NPM_SHIM)
    with pytest.raises(LaunchError, match="npm shim target"):
        resolve_command("copilot", path=str(shim_dir))
    script = touch(shim_dir / "node_modules" / "@github" / "copilot" / "npm-loader.js")
    with pytest.raises(LaunchError, match="node.exe"):
        resolve_command("copilot", path=str(shim_dir))
    node_dir = tmp_path / "nodejs"
    touch(node_dir / "node.exe")
    resolved = resolve_command("copilot", path=f"{shim_dir};{node_dir}")
    assert resolved.kind == "npm-shim"
    assert resolved.prefix == (str(node_dir / "node.exe"), str(script))
    touch(shim_dir / "node.exe")  # node next to the shim wins
    assert resolve_command("copilot", path=f"{shim_dir};{node_dir}").prefix[0] == str(shim_dir / "node.exe")
    # argv for an npm shim is a list (CreateProcess quoting), never a cmd.exe string
    assert isinstance(resolved.command_line(AWKWARD), list)


def test_exe_argv_and_stdin_arrive_unchanged(tmp_path):
    script = touch(tmp_path / "echo me.py", ECHO)
    out = tmp_path / "out.json"
    stdin = touch(tmp_path / "prompt.md", 'prompt "with" % ^ & | newline\nżółw\n')
    result = winproc.run(py(tmp_path), [str(script), str(out), *AWKWARD], cwd=tmp_path, env=build_env(),
                         stdout_path=tmp_path / "logs" / "stdout.log", stderr_path=tmp_path / "logs" / "stderr.log",
                         stdin_path=stdin, timeout=60)
    assert result.exit_code == 0 and not result.timed_out and result.launch_error is None
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["argv"] == AWKWARD
    assert data["stdin"] == stdin.read_text(encoding="utf-8")
    assert (tmp_path / "logs" / "stdout.log").read_text().strip() == "stdout-line"
    assert (tmp_path / "logs" / "stderr.log").read_text().strip() == "stderr-line"
    assert result.tree_control == "job-object"


def test_stdin_is_closed_when_no_file(tmp_path):
    script = touch(tmp_path / "e.py", ECHO)
    out = tmp_path / "o.json"
    winproc.run(py(tmp_path), [str(script), str(out)], cwd=tmp_path, env=build_env(),
                stdout_path=tmp_path / "1.log", stderr_path=tmp_path / "2.log", timeout=60)
    assert json.loads(out.read_text(encoding="utf-8"))["stdin"] == ""


def test_batch_file_runs_through_cmd_with_safe_awkward_args(tmp_path):
    script = touch(tmp_path / "echo me.py", ECHO)
    batch = touch(tmp_path / "dir (x86) & co" / "fake tool.cmd", f'@"{sys.executable}" "{script}" %*\r\n')
    out = tmp_path / "out.json"
    resolved = resolve_command("fake tool", path=str(batch.parent))
    safe = ["a b", "x & y", "(p)", "a^b", "<in>", "|pipe|", "!bang!", "ż"]
    result = winproc.run(resolved, [str(out), *safe], cwd=tmp_path, env=build_env(),
                         stdout_path=tmp_path / "1.log", stderr_path=tmp_path / "2.log", timeout=60)
    assert result.exit_code == 0, (tmp_path / "2.log").read_text(errors="replace")
    assert json.loads(out.read_text(encoding="utf-8"))["argv"] == safe


@pytest.mark.parametrize("arg", ['say "hi"', "100%", "a\nb", "a\rb"])
def test_batch_file_refuses_unsafe_args(tmp_path, arg):
    batch = touch(tmp_path / "t.cmd", "@echo off\r\n")
    resolved = resolve_command(str(batch))
    with pytest.raises(UnsafeArgument):
        resolved.command_line([arg])
    result = winproc.run(resolved, [arg], cwd=tmp_path, env=build_env(), stdout_path=tmp_path / "1.log",
                         stderr_path=tmp_path / "2.log", timeout=10)
    assert result.launch_error and "unsafe argument" in result.launch_error and result.exit_code is None


TREE = r'''
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
if sys.argv[2] == "sleep":
    time.sleep(120)
'''


def wait_dead(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not winproc.process_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_timeout_kills_the_whole_process_tree(tmp_path):
    script = touch(tmp_path / "tree.py", TREE)
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    result = winproc.run(py(tmp_path), [str(script), str(pid_file), "sleep"], cwd=tmp_path, env=build_env(),
                         stdout_path=tmp_path / "1.log", stderr_path=tmp_path / "2.log", timeout=3)
    assert result.timed_out and result.kill_method == "job-object"
    assert time.monotonic() - started < 30
    grandchild = int(pid_file.read_text())
    assert wait_dead(grandchild)
    assert not winproc.process_alive(result.pid)


def test_cancel_event_kills_the_whole_process_tree_and_is_not_a_timeout(tmp_path):
    import threading
    script = touch(tmp_path / "tree.py", TREE)
    pid_file = tmp_path / "grandchild.pid"
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    started = time.monotonic()
    result = winproc.run(py(tmp_path), [str(script), str(pid_file), "sleep"], cwd=tmp_path, env=build_env(),
                         stdout_path=tmp_path / "1.log", stderr_path=tmp_path / "2.log", timeout=60, cancel=cancel)
    assert result.cancelled and not result.timed_out and result.kill_method == "job-object"
    assert time.monotonic() - started < 30
    assert wait_dead(int(pid_file.read_text())) and not winproc.process_alive(result.pid)


def test_cancel_event_not_set_changes_nothing(tmp_path):
    import threading
    script = touch(tmp_path / "tree.py", TREE)
    pid_file = tmp_path / "grandchild.pid"
    result = winproc.run(py(tmp_path), [str(script), str(pid_file), "exit"], cwd=tmp_path, env=build_env(),
                         stdout_path=tmp_path / "1.log", stderr_path=tmp_path / "2.log", timeout=60,
                         cancel=threading.Event())
    assert result.exit_code == 0 and not result.cancelled and not result.timed_out


def test_timeout_still_wins_when_cancel_is_given_but_never_set(tmp_path):
    import threading
    script = touch(tmp_path / "tree.py", TREE)
    result = winproc.run(py(tmp_path), [str(script), str(tmp_path / "g.pid"), "sleep"], cwd=tmp_path, env=build_env(),
                         stdout_path=tmp_path / "1.log", stderr_path=tmp_path / "2.log", timeout=2,
                         cancel=threading.Event())
    assert result.timed_out and not result.cancelled and result.kill_method == "job-object"


def test_descendants_do_not_outlive_a_normal_exit(tmp_path):
    script = touch(tmp_path / "tree.py", TREE)
    pid_file = tmp_path / "grandchild.pid"
    result = winproc.run(py(tmp_path), [str(script), str(pid_file), "exit"], cwd=tmp_path, env=build_env(),
                         stdout_path=tmp_path / "1.log", stderr_path=tmp_path / "2.log", timeout=60)
    assert result.exit_code == 0 and not result.timed_out
    assert wait_dead(int(pid_file.read_text()))


def test_launch_failure_is_returned_not_raised(tmp_path):
    missing = ResolvedCommand("x", tmp_path / "missing.exe", "exe", (str(tmp_path / "missing.exe"),))
    result = winproc.run(missing, [], cwd=tmp_path, env=build_env(), stdout_path=tmp_path / "1.log",
                         stderr_path=tmp_path / "2.log", timeout=5)
    assert result.launch_error and result.exit_code is None and result.tree_control == "none"


def test_env_is_an_allowlist_and_never_passes_blanket_permissions():
    base = {"Path": r"C:\x", "SystemRoot": r"C:\Windows", "OPENAI_API_KEY": "sk-secret", "GH_TOKEN": "t",
            "COPILOT_ALLOW_ALL": "true", "CODEX_HOME": r"C:\c", "AWS_SECRET_ACCESS_KEY": "s"}
    env = build_env(base, ("CODEX_HOME", "copilot_allow_all"))
    assert env == {"Path": r"C:\x", "SystemRoot": r"C:\Windows", "CODEX_HOME": r"C:\c"}
    assert "COPILOT_ALLOW_ALL" not in build_env(base, (), {"COPILOT_ALLOW_ALL": "1"})
    assert build_env(base, ("OPENAI_API_KEY",))["OPENAI_API_KEY"] == "sk-secret"  # only when named


def test_cmd_exe_comes_from_systemroot_not_comspec():
    assert winproc.cmd_exe_path({"SystemRoot": r"D:\Win", "COMSPEC": r"C:\evil.exe"}) == r"D:\Win\System32\cmd.exe"
