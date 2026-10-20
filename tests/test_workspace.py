"""Worktree lifecycle manager (T23) in temporary Git repositories. No provider is started and no
Git command runs outside tmp_path."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from multi_agent_ai import workspace as ws
from multi_agent_ai.board import render_subtask_table, write_plan
from multi_agent_ai.cli import main
from multi_agent_ai.models import Plan, Subtask
from multi_agent_ai.state import init_state_root
from multi_agent_ai.workers import gitops

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")

GIT = shutil.which("git")
TASK = "demo"

PLAN_BODY = """## Goal

Exercise the workspace manager.

## Subtasks

""" + render_subtask_table((
    Subtask("A", "first", "a.txt", (), True, "dev-a", "dev-b", "in progress"),
    Subtask("B", "needs A", "b.txt", ("A",), True, "dev-b", "dev-a", "open"),
    Subtask("C", "independent", "c.txt", (), True, "dev-c", "waived", "in progress"),
))


def git(cwd, *args):
    out = subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture(autouse=True)
def only_tmp_git(monkeypatch, tmp_path):
    """Fail any Git call whose working directory is outside tmp_path."""
    real = gitops.git
    root = tmp_path.resolve()
    calls = []

    def guarded(cwd, *args, **kw):
        assert Path(cwd).resolve().is_relative_to(root), f"git {args} ran outside tmp_path: {cwd}"
        calls.append(args)
        return real(cwd, *args, **kw)

    monkeypatch.setattr(gitops, "git", guarded)
    yield
    # D18: the manager never deletes a branch, whatever the test did
    deletes = [a for a in calls if a[:1] == ("branch",) and any(x in ("-d", "-D", "--delete") for x in a)]
    assert not deletes, f"the manager deleted a branch: {deletes}"


class Ctx:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.project = tmp_path / "My Project"
        self.project.mkdir()
        git(self.project, "init", "-q", "-b", "main")
        hooks = tmp_path / "no hooks"
        hooks.mkdir()
        for key, value in (("user.name", "Test User"), ("user.email", "t@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.hooksPath", str(hooks)), ("core.autocrlf", "false")):
            git(self.project, "config", key, value)
        (self.project / ".gitignore").write_text(".multi-agent-ai/\n", encoding="utf-8")
        (self.project / "shared.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "base")
        self.base = git(self.project, "rev-parse", "HEAD")
        self.state = init_state_root(self.project)
        write_plan(self.state.path, Plan(TASK, "lead", 1, "2026-10-20 10:00", body=PLAN_BODY, base_commit=self.base[:7]),
                   author="lead")
        self.wt_root = tmp_path / "wt dir"

    def integration(self, execute=True, **kw):
        return ws.create_integration(self.project, self.state, TASK, worktrees_root=self.wt_root, execute=execute, **kw)

    def subtask(self, name, execute=True, **kw):
        return ws.create_subtask_worktree(self.project, self.state, TASK, name, worktrees_root=self.wt_root,
                                          execute=execute, **kw)

    def commit(self, name, filename, text, message=None):
        path = Path(ws.read_records(self.state, TASK)[name].path)
        (path / filename).write_text(text, encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", message or f"work on {name}")
        return git(path, "rev-parse", "HEAD")

    def review(self, name, sha, status="agreed", reviewer="codex"):
        path = self.state.tasks / TASK / f"review-{name}-{reviewer}.md"
        path.write_text(f"status: {status}\nreviewer: {reviewer}\nsubtask: {name}\nreviewed: commit {sha[:7]} on "
                        f"task/{TASK}/{name}\nplan-revision: 1\n\nbody\n", encoding="utf-8")

    def tree(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if ".git" not in p.parts)

    def tip(self, branch):
        return git(self.project, "rev-parse", branch)


@pytest.fixture
def ctx(tmp_path):
    return Ctx(tmp_path)


def code_of(call):
    with pytest.raises(ws.WorkspaceError) as caught:
        call()
    return caught.value.code


# --- integration branch and subtask worktrees ---------------------------------------------------


def test_create_integration_dry_run_then_execute(ctx):
    before = ctx.tree()
    dry = ctx.integration(execute=False)
    assert not dry.executed and ctx.tree() == before and dry.record.branch == f"task/{TASK}/integration"
    result = ctx.integration()
    path = Path(result.record.path)
    assert result.executed and path == ctx.wt_root / "My Project-integration" and path.is_dir()
    info = gitops.worktree_info(path)
    assert info.is_linked_worktree and info.branch == f"task/{TASK}/integration" and info.head == ctx.base
    assert ws.read_records(ctx.state, TASK)["integration"].base == ctx.base


def test_integration_branch_exists_with_worktree_is_refused_but_reused_when_free(ctx):
    first = ctx.integration()
    assert code_of(ctx.integration) == "branch_exists"
    git(ctx.project, "worktree", "remove", first.record.path)
    again = ctx.integration()
    assert again.executed and again.record.reused and gitops.worktree_info(Path(again.record.path)).head == ctx.base


def test_bad_base_and_missing_base(ctx):
    assert code_of(lambda: ctx.integration(base_commit="deadbeefdeadbeef")) == "bad_base"
    assert code_of(lambda: ctx.integration(base_commit="not-a-sha")) == "bad_base"
    assert not (ctx.wt_root / "My Project-integration").exists()


def test_unmerged_base_is_allowed_with_a_warning(ctx):
    git(ctx.project, "checkout", "-q", "-b", "side")
    (ctx.project / "side.txt").write_text("x", encoding="utf-8")
    git(ctx.project, "add", "-A")
    git(ctx.project, "commit", "-q", "-m", "side")
    side = git(ctx.project, "rev-parse", "HEAD")
    git(ctx.project, "checkout", "-q", "main")
    result = ctx.integration(base_commit=side, execute=False)
    assert any("not reachable" in w for w in result.warnings)


def test_subtask_worktree_is_created_from_the_plan_base_and_recorded(ctx):
    result = ctx.subtask("A")
    path = Path(result.record.path)
    assert path == ctx.wt_root / "My Project-A" and result.record.base_from == "plan"
    info = gitops.worktree_info(path)
    assert info.branch == f"task/{TASK}/A" and info.head == ctx.base and info.is_linked_worktree
    assert not gitops.snapshot(path).status
    assert ws.read_records(ctx.state, TASK)["A"].state == "created"


@pytest.mark.parametrize("protected", [("main", "master", "task/demo/A")])
def test_protected_branch_is_refused(ctx, protected):
    assert code_of(lambda: ws.create_subtask_worktree(ctx.project, ctx.state, TASK, "A", worktrees_root=ctx.wt_root,
                                                      protected=protected, execute=True)) == "protected_branch"
    assert not (ctx.wt_root / "My Project-A").exists()


def test_reserved_and_unknown_subtask_ids(ctx):
    assert code_of(lambda: ctx.subtask("integration")) == "protected_branch"
    assert code_of(lambda: ctx.subtask("nope")) == "plan_mismatch"
    assert code_of(lambda: ctx.subtask("..bad")) in {"plan_mismatch", "path_outside_policy"}


def test_existing_branch_or_path_is_refused(ctx):
    (ctx.project / "other.txt").write_text("x", encoding="utf-8")
    git(ctx.project, "add", "-A")
    git(ctx.project, "commit", "-q", "-m", "elsewhere")
    git(ctx.project, "branch", f"task/{TASK}/A")  # at a commit that is not the plan's base
    with pytest.raises(ws.WorkspaceError) as caught:
        ctx.subtask("A")
    assert caught.value.code == "branch_exists" and f"branch -D task/{TASK}/A" in str(caught.value)
    git(ctx.project, "reset", "-q", "--hard", ctx.base)
    (ctx.wt_root / "My Project-C").mkdir(parents=True)
    assert code_of(lambda: ctx.subtask("C")) == "worktree_exists"


@pytest.mark.parametrize("make_root, code", [
    (lambda c: c.project / "inside", "path_outside_policy"),
    (lambda c: c.state.path / "wt", "path_outside_policy"),
    (lambda c: Path("relative") / "wt", "path_outside_policy"),
    (lambda c: c.tmp / ("x" * 250), "path_outside_policy"),
])
def test_worktree_path_policy(ctx, make_root, code):
    root = make_root(ctx)
    assert code_of(lambda: ws.create_subtask_worktree(ctx.project, ctx.state, TASK, "A", worktrees_root=root,
                                                      execute=True)) == code


def test_failure_after_worktree_add_removes_only_the_worktree_and_a_retry_succeeds(ctx, monkeypatch):
    real = gitops.worktree_info

    def boom(path):
        if Path(path).name == "My Project-A":
            raise gitops.GitError("injected failure after worktree add")
        return real(path)

    monkeypatch.setattr(gitops, "worktree_info", boom)
    assert code_of(lambda: ctx.subtask("A")) == "git_failed"
    monkeypatch.setattr(gitops, "worktree_info", real)
    assert not (ctx.wt_root / "My Project-A").exists()
    assert git(ctx.project, "branch", "--list", f"task/{TASK}/A").strip() != ""  # D18: the branch is kept
    assert "A" not in ws.read_records(ctx.state, TASK)
    assert len(git(ctx.project, "worktree", "list").splitlines()) == 1
    retry = ctx.subtask("A")  # the leftover branch is at the expected base and has no worktree
    assert retry.executed and retry.record.reused
    path = Path(retry.record.path)
    info = gitops.worktree_info(path)
    assert info.branch == f"task/{TASK}/A" and info.head == ctx.base and not gitops.snapshot(path).status


def test_branch_with_a_worktree_is_not_reused(ctx):
    ctx.subtask("A")
    with pytest.raises(ws.WorkspaceError) as caught:
        ctx.subtask("A")
    assert caught.value.code == "branch_exists" and "has a worktree" in caught.value.message


def test_the_module_never_issues_a_branch_delete():
    import re

    pattern = re.compile(r"""["']branch["']\s*,[^)\n]*["'](?:-d|-D|--delete)["']""")
    for name in ("workspace.py", "workspace_cli.py"):
        source = (Path(ws.__file__).parent / name).read_text(encoding="utf-8")
        assert not pattern.findall(source), name


