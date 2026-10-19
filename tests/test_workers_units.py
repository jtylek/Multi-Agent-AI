"""Adapters (argv exactness, no bypass flags), prompt builder, failure classification.

Nothing here launches a process.
"""

from pathlib import Path

import pytest

from multi_agent_ai.board import parse_plan, render_subtask_table
from multi_agent_ai.document import parse_text
from multi_agent_ai.models import Plan, Subtask
from multi_agent_ai.workers import failures as fc
from multi_agent_ai.workers.base import FORBIDDEN_FLAGS, LaunchRequest, ProfileUnconfigured, forbidden_in, token_counts
from multi_agent_ai.workers.codex import CodexAdapter
from multi_agent_ai.workers.copilot import (
    PROFILES,
    CopilotAdapter,
    CopilotProfile,
    final_message_from_events,
    verification_pattern,
)
from multi_agent_ai.workers.prompt import build_prompt, verification_command
from multi_agent_ai.workers.runner import ADAPTERS

WT = Path(r"C:\work\wt T1")
RUN = Path(r"C:\state root\runs\att-1")
PROMPT = 'Line 1 "quoted" 100% ^ & |\nżółw\n'
VERIFY = r".venv\Scripts\python -m pytest -q"
SESSION = "0cb916db-26aa-40f2-86b5-1ba81b225fd2"


def request(mode, model=None, verify=VERIFY):
    return LaunchRequest(WT, mode, PROMPT, RUN / "prompt.md", RUN, model, SESSION, verify)


# --- exact argv ------------------------------------------------------------------------------


def test_codex_review_argv_exact():
    inv = CodexAdapter().build_invocation(request("review"))
    assert list(inv.args) == ["exec", "-C", str(WT), "-s", "read-only", "--ephemeral", "--color", "never",
                              "--json", "-o", str(RUN / "final-message.md"), "-"]
    assert inv.profile == "codex-review"
    assert inv.stdin_path == RUN / "prompt.md"  # prompt via stdin, never argv
    assert PROMPT not in inv.args


def test_codex_implement_argv_exact_with_model():
    inv = CodexAdapter().build_invocation(request("implement", model="gpt-5.5"))
    assert list(inv.args) == ["exec", "-C", str(WT), "-s", "workspace-write", "--ephemeral", "--color", "never",
                              "--json", "-o", str(RUN / "final-message.md"), "-m", "gpt-5.5", "-"]
    assert inv.profile == "codex-implement"


def test_copilot_review_argv_exact():
    inv = CopilotAdapter().build_invocation(request("review"))
    assert list(inv.args) == [
        "-C", str(WT), "-p", PROMPT, "--output-format", "json", "--silent", "--session-id", SESSION,
        "--usage-output-file", str(RUN / "usage.json"), "--disallow-temp-dir",
        "--available-tools=bash,view,grep,glob",
        r"--allow-tool=shell(git diff),shell(git log),shell(git show),shell(.venv\Scripts\python -m pytest -q)",
        "--deny-tool=shell(git commit),shell(git push),url",
    ]
    assert inv.profile == "copilot-review"
    assert inv.redacted_args(PROMPT)[3] == "<PROMPT>"


def test_copilot_implement_argv_exact_with_model():
    inv = CopilotAdapter().build_invocation(request("implement", model="claude-sonnet-5"))
    assert list(inv.args) == [
        "-C", str(WT), "-p", PROMPT, "--output-format", "json", "--silent", "--session-id", SESSION,
        "--usage-output-file", str(RUN / "usage.json"), "--disallow-temp-dir", "--model", "claude-sonnet-5",
        "--available-tools=bash,create,edit,view,grep,glob",
        r"--allow-tool=shell(git diff),shell(git log),shell(git show),shell(.venv\Scripts\python -m pytest -q),write",
        "--deny-tool=shell(git commit),shell(git push),url",
    ]
    assert inv.profile == "copilot-implement"


@pytest.mark.parametrize("verify", [None, "", "a, b", "run(x)", "line1\nline2", "--flag"])
def test_copilot_without_usable_verification_pattern_is_unconfigured(verify):
    with pytest.raises(ProfileUnconfigured):
        CopilotAdapter().build_invocation(request("review", verify=verify))


