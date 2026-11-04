"""Saved project and recent-project behavior without a Tk display."""

import json

import pytest

from multi_agent_ai.gui_projects import load_projects, remember_project


def test_save_and_reopen_restores_folder_and_coordinator(tmp_path):
    settings = tmp_path / "settings" / "projects.json"
    first = tmp_path / "First Project"
    second = tmp_path / "Second Project"
    first.mkdir()
    second.mkdir()

    saved = remember_project(first, "claude", path=settings)
    assert saved.current == str(first.resolve())
    assert saved.coordinator_for(first) == "claude"
    assert json.loads(settings.read_text(encoding="utf-8"))["version"] == 1

    remember_project(second, "copilot", path=settings)
    reopened = remember_project(first, path=settings)
    assert reopened.recent == (str(first.resolve()), str(second.resolve()))
    assert reopened.coordinator_for(first) == "claude"
    assert reopened.coordinator_for(second) == "copilot"
    assert load_projects(settings) == reopened


def test_invalid_settings_are_reported_and_not_replaced(tmp_path):
    settings = tmp_path / "projects.json"
    settings.write_text("broken JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read project settings"):
        remember_project(tmp_path, "codex", path=settings)
    assert settings.read_text(encoding="utf-8") == "broken JSON"


def test_recent_projects_are_bounded(tmp_path):
    settings = tmp_path / "projects.json"
    for number in range(12):
        folder = tmp_path / f"project-{number}"
        folder.mkdir()
        remember_project(folder, path=settings)
    recent = load_projects(settings).recent
    assert len(recent) == 10
    assert recent[0] == str((tmp_path / "project-11").resolve())
