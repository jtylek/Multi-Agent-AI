"""Non-modal Live Activity window over the project's state root."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .activity import ActivityWatcher

MAX_ROWS = 500


class ActivityWindow(tk.Toplevel):
    """Show newly observed state-root evidence; it stays usable beside other windows."""

    def __init__(self, parent: tk.Misc, state_root: Path):
        super().__init__(parent)
        self.title("Multi-Agent-AI — Live Activity")
        self.geometry("1050x650")
        self.state_root = Path(state_root)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Project activity: {self.state_root}").pack(anchor="w")
        ttk.Label(frame, text="Updates every second. A request is recorded, not necessarily delivered; "
                              "only an acknowledgment proves receipt.").pack(anchor="w", pady=(3, 8))
        columns = ("time", "event", "summary", "source")
        self.table = ttk.Treeview(frame, columns=columns, show="headings", height=13)
        for key, width in (("time", 155), ("event", 185), ("summary", 310), ("source", 340)):
            self.table.heading(key, text=key.title())
            self.table.column(key, width=width, stretch=True)
        self.table.pack(fill="both", expand=True)
        self.detail = tk.Text(frame, height=12, wrap="word", state="disabled")
        self.detail.pack(fill="both", expand=True, pady=(8, 0))
        self.watcher = ActivityWatcher(self.state_root)
        self.details: dict[str, str] = {}
        self.table.bind("<<TreeviewSelect>>", self._select)
        self.refresh()

    def _select(self, _event=None):
        selection = self.table.selection()
        if selection:
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0", self.details.get(selection[0], ""))
            self.detail.configure(state="disabled")

    def refresh(self):
        if not self.winfo_exists():
            return
        for item in self.watcher.poll():
            row = self.table.insert("", "end", values=(item.at, item.event, item.summary, item.source))
            self.details[row] = item.detail
            self.table.see(row)
        while len(self.table.get_children()) > MAX_ROWS:
            oldest = self.table.get_children()[0]
            self.details.pop(oldest, None)
            self.table.delete(oldest)
        self.after(1000, self.refresh)

    def show(self):
        """Bring an already-open window to the front."""
        self.deiconify()
        self.lift()
        self.focus_set()
