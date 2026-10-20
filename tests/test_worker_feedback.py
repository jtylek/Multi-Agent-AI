"""Optional correction-round feedback in the worker prompt (T27, decision D19): bounded, scrubbed, off by default."""

from pathlib import Path

from multi_agent_ai.workers import prompt as pr
from test_workers_units import prompt_for  # noqa: F401  (same fixtures as the prompt tests)
from test_workers_units import make_plan, WT


def build(feedback=None, mode="implement"):
    plan = make_plan()
    return pr.build_prompt(plan=plan, subtask=plan.subtask("T1"), agent_id="codex-w1", provider="codex", mode=mode,
                           worktree=WT, branch="task/demo/T1", board_path=Path(r"C:p.multi-agent-ai	asksdemoplan.md"),
                           commit="abc1234" if mode == "review" else None, feedback=feedback)


def test_no_feedback_leaves_the_prompt_unchanged():
    assert build(None) == build("") == prompt_for("implement")
    assert pr.FEEDBACK_HEADING not in build(None)


def test_feedback_is_appended_after_the_rules():
    text = build("FAILED tests/test_x.py::test_y - assert 1 == 2")
    assert text.index("Rules (from the orchestrator") < text.index(pr.FEEDBACK_HEADING)
    assert text.rstrip().endswith("assert 1 == 2") and "\r" not in text


def test_feedback_is_capped_to_its_tail():
    big = "start-marker\n" + "x" * 20000 + "\nend-marker"
    text = pr.sanitize_feedback(big)
    assert len(text) < pr.FEEDBACK_LIMIT + 100 and text.endswith("end-marker") and "start-marker" not in text


def test_secret_looking_environment_values_are_scrubbed():
    env = {"MY_API_TOKEN": "s3cr3t-value-123", "PATH": r"C:\Windows", "SHORT_KEY": "abc"}
    out = pr.sanitize_feedback("error: bad token s3cr3t-value-123 in C:\\Windows abc", env=env)
    assert "s3cr3t-value-123" not in out and "[redacted]" in out
    assert r"C:\Windows" in out and "abc" in out  # not secret-named, or too short to be a secret
