"""The team-task proof of concept: plan, parallel dispatch, integration, save and compile check."""

import json
import threading

from multi_agent_ai.team_demo import (
    SUBTASKS, extract_code, fallback_plan, parse_plan, run_team_task,
)

GOOD_FILE = "```python\nimport sys\n\ndef evaluate(e):\n    return 1.0\n\nif __name__ == '__main__':\n    pass\n```"


def _harness(answers):
    """``answers(provider, message) -> str`` with recorded calls, events and pane output."""
    calls, events, shown = [], [], []
    lock = threading.Lock()

    def ask(provider, message):
        with lock:
            calls.append((provider, message))
        return answers(provider, message)

    def record(kind, summary, **fields):
        with lock:
            events.append((kind, summary, fields))

    def show(provider, speaker, text):
        with lock:
            shown.append((provider, speaker))

    return ask, record, show, calls, events, shown


def test_coordinator_plan_dispatches_each_subtask_and_saves_a_compiling_file(tmp_path):
    plan = {"assignments": [{"subtask": "logic", "agent": "codex", "instructions": "use ast"},
                            {"subtask": "ui", "agent": "copilot", "instructions": ""},
                            {"subtask": "tests", "agent": "codex", "instructions": ""}]}

    def answers(provider, message):
        if provider == "claude" and "Reply with only JSON" in message:
            return "Here is the plan:\n" + json.dumps(plan)
        if provider == "claude":
            return GOOD_FILE
        return f"```python\n# part by {provider}\n```"

    ask, record, show, calls, events, _ = _harness(answers)
    result = run_team_task("claude", ("codex", "copilot"), ask=ask, record=record, show=show,
                           cancel=threading.Event(), output_dir=tmp_path / "team-task")
    assert result.outcome == "saved"
    assert (tmp_path / "team-task" / "calculator.py").read_text(encoding="utf-8").startswith("import sys")
    kinds = [kind for kind, _, _ in events]
    assert kinds[:2] == ["team_task_started", "plan_received"]
    assert kinds.count("task_dispatched") == 3 and kinds.count("subtask_completed") == 3
    assert kinds[-3:] == ["integration_started", "result_saved", "team_task_finished"]
    dispatched = {fields["subtask"]: fields["provider"] for kind, _, fields in events if kind == "task_dispatched"}
    assert dispatched == {"logic": "codex", "ui": "copilot", "tests": "codex"}
    assert "use ast" in next(m for p, m in calls if "(logic)" in m)
    assert "part by copilot" in calls[-1][1]  # the integration prompt carries the workers' parts


def test_unusable_plan_falls_back_and_failed_subtask_goes_to_the_coordinator(tmp_path):
    def answers(provider, message):
        if provider == "claude" and "Reply with only JSON" in message:
            return "I would rather not"
        if provider == "codex":
            raise RuntimeError("codex timed out")
        return GOOD_FILE if provider == "claude" else "```python\n# ui\n```"

    ask, record, show, calls, events, _ = _harness(answers)
    result = run_team_task("claude", ("codex", "copilot"), ask=ask, record=record, show=show,
                           cancel=threading.Event(), output_dir=tmp_path)
    assert result.outcome == "saved"
    kinds = [kind for kind, _, _ in events]
    assert "plan_fallback" in kinds and "subtask_failed" in kinds
    assert "write that part yourself" in calls[-1][1]


def test_non_compiling_result_is_reported_not_hidden(tmp_path):
    def answers(provider, message):
        if "Reply with only JSON" in message:
            return "{}"
        return "```python\ndef broken(:\n```"

    ask, record, show, *_ = _harness(answers)
    result = run_team_task("claude", ("codex",), ask=ask, record=record, show=show,
                           cancel=threading.Event(), output_dir=tmp_path)
    assert result.outcome == "saved_not_compiling"


def test_without_a_state_root_nothing_is_saved():
    ask, record, show, *_ = _harness(lambda p, m: "{}" if "JSON" in m else GOOD_FILE)
    result = run_team_task("claude", (), ask=ask, record=record, show=show,
                           cancel=threading.Event(), output_dir=None)
    assert result.outcome == "not_saved"


def test_plan_validation_and_helpers():
    workers = ("codex", "copilot")
    good = {"assignments": [{"subtask": name, "agent": "codex"} for name in SUBTASKS]}
    assert parse_plan(json.dumps(good), workers)[0] is not None
    assert parse_plan(json.dumps({"assignments": [{"subtask": "logic", "agent": "gemini"}]}), workers)[0] is None
    assert parse_plan(json.dumps({"assignments": good["assignments"][:2]}), workers)[0] is None
    assert [a["agent"] for a in fallback_plan(workers)] == ["codex", "copilot", "codex"]
    assert extract_code("text\n```python\nx = 1\n```\nmore") == "x = 1\n"
    assert extract_code("no code here") is None
