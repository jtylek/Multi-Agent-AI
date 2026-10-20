"""tools/watch_board.py: the read-only board watch and its safe-to-clear summary."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "watch_board.py"


@pytest.fixture(scope="module")
def watch():
    spec = importlib.util.spec_from_file_location("watch_board_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status(path: Path, agent: str, *, safe: str | None = None, bom: bool = False, crlf: bool = False) -> None:
    lines = [f"agent: {agent}", "state: review", "last-commit: abc", "blockers: none"]
    if safe is not None:
        lines.append(f"safe-to-clear: {safe}")
    lines += ["", "body line"]
    text = ("\r\n" if crlf else "\n").join(lines) + "\n"
    path.write_text(("﻿" if bom else "") + text, encoding="utf-8", newline="")


@pytest.fixture
def state(tmp_path):
    root = tmp_path / ".multi-agent-ai"
    (root / "tasks" / "t").mkdir(parents=True)
    (root / "messages").mkdir()
    return root


def test_safe_to_clear_lists_only_agents_that_say_yes(watch, state):
    task = state / "tasks" / "t"
    status(task / "status-a.md", "a", safe="yes")
    status(task / "status-b.md", "b", safe="no")
    status(task / "status-c.md", "c")  # field absent means no
    status(task / "status-d.md", "d", safe="YES ")
    assert watch.safe_to_clear(state, "t") == ["a", "d"]


def test_safe_to_clear_tolerates_bom_crlf_and_agent_from_file_name(watch, state):
    task = state / "tasks" / "t"
    status(task / "status-bom.md", "bom", safe="yes", bom=True, crlf=True)
    (task / "status-noagent.md").write_text("state: review\nsafe-to-clear: yes\n", encoding="utf-8")
    assert watch.safe_to_clear(state, "t") == ["bom", "noagent"]


def test_safe_to_clear_ignores_text_after_the_header_block(watch, state):
    task = state / "tasks" / "t"
    (task / "status-x.md").write_text("agent: x\nstate: review\n\nsafe-to-clear: yes\n", encoding="utf-8")
    assert watch.safe_to_clear(state, "t") == []


def test_safe_to_clear_without_task_directory_is_empty(watch, tmp_path):
    assert watch.safe_to_clear(tmp_path / "nothing", "t") == []


def run_watch(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(TOOL), "--project-root", str(project), "--task", "t", *args],
        capture_output=True, text=True, timeout=60,
    )


def test_once_is_a_silent_baseline_and_writes_nothing(state):
    task = state / "tasks" / "t"
    status(task / "status-a.md", "a", safe="yes")
    before = sorted(p.name for p in state.rglob("*"))
    result = run_watch(state.parent, "--once")
    assert result.returncode == 0
    assert result.stdout == ""
    assert sorted(p.name for p in state.rglob("*")) == before


def test_changes_after_baseline_are_reported_with_the_safe_summary(state):
    task = state / "tasks" / "t"
    status(task / "status-a.md", "a", safe="no")
    proc = subprocess.Popen(
        [sys.executable, "-I", str(TOOL), "--project-root", str(state.parent), "--task", "t",
         "--interval", "0.2"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        time.sleep(1.0)  # baseline pass
        status(task / "status-a.md", "a", safe="yes")
        deadline = time.time() + 15
        lines: list[str] = []
        while time.time() < deadline and not any(line.startswith("SAFE TO CLEAR") for line in lines):
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip())
    finally:
        proc.kill()
        proc.wait(timeout=15)
        proc.stdout.close()
        proc.stderr.close()
    assert any(line.startswith("tasks") is False and "status-a.md" in line and "safe-to-clear: yes" in line
               for line in lines), lines
    assert "SAFE TO CLEAR now: a" in lines
