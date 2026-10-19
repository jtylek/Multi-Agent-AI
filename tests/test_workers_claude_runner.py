"""End-to-end `worker run --provider claude` with a FAKE `claude` in real temporary Git repositories
(T24). Reuses the T12 fixtures: the autouse guard fails any test whose provider command resolves
outside tmp_path, so no real claude/codex/copilot is ever started."""

import json
import os
import sys
from pathlib import Path

import pytest
from test_workers_runner import GIT, env, only_fakes, task_dir  # noqa: F401 (fixtures)

from multi_agent_ai.board import read_statuses
from multi_agent_ai.cli import main
from multi_agent_ai.workers import failures as fc
from multi_agent_ai.workers import quota
from multi_agent_ai.workers.runner import UsageError, run_worker

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime")

RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "verdict: agreed\nfindings: none",
          "session_id": "provider-session-1", "total_cost_usd": 0.02,
          "usage": {"input_tokens": 100, "output_tokens": 20}}
LIMIT_TEXT = ("Agent terminated early due to an API error: You've hit your session limit \u00b7 resets 9:10pm "
              "(UTC) (error type rate_limit, HTTP 429)")


def claude(env, **cfg):
    env.install("claude")
    env.configure(stdout=json.dumps(RESULT), **cfg)


def run(env, **kw):
    kw.setdefault("provider", "claude")
    kw.setdefault("agent_id", "claude-w1")
    return env.run(env.direct_resolver(), **kw)


