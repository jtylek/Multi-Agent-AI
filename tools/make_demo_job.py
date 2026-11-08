"""Create or validate a disposable repository for end-to-end orchestration demos."""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tempfile
import venv
from datetime import datetime
from pathlib import Path
from typing import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from multi_agent_ai.board import read_plan  # noqa: E402
from multi_agent_ai.verify import parse_command  # noqa: E402
from multi_agent_ai.workers.prompt import VerificationNotDeclared, verification_command  # noqa: E402

SUBTASKS = (
    ("multiply", "Multiplication implementation", "src/multiply.py", (), "codex-demo-multiply", "test_multiply.py"),
    ("divide", "Division implementation", "src/divide.py", (), "copilot-demo-divide", "test_divide.py"),
    ("write-tests", "Write integration tests", "tests/test_math.py", ("multiply", "divide"),
     "claude-demo-test-writer", "test_write_tests.py"),
)


class DemoJobError(RuntimeError):
    """A requested demo job could not be safely created or validated."""


def _default_root() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return Path(tempfile.gettempdir()) / "multi-agent-ai-demo" / stamp


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    repo = REPOSITORY_ROOT.resolve()
    if _is_within(resolved, repo):
        raise DemoJobError(f"refusing a demo root inside the repository: {resolved}")
    return resolved


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DemoJobError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _verification_python(root: Path, no_venv: bool) -> str:
    if not no_venv:
        interpreter = root / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        return f'"{interpreter}"'
    return f'"{sys.executable}"'


def _plan_text(base_commit: str, python: str) -> str:
    rows = [
        "| id | what | files/area | depends on | worktree | owner | reviewer | state |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for subtask_id, what, files, dependencies, owner, _ in SUBTASKS:
        dependency_text = ", ".join(dependencies) if dependencies else "-"
        rows.append(
            f"| {subtask_id} | {what} | {files} | {dependency_text} | yes | "
            f"{owner} | demo-reviewer | assigned |"
        )

    verify_lines = [
        f"verify: {subtask_id}: `{python} -m pytest -q verify/{verify_file}`"
        for subtask_id, _, _, _, _, verify_file in SUBTASKS
    ]
    return (
        "task: demo\n"
        "coordinator: demo-coordinator\n"
        "revision: 1\n"
        f"updated: {datetime.now():%Y-%m-%d %H:%M}\n"
        f"base-commit: {base_commit}\n"
        "integration-branch: task/demo/integration\n"
        "\n"
        "## Goal\n"
        "Complete a small, dependency-ordered math job in this disposable repository.\n"
        "\n"
        "## Acceptance criteria\n"
        "- `src/multiply.py` exports `multiply(left, right)` and returns the product.\n"
        "- `src/divide.py` exports `divide(numerator, denominator)` and returns the quotient; "
        "division by zero raises `ZeroDivisionError`.\n"
        "- `tests/test_math.py` contains passing tests for `multiply(6, 7) == 42` and "
        "`divide(12, 3) == 4`.\n"
        "- The three subtasks own disjoint files. The test-writing task starts only after "
        "multiplication and division are complete.\n"
        "\n"
        "## Verification\n"
        + "\n".join(verify_lines)
        + "\n\n"
        + "\n".join(rows)
        + "\n"
    )


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _create_venv(root: Path) -> None:
    environment = root / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    interpreter = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run(
        [str(interpreter), "-m", "pip", "install", "pytest"],
        cwd=root, check=True,
    )


def create_demo_job(root: Path, *, no_venv: bool = False) -> tuple[Path, Path]:
    """Create the repository and plan, returning (root, plan_path)."""
    if sys.version_info < (3, 11):
        raise DemoJobError("make_demo_job.py requires Python 3.11 or newer")
    root = _safe_root(root)
    if root.exists():
        if not root.is_dir():
            raise DemoJobError(f"demo root exists and is not a directory: {root}")
        if any(root.iterdir()):
            raise DemoJobError(f"demo root is not empty; refusing to overwrite it: {root}")
    root.mkdir(parents=True, exist_ok=True)

    _write(root, ".gitignore", ".multi-agent-ai/\nvenv/\n__pycache__/\n.pytest_cache/\n")
    _write(root, "src/.gitkeep", "")
    _write(
        root, "verify/test_multiply.py",
        "from src.multiply import multiply\n\n"
        "def test_multiply_returns_product():\n"
        "    assert multiply(6, 7) == 42\n\n"
        "def test_multiply_handles_zero():\n"
        "    assert multiply(0, 7) == 0\n",
    )
    _write(
        root, "verify/test_divide.py",
        "from src.divide import divide\n\n"
        "def test_divide_returns_quotient():\n"
        "    assert divide(12, 3) == 4\n\n"
        "def test_divide_by_zero_raises():\n"
        "    try:\n"
        "        divide(1, 0)\n"
        "    except ZeroDivisionError:\n"
        "        return\n"
        "    raise AssertionError('divide(1, 0) must raise ZeroDivisionError')\n",
    )
    _write(root, "verify/test_write_tests.py", _write_tests_verifier())

    _git(root, "init", "--initial-branch=master")
    _git(root, "config", "user.name", "Multi-Agent AI Demo")
    _git(root, "config", "user.email", "demo@invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.hooksPath", str(root / ".git" / "no-hooks"))
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", ".gitignore", "src/.gitkeep", "verify")
    _git(root, "commit", "-m", "Initial disposable demo repository")
    base_commit = _git(root, "rev-parse", "HEAD")

    if not no_venv:
        _create_venv(root)

    plan_path = root / ".multi-agent-ai" / "tasks" / "demo" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        _plan_text(base_commit, _verification_python(root, no_venv)), encoding="utf-8", newline="\n",
    )
    return root, plan_path


