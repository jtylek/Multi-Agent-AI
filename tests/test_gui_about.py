"""The About dialog exposes the project license and repository link."""

import pytest

pytest.importorskip("tkinter")

from multi_agent_ai import gui


def test_about_dialog_shows_license_and_opens_repository(monkeypatch):
    widgets = []
    opened = []

    class Widget:
        def __init__(self, parent=None, **options):
            self.options = options
            widgets.append(self)

        def pack(self, **options):
            pass

        def title(self, value):
            pass

        def resizable(self, width, height):
            pass

        def transient(self, parent):
            pass

        def destroy(self):
            pass

        # The dialog withdraws itself, measures and centers over the main window.
        def withdraw(self):
            pass

        def update_idletasks(self):
            pass

        def deiconify(self):
            pass

        def grab_set(self):
            pass

        def winfo_reqwidth(self):
            return 300

        def winfo_reqheight(self):
            return 200

        def geometry(self, value):
            self.geometry_value = value

    class Parent:
        def winfo_rootx(self):
            return 100

        def winfo_rooty(self):
            return 100

        def winfo_width(self):
            return 900

        def winfo_height(self):
            return 600

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

    monkeypatch.setattr(gui.tk, "Toplevel", Widget)
    for kind in ("Frame", "Label", "Button"):
        monkeypatch.setattr(gui.ttk, kind, Widget)
    monkeypatch.setattr(gui.webbrowser, "open", opened.append)

    gui.ControlPanel.show_about(Parent())

    dialog = widgets[0]
    assert dialog.geometry_value == "+400+300"  # centered over the 900x600 main window
    labels = [widget.options.get("text", "") for widget in widgets]
    assert "Free software under the MIT License." in gui.ABOUT_TEXT
    assert "Copyright (c) 2026 by Janusz Tylek" in gui.ABOUT_TEXT
    assert gui.ABOUT_TEXT in labels
    assert gui.PROJECT_URL in labels
    open_button = next(widget for widget in widgets
                       if widget.options.get("text") == "Open GitHub repository")
    open_button.options["command"]()
    assert opened == ["https://github.com/jtylek/Multi-Agent-AI"]
