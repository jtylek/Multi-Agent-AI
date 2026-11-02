"""Windows-friendly Tkinter control panel for local provider discovery and doctor."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import doctor
from .state import resolve_state_root


class ControlPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Multi-Agent-AI Control Panel")
        self.geometry("900x590")
        self.minsize(700, 450)
        self.project = tk.StringVar(value=str(Path.cwd()))
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Multi-Agent-AI", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Local AI provider discovery and environment diagnostics").pack(anchor="w", pady=(0, 12))
        project_row = ttk.Frame(outer)
        project_row.pack(fill="x", pady=6)
        ttk.Label(project_row, text="Project folder:").pack(side="left")
        ttk.Entry(project_row, textvariable=self.project).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(project_row, text="Browse…", command=self.browse).pack(side="left")
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=8)
        self.discover_button = ttk.Button(actions, text="Discover AI Agents", command=self.discover)
        self.discover_button.pack(side="left", padx=(0, 8))
        self.doctor_button = ttk.Button(actions, text="Run Doctor", command=self.run_doctor)
        self.doctor_button.pack(side="left")
        self.status = tk.StringVar(value="Ready. Select your project folder.")
        ttk.Label(outer, textvariable=self.status).pack(anchor="w", pady=(4, 8))
        self.table = ttk.Treeview(outer, columns=("provider", "status", "details"), show="headings")
        for key, label, width in (("provider", "Provider / Check", 190),
                                  ("status", "Status", 110), ("details", "Details", 520)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, stretch=True)
        self.table.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(self.table, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.table.tag_configure("ok", foreground="#187038")
        self.table.tag_configure("warn", foreground="#9b6200")
        self.table.tag_configure("fail", foreground="#b52222")
        self.after(100, self.poll)

    def browse(self):
        path = filedialog.askdirectory(initialdir=self.project.get())
        if path:
            self.project.set(path)

    def start(self, label, task):
        if self.busy:
            return
        root = Path(self.project.get()).expanduser().resolve()
        if not root.is_dir():
            messagebox.showerror("Invalid project", "Select an existing project folder.")
            return
        self.busy = True
        self.status.set(label + "…")
        self.table.delete(*self.table.get_children())
        self.discover_button.configure(state="disabled")
        self.doctor_button.configure(state="disabled")
        def worker():
            try:
                self.events.put(("result", task(root)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def discover(self):
        def task(root):
            # Reuse doctor's safe, local --version probes to discover installed
            # providers. This is NOT remote session enumeration or authentication.
            import os
            from . import winproc
            from .workers.versions import read_cli_version
            rows = []
            for command, display, install, signin in doctor.PROVIDERS:
                checks = doctor.check_provider(command, display, install, signin,
                                               os.environ, winproc.resolve_command,
                                               read_cli_version)
                rows.append((display, checks[0].status, checks[0].detail))
                if len(checks) > 1:
                    rows.append((display + " version", checks[1].status, checks[1].detail))
            return rows
        self.start("Discovering installed AI providers", task)

    def run_doctor(self):
        def task(root):
            checks = doctor.run_doctor(root, resolve_state_root(root))
            return [(c.name, c.status, c.detail + (" | Fix: " + c.fix if c.fix and c.status != "ok" else ""))
                    for c in checks]
        self.start("Checking environment", task)

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "error":
                    self.status.set("Operation failed")
                    messagebox.showerror("Multi-Agent-AI", payload)
                else:
                    for name, status, detail in payload:
                        self.table.insert("", "end", values=(name, status, detail),
                                          tags=(status,))
                    self.status.set(f"Completed: {len(payload)} checks")
                self.busy = False
                self.discover_button.configure(state="normal")
                self.doctor_button.configure(state="normal")
        except queue.Empty:
            pass
        self.after(100, self.poll)


def main():
    ControlPanel().mainloop()


if __name__ == "__main__":
    main()