def _write_tests_verifier() -> str:
    return (
        "import ast\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "TEST_FILE = ROOT / 'tests' / 'test_math.py'\n"
        "\n"
        "def _has_expected_assert(tree, function_name, expected):\n"
        "    for function in (node for node in ast.walk(tree) "
        "if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):\n"
        "        if not function.name.startswith('test_'):\n"
        "            continue\n"
        "        for assertion in (node for node in ast.walk(function) if isinstance(node, ast.Assert)):\n"
        "            expression = assertion.test\n"
        "            if not isinstance(expression, ast.Compare):\n"
        "                continue\n"
        "            operands = [expression.left, *expression.comparators]\n"
        "            for left, right in zip(operands, operands[1:]):\n"
        "                calls = [node for node in ast.walk(left) if isinstance(node, ast.Call)]\n"
        "                names = [node.func.id for node in calls if isinstance(node.func, ast.Name)]\n"
        "                if function_name in names and isinstance(right, ast.Constant) "
        "and right.value == expected:\n"
        "                    return True\n"
        "                calls = [node for node in ast.walk(right) if isinstance(node, ast.Call)]\n"
        "                names = [node.func.id for node in calls if isinstance(node.func, ast.Name)]\n"
        "                if function_name in names and isinstance(left, ast.Constant) "
        "and left.value == expected:\n"
        "                    return True\n"
        "    return False\n"
        "\n"
        "def test_math_tests_exist_and_check_both_results():\n"
        "    assert TEST_FILE.is_file(), 'the worker must create tests/test_math.py'\n"
        "    tree = ast.parse(TEST_FILE.read_text(encoding='utf-8'), filename=str(TEST_FILE))\n"
        "    assert _has_expected_assert(tree, 'multiply', 42), "
        "'tests/test_math.py must assert multiply(6, 7) == 42'\n"
        "    assert _has_expected_assert(tree, 'divide', 4), "
        "'tests/test_math.py must assert divide(12, 3) == 4'\n"
        "    result = subprocess.run(\n"
        "        [sys.executable, '-m', 'pytest', '-q', str(TEST_FILE)],\n"
        "        cwd=ROOT, capture_output=True, text=True, check=False,\n"
        "    )\n"
        "    assert result.returncode == 0, result.stdout + result.stderr\n"
    )


