"""T28: Copilot shell-tool profile field, parent-directory pre-creation and full usage totals.
Temporary Git repositories and fake executables only; no provider is started."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_workers_runner import CODEX_OK, FINAL, GIT, env, only_fakes, task_dir  # noqa: F401 (fixtures)

from multi_agent_ai.cli import main
from multi_agent_ai.state import StateIndex
from multi_agent_ai.workers import copilot as cp
from multi_agent_ai.workers import failures as fc
from multi_agent_ai.workers import scaffold
from multi_agent_ai.workers.base import LaunchRequest, forbidden_in
from multi_agent_ai.workers.runner import preview_worker

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")

REAL_USAGE = {
    "totalPremiumRequestCost": 1, "totalUserRequests": 1, "totalNanoAiu": 2243970000, "totalApiDurationMs": 6901,
    "tokenDetails": {"input": {"tokenCount": 10}, "cache_read": {"tokenCount": 27097},
                     "cache_write": {"tokenCount": 5100}, "output": {"tokenCount": 696}},
    "modelMetrics": {"claude-sonnet-5.5": {"requests": {"count": 4, "cost": 1},
                                           "usage": {"inputTokens": 32207, "outputTokens": 696, "reasoningTokens": 64}}},
    "currentModel": "claude-sonnet-5.5", "lastCallInputTokens": 8481, "lastCallOutputTokens": 262,
}


# --- 1. shell tool is profile data ---------------------------------------------------------------


def test_shell_tool_is_a_data_field_and_the_default_is_not_bash():
    assert cp.SHELL_TOOL == "powershell"
    review, implement = cp.PROFILES["review"], cp.PROFILES["implement"]
    assert review.available_tools == ("powershell", "view", "grep", "glob")
    assert implement.available_tools == ("powershell", "create", "edit", "view", "grep", "glob")
    assert "bash" not in review.available_tools + implement.available_tools
    flipped = cp.make_profiles("bash")
    assert flipped["review"].available_tools[0] == "bash" and flipped["implement"].available_tools[0] == "bash"


def test_the_shell_permission_kind_does_not_depend_on_the_tool_name():
    for tool in ("powershell", "bash", "pwsh"):
        profiles = cp.make_profiles(tool)
        for profile in profiles.values():
            argv = profile.argv("shell(python -m pytest -q)")
            assert argv[1] == "--allow-tool=shell(git diff),shell(git log),shell(git show),shell(python -m pytest -q)" + (
                ",write" if profile.name.endswith("implement") else "")
            assert argv[2] == "--deny-tool=shell(git commit),shell(git push),url"
            assert forbidden_in(argv) == []


def test_review_profile_still_cannot_write_whatever_the_shell_tool():
    with pytest.raises(ValueError, match="review profile"):
        cp.CopilotProfile("x-review", ("powershell", "edit"), (), (), ())


def test_adapter_uses_the_supplied_profiles():
    request = LaunchRequest(Path(r"C:\wt"), "review", "p", Path(r"C:\r\prompt.md"), Path(r"C:\r"), None, "sid", "pytest -q")
    args = cp.CopilotAdapter(cp.make_profiles("bash")).build_invocation(request).args
    assert "--available-tools=bash,view,grep,glob" in args
    assert "--available-tools=powershell,view,grep,glob" in cp.CopilotAdapter().build_invocation(request).args


# --- 3. usage totals ------------------------------------------------------------------------------


def test_usage_totals_from_a_real_usage_file():
    totals = cp.usage_totals(REAL_USAGE)
    assert totals == {
        "input_tokens": 10, "cache_read_tokens": 27097, "cache_write_tokens": 5100, "output_tokens": 696,
        "reasoning_tokens": 64, "request_count": 4, "models": ["claude-sonnet-5.5"], "premium_request_cost": 1,
        "user_requests": 1, "api_duration_ms": 6901, "nano_aiu": 2243970000, "model": "claude-sonnet-5.5",
        "last_call_input_tokens": 8481, "last_call_output_tokens": 262, "source": "measured",
    }


@pytest.mark.parametrize("document", [None, [], "x", {}, {"unrelated": 1}, {"tokenDetails": "n/a", "modelMetrics": 5}])
def test_usage_totals_never_invents_values(document):
    assert cp.usage_totals(document) is None


def test_usage_totals_tolerates_partial_and_odd_fields():
    totals = cp.usage_totals({"tokenDetails": {"input": {"tokenCount": 5}, "output": {"tokenCount": True},
                                                "cache_read": 7}, "modelMetrics": {"m": "bad"},
                              "totalPremiumRequestCost": "1"})
    assert totals == {"input_tokens": 5, "models": ["m"], "source": "measured"}


def test_copilot_run_records_totals_and_keeps_last_call_values(env):
    events = [{"type": "assistant.message", "data": {"content": "verdict: agreed\n"}}]
    env.configure(stdout="".join(json.dumps(e) + "\n" for e in events), usage=REAL_USAGE)
    outcome = env.run(env.direct_resolver(), provider="copilot", agent_id="copilot-w1", verify_command="pytest -q")
    assert outcome.failure_class is None, outcome.result
    assert outcome.result["usage_totals"]["cache_read_tokens"] == 27097
    assert outcome.result["usage_totals"]["source"] == "measured"
    assert outcome.result["usage"]["lastCallInputTokens"] == 8481  # the older flat view is kept
    assert "usage_totals" not in outcome.result.get("provider_session", {})
    with StateIndex.open(env.root.database_path) as index:
        details = index.attempt_details(outcome.attempt_id)
    assert details["usage_totals"]["premium_request_cost"] == 1 and details["usage_totals"]["request_count"] == 4


def test_missing_usage_file_leaves_totals_unset(env):
    events = [{"type": "assistant.message", "data": {"content": "verdict: agreed\n"}}]
    env.configure(stdout="".join(json.dumps(e) + "\n" for e in events))
    outcome = env.run(env.direct_resolver(), provider="copilot", agent_id="copilot-w1", verify_command="pytest -q")
    assert outcome.result["usage"] == "unknown" and "usage_totals" not in outcome.result


# --- 2. directory pre-creation: unit ---------------------------------------------------------------


@pytest.fixture
def wt(tmp_path):
    path = tmp_path / "worktree dir"
    path.mkdir()
    (path / "existing").mkdir()
    return path


def names(plan):
    return plan.to_create


def test_parent_of_a_file_is_planned_and_nothing_is_written(wt):
    before = sorted(p.name for p in wt.rglob("*"))
    plan = scaffold.plan_directories(wt, "src/pkg/mod.py, tests/test_mod.py")
    assert plan.to_create == ["src", "src/pkg", "tests"] and plan.existing == []
    assert sorted(p.name for p in wt.rglob("*")) == before


def test_area_glob_annotation_and_separators(wt):
    plan = scaffold.plan_directories(wt, "docs/guide/ ; lib\\core\\*.py (new); existing/x.txt; README.md; -")
    assert plan.to_create == ["docs", "docs/guide", "lib", "lib/core"]
    assert plan.existing == ["existing"] and "README.md" in plan.skipped and "-" in plan.skipped


def test_glob_prefix_stops_at_the_first_wildcard(wt):
    assert scaffold.plan_directories(wt, "a/b*/c/d.py").to_create == ["a"]
    assert scaffold.plan_directories(wt, "*.py").to_create == []


def test_create_directories_is_deterministic_and_idempotent(wt):
    cell = "src/pkg/a.py, src/pkg/b.py, src/other/"
    assert scaffold.create_directories(wt, cell) == ["src", "src/pkg", "src/other"]
    assert (wt / "src" / "pkg").is_dir() and (wt / "src" / "other").is_dir()
    assert scaffold.create_directories(wt, cell) == []


def test_case_insensitive_duplicates_are_created_once(wt):
    assert scaffold.create_directories(wt, "Src/a.py, src/b.py") == ["Src"]


@pytest.mark.parametrize("cell", [
    "../outside/a.py", "src/../../x.py", "..\\x.py", "a/..", "/abs/x.py", "\\rooted\\x.py", "C:\\Windows\\x.py",
    "C:x.py", "\\\\server\\share\\x.py", "//server/share/x.py", "\\\\?\\C:\\x.py", ".git/hooks/x.py", "a/.GIT/b",
    "src/CON/x.py", "src/aux.txt/y.py", "src/trailing./x.py", "src/ads:stream/x.py", "ok/a.py, ../bad.py",
])
def test_unsafe_paths_are_refused_and_nothing_is_created(wt, cell):
    before = sorted(p.name for p in wt.rglob("*"))
    with pytest.raises(scaffold.PathRefused):
        scaffold.plan_directories(wt, cell)
    with pytest.raises(scaffold.PathRefused):
        scaffold.create_directories(wt, cell)
    assert sorted(p.name for p in wt.rglob("*")) == before  # all or none, even for "ok/a.py, ../bad.py"


def test_existing_file_where_a_directory_is_needed_is_refused(wt):
    (wt / "src").write_text("not a directory", encoding="utf-8")
    with pytest.raises(scaffold.PathRefused, match="not a directory"):
        scaffold.plan_directories(wt, "src/a.py")


def make_junction(link: Path, target: Path) -> bool:
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    return result.returncode == 0


def test_junction_to_the_outside_is_refused(wt, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    if not make_junction(wt / "linked", outside):
        pytest.skip("cannot create a junction here")
    with pytest.raises(scaffold.PathRefused, match="symbolic link or junction"):
        scaffold.plan_directories(wt, "linked/sub/a.py")
    with pytest.raises(scaffold.PathRefused):
        scaffold.create_directories(wt, "linked/a.py")
    assert list(outside.iterdir()) == []


def test_symlink_is_refused_when_the_platform_allows_creating_one(wt, tmp_path):
    outside = tmp_path / "outside2"
    outside.mkdir()
    try:
        os.symlink(outside, wt / "symlinked", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges here")
    with pytest.raises(scaffold.PathRefused):
        scaffold.plan_directories(wt, "symlinked/a.py")
    assert list(outside.iterdir()) == []


# --- 2. directory pre-creation: runner and CLI ---------------------------------------------------


def set_files(env, cell):
    from multi_agent_ai.board import read_plan, write_plan
    from dataclasses import replace

    plan = read_plan(task_dir(env)).value
    subtasks = tuple(replace(s, files=cell) if s.id == "T1" else s for s in plan.subtasks)
    body = plan.body.replace("src/widget.py", cell)
    write_plan(env.root.path, replace(plan, subtasks=subtasks, body=body, revision=plan.revision + 1), author="lead")


def test_implement_run_creates_the_parents_before_the_worker_starts(env):
    env.install("codex")
    set_files(env, "src/pkg/widget.py")
    env.configure(stdout=CODEX_OK, final="done", run=[["cmd", "/c", "if exist src\\pkg (exit 0) else (exit 9)"]],
                  write={"src/pkg/widget.py": "x = 1\n"})
    outcome = env.run(mode="implement")
    assert outcome.failure_class is None, outcome.result
    assert outcome.result["created_directories"] == ["src", "src/pkg"]
    assert outcome.result["files_changed"] == ["src/pkg/widget.py"]
    with StateIndex.open(env.root.database_path) as index:
        assert index.attempt_details(outcome.attempt_id)["created_directories"] == ["src", "src/pkg"]


def test_empty_pre_created_directories_do_not_count_as_changes(env):
    env.install("codex")
    set_files(env, "src/pkg/widget.py")
    env.configure(stdout=CODEX_OK, final="nothing to do")
    outcome = env.run(mode="implement")
    assert outcome.failure_class == fc.NO_CHANGES  # git ignores empty directories
    assert (env.worktree / "src" / "pkg").is_dir()


def test_review_runs_create_nothing(env):
    env.install("codex")
    set_files(env, "src/pkg/widget.py")
    env.configure(stdout=CODEX_OK, final=FINAL)
    outcome = env.run()
    assert outcome.failure_class is None and "created_directories" not in outcome.result
    assert not (env.worktree / "src").exists()


def test_escaping_files_cell_is_an_environment_error_and_the_provider_never_starts(env):
    env.install("codex")
    set_files(env, "../outside/widget.py")
    env.configure(stdout=CODEX_OK, final="done", write={"a.txt": "a\n"})
    outcome = env.run(mode="implement")
    assert outcome.failure_class == fc.ENVIRONMENT and "cannot be prepared" in outcome.result["failure_detail"]
    assert not env.launched() and not (env.tmp / "outside").exists()


def test_dry_run_reports_directories_and_writes_nothing(env, monkeypatch, capsys):
    env.install("codex")
    set_files(env, "src/pkg/widget.py")
    monkeypatch.setenv("PATH", f"{env.fake_dir}{os.pathsep}{Path(GIT).parent}")
    before = sorted(map(str, env.root.path.rglob("*")))
    code = main(["worker", "run", "--provider", "codex", "--task", "demo", "--subtask", "T1", "--agent-id", "codex-w1",
                 "--mode", "implement", "--worktree", str(env.worktree), "--project-root", str(env.primary)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["directories_to_create"] == ["src", "src/pkg"] and out["problems"] == []
    assert not (env.worktree / "src").exists() and sorted(map(str, env.root.path.rglob("*"))) == before


def test_dry_run_lists_a_refused_path_as_a_problem(env, monkeypatch):
    env.install("codex")
    set_files(env, "C:\\Windows\\x.py")
    preview = preview_worker(env.request(mode="implement"), env=env.env)
    assert any("cannot be prepared" in p for p in preview["problems"]) and not (env.worktree / "Windows").exists()
