import tomllib
from pathlib import Path

import pytest

from multi_agent_ai.cli import main
from multi_agent_ai import profile
from multi_agent_ai.profile import PROFILE_FILE, STARTER_PROFILE, ensure_starter_profile, profile_path
from multi_agent_ai.state import init_state_root, read_config


def test_init_creates_project_neutral_profile_without_gitignore(tmp_path):
    project = tmp_path / "Target Project"
    project.mkdir()
    assert main(["init", "--project-root", str(project)]) == 0
    profile = project / ".multi-agent-ai" / PROFILE_FILE
    assert profile.is_file()
    assert read_config(init_state_root(project))["profile"] == str(profile.resolve())
    text = profile.read_text(encoding="utf-8")
    assert "schema_version = 1" in text
    assert "state_root = \".multi-agent-ai/\"" in text
    assert "root = \"..\"" in text
    assert "Epesi" not in text
    assert not (project / ".gitignore").exists()


def test_init_never_overwrites_existing_profile_or_profile_path_changes(tmp_path):
    project = tmp_path / "Target Project"
    project.mkdir()
    profile = profile_path(project, "storage/framework/agents")
    profile.parent.mkdir(parents=True)
    profile.write_text("# owner configuration\n", encoding="utf-8")
    init_state_root(project, "storage/framework/agents")
    assert profile.read_text(encoding="utf-8") == "# owner configuration\n"
    assert profile == project / "storage" / "framework" / "agents" / PROFILE_FILE


def test_custom_state_root_profile_is_toml_and_points_to_real_roots(tmp_path):
    project = tmp_path / "Target Project"
    project.mkdir()
    root = init_state_root(project, "storage/framework/agents")
    profile = tomllib.loads((root.path / PROFILE_FILE).read_text(encoding="utf-8"))
    assert profile["profile"]["schema_version"] == 1
    assert profile["profile"]["root"] == "../../.."
    assert profile["state"]["state_root"] == "storage/framework/agents/"
    assert profile["safety"]["deployment_requires_user_approval"] is True
    assert profile["safety"]["deployment_source"] == "clean-verified-artifact"
    assert profile["safety"]["deployment_requires_clean_tree"] is True
    assert profile["safety"]["live_branches"] == []


def test_starter_constant_is_valid_toml_with_schema_safety_fields():
    profile = tomllib.loads(STARTER_PROFILE)
    assert profile["profile"]["schema_version"] == 1
    assert set(profile["safety"]) >= {
        "live_branches", "deployment_requires_user_approval", "deployment_source",
        "deployment_requires_clean_tree",
    }


def test_ensure_profile_is_idempotent_for_windows_style_project_path(tmp_path):
    project = tmp_path / "Windows Project"
    project.mkdir()
    first = ensure_starter_profile(project)
    before = first.read_bytes()
    second = ensure_starter_profile(project)
    assert first == second and second.read_bytes() == before


def test_cross_drive_relpath_falls_back_to_absolute_paths(tmp_path, monkeypatch):
    project = tmp_path / "Target Project"
    state = tmp_path / "state-root"
    project.mkdir()

    def fail_relpath(*args):
        raise ValueError("different drives")

    monkeypatch.setattr(profile.os.path, "relpath", fail_relpath)
    path = ensure_starter_profile(project, state)
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    assert document["profile"]["root"] == project.resolve().as_posix()
    assert document["state"]["state_root"] == state.resolve().as_posix() + "/"


def test_render_failure_does_not_leave_zero_byte_profile(tmp_path, monkeypatch):
    project = tmp_path / "Target Project"
    state = tmp_path / "state-root"
    project.mkdir()
    path = profile_path(project, state)

    def fail_render(*args):
        raise RuntimeError("render failed")

    monkeypatch.setattr(profile, "_render_profile", fail_render)
    with pytest.raises(RuntimeError, match="render failed"):
        ensure_starter_profile(project, state)
    assert not path.exists()
