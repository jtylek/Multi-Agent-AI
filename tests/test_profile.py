from pathlib import Path

from multi_agent_ai.cli import main
from multi_agent_ai.profile import PROFILE_FILE, ensure_starter_profile, profile_path
from multi_agent_ai.state import init_state_root


def test_init_creates_project_neutral_profile_without_gitignore(tmp_path):
    project = tmp_path / "Target Project"
    project.mkdir()
    assert main(["init", "--project-root", str(project)]) == 0
    profile = project / ".multi-agent-ai" / PROFILE_FILE
    assert profile.is_file()
    text = profile.read_text(encoding="utf-8")
    assert "schema_version = 1" in text
    assert "state_root = \".multi-agent-ai/\"" in text
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


def test_ensure_profile_is_idempotent_for_windows_style_project_path(tmp_path):
    project = tmp_path / "Windows Project"
    project.mkdir()
    first = ensure_starter_profile(project)
    before = first.read_bytes()
    second = ensure_starter_profile(project)
    assert first == second and second.read_bytes() == before