def test_primary_checkout_is_untouched_by_creation(ctx):
    head = git(ctx.project, "rev-parse", "HEAD")
    ctx.integration()
    ctx.subtask("A")
    assert git(ctx.project, "rev-parse", "HEAD") == head == ctx.base
    assert git(ctx.project, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(ctx.project, "status", "--porcelain") == ""


def test_lock_file_blocks_a_second_operation_and_is_kept(ctx):
    lock = ctx.state.tasks / TASK / ws.LOCK_FILE
    lock.write_text("held\n", encoding="utf-8")
    assert code_of(lambda: ctx.subtask("A")) == "locked"
    assert lock.is_file()
    assert not (ctx.wt_root / "My Project-A").exists()
    lock.unlink()
    assert ctx.subtask("A").executed and not lock.exists()


# --- check and list -----------------------------------------------------------------------------


def test_check_reports_dirty_locked_and_orphans(ctx):
    ctx.integration()
    a = ctx.subtask("A")
    listing = ws.list_worktrees(ctx.project, ctx.state, TASK)
    assert [s.problems for s in listing] == [[], []]
    (Path(a.record.path) / "scratch.txt").write_text("x", encoding="utf-8")
    git(ctx.project, "worktree", "lock", a.record.path)
    orphan = ctx.tmp / "orphan"
    git(ctx.project, "worktree", "add", "-q", "-b", f"task/{TASK}/X", str(orphan))
    listing = {s.subtask: s for s in ws.list_worktrees(ctx.project, ctx.state, TASK)}
    assert any("dirty_worktree" in p for p in listing["A"].problems) and listing["A"].locked
    assert any("orphan" in p for p in listing["X"].problems)
    assert listing["integration"].problems == []


def test_state_root_inside_a_worktree_is_not_dirty(ctx):
    ctx.integration()
    record = ws.read_records(ctx.state, TASK)["integration"]
    status = ws.check_worktree(ctx.project, ctx.state, Path(record.path), "integration", record.branch, record.base)
    assert status.clean and status.problems == [] and status.ahead_of_base == 0


def test_preflight_gate(ctx):
    assert code_of(lambda: ws.preflight_for_execution(ctx.project, ctx.state, TASK, "A")) == "not_a_worktree"
    a = ctx.subtask("A")
    assert ws.preflight_for_execution(ctx.project, ctx.state, TASK, "A").problems == []
    (Path(a.record.path) / "dirty.txt").write_text("x", encoding="utf-8")
    assert any("dirty" in p for p in ws.preflight_for_execution(ctx.project, ctx.state, TASK, "A").problems)


# --- integration --------------------------------------------------------------------------------


def make_three(ctx):
    ctx.integration()
    ctx.subtask("A")
    ctx.subtask("C")
    a = ctx.commit("A", "a.txt", "a\n")
    c = ctx.commit("C", "c.txt", "c\n")
    return a, c


def test_integrate_in_dependency_order_with_reviews(ctx):
    a, c = make_three(ctx)
    ctx.review("A", a)
    main_before = ctx.tip("main")
    dry = ws.integrate(ctx.project, ctx.state, TASK, ["A", "C"])
    assert [i.result for i in dry.items] == ["would_merge", "would_merge"] and not dry.executed
    assert ctx.tip(f"task/{TASK}/integration") == ctx.base  # a dry run merges nothing
    report = ws.integrate(ctx.project, ctx.state, TASK, ["A", "C"], execute=True)  # C: review waived
    assert report.ok and [i.result for i in report.items] == ["merged", "merged"]
    integration = Path(ws.read_records(ctx.state, TASK)["integration"].path)
    assert (integration / "a.txt").is_file() and (integration / "c.txt").is_file()
    subjects = git(integration, "log", "--merges", "--format=%s").splitlines()
    assert subjects == [f"merge: {TASK}/C ({c[:7]})", f"merge: {TASK}/A ({a[:7]})"]
    assert ctx.tip("main") == main_before and git(ctx.project, "status", "--porcelain") == ""
    records = ws.read_records(ctx.state, TASK)
    assert records["A"].state == "merged" and records["A"].merge_commit
    again = ws.integrate(ctx.project, ctx.state, TASK, ["A"], execute=True)
    assert again.items[0].result == "already_merged"


def test_dependant_must_wait_and_is_branched_from_the_integration_tip(ctx):
    a, _ = make_three(ctx)
    assert code_of(lambda: ctx.subtask("B")) == "dependency_unmet"
    ctx.review("A", a)
    ws.integrate(ctx.project, ctx.state, TASK, ["A"], execute=True)
    b = ctx.subtask("B")
    assert b.record.base_from == "integration" and b.record.base == ctx.tip(f"task/{TASK}/integration")
    assert (Path(b.record.path) / "a.txt").is_file()
    bsha = ctx.commit("B", "b.txt", "b\n")
    ctx.review("B", bsha)
    report = ws.integrate(ctx.project, ctx.state, TASK, ["B"], execute=True)
    assert report.ok and report.items[0].result == "merged"


def test_wrong_order_is_dependency_unmet_and_stops(ctx):
    a, _ = make_three(ctx)
    ctx.review("A", a)
    ctx.subtask  # noqa: B018 (B's worktree is not needed; its branch is)
    git(ctx.project, "branch", f"task/{TASK}/B", ctx.tip(f"task/{TASK}/integration"))
    report = ws.integrate(ctx.project, ctx.state, TASK, ["B", "A"], execute=True)
    assert report.items[0].code == "dependency_unmet" and report.items[1].result == "not_attempted"
    assert not report.ok and ctx.tip(f"task/{TASK}/integration") == ctx.base


@pytest.mark.parametrize("mode", ["missing", "changes", "other_sha", "moved_after_review"])
def test_unreviewed_commits_are_never_merged(ctx, mode):
    a, _ = make_three(ctx)
    if mode == "changes":
        ctx.review("A", a, status="changes requested")
    elif mode == "other_sha":
        ctx.review("A", ctx.base)
    elif mode == "moved_after_review":
        ctx.review("A", a)
        ctx.commit("A", "a2.txt", "late\n")
    report = ws.integrate(ctx.project, ctx.state, TASK, ["A"], execute=True)
    assert report.items[0].code == "unreviewed" and ctx.tip(f"task/{TASK}/integration") == ctx.base


def test_conflict_aborts_leaves_integration_clean_and_stops(ctx):
    ctx.integration()
    ctx.subtask("A")
    ctx.subtask("C")
    a = ctx.commit("A", "shared.txt", "line1\nA says\nline3\n")
    c = ctx.commit("C", "shared.txt", "line1\nC says\nline3\n")
    ctx.review("A", a)
    report = ws.integrate(ctx.project, ctx.state, TASK, ["A", "C"], execute=True)
    assert [i.result for i in report.items] == ["merged", "conflict"]
    assert report.items[1].code == "merge_conflict" and report.items[1].conflicts == ["shared.txt"]
    integration = Path(ws.read_records(ctx.state, TASK)["integration"].path)
    assert not gitops.snapshot(integration).status and "A says" in (integration / "shared.txt").read_text(encoding="utf-8")
    assert ws.read_records(ctx.state, TASK)["C"].state != "merged" and c != a


def test_dirty_integration_worktree_blocks_integration(ctx):
    a, _ = make_three(ctx)
    ctx.review("A", a)
    integration = Path(ws.read_records(ctx.state, TASK)["integration"].path)
    (integration / "stray.txt").write_text("x", encoding="utf-8")
    report = ws.integrate(ctx.project, ctx.state, TASK, ["A"], execute=True)
    assert report.items[0].code == "dirty_worktree"


def test_integrating_into_a_protected_integration_branch_is_refused(ctx):
    make_three(ctx)
    assert code_of(lambda: ws.integrate(ctx.project, ctx.state, TASK, ["A"], protected=("main", f"task/{TASK}/integration"),
                                        execute=True)) == "protected_branch"


# --- removal and retention ----------------------------------------------------------------------


def merged_a(ctx):
    a, _ = make_three(ctx)
    ctx.review("A", a)
    ws.integrate(ctx.project, ctx.state, TASK, ["A"], execute=True)
    return Path(ws.read_records(ctx.state, TASK)["A"].path)


def test_remove_merged_worktree_keeps_the_branch(ctx):
    path = merged_a(ctx)
    dry = ws.remove_worktree(ctx.project, ctx.state, TASK, "A")
    assert not dry.executed and path.is_dir()
    done = ws.remove_worktree(ctx.project, ctx.state, TASK, "A", execute=True)
    assert done.executed and not path.exists()
    assert git(ctx.project, "branch", "--list", f"task/{TASK}/A").strip() != ""
    assert ws.read_records(ctx.state, TASK)["A"].state == "removed"
    assert code_of(lambda: ws.remove_worktree(ctx.project, ctx.state, TASK, "A", execute=True)) == "unsafe_remove"


def test_remove_refuses_unmerged_dirty_locked_and_unknown(ctx):
    path = merged_a(ctx)
    assert code_of(lambda: ws.remove_worktree(ctx.project, ctx.state, TASK, "C", execute=True)) == "unsafe_remove"
    assert code_of(lambda: ws.remove_worktree(ctx.project, ctx.state, TASK, "ZZ", execute=True)) == "unsafe_remove"
    (path / "dirty.txt").write_text("x", encoding="utf-8")
    assert code_of(lambda: ws.remove_worktree(ctx.project, ctx.state, TASK, "A", execute=True)) == "unsafe_remove"
    (path / "dirty.txt").unlink()
    git(ctx.project, "worktree", "lock", str(path))
    assert code_of(lambda: ws.remove_worktree(ctx.project, ctx.state, TASK, "A", execute=True)) == "unsafe_remove"
    assert path.is_dir()


def test_retained_failed_worktree_is_kept_unless_the_user_discards(ctx):
    make_three(ctx)
    c_path = Path(ws.read_records(ctx.state, TASK)["C"].path)
    head_before = git(c_path, "rev-parse", "HEAD")
    record = ws.retain_failed(ctx.state, TASK, "C", "timeout; runs/20261020-x", execute=True)
    assert record.state == "retained" and "timeout" in record.note
    assert git(c_path, "rev-parse", "HEAD") == head_before and c_path.is_dir()
    assert code_of(lambda: ws.remove_worktree(ctx.project, ctx.state, TASK, "C", execute=True)) == "unsafe_remove"
    dry = ws.remove_worktree(ctx.project, ctx.state, TASK, "C", discard_failed=True)
    assert not dry.executed and c_path.is_dir()
    ws.remove_worktree(ctx.project, ctx.state, TASK, "C", execute=True, discard_failed=True)
    assert not c_path.exists() and git(ctx.project, "branch", "--list", f"task/{TASK}/C").strip() != ""
    assert code_of(lambda: ws.retain_failed(ctx.state, TASK, "nope", "x", execute=True)) == "not_a_worktree"


# --- CLI ------------------------------------------------------------------------------------------


def run_cli(ctx, *args):
    return main(["workspace", *args, "--task", TASK, "--project-root", str(ctx.project)])


def test_cli_dry_run_by_default_and_execute(ctx, capsys):
    before = ctx.tree()
    assert run_cli(ctx, "create-integration", "--worktrees-root", str(ctx.wt_root)) == 0
    captured = capsys.readouterr()
    assert "would create" in captured.out and "dry run" in captured.err and ctx.tree() == before
    assert run_cli(ctx, "create-integration", "--worktrees-root", str(ctx.wt_root), "--execute") == 0
    assert "created" in capsys.readouterr().out and (ctx.wt_root / "My Project-integration").is_dir()
    assert run_cli(ctx, "create", "--subtask", "A", "--worktrees-root", str(ctx.wt_root), "--execute", "--json") == 0
    assert json.loads(capsys.readouterr().out)["record"]["branch"] == f"task/{TASK}/A"
    assert run_cli(ctx, "list", "--json") == 0 and len(json.loads(capsys.readouterr().out)) == 2
    assert run_cli(ctx, "check") == 0


def test_cli_refusals_exit_1_with_the_code_and_usage_errors_exit_2(ctx, capsys, tmp_path):
    assert run_cli(ctx, "create", "--subtask", "B", "--worktrees-root", str(ctx.wt_root), "--execute") == 1
    assert capsys.readouterr().err.startswith("dependency_unmet:")
    assert main(["workspace", "list", "--task", TASK, "--project-root", str(tmp_path / "nowhere")]) == 2
    assert "state root not found" in capsys.readouterr().err
    assert run_cli(ctx, "integrate", "--order", "A") == 1
    assert capsys.readouterr().err.startswith("not_a_worktree:")


def test_cli_integrate_and_remove_end_to_end(ctx, capsys):
    a, _ = make_three(ctx)
    ctx.review("A", a)
    capsys.readouterr()
    assert run_cli(ctx, "integrate", "--order", "A,C") == 0
    assert "would_merge" in capsys.readouterr().out
    assert run_cli(ctx, "integrate", "--order", "A,C", "--execute") == 0
    assert "integration tip" in capsys.readouterr().out
    assert run_cli(ctx, "remove", "--subtask", "A") == 0 and Path(ws.read_records(ctx.state, TASK)["A"].path).is_dir()
    assert run_cli(ctx, "remove", "--subtask", "A", "--execute") == 0
    assert run_cli(ctx, "retain", "--subtask", "C", "--reason", "kept") == 0  # dry run
    assert ws.read_records(ctx.state, TASK)["C"].state != "retained"
    assert run_cli(ctx, "retain", "--subtask", "C", "--reason", "kept", "--execute") == 0
    assert ws.read_records(ctx.state, TASK)["C"].state == "retained"
    assert run_cli(ctx, "check", "--json") in (0, 1)
