"""Verification engine tests use only temporary Git repositories and fake commands."""

import json
import subprocess
import sys
import shutil
from pathlib import Path

import pytest

from multi_agent_ai.cli import main
from multi_agent_ai.state import init_state_root
from multi_agent_ai.verify import CommandSpec, VerificationSpec, verify


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="verification command resolution is Windows-native")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Verification Test")
    (path / "README.md").write_text("test\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial")
    return path


def command(code: str) -> CommandSpec:
    return CommandSpec((sys.executable, "-c", code))


def test_verify_records_outputs_and_expected_file(tmp_path):
    path = repo(tmp_path)
    result = verify(path, VerificationSpec(
        commands=(command("from pathlib import Path; Path('result.txt').write_text('ok')"),),
        expected_files=("result.txt",), expected_clean=False,
    ), run_dir=tmp_path / "run")
    assert result.ok
    assert result.status == "passed"
    assert result.dirty is True
    assert Path(result.commands[0].stdout_file).is_file()
    assert (tmp_path / "run" / "result.json").is_file()


def test_missing_executable_is_environment_error(tmp_path):
    path = repo(tmp_path)
    result = verify(path, VerificationSpec(commands=(CommandSpec(("definitely-missing.exe",)),)))
    assert result.status == "environment_error"
    assert result.commands[0].status == "environment_error"


def test_nonzero_command_is_verification_failure(tmp_path):
    path = repo(tmp_path)
    result = verify(path, VerificationSpec(commands=(command("raise SystemExit(3)"),)))
    assert result.status == "verification_failed"
    assert result.commands[0].exit_code == 3


def test_relative_worktree_executable_is_resolved_from_cwd(tmp_path, monkeypatch):
    path = repo(tmp_path)
    fake = path / ".venv" / "Scripts" / "python.exe"
    fake.parent.mkdir(parents=True)
    shutil.copy2(sys.executable, fake)
    # A copied interpreter under a venv-shaped path otherwise exits with
    # ``No pyvenv.cfg file`` before command execution.
    (fake.parent.parent / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    result = verify(path, VerificationSpec(commands=(CommandSpec((r".venv\Scripts\python", "-c", "pass")),)))
    assert result.ok


def test_preflight_failure_is_environment_error_and_evidence_is_recorded(tmp_path):
    path = repo(tmp_path)
    result = verify(path, VerificationSpec(commands=(command("pass"),), preflight=(command("raise SystemExit(2)"),)))
    assert result.status == "environment_error"
    assert result.dirty is False and result.commit
    assert len(result.commands) == 3  # preflight plus best-effort status and HEAD


def test_environment_is_allowlisted(tmp_path):
    path = repo(tmp_path)
    result = verify(path, VerificationSpec(
        commands=(command("import os; open('env.txt','w').write(os.getenv('VISIBLE','') + '|' + os.getenv('SECRET',''))"),),
        expected_files=("env.txt",), expected_clean=False, environment=("VISIBLE",),
    ), environment={"VISIBLE": "yes", "SECRET": "no"})
    assert result.ok
    assert (path / "env.txt").read_text(encoding="utf-8") == "yes|"


def test_multiple_commands_stop_at_first_failure_and_keep_git_evidence(tmp_path):
    path = repo(tmp_path)
    result = verify(path, VerificationSpec(commands=(command("raise SystemExit(1)"), command("open('later.txt','w')"))))
    assert result.status == "verification_failed"
    assert not (path / "later.txt").exists()
    assert result.dirty is False and result.commit
    assert len(result.commands) == 3


def test_expected_commit_match_and_mismatch(tmp_path):
    path = repo(tmp_path)
    commit = git(path, "rev-parse", "HEAD")
    assert verify(path, VerificationSpec(commands=(command("pass"),), expected_commit=commit)).ok
    mismatch = verify(path, VerificationSpec(commands=(command("pass"),), expected_commit="0" * 7))
    assert mismatch.status == "verification_failed"


def test_expected_file_and_commit_validation(tmp_path):
    path = repo(tmp_path)
    with pytest.raises(ValueError, match="inside the worktree"):
        verify(path, VerificationSpec(commands=(command("pass"),), expected_files=("..\\outside.txt",)))
    with pytest.raises(ValueError, match="7-40"):
        verify(path, VerificationSpec(commands=(command("pass"),), expected_commit=""))


def test_default_run_directory_is_outside_worktree(tmp_path):
    path = repo(tmp_path)
    result = verify(path, VerificationSpec(commands=(command("pass"),), expected_clean=True))
    assert not Path(result.run_dir).resolve().is_relative_to(path.resolve())


def test_cli_task_subtask_uses_plan_entries_and_json_exit_codes(tmp_path, capsys):
    path = repo(tmp_path)
    root = init_state_root(path)
    task = root.tasks / "demo"
    task.mkdir()
    (task / "plan.md").write_text(
        "task: demo\ncoordinator: lead\nrevision: 1\nupdated: 2026-10-20 01:00\n\n"
        "## Verification\n- verify: A: `git status --porcelain`\n"
        "* verify: B: `git rev-parse HEAD`\n1. verify: C: `git log -1 --format=%H`\n\n"
        "| id | what | files/area | depends on | worktree | owner | reviewer | state |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| A | a | a.py | - | yes | dev-a | dev-b | assigned |\n"
        "| B | b | b.py | - | yes | dev-a | dev-b | assigned |\n"
        "| C | c | c.py | - | yes | dev-a | dev-b | assigned |\n",
        encoding="utf-8",
    )
    for subtask, expected in (("A", "git status --porcelain"), ("B", "git rev-parse HEAD"), ("C", "git log -1 --format=%H")):
        code = main(["verify", "--worktree", str(path), "--task", "demo", "--subtask", subtask,
                     "--project-root", str(path), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["commands"][0]["argv"] == expected.split()

