"""doctor: read-only checks with injected resolver, version reader and git probe (T20).

No provider CLI is started: the resolver returns fake paths and the version reader is a stub.
"""

import json
from pathlib import Path

import pytest

from multi_agent_ai import doctor, winproc
from multi_agent_ai.cli import main
from multi_agent_ai.state import init_state_root, resolve_state_root
from multi_agent_ai.workers.versions import CliVersion


def resolver_for(found):
    def resolve(name, path=None, env=None):
        if name not in found:
            raise winproc.LaunchError(f"{name}: no .exe/.cmd/.bat found on PATH")
        return winproc.ResolvedCommand(name, Path(f"C:/tools/{name}.exe"), "exe", (f"C:/tools/{name}.exe",))
    return resolve


def versions(mapping):
    calls = []

    def reader(command, env):
        calls.append(command.name)
        return mapping.get(command.name, CliVersion(None, "", "version probe exited 1"))
    reader.calls = calls
    return reader


def git_ok(*args, cwd=None):
    return (0, "git version 2.50.0.windows.1") if args == ("--version",) else (0, "true")


def by_name(checks):
    return {c.name: c for c in checks}


@pytest.fixture
def root(tmp_path):
    return init_state_root(tmp_path)


def run(root, tmp_path, found=("codex", "copilot", "claude"), reader=None, **kw):
    reader = reader or versions({n: CliVersion("1.2.3", n) for n in ("codex", "copilot", "claude")})
    return doctor.run_doctor(tmp_path, root, env={"PATH": ""}, resolver=resolver_for(found), version_reader=reader,
                             git_probe=kw.pop("git_probe", git_ok), **kw), reader


def test_everything_present(root, tmp_path):
    checks, reader = run(root, tmp_path)
    named = by_name(checks)
    assert named["Python"].status == doctor.OK and named["Git"].status == doctor.OK
    assert named["Project repository"].status == doctor.OK
    for display in ("Codex CLI", "GitHub Copilot CLI", "Claude Code"):
        assert named[display].status == doctor.OK and named[f"{display} version"].detail == "1.2.3"
        assert named[f"{display} sign-in"].status == doctor.NOT_CHECKED
        assert "never" in named[f"{display} sign-in"].detail
    assert named["State root"].status == doctor.OK and named["State index"].status == doctor.OK
    assert reader.calls == ["codex", "copilot", "claude"]
    assert doctor.exit_code(checks) == 0


def test_missing_cli_is_a_warning_with_a_fix_and_is_not_probed(root, tmp_path):
    checks, reader = run(root, tmp_path, found=("codex",))
    named = by_name(checks)
    assert named["GitHub Copilot CLI"].status == doctor.WARN and "npm install" in named["GitHub Copilot CLI"].fix
    assert "GitHub Copilot CLI version" not in named and reader.calls == ["codex"]
    assert doctor.exit_code(checks) == 0


def test_no_provider_cli_at_all_fails(root, tmp_path):
    checks, _ = run(root, tmp_path, found=())
    assert by_name(checks)["Provider CLIs"].status == doctor.FAIL and doctor.exit_code(checks) == 1


def test_unreadable_version_is_a_warning(root, tmp_path):
    checks, _ = run(root, tmp_path, reader=versions({}))
    named = by_name(checks)
    assert named["Codex CLI version"].status == doctor.WARN and "exited 1" in named["Codex CLI version"].detail
    assert "reinstall" in named["Codex CLI version"].fix


def test_old_python_fails(root, tmp_path):
    checks, _ = run(root, tmp_path, python_version=(3, 10, 4))
    assert by_name(checks)["Python"].status == doctor.FAIL and doctor.exit_code(checks) == 1
    assert doctor.check_python((3, 11, 0)).status == doctor.OK


def test_missing_git_and_not_a_repository(root, tmp_path):
    def no_git(*args, cwd=None):
        raise doctor.gitops.GitError("git.exe not found on PATH")

    checks, _ = run(root, tmp_path, git_probe=no_git)
    assert by_name(checks)["Git"].status == doctor.FAIL and "git-scm.com" in by_name(checks)["Git"].fix

    def not_repo(*args, cwd=None):
        return (0, "git version 2") if args == ("--version",) else (128, "fatal: not a git repository")

    checks, _ = run(root, tmp_path, git_probe=not_repo)
    assert by_name(checks)["Project repository"].status == doctor.FAIL


def test_missing_state_root_fails_with_init_hint(tmp_path):
    checks, _ = run(resolve_state_root(tmp_path), tmp_path)
    state = by_name(checks)["State root"]
    assert state.status == doctor.FAIL and "init" in state.fix and doctor.exit_code(checks) == 1


def test_partial_state_root_warns(tmp_path):
    root = resolve_state_root(tmp_path)
    root.path.mkdir()
    named = by_name(run(root, tmp_path)[0])
    assert named["State root layout"].status == doctor.WARN and named["State index"].status == doctor.WARN


def test_doctor_writes_nothing(root, tmp_path):
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    run(root, tmp_path)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def test_text_and_json_output(root, tmp_path):
    checks, _ = run(root, tmp_path, found=("codex",))
    text = doctor.format_text(checks)
    assert "[NOT CHECKED]" in text and "-> Install it with" in text and "Nothing was changed." in text
    data = doctor.to_dict(checks)
    assert data["ok"] is True and {"name", "status", "detail", "fix"} <= set(data["checks"][0])


def test_cli_doctor_with_no_provider_on_path(tmp_path, monkeypatch, capsys):
    """End to end through the CLI with PATH holding only git: no provider can be found or started."""
    init_state_root(tmp_path)
    git = doctor.gitops.git_executable()
    monkeypatch.setattr(doctor, "_git_probe",
                        lambda *a, cwd=None: (0, "git version x") if a == ("--version",) else (0, "true"))
    monkeypatch.setenv("PATH", str(Path(git).parent))
    seen = []
    monkeypatch.setattr(doctor, "read_cli_version", lambda *a, **k: seen.append(a) or CliVersion("1.0.0"))
    code = main(["doctor", "--project-root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False and seen == []
    assert any(c["name"] == "Provider CLIs" and c["status"] == "fail" for c in out["checks"])
