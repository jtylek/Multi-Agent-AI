"""End-to-end worker runs with FAKE provider executables in real temporary Git repositories.

Real-provider safety (plan revision 24): the worker's PATH is built explicitly and contains
only the fake directory under tmp_path, and the autouse guard fails any test whose provider
command resolves to a file outside tmp_path. With the fake missing, resolution fails closed
(``launch_error``). No real codex/copilot/claude/code is ever started.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from multi_agent_ai import winproc
from multi_agent_ai.board import read_statuses, render_subtask_table, write_plan, write_status
from multi_agent_ai.cli import main
from multi_agent_ai.document import read_document
from multi_agent_ai.mailbox import write_ack, write_request
from multi_agent_ai.models import Ack, Attempt, Plan, Request, Status, Subtask
from multi_agent_ai.state import StateIndex, init_state_root
from multi_agent_ai.workers import failures as fc
from multi_agent_ai.workers.runner import UsageError, WorkerRequest, run_worker

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime")

GIT = shutil.which("git")

FAKE = r'''
import json, os, pathlib, subprocess, sys, time
here = pathlib.Path(__file__).resolve().parent
cfg_file = here / "fake-config.json"
cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
args = sys.argv[1:]
if args == ["--version"]:  # the version probe: answered without counting as a launch
    sys.stdout.write(cfg.get("version", "fake-cli 1.2.3"))
    sys.exit(cfg.get("version_exit", 0))
stdin = sys.stdin.read()
record = {"argv": args, "stdin": stdin, "cwd": os.getcwd(), "env": sorted(os.environ)}
(here / "fake-record.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
for rel, text in cfg.get("write", {}).items():
    p = pathlib.Path(os.getcwd(), rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
for cmd in cfg.get("run", []):
    subprocess.run(cmd, cwd=os.getcwd(), stdin=subprocess.DEVNULL, capture_output=True)
if "-o" in args and cfg.get("final") is not None:
    pathlib.Path(args[args.index("-o") + 1]).write_bytes(cfg["final"].encode("utf-8"))
if "--usage-output-file" in args and cfg.get("usage") is not None:
    pathlib.Path(args[args.index("--usage-output-file") + 1]).write_text(json.dumps(cfg["usage"]), encoding="utf-8")
sys.stdout.write(cfg.get("stdout", ""))
sys.stdout.flush()
sys.stderr.write(cfg.get("stderr", ""))
sys.stderr.flush()
time.sleep(cfg.get("sleep", 0))
sys.exit(cfg.get("exit", 0))
'''

CODEX_OK = ('{"type":"thread.started","thread_id":"t-1"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":2,"output_tokens":5}}\n')
FINAL = 'verdict: agreed\r\ncommit reviewed: x\ntests: "ok" 100%\nfindings: none — żółw\n'

PLAN_BODY = """## Goal

Ship the widget.

## Acceptance criteria

1. Widget renders.
2. Verification command (exact): `python -m pytest -q`.

## Subtasks

