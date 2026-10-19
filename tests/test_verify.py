"""Verification engine tests use only temporary Git repositories and fake commands."""

import subprocess
import sys
from pathlib import Path

import pytest

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