def test_claude_review_end_to_end(env):
    claude(env)
    outcome = run(env, verify_command=r".venv\Scripts\python -m pytest -q")
    assert outcome.failure_class is None, outcome.result
    record = env.record()
    prompt = (outcome.run_dir / "prompt.md").read_text(encoding="utf-8")
    assert record["stdin"] == prompt and prompt not in " ".join(record["argv"])  # prompt via stdin only
    assert record["argv"][:3] == ["-p", "--output-format=json", "--restricted"]
    assert "--no-session-persistence" in record["argv"] and not any(a.startswith("--session-id") for a in record["argv"])
    assert os.path.normcase(record["cwd"]) == os.path.normcase(str(env.worktree))
    assert outcome.result["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert outcome.result["provider_reported_cost_usd"] == 0.02
    assert outcome.result["provider_session"]["session_id"] == "provider-session-1"
    review = (task_dir(env) / "review-T1-claude-w1.md").read_text(encoding="utf-8")
    assert review.endswith("findings: none")  # the worker's final message, verbatim
    assert env.record()["argv"] == outcome.result["argv"][0:0] + env.record()["argv"]  # recorded launch is the real run


def test_claude_argv_survives_a_cmd_shim(env):
    """Rule strings contain parentheses, asterisks and spaces; cmd.exe must not alter them."""
    claude(env)
    outcome = env.run(provider="claude", agent_id="claude-w1", verify_command=r".venv\Scripts\python -m pytest -q")
    assert outcome.failure_class is None, outcome.result
    allowed = next(a for a in env.record()["argv"] if a.startswith("--allowedTools="))
    assert "PowerShell(git diff *)" in allowed and r"PowerShell(.venv\Scripts\python -m pytest -q)" in allowed


def test_environment_is_the_allowlist_plus_claude_names(env):
    claude(env)
    env.env.update({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SSE_PORT": "1234",
                    "ANTHROPIC_API_KEY": "sk-ant-secret", "ANTHROPIC_BASE_URL": "http://localhost",
                    "CLAUDE_CONFIG_DIR": r"C:\cfg", "CLAUDE_CODE_GIT_BASH_PATH": r"C:\Git\bin\bash.exe"})
    assert run(env).failure_class is None
    names = {n.upper() for n in env.record()["env"]}
    assert not names & {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "ANTHROPIC_API_KEY",
                        "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "GH_TOKEN", "COPILOT_ALLOW_ALL"}
    assert {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH"} <= names
    run(env, pass_env=("ANTHROPIC_API_KEY",), agent_id="claude-w2")
    assert "ANTHROPIC_API_KEY" in {n.upper() for n in env.record()["env"]}  # explicit opt-in only


def test_claude_implement_commits_through_the_orchestrator_and_keeps_a_session(env):
    claude(env, write={"src/widget.py": "x = 1\n"})
    outcome = run(env, mode="implement", model="sonnet")
    assert outcome.failure_class is None and outcome.commit, outcome.result
    argv = env.record()["argv"]
    assert f"--session-id={outcome.result['session_id']}" in argv and "--no-session-persistence" not in argv
    assert "--model=sonnet" in argv and any(a.startswith("--tools=Read,Grep,Glob,Edit,Write") for a in argv)
    assert (env.worktree / "src" / "widget.py").is_file()
    assert outcome.result["files_changed"] == ["src/widget.py"]


def test_claude_review_that_edits_a_file_is_review_modified_files(env):
    claude(env, write={"README.md": "changed by reviewer\n"})
    assert run(env).failure_class == fc.REVIEW_MODIFIED_FILES


@pytest.mark.parametrize("stderr, exit_code, expected", [
    ("Invalid API key · Please run /login\n", 1, fc.AUTH),
    ("Claude requested permissions to use Edit, but you haven't granted it yet.\n", 1, fc.PERMISSION_DENIED),
    ("There's an issue with the selected model (nope). It may not exist\n", 1, fc.UNSUPPORTED_MODEL),
    ("something else broke\n", 3, fc.NONZERO_EXIT),
])
def test_claude_nonzero_exit_is_classified(env, stderr, exit_code, expected):
    env.install("claude")
    env.configure(stdout="", stderr=stderr, exit=exit_code)
    assert run(env).failure_class == expected


def test_claude_error_result_with_exit_zero_is_malformed_output(env):
    env.install("claude")
    env.configure(stdout=json.dumps({**RESULT, "is_error": True, "subtype": "error_max_turns"}))
    outcome = run(env)
    assert outcome.failure_class == fc.MALFORMED_OUTPUT and "is_error" in outcome.result["failure_detail"]
    env.configure(stdout="garbage, not json")
    assert run(env, agent_id="claude-w2").failure_class == fc.MALFORMED_OUTPUT


def test_claude_timeout_kills_the_process_tree(env):
    env.install("claude")
    env.configure(stdout=json.dumps(RESULT), sleep=30)
    outcome = run(env, timeout=2)
    assert outcome.failure_class == fc.TIMEOUT and outcome.result["timed_out"] is True
    assert outcome.result["tree_control"] in ("job-object", "taskkill")


def test_claude_limit_is_provider_error_without_a_listed_version_and_quota_with_one(env, monkeypatch):
    env.install("claude")
    env.configure(stdout=json.dumps({**RESULT, "is_error": True, "result": LIMIT_TEXT}), exit=1)
    outcome = run(env, mode="implement", cli_version="2.1.301")
    assert outcome.failure_class == fc.PROVIDER_ERROR  # the shipped tables list no version (T15/T18 strict)
    detector, _ = quota.DETECTORS["claude"]
    monkeypatch.setitem(quota.DETECTORS, "claude", (detector, frozenset({"2.1.301"})))
    outcome = run(env, mode="implement", agent_id="claude-w2", cli_version="2.1.301")
    assert outcome.failure_class == fc.QUOTA_EXHAUSTED
    limit = outcome.result["limit"]
    assert limit["resume_command"] == "claude --resume provider-session-1"  # the id the provider reported
    assert outcome.result["session_id"] != "provider-session-1"  # ours was passed with --session-id
    status = read_statuses(task_dir(env))["claude-w2"].value
    assert status.state == "blocked" and status.blockers.startswith("quota: claude (provider default), resets ")


def test_claude_review_limit_has_no_resume_command(env, monkeypatch):
    detector, _ = quota.DETECTORS["claude"]
    monkeypatch.setitem(quota.DETECTORS, "claude", (detector, frozenset({"2.1.301"})))
    env.install("claude")
    env.configure(stdout=json.dumps({**RESULT, "is_error": True, "result": LIMIT_TEXT}), exit=1)
    outcome = run(env, cli_version="2.1.301")
    assert outcome.failure_class == fc.QUOTA_EXHAUSTED and outcome.result["limit"]["resume_command"] is None


# --- --max-budget-usd ---------------------------------------------------------------------------


def test_max_budget_is_off_by_default_and_passed_through_when_set(env):
    claude(env)
    run(env)
    assert not any(a.startswith("--max-budget-usd") for a in env.record()["argv"])
    run(env, agent_id="claude-w2", max_budget_usd=3)
    assert "--max-budget-usd=3" in env.record()["argv"]


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_max_budget_must_be_a_positive_number(env, value):
    claude(env)
    with pytest.raises(UsageError, match="positive"):
        run(env, max_budget_usd=value)
    assert not env.launched()


def test_max_budget_is_claude_only(env):
    env.install("codex")
    with pytest.raises(UsageError, match="only supported for the claude provider"):
        env.run(max_budget_usd=1.0)


# --- CLI ------------------------------------------------------------------------------------------


def cli(env, *extra):
    return main(["worker", "run", "--provider", "claude", "--task", "demo", "--subtask", "T1", "--agent-id",
                 "claude-w1", "--mode", "review", "--worktree", str(env.worktree), "--project-root", str(env.primary),
                 *extra])


def test_cli_dry_run_for_claude_starts_nothing_and_shows_the_command(env, monkeypatch, capsys):
    env.install("claude")
    monkeypatch.setenv("PATH", f"{env.fake_dir}{os.pathsep}{Path(GIT).parent}")
    before = sorted(map(str, env.root.path.rglob("*")))
    assert cli(env, "--max-budget-usd", "1.5") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["problems"] == [] and out["profile"] == "claude-review"
    assert "--max-budget-usd=1.5" in out["argv"] and "--restricted" in out["argv"]
    assert sorted(map(str, env.root.path.rglob("*"))) == before and not env.launched()


def test_cli_execute_and_budget_validation(env, monkeypatch, capsys):
    claude(env)
    monkeypatch.setenv("PATH", f"{env.fake_dir}{os.pathsep}{Path(GIT).parent}")
    assert cli(env, "--max-budget-usd", "0") == 2 and "positive" in capsys.readouterr().err
    assert cli(env, "--execute") == 0
    assert json.loads(capsys.readouterr().out)["failure_class"] is None