def check_demo_root(root: Path) -> list[str]:
    """Validate a demo plan with the project's parser and return human-readable problems."""
    problems: list[str] = []
    plan_dir = root / ".multi-agent-ai" / "tasks" / "demo"
    parsed = read_plan(plan_dir)
    problems.extend(parsed.errors)
    problems.extend(f"warning: {warning}" for warning in parsed.warnings)
    if parsed.value is None:
        return problems or ["plan could not be read"]

    plan = parsed.value
    expected_ids = {item[0] for item in SUBTASKS}
    actual_ids = {item.id for item in plan.subtasks}
    if actual_ids != expected_ids:
        problems.append(f"expected subtasks {sorted(expected_ids)}, found {sorted(actual_ids)}")
    for subtask_id, _, _, _, _, verify_file in SUBTASKS:
        try:
            argv = parse_command(verification_command(plan, subtask_id))
        except (ValueError, VerificationNotDeclared) as exc:
            problems.append(str(exc))
            continue
        if argv[-1].replace("\\", "/") != f"verify/{verify_file}":
            problems.append(f"{subtask_id}: verification must target verify/{verify_file}")
    return problems


def _worker_command(root: Path, subtask_id: str) -> str:
    provider = next(owner.split("-", 1)[0] for sid, _, _, _, owner, _ in SUBTASKS if sid == subtask_id)
    return (
        f"multi-agent-ai worker run --provider {provider}"
        f" --task demo --subtask {subtask_id} --agent-id {provider}-demo-reviewer"
        f' --mode review --worktree "<worktree>" --project-root "{root}"'
    )


def _print_commands(root: Path, plan_path: Path, *, no_venv: bool) -> None:
    print(f"Demo root: {root}")
    print(f"Plan: {plan_path}")
    print("Job dry run (does not contact providers):")
    print(f'  multi-agent-ai job start --task demo --providers codex,copilot,claude --project-root "{root}"')
    print("Real job (uses provider quota; run only after reviewing the dry run and signing in):")
    print(
        f'  multi-agent-ai job start --task demo --providers codex,copilot,claude '
        f"--verify-command '{_verification_python(root, no_venv)} -m pytest -q verify tests' "
        f'--project-root "{root}" --execute --approved-provider-run'
    )
    print("Verification and worker review dry-run commands (replace <worktree> with a task worktree):")
    for subtask_id, _, _, _, _, _ in SUBTASKS:
        print(
            f'  multi-agent-ai verify --worktree "<worktree>" --task demo '
            f'--subtask {subtask_id} --project-root "{root}"'
        )
        print(f"  {_worker_command(root, subtask_id)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="demo repository directory")
    parser.add_argument("--no-venv", action="store_true", help="do not create a venv or install pytest")
    parser.add_argument("--check", action="store_true", help="validate an existing demo root with the plan parser")
    args = parser.parse_args(argv)

    try:
        if args.check:
            if args.root is None:
                parser.error("--check requires --root")
            root = args.root.expanduser().resolve()
            problems = check_demo_root(root)
            if problems:
                for problem in problems:
                    print(f"ERROR: {problem}", file=sys.stderr)
                return 1
            print(f"Plan is valid: {root / '.multi-agent-ai' / 'tasks' / 'demo' / 'plan.md'}")
            return 0

        root, plan_path = create_demo_job(args.root or _default_root(), no_venv=args.no_venv)
    except (DemoJobError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"make_demo_job: {exc}", file=sys.stderr)
        return 1

    _print_commands(root, plan_path, no_venv=args.no_venv)
    if args.no_venv:
        print("Note: --no-venv uses this interpreter; pytest must already be installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
