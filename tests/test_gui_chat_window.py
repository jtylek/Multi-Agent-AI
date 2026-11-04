"""Exercise the modal chat with fake provider calls and real Tk widgets."""

import time
import tkinter as tk

import pytest

from multi_agent_ai import gui_chat_window
from multi_agent_ai.gui import ControlPanel


def test_hello_world_reaches_each_output_pane(monkeypatch, tmp_path):
    calls = []

    def fake_ask(provider, root, coordinator, history, message, **kwargs):
        calls.append((provider, root, coordinator, message))
        return "Hello World!"

    monkeypatch.setattr(gui_chat_window, "detected_chat_providers",
                        lambda: ("codex", "claude", "copilot"))
    monkeypatch.setattr(gui_chat_window, "ask_provider", fake_ask)
    try:
        parent = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display or Tcl library unavailable: {exc}")
    parent.withdraw()
    try:
        coordinator = tk.StringVar(master=parent, value="claude")
        chat = gui_chat_window.ProjectChatWindow(parent, tmp_path, coordinator)
        try:
            chat.test_all_agents()
            deadline = time.monotonic() + 5
            while chat.pending and time.monotonic() < deadline:
                parent.update()
                time.sleep(0.01)
            assert not chat.pending
            assert {call[0] for call in calls} == {"codex", "claude", "copilot"}
            assert all(call[2] == "claude" for call in calls)
            for output in chat.outputs.values():
                assert "Hello World!" in output.get("1.0", "end")
            assert "Hello World!" in chat.coordinator_output.get("1.0", "end")
            assert int(chat.conversation_frame.grid_info()["row"]) == 2
            assert int(chat.agent_panes.grid_info()["row"]) == 7
            chat.coordinator_choice.set("Codex")
            chat._select_coordinator()
            assert coordinator.get() == "codex"
            assert "Hello World!" in chat.coordinator_output.get("1.0", "end")
        finally:
            chat.close()
    finally:
        parent.destroy()


def test_control_panel_saves_and_reopens_project(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "user-settings"))
    project = tmp_path / "project"
    project.mkdir()
    try:
        panel = ControlPanel()
    except tk.TclError as exc:
        pytest.skip(f"Tk display or Tcl library unavailable: {exc}")
    panel.withdraw()
    try:
        panel.project.set(str(project))
        panel.coordinator.set("claude")
        panel.save_project()
        assert panel.projects.current == str(project.resolve())
        panel.coordinator.set("codex")
        panel.open_project_path(str(project))
        assert panel.coordinator.get() == "claude"
        assert panel.recent_menu.index("end") == 0
    finally:
        panel.destroy()
