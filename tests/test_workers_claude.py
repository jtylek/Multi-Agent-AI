"""Claude adapter: exact argv, profile data, forbidden flags and defensive JSON parsing (T24).
No `claude` executable is started here."""

import json
from pathlib import Path

import pytest

from multi_agent_ai.workers import claude as cl
from multi_agent_ai.workers.base import FORBIDDEN_FLAGS, LaunchRequest, ProfileUnconfigured, forbidden_in
from multi_agent_ai.workers.claude import ClaudeAdapter, ClaudeProfile, parse_result, verification_rule

WT = Path(r"C:\work\wt T1")
RUN = Path(r"C:\state root\runs\att-1")
PROMPT = 'Line 1 "quoted" 100% ^ & |\nżółw\n'
VERIFY = r".venv\Scripts\python -m pytest -q"
SESSION = "0cb916db-26aa-40f2-86b5-1ba81b225fd2"


def request(mode, model=None, verify=VERIFY, budget=None, session=SESSION):
    return LaunchRequest(WT, mode, PROMPT, RUN / "prompt.md", RUN, model, session, verify, budget)


def argv(mode, **kw):
    return list(ClaudeAdapter().build_invocation(request(mode, **kw)).args)


COMMON = ["-p", "--output-format=json", "--restricted", "--safe-mode", "--strict-mcp-config",
          "--disable-slash-commands", "--no-chrome", "--permission-prompts=none"]
GIT_ALLOW = "PowerShell(git diff *),PowerShell(git log *),PowerShell(git show *),PowerShell(git status *)"
GIT_DENY = ",".join(f"PowerShell({g})" for g in cl.GIT_DENY)


def test_review_argv_exact():
    inv = ClaudeAdapter().build_invocation(request("review"))
    assert list(inv.args) == [
        *COMMON,
        "--tools=Read,Grep,Glob,PowerShell",
        f"--allowedTools=Read,Grep,Glob,{GIT_ALLOW},PowerShell(.venv\\Scripts\\python -m pytest -q)",
        f"--disallowedTools=Edit,Write,NotebookEdit,WebFetch,WebSearch,{GIT_DENY}",
        "--no-session-persistence",
    ]
    assert inv.profile == "claude-review" and inv.stdin_path == RUN / "prompt.md"
    assert PROMPT not in inv.args and not any(PROMPT in a for a in inv.args)  # prompt only on stdin


def test_implement_argv_exact_with_model_and_budget():
    inv = ClaudeAdapter().build_invocation(request("implement", model="opus", budget=2.5))
    assert list(inv.args) == [
        *COMMON, "--model=opus", "--max-budget-usd=2.5",
        "--tools=Read,Grep,Glob,Edit,Write,PowerShell",
        f"--allowedTools=Read,Grep,Glob,Edit,Write,{GIT_ALLOW},PowerShell(.venv\\Scripts\\python -m pytest -q)",
        f"--disallowedTools=NotebookEdit,WebFetch,WebSearch,{GIT_DENY}",
        f"--session-id={SESSION}",
    ]
    assert inv.profile == "claude-implement"


def test_review_profile_cannot_edit_and_implement_can():
    review, implement = argv("review"), argv("implement")
    assert "Edit" not in next(a for a in review if a.startswith("--tools=")).split("=", 1)[1].split(",")
    assert any(a.startswith("--disallowedTools=Edit,Write") for a in review)
    assert "Edit" in next(a for a in implement if a.startswith("--tools=")).split("=", 1)[1].split(",")
    assert not any(a.startswith("--disallowedTools=Edit") for a in implement)


def test_shell_tool_is_a_profile_data_field():
    bash = {"review": ClaudeProfile("claude-review", "review", shell_tool="Bash")}
    args = list(ClaudeAdapter(bash).build_invocation(request("review", verify="python -m pytest -q")).args)
    assert "--tools=Read,Grep,Glob,Bash" in args
    assert any("Bash(python -m pytest -q)" in a and "PowerShell" not in a for a in args if a.startswith("--allowed"))
    assert cl.DEFAULT_SHELL_TOOL == "PowerShell"


@pytest.mark.parametrize("command", [None, "", "pytest (fast)", "a, b", "line1\nline2", "--flag"])
def test_unusable_verification_command_means_no_test_rule_not_a_failure(command):
    assert verification_rule(command) is None
    allowed = next(a for a in argv("review", verify=command) if a.startswith("--allowedTools="))
    assert "pytest" not in allowed and allowed.count("PowerShell(") == 4  # only the git read rules


