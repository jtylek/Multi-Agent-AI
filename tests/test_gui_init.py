"""Project initialization used by the control panel."""

from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")

from multi_agent_ai.gui import ControlPanel, initialize_project_state
from multi_agent_ai.state import resolve_state_root


def test_initialize_project_creates_state_and_keeps_existing_files(tmp_path):
    project = tmp_path / "Selected Project"
    other = tmp_path / "Other Project"
    project.mkdir()
    other.mkdir()

    rows = initialize_project_state(project)
    root = resolve_state_root(project)
    assert rows == [
        ("State root", "ok", "", str(root.path)),
        ("State index", "ok", "", str(root.database_path)),
    ]
    assert root.config_path.is_file()
    assert root.database_path.is_file()
    assert not resolve_state_root(other).path.exists()

    message = root.messages / "keep.txt"
    message.write_text("keep", encoding="utf-8")
    assert initialize_project_state(project) == rows
    assert message.read_text(encoding="utf-8") == "keep"


def test_initialize_button_uses_the_shared_background_action():
    calls = []
    panel = SimpleNamespace(start=lambda *args, **kwargs: calls.append((args, kwargs)))
    ControlPanel.initialize_project(panel)
    args, kwargs = calls[0]
    assert args == ("Initializing project", initialize_project_state)
    assert "Project initialized" in kwargs["completion"]
