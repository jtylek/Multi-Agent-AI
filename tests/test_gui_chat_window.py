"""Exercise the non-modal chat with fake provider calls and real Tk widgets."""

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


def test_chat_is_not_modal_and_opens_one_live_activity_window(monkeypatch, tmp_path):
    monkeypatch.setattr(gui_chat_window, "detected_chat_providers", lambda: ("claude",))
    try:
        parent = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display or Tcl library unavailable: {exc}")
    parent.withdraw()
    try:
        coordinator = tk.StringVar(master=parent, value="claude")
        chat = gui_chat_window.ProjectChatWindow(parent, tmp_path, coordinator)
        try:
            assert chat.grab_current() is None
            chat.open_activity()
            first = chat.activity_window
            assert first is not None and first.winfo_exists()
            assert chat.grab_current() is None
            chat.open_activity()
            assert chat.activity_window is first
        finally:
            chat.close()
    finally:
        parent.destroy()


@pytest.mark.parametrize("initialized", [True, False])
def test_chat_session_is_logged_only_in_an_initialized_project(monkeypatch, tmp_path, initialized):
    from multi_agent_ai.job import read_events
    from multi_agent_ai.state import init_state_root

    def fake_ask(provider, root, coordinator, history, message, *, on_event=None, **kwargs):
        on_event("tool_use", {"provider": provider, "summary": "Read README.md"})
        return "Hello World!"

    monkeypatch.setattr(gui_chat_window, "detected_chat_providers", lambda: ("claude",))
    monkeypatch.setattr(gui_chat_window, "ask_provider", fake_ask)
    if initialized:
        init_state_root(tmp_path)
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
        finally:
            chat.close()
        logs = list((tmp_path / ".multi-agent-ai").glob("chat/*/events.jsonl"))
        if not initialized:
            assert logs == []
            assert "not initialized" in chat.recording.get()
            return
        events, _ = read_events(logs[0])
        assert [e["kind"] for e in events] == ["session_started", "message_sent", "tool_use",
                                               "reply_received", "session_closed"]
        assert events[3]["text"] == "Hello World!"
    finally:
        parent.destroy()


def test_team_task_test_option_dispatches_and_logs(monkeypatch, tmp_path):
    from multi_agent_ai.job import read_events
    from multi_agent_ai.state import init_state_root

    def fake_ask(provider, root, coordinator, history, message, **kwargs):
        if "Reply with only JSON" in message:
            return "{}"  # unusable plan -> built-in split
        if provider == coordinator:
            return "```python\nprint('calculator')\n```"
        return "```python\n# part\n```"

    monkeypatch.setattr(gui_chat_window, "detected_chat_providers", lambda: ("codex", "claude", "copilot"))
    monkeypatch.setattr(gui_chat_window, "ask_provider", fake_ask)
    init_state_root(tmp_path)
    try:
        parent = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display or Tcl library unavailable: {exc}")
    parent.withdraw()
    try:
        coordinator = tk.StringVar(master=parent, value="claude")
        chat = gui_chat_window.ProjectChatWindow(parent, tmp_path, coordinator)
        try:
            chat.test_choice.set(next(k for k, v in gui_chat_window.TESTS.items() if v == "team"))
            chat.run_selected_test()
            assert str(chat.test_button.cget("state")) == "disabled"
            deadline = time.monotonic() + 10
            while chat.team_running and time.monotonic() < deadline:
                parent.update()
                time.sleep(0.01)
            assert not chat.team_running
            assert "compiles" in chat.status.get()
            assert "From coordinator claude" in chat.outputs["codex"].get("1.0", "end")
            events, _ = read_events(chat.log.path)
            kinds = [e["kind"] for e in events]
            assert kinds.count("task_dispatched") == 3
            assert kinds[-1] == "team_task_finished"
            assert (chat.log.path.parent / "team-task" / "calculator.py").is_file()
        finally:
            chat.close()
    finally:
        parent.destroy()


def test_control_panel_reuses_open_chat_window(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "user-settings"))
    monkeypatch.setattr(gui_chat_window, "detected_chat_providers", lambda: ("claude",))
    project = tmp_path / "project"
    project.mkdir()
    try:
        panel = ControlPanel()
    except tk.TclError as exc:
        pytest.skip(f"Tk display or Tcl library unavailable: {exc}")
    panel.withdraw()
    try:
        panel.project.set(str(project))
        panel.open_project_chat()
        first = panel.chat_window
        panel.open_project_chat()
        assert panel.chat_window is first
    finally:
        panel.destroy()


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
