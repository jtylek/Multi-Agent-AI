"""Proof-of-concept team task: the coordinator splits work, workers answer, the coordinator integrates.

Every agent call is the chat's read-only call (``gui_chat.ask_provider``); no agent edits project
files. This deterministic code does the mechanics: it asks the coordinator for a plan, dispatches the
subtasks in parallel, hands the answers back to the coordinator, saves the integrated file into the
chat session folder and checks that it compiles. It never runs the generated program.

Dispatch is recorded in the chat log (``task_dispatched``), not as mailbox requests: read-only
workers cannot write their own acknowledgment, and an acknowledgment written on their behalf would
be fake evidence (AGENTS.md section 7).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

TEAM_TIMEOUT_SECONDS = 300
MAX_PART_CHARACTERS = 6_000
RESULT_FILE = "calculator.py"

TEAM_TASK = ("Build a small desktop calculator in Python using Tkinter and only the standard library. "
             "It is a demonstration of AI agents working together; keep each part short.")

SUBTASKS: dict[str, str] = {
    "logic": ("Write a Python function evaluate(expression: str) -> float that safely evaluates numbers, "
              "decimals, + - * /, unary minus and parentheses WITHOUT eval() (use the ast module). Raise "
              "ValueError for invalid input; let ZeroDivisionError propagate."),
    "ui": ("Write a Tkinter class CalculatorApp(tk.Tk) with a display Entry and buttons for 0-9 . + - * / "
           "( ) C and =. On = call evaluate(expression), which another agent writes (assume it exists), and "
           "show 'Error' on ValueError or ZeroDivisionError."),
    "tests": ("Write a function self_test() that uses assert statements to check evaluate() on six cases: "
              "operator precedence, parentheses, decimals, unary minus, division by zero raising "
              "ZeroDivisionError and invalid input raising ValueError. Print 'self-test passed' at the end."),
}

PART_SUFFIX = "\n\nReturn only the Python code for your part in one ```python code block, no file edits."

Ask = Callable[[str, str], str]
"""``ask(provider, message) -> answer``; raises on failure."""
Record = Callable[..., None]
"""``record(kind, summary, **fields)``: thread-safe chat-log writer."""
Show = Callable[[str, str, str], None]
"""``show(provider, speaker, text)``: thread-safe display in that provider's pane."""


@dataclass
class TeamResult:
    outcome: str
    """``saved`` (file written and it compiles), ``saved_not_compiling``, ``no_code``, ``not_saved``
    or ``failed``."""
    detail: str
    path: Path | None = None


def plan_prompt(coordinator: str, workers: tuple[str, ...]) -> str:
    return (
        f"You are the coordinator of a team task. Task: {TEAM_TASK}\n"
        f"Workers available: {', '.join(workers)}. You are {coordinator}; you integrate the parts later.\n"
        f"The task has exactly these subtasks: {json.dumps(SUBTASKS, indent=1)}\n"
        "Assign every subtask to one worker; every worker should get at least one subtask when there are "
        "enough subtasks. Add one sentence of instructions per subtask.\n"
        'Reply with only JSON: {"assignments": [{"subtask": "logic", "agent": "<worker>", '
        '"instructions": "..."}]}'
    )


def parse_plan(answer: str, workers: tuple[str, ...]) -> tuple[list[dict[str, str]] | None, str]:
    """Validated assignments from the coordinator's answer, or ``(None, reason)``."""
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    if not match:
        return None, "no JSON object in the coordinator's plan"
    try:
        value = json.loads(match.group(0))
    except ValueError as exc:
        return None, f"plan is not valid JSON: {exc}"
    items = value.get("assignments") if isinstance(value, dict) else None
    if not isinstance(items, list):
        return None, "plan has no assignments list"
    plan = []
    for item in items:
        if not isinstance(item, dict) or item.get("subtask") not in SUBTASKS or item.get("agent") not in workers:
            return None, f"invalid assignment: {item!r}"
        plan.append({"subtask": item["subtask"], "agent": item["agent"],
                     "instructions": str(item.get("instructions", ""))[:500]})
    if sorted(a["subtask"] for a in plan) != sorted(SUBTASKS):
        return None, "plan does not assign each subtask exactly once"
    return plan, ""


def fallback_plan(workers: tuple[str, ...]) -> list[dict[str, str]]:
    return [{"subtask": name, "agent": workers[index % len(workers)], "instructions": ""}
            for index, name in enumerate(SUBTASKS)]