def test_verification_pattern_keeps_full_command_prefix():
    assert verification_pattern(r" .venv\Scripts\python.exe   -m pytest ") == r"shell(.venv\Scripts\python.exe -m pytest)"


# --- no bypass flag, ever ----------------------------------------------------------------------


def all_generated_argvs():
    for name, factory in ADAPTERS.items():
        for mode in ("review", "implement"):
            for model in (None, "m"):
                yield name, mode, list(factory().build_invocation(request(mode, model)).args)


def test_no_adapter_argv_contains_a_bypass_flag():
    seen = 0
    for name, mode, argv in all_generated_argvs():
        seen += 1
        assert forbidden_in(argv) == [], (name, mode, argv)
        for arg in argv:
            low = arg.lower()
            assert "allow-all" not in low and "yolo" not in low and "dangerously" not in low, arg
            assert "danger-full-access" not in low and "full-auto" not in low, arg
    assert seen == len(ADAPTERS) * 4


def test_forbidden_detection_covers_equals_form_and_sandbox_value():
    assert forbidden_in(["--allow-all-tools=true", "-s", "danger-full-access", "--yolo"]) == [
        "--allow-all-tools", "danger-full-access", "--yolo"]
    assert "--add-dir" in FORBIDDEN_FLAGS and "--no-ask-user" in FORBIDDEN_FLAGS


def test_copilot_profiles_reject_broadening():
    with pytest.raises(ValueError, match="forbidden"):
        CopilotProfile("x-implement", ("bash",), ("--allow-all-tools",), (), ())
    with pytest.raises(ValueError, match="review profile"):
        CopilotProfile("x-review", ("bash", "edit"), (), (), ())
    with pytest.raises(ValueError, match="review profile"):
        CopilotProfile("x-review", ("bash",), (), ("write",), ())
    with pytest.raises(ValueError, match="comma"):
        CopilotProfile("x-review", ("bash,view",), (), (), ())
    assert "write" not in PROFILES["review"].allow_tools_after
    assert "url" in PROFILES["review"].deny_tools and "url" in PROFILES["implement"].deny_tools


# --- output parsing ----------------------------------------------------------------------------


def test_copilot_final_message_extraction_is_defensive():
    events = [{"type": "assistant.message_delta", "data": {"content": "partial"}},
              {"type": "assistant.message", "data": {"content": "first"}},
              {"type": "tool.result", "content": "ignored"},
              {"type": "assistant.message", "data": {"content": "final answer"}}]
    assert final_message_from_events(events) == "final answer"
    assert final_message_from_events([{"type": "result", "text": "x"}]) is None


def test_token_counts_only_reported_integers():
    assert token_counts({"input_tokens": 3, "output_tokens": 4, "model": "x", "flag": True}) == {
        "input_tokens": 3, "output_tokens": 4}
    assert token_counts({"usage": {"totalTokens": 9}}) == {"usage.totalTokens": 9}
    assert token_counts("nope") == {}


# --- prompt --------------------------------------------------------------------------------


PLAN_BODY = """## Goal

Ship the widget.

## Acceptance criteria

1. Widget renders.
2. Verification command (exact): `py -3.12 -m venv .venv` then `.venv\\Scripts\\python -m pytest -q`.

### T9 acceptance criteria

Not for T1.

## Subtasks

""" + render_subtask_table((
    Subtask("T1", "widget", "src/widget.py", (), True, "dev-a", "dev-b", "in progress"),
    Subtask("T9", "other", "src/other.py", (), True, "dev-b", "dev-a", "open"),
))


def make_plan() -> Plan:
    text = "task: demo\ncoordinator: lead\nrevision: 4\nupdated: 2026-10-19 10:00\n\n" + PLAN_BODY
    parsed = parse_plan(parse_text(text), "demo")
    assert parsed.ok, parsed.errors
    return parsed.value


def prompt_for(mode="review", commit="abc1234"):
    plan = make_plan()
    return build_prompt(plan=plan, subtask=plan.subtask("T1"), agent_id="codex-w1", provider="codex", mode=mode,
                        worktree=WT, branch="task/demo/T1", board_path=Path(r"C:\p\.multi-agent-ai\tasks\demo\plan.md"),
                        commit=commit if mode == "review" else None)


