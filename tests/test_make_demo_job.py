"""Tests for the disposable demo-job generator; all repositories are temporary and local."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from multi_agent_ai.board import read_plan
from multi_agent_ai.cli import main as cli_main
from multi_agent_ai.job import JobOptions, build_plan
from multi_agent_ai.state import resolve_state_root
from tools.make_demo_job import DemoJobError, check_demo_root, create_demo_job, main as generator_main


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def verify_subtask(root: Path, subtask: str, capsys) -> tuple[int, dict[str, object]]:
    code = cli_main([
        "verify", "--worktree", str(root), "--task", "demo", "--subtask", subtask,
        "--project-root", str(root), "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def test_create_demo_job_has_valid_plan_and_real_master_commit(tmp_path):
    root = tmp_path / "demo"
    created_root, plan_path = create_demo_job(root, no_venv=True)

    assert created_root == root.resolve()
    assert plan_path == root / ".multi-agent-ai" / "tasks" / "demo" / "plan.md"
    assert (root / ".gitignore").read_text(encoding="utf-8").splitlines()[0] == ".multi-agent-ai/"
    assert (root / "src" / ".gitkeep").is_file()
    assert git(root, "branch", "--show-current") == "master"
    head = git(root, "rev-parse", "HEAD")
    assert len(head) == 40
    parsed = read_plan(plan_path.parent)
    assert parsed.ok
    assert parsed.value is not None
    assert parsed.value.base_commit == head
    assert {subtask.id for subtask in parsed.value.subtasks} == {"multiply", "divide", "write-tests"}
    owners = [subtask.owner_id for subtask in parsed.value.subtasks]
    assert len(set(owners)) == 3
    dry_run = build_plan(resolve_state_root(root), "demo", JobOptions(), available=lambda provider: True)
    assert dry_run.problems == []
    test_task = next(subtask for subtask in parsed.value.subtasks if subtask.id == "write-tests")
    assert set(test_task.depends_on) == {"multiply", "divide"}
    for subtask_id, verify_file in (
        ("multiply", "test_multiply.py"),
        ("divide", "test_divide.py"),
        ("write-tests", "test_write_tests.py"),
    ):
        assert f"verify: {subtask_id}:" in plan_path.read_text(encoding="utf-8")
        assert (root / "verify" / verify_file).is_file()


def test_generated_verification_fails_until_each_solution_is_correct(tmp_path, capsys):
    root, _ = create_demo_job(tmp_path / "demo", no_venv=True)

    for subtask in ("multiply", "divide", "write-tests"):
        code, result = verify_subtask(root, subtask, capsys)
        assert code != 0
        assert result["status"] == "verification_failed"

    (root / "src" / "multiply.py").write_text(
        "def multiply(left, right):\n    return left + right\n", encoding="utf-8",
    )
    code, result = verify_subtask(root, "multiply", capsys)
    assert code != 0
    assert result["status"] == "verification_failed"
    (root / "src" / "multiply.py").write_text(
        "def multiply(left, right):\n    return left * right\n", encoding="utf-8",
    )
    code, result = verify_subtask(root, "multiply", capsys)
    assert code == 0
    assert result["status"] == "passed"

    (root / "src" / "divide.py").write_text(
        "def divide(numerator, denominator):\n    return numerator + denominator\n", encoding="utf-8",
    )
    code, result = verify_subtask(root, "divide", capsys)
    assert code != 0
    assert result["status"] == "verification_failed"
    (root / "src" / "divide.py").write_text(
        "def divide(numerator, denominator):\n    return numerator / denominator  # corrected\n", encoding="utf-8",
    )
    code, result = verify_subtask(root, "divide", capsys)
    assert code == 0
    assert result["status"] == "passed"

    tests_file = root / "tests" / "test_math.py"
    tests_file.parent.mkdir()
    tests_file.write_text(
        "from src.divide import divide\n"
        "from src.multiply import multiply\n\n"
        "def test_multiply():\n    assert multiply(6, 7) == 41\n\n"
        "def test_divide():\n    assert divide(12, 3) == 3\n",
        encoding="utf-8",
    )
    code, result = verify_subtask(root, "write-tests", capsys)
    assert code != 0
    assert result["status"] == "verification_failed"
    tests_file.write_text(
        "from src.divide import divide\n"
        "from src.multiply import multiply\n\n"
        "def test_multiply():\n    assert multiply(6, 7) == 42  # corrected\n\n"
        "def test_divide():\n    assert divide(12, 3) == 4  # corrected\n",
        encoding="utf-8",
    )
    code, result = verify_subtask(root, "write-tests", capsys)
    assert code == 0
    assert result["status"] == "passed"


def test_check_uses_plan_parser_and_reports_invalid_plan(tmp_path):
    root, plan_path = create_demo_job(tmp_path / "demo", no_venv=True)
    assert check_demo_root(root) == []
    assert generator_main(["--check", "--root", str(root)]) == 0

    plan_path.write_text("not a plan\n", encoding="utf-8")
    problems = check_demo_root(root)
    assert problems
    assert generator_main(["--check", "--root", str(root)]) == 1


def test_cli_prints_verification_and_dry_run_commands(tmp_path, capsys):
    root = tmp_path / "demo"
    assert generator_main(["--root", str(root), "--no-venv"]) == 0
    output = capsys.readouterr().out
    assert str(root.resolve()) in output
    assert str(root / ".multi-agent-ai" / "tasks" / "demo" / "plan.md") in output
    for subtask in ("multiply", "divide", "write-tests"):
        assert f"--task demo --subtask {subtask}" in output
        assert f"--project-root \"{root.resolve()}\"" in output
    assert output.count("multi-agent-ai verify") == 3
    assert output.count("multi-agent-ai worker run") == 3
    assert f'multi-agent-ai job start --task demo --project-root "{root.resolve()}"' in output
    for line in output.splitlines():
        if "multi-agent-ai worker run" in line:
            assert '--mode review --worktree "<worktree>"' in line
            assert f'--project-root "{root.resolve()}"' in line


def test_generator_refuses_repository_paths():
    with pytest.raises(DemoJobError, match="inside the repository"):
        create_demo_job(Path(__file__).resolve().parents[1] / "unsafe-demo", no_venv=True)