def test_verification_command_whitespace_is_normalised():
    assert verification_rule("  python   -m  pytest -q ") == "python -m pytest -q"


def test_implement_needs_a_session_id_and_unknown_mode_is_unconfigured():
    with pytest.raises(ProfileUnconfigured, match="session id"):
        ClaudeAdapter().build_invocation(request("implement", session=None))
    with pytest.raises(ProfileUnconfigured, match="no Claude profile"):
        ClaudeAdapter({}).build_invocation(request("review"))


def test_no_bypass_or_remote_flag_in_any_claude_argv():
    for mode in ("review", "implement"):
        for model in (None, "m"):
            for budget in (None, 1.0):
                args = argv(mode, model=model, budget=budget)
                assert forbidden_in(args) == []
                joined = " ".join(args).lower()
                for word in ("dangerously", "bypass", "permission-mode", "--add-dir", "--settings", "--chrome",
                             "--remote", "--cloud", "--teleport", "--worktree", "--fallback-model", "--resume"):
                    assert word not in joined, (mode, word)
                assert "--no-chrome" in args and "--permission-prompts=none" in args and "--restricted" in args


def test_forbidden_list_covers_the_claude_bypass_and_remote_flags():
    for flag in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--permission-mode",
                 "bypassPermissions", "--chrome", "--remote-control", "--cloud", "--teleport", "--bg", "--worktree",
                 "--settings", "--plugin-dir", "--plugin-url", "--file", "--betas", "--add-dir"):
        assert flag in FORBIDDEN_FLAGS
    assert forbidden_in(["--permission-mode=bypassPermissions", "--worktree"]) == ["--permission-mode", "--worktree"]


# --- output parsing ----------------------------------------------------------------------------


def parse(tmp_path, text):
    path = tmp_path / "stdout.log"
    path.write_text(text, encoding="utf-8")
    return ClaudeAdapter().parse_output(path, ClaudeAdapter().build_invocation(request("review")))


GOOD = {"type": "result", "subtype": "success", "is_error": False, "result": "verdict: agreed", "session_id": "s-1",
        "total_cost_usd": 0.0123, "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2,
                                           "service_tier": "standard"}}


def test_parse_success(tmp_path):
    out = parse(tmp_path, json.dumps(GOOD))
    assert out.final_message == "verdict: agreed" and out.malformed is None
    assert out.usage == {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2}
    assert out.session == {"session_id": "s-1", "provider_reported_cost_usd": 0.0123}


def test_parse_tolerates_leading_log_lines_trailing_newline_and_event_lists(tmp_path):
    assert parse(tmp_path, "warning: something\n" + json.dumps(GOOD) + "\n").final_message == "verdict: agreed"
    events = [{"type": "system"}, GOOD, {"type": "other"}]
    assert parse(tmp_path, json.dumps(events)).final_message == "verdict: agreed"


@pytest.mark.parametrize("text, fragment", [
    ("", "no JSON result"),
    ("not json at all", "no JSON result"),
    ("[1, 2, 3]", "no JSON result"),
    (json.dumps({**GOOD, "is_error": True, "subtype": "error_during_execution"}), "is_error"),
    (json.dumps({**GOOD, "result": "   "}), "no result text"),
    (json.dumps({k: v for k, v in GOOD.items() if k != "result"}), "no result text"),
    (json.dumps({**GOOD, "result": 5}), "no result text"),
])
def test_parse_malformed_variants(tmp_path, text, fragment):
    assert fragment in parse(tmp_path, text).malformed


def test_parse_missing_file_and_odd_fields(tmp_path):
    out = ClaudeAdapter().parse_output(tmp_path / "missing.log", ClaudeAdapter().build_invocation(request("review")))
    assert out.final_message is None and "cannot read stdout" in out.malformed
    odd = parse(tmp_path, json.dumps({**GOOD, "usage": "n/a", "session_id": 5, "total_cost_usd": True}))
    assert odd.usage == "unknown" and odd.session == {} and odd.malformed is None


def test_parse_result_returns_the_last_result_event():
    first, last = {**GOOD, "result": "one"}, {**GOOD, "result": "two"}
    assert parse_result(json.dumps([first, last]))[0]["result"] == "two"