""" + render_subtask_table((
    Subtask("T1", "widget", "src/widget.py", (), True, "dev-a", "dev-b", "in progress"),
    Subtask("T2", "other", "src/other.py", (), True, "dev-b", "dev-a", "open"),
))


def git(cwd, *args):
    out = subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture(autouse=True)
def only_fakes(monkeypatch, tmp_path):
    """Fail the test if a provider command resolves outside tmp_path (never a real CLI)."""
    real = winproc.resolve_command
    root = tmp_path.resolve()

    def guarded(name, path=None, env=None):
        resolved = real(name, path=path, env=env)
        for item in (resolved.path, *resolved.prefix[1:]):
            assert Path(item).resolve().is_relative_to(root), f"{name} resolved outside tmp_path: {item}"
        return resolved

    monkeypatch.setattr(winproc, "resolve_command", guarded)
    yield


class Env:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.primary = tmp_path / "Primary Checkout"
        self.primary.mkdir()
        git(self.primary, "init", "-q", "-b", "main")
        hooks = tmp_path / "no hooks"
        hooks.mkdir()
        for key, value in (("user.name", "Test User"), ("user.email", "test@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.hooksPath", str(hooks)),
                           ("core.autocrlf", "false")):
            git(self.primary, "config", key, value)
        (self.primary / ".gitignore").write_text(".multi-agent-ai/\n__pycache__/\n", encoding="utf-8")
        (self.primary / "README.md").write_text("hello\n", encoding="utf-8")
        git(self.primary, "add", "-A")
        git(self.primary, "commit", "-q", "-m", "base")
        self.base = git(self.primary, "rev-parse", "HEAD")
        self.worktree = tmp_path / "wt T1"
        git(self.primary, "worktree", "add", "-q", "-b", "task/demo/T1", str(self.worktree))
        self.root = init_state_root(self.primary)
        write_plan(self.root.path, Plan("demo", "lead", 3, "2026-10-19 10:00", body=PLAN_BODY,
                                        base_commit=self.base[:7], integration_branch="task/demo/integration"),
                   author="lead")
        self.fake_dir = tmp_path / "fake bin"
        self.fake_dir.mkdir()
        self.fake_script = self.fake_dir / "fake_provider.py"
        self.fake_script.write_text(FAKE, encoding="utf-8")
        self.env = {"PATH": str(self.fake_dir), "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
                    "OPENAI_API_KEY": "sk-test-secret", "COPILOT_ALLOW_ALL": "true", "GH_TOKEN": "gh-secret"}

    def install(self, name: str) -> Path:
        shim = self.fake_dir / f"{name}.cmd"
        shim.write_text(f'@"{sys.executable}" "{self.fake_script}" %*\r\n', encoding="utf-8")
        return shim

    def configure(self, **cfg):
        (self.fake_dir / "fake-config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def record(self) -> dict:
        return json.loads((self.fake_dir / "fake-record.json").read_text(encoding="utf-8"))

    def launched(self) -> bool:
        return (self.fake_dir / "fake-record.json").exists()

    def request(self, **kw) -> WorkerRequest:
        values = dict(provider="codex", task_id="demo", subtask_id="T1", agent_id="codex-w1", mode="review",
                      worktree=self.worktree, project_root=self.primary, state_root=self.root, timeout=60)
        values.update(kw)
        return WorkerRequest(**values)

    def run(self, resolver=None, cli_version=None, **kw):
        return run_worker(self.request(**kw), env=self.env, resolver=resolver, cli_version=cli_version)

    def direct_resolver(self):
        """Run the fake as an exe (python + script) so argv is not limited by cmd.exe."""
        def resolve(name, path=None, env=None):
            resolved = winproc.ResolvedCommand(name, self.fake_script, "exe", (sys.executable, str(self.fake_script)))
            assert self.fake_script.resolve().is_relative_to(self.tmp.resolve())
            return resolved
        return resolve


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def task_dir(env):
    return env.root.tasks / "demo"


def no_acks_anywhere(env):
    return not list(env.root.path.rglob("ack-*.md"))


# --- review mode -------------------------------------------------------------------------------


def test_codex_review_success_end_to_end(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final=FINAL)
    outcome = env.run()
    assert outcome.failure_class is None, outcome.result
    run_dir = outcome.run_dir
    assert run_dir.parent == env.root.path / "runs"
    assert {p.name for p in run_dir.iterdir()} == {"prompt.md", "stdout.log", "stderr.log", "final-message.md",
                                                  "result.json"}

    record = env.record()
    assert record["argv"] == ["exec", "-C", str(env.worktree), "-s", "read-only", "--ephemeral", "--color", "never",
                              "--json", "-o", str(run_dir / "final-message.md"), "-"]
    assert record["stdin"] == (run_dir / "prompt.md").read_text(encoding="utf-8")
    assert os.path.normcase(record["cwd"]) == os.path.normcase(str(env.worktree))
    names = {n.upper() for n in record["env"]}
    assert "OPENAI_API_KEY" not in names and "COPILOT_ALLOW_ALL" not in names and "GH_TOKEN" not in names

    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["exit_code"] == 0 and result["failure_class"] is None and result["profile"] == "codex-review"
    assert result["usage"] == {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5}
    assert result["argv"][-1] == "-" and result["prompt_sha256"]
    assert result["git"]["commit_reviewed"] == env.base and result["tree_control"] == "job-object"

    review = task_dir(env) / "review-T1-codex-w1.md"
    assert outcome.review_path == review
    raw = review.read_bytes()
    assert raw.endswith(FINAL.encode("utf-8"))  # verbatim, CRLF included
    header = raw[: len(raw) - len(FINAL.encode("utf-8"))].decode("utf-8")
    assert header.endswith("\n\n") and "\r" not in header
    doc = read_document(review)
    assert doc.headers["agent"] == "codex-w1" and doc.headers["provider"] == "codex"
    assert doc.headers["commit"] == env.base and doc.headers["attempt"] == outcome.attempt_id
    assert doc.headers["profile"] == "codex-review" and doc.headers["exit-code"] == "0"
    assert doc.headers["plan-revision"] == "3"

    status = read_statuses(task_dir(env))["codex-w1"]
    assert status.ok, status.errors
    assert status.value.state == "review" and status.value.plan_revision == 3
    assert status.value.last_commit == "none" and "Managed worker: provider=codex" in status.value.body
    assert str(env.worktree) in status.value.branch

    with StateIndex.open(env.root.database_path) as index:
        attempt = index.get(Attempt, outcome.attempt_id)
        details = index.attempt_details(outcome.attempt_id)
    assert attempt.provider == "codex" and attempt.exit_code == 0 and attempt.failure_category is None
    assert attempt.model is None and attempt.number == 1
    assert details["mode"] == "review" and details["usage"]["output_tokens"] == 5
    assert git(env.worktree, "status", "--porcelain") == "" and no_acks_anywhere(env)


def test_usage_is_unknown_when_not_reported_and_attempts_are_numbered(env):
    env.install("codex")
    env.configure(stdout='{"type":"thread.started","thread_id":"t"}\n', final="ok\n")
    first = env.run()
    second = env.run(model="gpt-test")
    assert first.result["usage"] == "unknown" and second.ok
    with StateIndex.open(env.root.database_path) as index:
        assert index.attempt_details(first.attempt_id)["usage"] == "unknown"
        assert index.get(Attempt, second.attempt_id).number == 2
        assert index.get(Attempt, second.attempt_id).model == "gpt-test"
    assert env.record()["argv"][-3:] == ["-m", "gpt-test", "-"]


def test_review_that_modifies_files_fails_and_change_is_kept(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="done", write={"README.md": "changed by reviewer\n"})
    outcome = env.run()
    assert outcome.failure_class == fc.REVIEW_MODIFIED_FILES
    assert "README.md" in outcome.result["failure_detail"]
    assert (env.worktree / "README.md").read_text() == "changed by reviewer\n"  # reported, not reverted
    assert outcome.review_path is None and not (task_dir(env) / "review-T1-codex-w1.md").exists()
    status = read_statuses(task_dir(env))["codex-w1"].value
    assert status.state == "blocked" and status.blockers.startswith("review_modified_files")


def test_review_detects_new_untracked_file_and_edit_of_already_dirty_file(env):
    (env.worktree / "notes.txt").write_text("dirty before\n", encoding="utf-8")
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="done", write={"notes.txt": "edited again\n"})
    assert env.run().failure_class == fc.REVIEW_MODIFIED_FILES


def test_review_of_unknown_commit_is_environment_failure(env):
    env.install("codex")
    outcome = env.run(commit="deadbeefdeadbeef")
    assert outcome.failure_class == fc.ENVIRONMENT and not env.launched()


# --- implement mode ---------------------------------------------------------------------------


def test_implement_success_orchestrator_commits(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="summary: added widget", write={"src/widget.py": "x = 1\n"})
    outcome = env.run(mode="implement", model="gpt-test")
    assert outcome.failure_class is None, outcome.result
    assert env.record()["argv"][4] == "workspace-write"
    head = git(env.worktree, "rev-parse", "HEAD")
    assert outcome.commit == head and git(env.worktree, "rev-parse", "HEAD^") == env.base
    assert git(env.worktree, "rev-parse", "--abbrev-ref", "HEAD") == "task/demo/T1"
    message = git(env.worktree, "log", "-1", "--format=%B")
    for fragment in ("Agent: codex-w1", "Provider: codex", "Model: gpt-test", f"Attempt: {outcome.attempt_id}"):
        assert fragment in message
    assert git(env.worktree, "show", "--name-only", "--format=", "HEAD") == "src/widget.py"
    assert git(env.worktree, "status", "--porcelain") == ""
    assert git(env.primary, "rev-parse", "main") == env.base  # never merged
    status = read_statuses(task_dir(env))["codex-w1"].value
    assert status.state == "review" and status.last_commit == head
    assert outcome.review_path is None and no_acks_anywhere(env)


def test_state_root_inside_the_worktree_is_never_committed(env):
    # Not git-ignored (the repo's .gitignore only covers .multi-agent-ai/): logs, prompts and the
    # status files must still stay out of the orchestrator's commit.
    inside = init_state_root(env.worktree, "state-data")
    write_plan(inside.path, Plan("demo", "lead", 3, "2026-10-19 10:00", body=PLAN_BODY,
                                 base_commit=env.base[:7], integration_branch="task/demo/integration"),
               author="lead")
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="summary: added widget", write={"src/widget.py": "x = 1\n"})
    outcome = run_worker(env.request(mode="implement", state_root=inside), env=env.env)
    assert outcome.failure_class is None, outcome.result
    assert (inside.path / "runs" / outcome.attempt_id / "prompt.md").is_file()  # the run did write there
    assert git(env.worktree, "show", "--name-only", "--format=", "HEAD") == "src/widget.py"


def test_implement_without_changes_is_no_changes(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="nothing to do")
    outcome = env.run(mode="implement")
    assert outcome.failure_class == fc.NO_CHANGES and outcome.commit is None
    assert git(env.worktree, "rev-parse", "HEAD") == env.base


def test_worker_commit_is_a_git_policy_violation(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="done", write={"a.txt": "a\n"},
                  run=[[GIT, "add", "-A"], [GIT, "commit", "-q", "-m", "sneaky"]])
    outcome = env.run(mode="implement")
    assert outcome.failure_class == fc.GIT_POLICY_VIOLATION and outcome.commit is None
    assert "HEAD moved" in outcome.result["failure_detail"]


def test_worker_branch_at_head_is_a_git_policy_violation(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="done", write={"a.txt": "a\n"}, run=[[GIT, "branch", "sneaky"]])
    outcome = env.run(mode="implement")
    assert outcome.failure_class == fc.GIT_POLICY_VIOLATION
    assert "refs/heads/sneaky" in outcome.result["failure_detail"]


def test_implement_refuses_primary_dirty_live_and_wrong_branch(env):
    env.install("codex")
    assert env.run(mode="implement", worktree=env.primary).failure_class == fc.ENVIRONMENT
    assert env.run(mode="implement", live_branches=("task/demo/T1",)).failure_class == fc.ENVIRONMENT
    (env.worktree / "dirty.txt").write_text("x", encoding="utf-8")
    dirty = env.run(mode="implement")
    assert dirty.failure_class == fc.ENVIRONMENT and "dirty" in dirty.result["failure_detail"]
    (env.worktree / "dirty.txt").unlink()
    other = env.tmp / "wt other"
    git(env.primary, "worktree", "add", "-q", "-b", "feature/x", str(other))
    wrong = env.run(mode="implement", worktree=other)
    assert wrong.failure_class == fc.ENVIRONMENT and "task/demo/T1" in wrong.result["failure_detail"]
    assert not env.launched()
    status = read_statuses(task_dir(env))["codex-w1"].value
    assert status.state == "blocked" and status.blockers.startswith("environment")


def test_not_a_git_worktree_is_environment(env):
    env.install("codex")
    plain = env.tmp / "plain dir"
    plain.mkdir()
    assert env.run(worktree=plain).failure_class == fc.ENVIRONMENT


# --- usage limits (T15; synthetic provider output) ----------------------------------------------


@pytest.fixture
def trusted_versions(monkeypatch):
    """List version "t1" for every detector table; the shipped tables list none yet."""
    from multi_agent_ai.workers import quota

    for provider, (detector, _) in quota.DETECTORS.items():
        monkeypatch.setitem(quota.DETECTORS, provider, (detector, frozenset({"t1"})))


CODEX_LIMIT = ('{"type":"thread.started","thread_id":"thr-9"}\n'
               '{"type":"token_count","rate_limits":{"rate_limit_reached_type":"primary",'
               '"primary":{"used_percent":100,"resets_at":1792000000}}}\n')


def test_codex_quota_exhausted_records_blocker_limit_and_resume(env, trusted_versions):
    env.install("codex")
    env.configure(stdout=CODEX_LIMIT, exit=1, write={"src/partial.py": "x = 1\n"})
    outcome = env.run(mode="implement", model="gpt-5", cli_version="t1")
    assert outcome.failure_class == fc.QUOTA_EXHAUSTED and outcome.commit is None
    limit = outcome.result["limit"]
    assert limit["kind"] == "quota_exhausted" and limit["session"] == "thr-9"
    assert limit["resume_command"] == "codex exec resume thr-9"
    assert limit["head_at_launch"] == env.base and limit["status_at_launch"] == []
    assert limit["reset"] and limit["reset_text"].endswith("UTC (reported)")
    assert limit["counts_toward_success_rate"] is False and limit["raw_message"]
    assert "--ephemeral" not in env.record()["argv"]  # implement sessions stay resumable
    status = read_statuses(task_dir(env))["codex-w1"].value
    assert status.state == "blocked"
    assert status.blockers.startswith("quota: codex (gpt-5), resets 2026-") and status.blockers.endswith("(reported)")
    with StateIndex.open(env.root.database_path) as index:
        details = index.attempt_details(outcome.attempt_id)
    assert details["failure_class"] == "quota_exhausted" and details["limit"]["session"] == "thr-9"
    assert no_acks_anywhere(env)


def test_unknown_reset_is_reported_as_unknown(env, trusted_versions):
    env.install("codex")
    env.configure(stdout='{"type":"token_count","rate_limits":{"spend_control_reached":true}}\n', exit=1)
    outcome = env.run(cli_version="t1")
    assert outcome.failure_class == fc.QUOTA_EXHAUSTED and outcome.result["limit"]["reset"] is None
    status = read_statuses(task_dir(env))["codex-w1"].value
    assert status.blockers == "quota: codex (provider default), reset unknown"


def test_copilot_cap_uses_the_cap_prefix(env, trusted_versions):
    env.install("copilot")
    env.configure(stderr="Stopped: --max-ai-credits 5 reached", exit=1)
    outcome = env.run(env.direct_resolver(), provider="copilot", agent_id="copilot-w1", cli_version="t1")
    assert outcome.failure_class == fc.CAP_REACHED
    assert outcome.result["limit"]["resume_command"].startswith("copilot --resume ")
    status = read_statuses(task_dir(env))["copilot-w1"].value
    assert status.blockers.startswith("cap: copilot (provider default), reset unknown")


def test_bare_429_is_a_provider_error_and_a_plain_failure_stays_nonzero_exit(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, stderr="HTTP 429 Too Many Requests", exit=1)
    throttled = env.run()
    assert throttled.failure_class == fc.PROVIDER_ERROR and throttled.result["limit"]["counts_toward_success_rate"]
    env.configure(stdout=CODEX_OK, stderr="something broke", exit=3)
    assert env.run().failure_class == fc.NONZERO_EXIT


def test_run_worker_without_a_known_cli_version_never_reports_quota(env, trusted_versions):
    env.install("codex")
    env.configure(stdout=CODEX_LIMIT, exit=1, version_exit=1)
    outcome = env.run()  # no cli_version supplied and the probe fails: the version stays unknown
    assert outcome.failure_class == fc.PROVIDER_ERROR
    assert "unknown" in outcome.result["limit"]["reason"] and outcome.result["limit"]["reset"] is None
    status = read_statuses(task_dir(env))["codex-w1"].value
    assert status.blockers.startswith("provider_error") and not status.blockers.startswith("quota:")
    env.configure(stdout=CODEX_LIMIT, exit=1, version_exit=1)
    assert env.run(agent_id="codex-w2", cli_version="9.9.9").failure_class == fc.PROVIDER_ERROR  # unlisted


def test_shipped_tables_make_every_real_run_provider_error(env):
    env.install("codex")
    env.configure(stdout=CODEX_LIMIT, exit=1)
    assert env.run(cli_version="0.163.0").failure_class == fc.PROVIDER_ERROR  # no version is listed yet


# --- CLI version (T18) ---------------------------------------------------------------------------


def test_cli_version_is_read_from_the_cli_and_recorded(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final=FINAL, version="codex-cli 0.163.0")
    outcome = env.run()
    assert outcome.failure_class is None
    assert outcome.result["cli_version"] == {"version": "0.163.0", "raw": "codex-cli 0.163.0", "error": None}
    assert env.record()["argv"][0] == "exec"  # the recorded launch is the real run, not the probe
    with StateIndex.open(env.root.database_path) as index:
        assert index.attempt_details(outcome.attempt_id)["cli_version"] == "0.163.0"


def test_probed_version_keys_the_quota_tables(env, monkeypatch):
    from multi_agent_ai.workers import quota

    detector, _ = quota.DETECTORS["codex"]
    env.install("codex")
    env.configure(stdout=CODEX_LIMIT, exit=1, version="codex-cli 7.7.7")
    monkeypatch.setitem(quota.DETECTORS, "codex", (detector, frozenset({"7.7.7"})))
    assert env.run().failure_class == fc.QUOTA_EXHAUSTED  # listed version, read from the CLI itself
    monkeypatch.setitem(quota.DETECTORS, "codex", (detector, frozenset({"1.0.0"})))
    other = env.run(agent_id="codex-w2")
    assert other.failure_class == fc.PROVIDER_ERROR and "unlisted 7.7.7" in other.result["limit"]["reason"]


def test_failed_version_probe_is_recorded_and_does_not_block_the_run(env, monkeypatch):
    from multi_agent_ai.workers import quota

    detector, _ = quota.DETECTORS["codex"]
    monkeypatch.setitem(quota.DETECTORS, "codex", (detector, frozenset({"7.7.7"})))
    env.install("codex")
    env.configure(stdout=CODEX_LIMIT, exit=1, version="boom", version_exit=1)
    outcome = env.run()
    assert outcome.result["cli_version"]["version"] is None and "exited 1" in outcome.result["cli_version"]["error"]
    assert any("cli version unknown" in f for f in outcome.result["findings"])
    assert outcome.failure_class == fc.PROVIDER_ERROR  # unknown version: never a quota verdict


def test_version_reader_can_be_injected_and_cli_version_overrides_the_probe(env):
    from multi_agent_ai.workers.versions import CliVersion

    env.install("codex")
    env.configure(stdout=CODEX_OK, final=FINAL)
    calls = []

    def reader(command, child_env):
        calls.append(command.name)
        return CliVersion("2.0.0", "fake")

    outcome = run_worker(env.request(), env=env.env, version_reader=reader)
    assert calls == ["codex"] and outcome.result["cli_version"]["version"] == "2.0.0"
    outcome = run_worker(env.request(agent_id="codex-w2"), env=env.env, version_reader=reader, cli_version="3.0.0")
    assert calls == ["codex"] and outcome.result["cli_version"]["version"] == "3.0.0"


def test_dry_run_does_not_probe_the_cli(env, monkeypatch, capsys):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final=FINAL, version="codex-cli 0.163.0")
    monkeypatch.setenv("PATH", f"{env.fake_dir}{os.pathsep}{Path(GIT).parent}")
    code = main(["worker", "run", "--provider", "codex", "--task", "demo", "--subtask", "T1", "--agent-id",
                 "codex-w1", "--mode", "review", "--worktree", str(env.worktree), "--project-root", str(env.primary)])
    assert code == 0 and "not probed" in json.loads(capsys.readouterr().out)["cli_version"]


def test_a_limit_message_on_a_successful_exit_is_not_reclassified(env):
    env.install("codex")
    env.configure(stdout=CODEX_LIMIT, final=FINAL, exit=0)
    assert env.run().failure_class is None


# --- process failures ---------------------------------------------------------------------------


def test_missing_executable_fails_closed_as_launch_error(env):
    outcome = env.run()  # no fake installed: PATH holds only the empty fake dir
    assert outcome.failure_class == fc.LAUNCH_ERROR and outcome.exit_code is None
    result = json.loads((outcome.run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["failure_class"] == "launch_error"


def test_timeout_kills_and_classifies(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="late", sleep=60)
    outcome = env.run(timeout=2)
    assert outcome.failure_class == fc.TIMEOUT and outcome.result["timed_out"]
    assert outcome.result["kill_method"] == "job-object"


@pytest.mark.parametrize("stderr, expected", [
    ("error: unexpected argument '--bogus' found\n", fc.INVALID_ARGUMENTS),
    ("Error: model 'nope' is not supported\n", fc.UNSUPPORTED_MODEL),
    ("Not logged in. Run codex login.\n", fc.AUTH),
    ("Permission denied: tool shell(rm) is not allowed\n", fc.PERMISSION_DENIED),
    ("unexpected crash\n", fc.NONZERO_EXIT),
])
def test_nonzero_exit_is_classified_from_output(env, stderr, expected):
    env.install("codex")
    env.configure(stderr=stderr, exit=1)
    outcome = env.run(model="nope")
    assert outcome.failure_class == expected and outcome.exit_code == 1
    assert read_statuses(task_dir(env))["codex-w1"].value.state == "blocked"


def test_malformed_output(env):
    env.install("codex")
    env.configure(stdout="this is not json\n", final="x")
    assert env.run().failure_class == fc.MALFORMED_OUTPUT
    env.configure(stdout=CODEX_OK)  # no final message file
    assert env.run().failure_class == fc.MALFORMED_OUTPUT


# --- copilot ----------------------------------------------------------------------------------


def test_copilot_unconfigured_profile_never_launches(env):
    env.install("copilot")
    plan_without_command = PLAN_BODY.replace("`python -m pytest -q`", "as for T6")
    write_plan(env.root.path, Plan("demo", "lead", 4, "2026-10-19 11:00", body=plan_without_command,
                                   base_commit=env.base[:7], integration_branch="task/demo/integration"),
               author="lead")
    outcome = env.run(provider="copilot", agent_id="copilot-w1")
    assert outcome.failure_class == fc.PROFILE_UNCONFIGURED and not env.launched()
    assert "--verify-command" in outcome.result["failure_detail"]


def test_copilot_review_end_to_end_with_awkward_prompt(env):
    events = [{"type": "session.start"}, {"type": "assistant.message", "data": {"content": "verdict: agreed\n"}}]
    env.configure(stdout="".join(json.dumps(e) + "\n" for e in events), usage={"inputTokens": 7, "outputTokens": 3})
    outcome = env.run(env.direct_resolver(), provider="copilot", agent_id="copilot-w1",
                      verify_command=r'.venv\Scripts\python -m pytest -q')
    assert outcome.failure_class is None, outcome.result
    record = env.record()
    prompt = (outcome.run_dir / "prompt.md").read_text(encoding="utf-8")
    assert '"' in prompt and "\n" in prompt
    session = outcome.result["session_id"]
    assert record["argv"] == [
        "-C", str(env.worktree), "-p", prompt, "--output-format", "json", "--silent", "--session-id", session,
        "--usage-output-file", str(outcome.run_dir / "usage.json"), "--disallow-temp-dir",
        "--available-tools=bash,view,grep,glob",
        r"--allow-tool=shell(git diff),shell(git log),shell(git show),shell(.venv\Scripts\python -m pytest -q)",
        "--deny-tool=shell(git commit),shell(git push),url",
    ]
    assert record["stdin"] == ""
    argv = outcome.result["argv"]
    assert argv[argv.index("-p") + 1] == "<PROMPT>" and prompt not in json.dumps(outcome.result)
    assert outcome.result["usage"] == {"inputTokens": 7, "outputTokens": 3}
    assert (task_dir(env) / "review-T1-copilot-w1.md").read_text(encoding="utf-8").endswith("verdict: agreed\n")
    assert "COPILOT_ALLOW_ALL" not in {n.upper() for n in record["env"]}


def test_copilot_through_a_plain_batch_file_refuses_the_prompt(env):
    env.install("copilot")
    outcome = env.run(provider="copilot", agent_id="copilot-w1")
    assert outcome.failure_class == fc.INVALID_ARGUMENTS and not env.launched()


def test_copilot_tool_denial_is_reported(env):
    env.configure(stdout='{"type":"error","message":"Permission denied: tool bash was denied"}\n', exit=1)
    outcome = env.run(env.direct_resolver(), provider="copilot", agent_id="copilot-w1")
    assert outcome.failure_class == fc.PERMISSION_DENIED


# --- agent ids ----------------------------------------------------------------------------------


def test_agent_id_rules(env):
    env.install("codex")
    for bad in ("codex", "lead", "dev-b"):  # product name, coordinator, other row's owner
        with pytest.raises(UsageError):
            env.run(agent_id=bad)
    write_status(env.root.path, "demo", Status("chat-1", 3, "T2", "in progress", "none", "none run", "none",
                                               "2026-10-19 10:00"), author="chat-1")
    with pytest.raises(UsageError, match="status file"):
        env.run(agent_id="chat-1")
    write_request(env.root.path, Request("m1", "lead", ("codex-w9",), "hello"), author="lead")
    with pytest.raises(UsageError, match="mailbox"):
        env.run(agent_id="codex-w9")
    write_ack(env.root.path, Ack("m1", "codex-w9", "received"), author="codex-w9")
    with pytest.raises(UsageError):
        env.run(agent_id="codex-w9")
    with pytest.raises(UsageError):
        env.run(agent_id="../x")
    assert not env.launched()
    # the same managed worker may run again; another provider may not reuse its id
    env.configure(stdout=CODEX_OK, final="ok")
    assert env.run().ok and env.run().ok
    with pytest.raises(UsageError):
        env.run(provider="copilot")


def test_worker_never_writes_an_ack(env):
    write_request(env.root.path, Request("m1", "lead", ("dev-a",), "hello"), author="lead")
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="ok")
    env.run()
    env.run(mode="implement")
    assert no_acks_anywhere(env)
    assert sorted(p.name for p in (env.root.messages / "m1").iterdir()) == ["request.md"]


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_worker_run_with_fake_on_path(env, monkeypatch, capsys):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="verdict: agreed\n")
    git_dir = str(Path(GIT).parent)
    assert not any((Path(git_dir) / f"codex{ext}").exists() for ext in (".exe", ".cmd", ".bat", ".com"))
    monkeypatch.setenv("PATH", f"{env.fake_dir}{os.pathsep}{git_dir}")
    code = main(["worker", "run", "--provider", "codex", "--task", "demo", "--subtask", "T1", "--agent-id",
                 "codex-w1", "--mode", "review", "--worktree", str(env.worktree), "--project-root",
                 str(env.primary), "--timeout", "60", "--execute"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["failure_class"] is None and out["review_file"]


def _snapshot_tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_cli_worker_run_is_a_dry_run_by_default(env, monkeypatch, capsys):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="verdict: agreed\n")
    git_dir = str(Path(GIT).parent)
    monkeypatch.setenv("PATH", f"{env.fake_dir}{os.pathsep}{git_dir}")
    before = _snapshot_tree(env.root.path)
    code = main(["worker", "run", "--provider", "codex", "--task", "demo", "--subtask", "T1", "--agent-id",
                 "codex-w1", "--mode", "review", "--worktree", str(env.worktree), "--project-root",
                 str(env.primary)])
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert code == 0 and out["dry_run"] is True and out["problems"] == []
    assert out["argv"] and out["prompt_sha256"] and out["profile"] == "codex-review"
    assert "--execute" in captured.err
    assert _snapshot_tree(env.root.path) == before  # no runs dir, no status file
    assert not (env.fake_dir / "fake-record.json").exists()  # the provider was never started


def test_cli_dry_run_reports_problems_without_writing(env, monkeypatch, capsys):
    env.install("codex")
    git_dir = str(Path(GIT).parent)
    monkeypatch.setenv("PATH", f"{env.fake_dir}{os.pathsep}{git_dir}")
    before = _snapshot_tree(env.root.path)
    code = main(["worker", "run", "--provider", "codex", "--task", "demo", "--subtask", "T1", "--agent-id",
                 "codex-w1", "--mode", "review", "--worktree", str(env.worktree), "--project-root",
                 str(env.primary), "--commit", "deadbeefdeadbeef"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["problems"]
    assert _snapshot_tree(env.root.path) == before


def test_cli_dry_run_still_rejects_bad_requests_with_exit_2(env, capsys):
    code = main(["worker", "run", "--provider", "codex", "--task", "demo", "--subtask", "T1", "--agent-id",
                 "lead", "--mode", "review", "--worktree", str(env.worktree), "--project-root", str(env.primary)])
    assert code == 2


def test_cli_rejects_bad_requests_with_exit_2(env, capsys):
    base = ["worker", "run", "--provider", "codex", "--task", "demo", "--subtask", "T1", "--mode", "review",
            "--project-root", str(env.primary)]
    assert main(base + ["--agent-id", "codex-w1", "--worktree", "relative\\wt"]) == 2
    assert main(base + ["--agent-id", "lead", "--worktree", str(env.worktree)]) == 2
    assert main(base + ["--agent-id", "codex-w1", "--worktree", str(env.worktree), "--pass-env",
                        "COPILOT_ALLOW_ALL"]) == 2
    assert not (env.root.path / "runs").exists()


def test_guard_rejects_a_command_resolved_outside_tmp_path():
    system32 = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32")
    with pytest.raises(AssertionError, match="outside tmp_path"):
        winproc.resolve_command("cmd", path=system32)  # resolution only; nothing is run


def test_invalid_other_row_is_reported_not_fatal_but_invalid_own_row_is(env):
    env.install("codex")
    env.configure(stdout=CODEX_OK, final="ok")
    plan_file = task_dir(env) / "plan.md"
    text = plan_file.read_text(encoding="utf-8")
    plan_file.write_text(text.replace("| dev-b | dev-a | open |", "| dev-b | dev-a | merged (abc1234) |"),
                         encoding="utf-8")
    outcome = env.run()
    assert outcome.ok and any("merged (abc1234)" in f for f in outcome.result["findings"])
    with pytest.raises(UsageError, match="not a valid row"):
        env.run(subtask_id="T2")