def test_prompt_is_deterministic_and_complete():
    first, second = prompt_for(), prompt_for()
    assert first == second and first.encode("utf-8") == second.encode("utf-8")
    assert "\r" not in first and first.endswith("\n")
    for fragment in ("You are codex-w1, a managed codex worker", "plan revision 4", "| T1 | widget |",
                     "Ship the widget.", "Widget renders.", r".venv\Scripts\python -m pytest -q",
                     r"task board (read-only): C:\p\.multi-agent-ai\tasks\demo\plan.md",
                     "Do not commit", "push", "merge", "outside it", "Mode: REVIEW (read-only)",
                     "Do not modify anything", "commit abc1234", "verdict: agreed | changes requested",
                     "not an interactive chat"):
        assert fragment in first, fragment
    assert "Not for T1." not in first  # another subtask's criteria are not included


def test_prompt_implement_mode_differs_only_in_mode_text():
    text = prompt_for("implement")
    assert "Mode: IMPLEMENT." in text and "Leave your changes uncommitted" in text
    assert "Mode: REVIEW" not in text


def test_verification_command_is_the_last_inline_command():
    assert verification_command(make_plan(), "T1") == r".venv\Scripts\python -m pytest -q"


# --- failure classification (table-driven) -----------------------------------------------------


CASES = [
    (dict(profile_unconfigured=True, launch_error=True), fc.PROFILE_UNCONFIGURED),
    (dict(environment_error=True, launch_error=True), fc.ENVIRONMENT),
    (dict(launch_error=True, exit_code=None), fc.LAUNCH_ERROR),
    (dict(arguments_rejected=True), fc.INVALID_ARGUMENTS),
    (dict(review_modified=True, exit_code=0), fc.REVIEW_MODIFIED_FILES),
    (dict(review_modified=True, timed_out=True), fc.REVIEW_MODIFIED_FILES),
    (dict(git_violation=True, exit_code=0, mode="implement", changed_files=1), fc.GIT_POLICY_VIOLATION),
    (dict(timed_out=True, exit_code=1), fc.TIMEOUT),
    (dict(exit_code=1, stderr="Error: model 'gpt-x' is not supported"), fc.UNSUPPORTED_MODEL),
    (dict(exit_code=1, stderr="You do not have access to model o9"), fc.UNSUPPORTED_MODEL),
    (dict(exit_code=1, stderr="401 Unauthorized"), fc.AUTH),
    (dict(exit_code=1, stderr="Not logged in. Please run copilot login"), fc.AUTH),
    (dict(exit_code=1, stdout='{"type":"error","message":"Permission denied: shell(rm)"}'), fc.PERMISSION_DENIED),
    (dict(exit_code=1, stderr="Tool 'bash' was denied by policy"), fc.PERMISSION_DENIED),
    (dict(exit_code=2, stderr="error: unexpected argument '--full-auto' found"), fc.INVALID_ARGUMENTS),
    (dict(exit_code=1, stderr="error: unknown option '--foo'"), fc.INVALID_ARGUMENTS),
    (dict(exit_code=3, stderr="something broke"), fc.NONZERO_EXIT),
    (dict(exit_code=0, malformed=True), fc.MALFORMED_OUTPUT),
    (dict(exit_code=0, mode="implement", changed_files=0), fc.NO_CHANGES),
    (dict(exit_code=0, mode="implement", changed_files=2, commit_failed=True), fc.COMMIT_FAILED),
    (dict(exit_code=0, mode="implement", changed_files=2), None),
    (dict(exit_code=0), None),
    (dict(exit_code=0, stderr="401 Unauthorized"), None),  # output patterns only refine nonzero exits
]


@pytest.mark.parametrize("fields, expected", CASES)
def test_classify_table(fields, expected):
    fields = {"mode": "review", **fields}
    assert fc.classify(fc.Evidence(**fields)) == expected


def test_every_failure_class_is_reachable():
    assert {e for _, e in CASES if e} == set(fc.FAILURE_CLASSES)