def extract_code(answer: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", answer, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return answer.strip() + "\n" if re.search(r"^\s*(def|class|import) ", answer, re.MULTILINE) else None


def integration_prompt(parts: dict[str, str], failures: dict[str, str]) -> str:
    sections = [f"--- {name} (from a worker) ---\n{text[:MAX_PART_CHARACTERS]}" for name, text in parts.items()]
    missing = "".join(f"\nSubtask {name} failed ({error}); write that part yourself: {SUBTASKS[name]}"
                      for name, error in failures.items())
    return (
        f"You are the coordinator. Task: {TEAM_TASK}\nYour workers returned these parts:\n\n"
        + "\n\n".join(sections) + "\n" + missing + "\n\n"
        f"Combine them into ONE complete runnable file {RESULT_FILE}: evaluate, self_test, CalculatorApp and "
        "a main block that runs self_test() when '--self-test' is in sys.argv and otherwise starts the app. "
        "Fix any mismatch between the parts. Return only the file in one ```python code block."
    )


def run_team_task(coordinator: str, workers: tuple[str, ...], *, ask: Ask, record: Record, show: Show,
                  cancel: threading.Event, output_dir: Path | None) -> TeamResult:
    """Run plan -> parallel dispatch -> integration -> save + compile check. Never raises for agent
    failures; the outcome says what happened."""
    workers = workers or (coordinator,)
    record("team_task_started", f"coordinator {coordinator}, workers {', '.join(workers)}",
           coordinator=coordinator, workers=list(workers), task=TEAM_TASK)
    show(coordinator, "Team task", f"{TEAM_TASK}\nCoordinator: {coordinator}. Workers: {', '.join(workers)}.")

    # 1. Plan.
    try:
        answer = ask(coordinator, plan_prompt(coordinator, workers))
        plan, reason = parse_plan(answer, workers)
        show(coordinator, f"{coordinator} (plan)", answer)
    except Exception as exc:  # the coordinator call failed; the demo still shows dispatch
        plan, reason = None, f"plan call failed: {exc}"
    if cancel.is_set():
        record("team_task_finished", "cancelled during planning", outcome="cancelled")
        return TeamResult("failed", "cancelled")
    if plan is None:
        plan = fallback_plan(workers)
        record("plan_fallback", reason, provider=coordinator, assignments=plan)
        show(coordinator, "Orchestrator", f"Coordinator plan unusable ({reason}); using the built-in split.")
    else:
        record("plan_received", "; ".join(f"{a['subtask']} -> {a['agent']}" for a in plan),
               provider=coordinator, assignments=plan)

    # 2. Dispatch in parallel.
    parts: dict[str, str] = {}
    failures: dict[str, str] = {}
    lock = threading.Lock()

    def work(assignment: dict[str, str]):
        name, agent = assignment["subtask"], assignment["agent"]
        brief = SUBTASKS[name] + (f"\nCoordinator's instructions: {assignment['instructions']}"
                                  if assignment["instructions"] else "")
        record("task_dispatched", f"{name} -> {agent}", provider=agent, subtask=name, brief=brief)
        show(agent, f"From coordinator {coordinator} ({name})", brief)
        try:
            reply = ask(agent, f"Team task: {TEAM_TASK}\nYour subtask ({name}): {brief}{PART_SUFFIX}")
        except Exception as exc:
            with lock:
                failures[name] = str(exc)
            record("subtask_failed", f"{name} ({agent}): {exc}", provider=agent, subtask=name, error=str(exc))
            show(agent, "Error", str(exc))
            return
        with lock:
            parts[name] = reply
        record("subtask_completed", f"{name} ({agent}): {len(reply)} characters", provider=agent, subtask=name)
        show(agent, f"{agent} ({name})", reply)

    threads = [threading.Thread(target=work, args=(a,), daemon=True) for a in plan]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if cancel.is_set():
        record("team_task_finished", "cancelled during dispatch", outcome="cancelled")
        return TeamResult("failed", "cancelled")

    # 3. Integrate.
    record("integration_started", f"{len(parts)} part(s), {len(failures)} failed", provider=coordinator,
           received=sorted(parts), failed=sorted(failures))
    try:
        final = ask(coordinator, integration_prompt(parts, failures))
    except Exception as exc:
        record("team_task_finished", f"integration failed: {exc}", outcome="failed", provider=coordinator)
        show(coordinator, "Error", f"Integration failed: {exc}")
        return TeamResult("failed", f"integration failed: {exc}")
    show(coordinator, f"{coordinator} (integrated)", final)

    # 4. Save and syntax-check (never execute).
    code = extract_code(final)
    if code is None:
        record("team_task_finished", "no Python code in the integrated answer", outcome="no_code")
        return TeamResult("no_code", "the coordinator's final answer contains no Python code")
    if output_dir is None:
        record("team_task_finished", "result not saved (project not initialized)", outcome="not_saved")
        return TeamResult("not_saved", "initialize the project to save the result")
    path = Path(output_dir) / RESULT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    try:
        compile(code, str(path), "exec")
    except SyntaxError as exc:
        record("result_saved", f"{path} (does not compile: {exc.msg}, line {exc.lineno})",
               path=str(path), compiles=False)
        record("team_task_finished", "saved, but the file does not compile", outcome="saved_not_compiling")
        return TeamResult("saved_not_compiling", f"{exc.msg}, line {exc.lineno}", path)
    record("result_saved", f"{path} (compiles)", path=str(path), compiles=True)
    record("team_task_finished", "saved; compiles", outcome="saved")
    return TeamResult("saved", "the file compiles; it has not been run", path)
