"""Windows-friendly Tkinter control panel for local provider discovery and doctor."""
from __future__ import annotations

import queue
import shutil
import webbrowser
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import doctor, provider_auth
from .agent_catalog import CATALOG
from .gui_projects import ProjectLibrary, load_projects, remember_project
from .state import init_state_root, resolve_state_root

PROJECT_URL = "https://github.com/jtylek/Multi-Agent-AI"
ABOUT_TEXT = (
    "Multi-Agent-AI\n"
    "Free software under the MIT License.\n"
    "Copyright (c) 2026 by Janusz Tylek"
)


def open_project_repository() -> None:
    webbrowser.open(PROJECT_URL)


def initialize_project_state(project_root: Path) -> list[tuple[str, str, str, str]]:
    """Use the same state initialization as ``multi-agent-ai init``."""
    root = init_state_root(project_root)
    return [
        ("State root", "ok", "", str(root.path)),
        ("State index", "ok", "", str(root.database_path)),
    ]


class ControlPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Multi-Agent-AI Control Panel")
        self.geometry("960x620")
        self.minsize(820, 470)
        try:
            self.projects = load_projects()
            settings_error = None
        except ValueError as exc:
            self.projects = ProjectLibrary(None, (), {})
            settings_error = str(exc)
        current = self.projects.current
        initial = current if current and Path(current).is_dir() else str(Path.cwd())
        self.project = tk.StringVar(value=initial)
        self.coordinator = tk.StringVar(value=self.projects.coordinator_for(initial))
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Multi-Agent-AI", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Button(header, text="About", command=self.show_about).pack(side="right")
        ttk.Label(outer, text="Local AI provider discovery and environment diagnostics").pack(anchor="w", pady=(0, 10))
        self.chat_window = None

        # Project group (left) and the primary Project AI Chat action (right).
        top = ttk.Frame(outer)
        top.pack(fill="x")
        project_group = ttk.LabelFrame(top, text="Project", padding=8)
        project_group.pack(side="left", fill="x", expand=True)
        project_row = ttk.Frame(project_group)
        project_row.pack(fill="x")
        ttk.Label(project_row, text="Folder:").pack(side="left")
        ttk.Entry(project_row, textvariable=self.project).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(project_row, text="Browse…", command=self.browse).pack(side="left")
        project_actions = ttk.Frame(project_group)
        project_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(project_actions, text="Open Project", command=self.open_project).pack(side="left")
        self.recent_button = ttk.Menubutton(project_actions, text="Open Recent")
        self.recent_button.pack(side="left", padx=(8, 0))
        self.recent_menu = tk.Menu(self.recent_button, tearoff=False)
        self.recent_button.configure(menu=self.recent_menu)
        self.refresh_recent()
        ttk.Button(project_actions, text="Save Project", command=self.save_project).pack(side="left", padx=(8, 0))
        self.init_button = ttk.Button(project_actions, text="Initialize Project", command=self.initialize_project)
        self.init_button.pack(side="left", padx=(8, 0))
        # A classic tk.Button: ttk's native Windows theme ignores background colours.
        self.chat_button = tk.Button(top, text="Project AI Chat", command=self.open_project_chat,
                                     font=("Segoe UI", 13, "bold"), bg="#0b5cad", fg="white",
                                     activebackground="#084a8c", activeforeground="white",
                                     disabledforeground="#a9c3df", relief="flat", cursor="hand2",
                                     padx=22, pady=10)
        self.chat_button.pack(side="left", fill="y", padx=(12, 0), pady=(7, 0))

        # Agent management and diagnostics groups.
        tools = ttk.Frame(outer)
        tools.pack(fill="x", pady=(10, 4))
        agents_group = ttk.LabelFrame(tools, text="AI agents", padding=8)
        agents_group.pack(side="left", fill="x", expand=True)
        self.discover_button = ttk.Button(agents_group, text="Discover AI Agents", command=self.discover)
        self.discover_button.pack(side="left")
        self.connect_button = ttk.Button(agents_group, text="Connect Selected Agent", command=self.connect)
        self.connect_button.pack(side="left", padx=(8, 0))
        self.refresh_button = ttk.Button(agents_group, text="Refresh Connection Status", command=self.refresh_connections)
        self.refresh_button.pack(side="left", padx=(8, 0))
        self.catalog_button = ttk.Button(agents_group, text="Free / Open Agents", command=self.show_catalog)
        self.catalog_button.pack(side="left", padx=(8, 0))
        diagnostics_group = ttk.LabelFrame(tools, text="Diagnostics", padding=8)
        diagnostics_group.pack(side="left", padx=(12, 0))
        self.doctor_button = ttk.Button(diagnostics_group, text="Run Doctor", command=self.run_doctor)
        self.doctor_button.pack(side="left")
        ttk.Label(outer, text="Select a discovered provider row, then Connect. Sign-in is handled by the provider in a separate terminal/browser.").pack(anchor="w")
        self.status = tk.StringVar(value=settings_error or "Ready. Select your project folder.")
        ttk.Label(outer, textvariable=self.status).pack(anchor="w", pady=(4, 8))
        self.table = ttk.Treeview(outer, columns=("provider", "status", "connection", "details"), show="headings")
        for key, label, width in (("provider", "Provider / Check", 190),
                                  ("status", "Status", 95), ("connection", "Connection", 125), ("details", "Details", 470)):
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
            self.coordinator.set(self.projects.coordinator_for(path))

    def refresh_recent(self):
        self.recent_menu.delete(0, "end")
        available = [path for path in self.projects.recent if Path(path).is_dir()]
        if not available:
            self.recent_menu.add_command(label="No recent projects", state="disabled")
        for path in available:
            self.recent_menu.add_command(label=path,
                                         command=lambda selected=path: self.open_project_path(selected))

    def open_project_path(self, folder):
        try:
            self.projects = remember_project(folder)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Open Project", str(exc))
            return
        self.project.set(self.projects.current)
        self.coordinator.set(self.projects.coordinator_for(self.projects.current))
        self.refresh_recent()
        self.status.set(f"Opened project: {self.projects.current}")

    def open_project(self):
        path = filedialog.askdirectory(initialdir=self.project.get())
        if path:
            self.open_project_path(path)

    def save_project(self):
        try:
            self.projects = remember_project(self.project.get(), self.coordinator.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Save Project", str(exc))
            return
        self.project.set(self.projects.current)
        self.refresh_recent()
        self.status.set(f"Saved project: {self.projects.current}")

    def open_project_chat(self):
        root = Path(self.project.get()).expanduser().resolve()
        if not root.is_dir():
            messagebox.showerror("Project AI Chat", "Select an existing project folder.")
            return
        from .gui_chat_window import ProjectChatWindow

        window = self.chat_window
        if window is not None and window.winfo_exists() and window.project_root == root:
            window.show()
            return
        self.chat_window = ProjectChatWindow(self, root, self.coordinator)

    def show_about(self):
        window = tk.Toplevel(self)
        window.title("About Multi-Agent-AI")
        window.resizable(False, False)
        window.transient(self)
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=ABOUT_TEXT, justify="left").pack(anchor="w")
        ttk.Label(frame, text=PROJECT_URL).pack(anchor="w", pady=(12, 4))
        ttk.Button(frame, text="Open GitHub repository", command=open_project_repository).pack(anchor="w")
        ttk.Button(frame, text="Close", command=window.destroy).pack(anchor="e", pady=(16, 0))
        # Center over the main window (clamped to the screen).
        window.withdraw()
        window.update_idletasks()
        w, h = window.winfo_reqwidth(), window.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 2
        x = max(0, min(x, self.winfo_screenwidth() - w))
        y = max(0, min(y, self.winfo_screenheight() - h))
        window.geometry(f"+{x}+{y}")
        window.deiconify()
        window.grab_set()

    def start(self, label, task, *, completion=None):
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
        self.refresh_button.configure(state="disabled")
        self.init_button.configure(state="disabled")
        self.chat_button.configure(state="disabled")
        def worker():
            try:
                self.events.put(("result", (task(root), completion)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def initialize_project(self):
        self.start("Initializing project", initialize_project_state,
                   completion="Project initialized. State root and SQLite index are ready.")

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
                connection = provider_auth.check_connection(command)
                rows.append((display, checks[0].status, connection.status, checks[0].detail + " | " + connection.detail))
                if len(checks) > 1:
                    rows.append((display + " version", checks[1].status, "", checks[1].detail))
            return rows
        self.start("Discovering installed AI providers", task)

    def show_catalog(self):
        window = tk.Toplevel(self)
        window.title("Free-access and Open-source AI Agents")
        window.geometry("920x520")
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Catalog: free plans, open-source agents and local options", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Free software does not guarantee free model usage. Gemini, Qwen, OpenCode, Aider and Goose currently have review-only adapters.").pack(anchor="w", pady=(4, 10))
        columns = ("name", "installed", "access", "integration")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for key, width in (("name", 165), ("installed", 100), ("access", 440), ("integration", 135)):
            tree.heading(key, text=key.title())
            tree.column(key, width=width)
        for entry in CATALOG:
            found = bool(entry.cli and (shutil.which(entry.cli) or shutil.which(entry.cli + ".cmd")))
            tree.insert("", "end", iid=entry.key, values=(entry.name, "Installed" if found else "Not detected", entry.access, ("Review only" if entry.key in {"gemini", "qwen", "opencode", "aider", "goose"} else "Supported") if entry.adapter else "Catalog only"))
        tree.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=8)
        def open_selected():
            selected = tree.selection()
            if selected:
                from .agent_catalog import get_entry
                webbrowser.open(get_entry(selected[0]).url)
        def connect_selected():
            selected = tree.selection()
            if not selected:
                return
            key = selected[0]
            if key not in provider_auth.LOGIN_COMMANDS:
                messagebox.showinfo("Not integrated", "This agent is catalog-only. Open its website for installation instructions; no orchestrator adapter or login workflow exists yet.", parent=window)
                return
            try:
                messagebox.showinfo("Provider login", provider_auth.launch_login(key), parent=window)
            except (OSError, RuntimeError, ValueError) as exc:
                messagebox.showerror("Provider login", str(exc), parent=window)
        ttk.Button(buttons, text="Open Provider Website", command=open_selected).pack(side="left")
        ttk.Button(buttons, text="Connect Supported Provider", command=connect_selected).pack(side="left", padx=8)

    def connect(self):
        selected = self.table.selection()
        if not selected:
            messagebox.showinfo("Connect agent", "Select a provider in the results table first.")
            return
        name = str(self.table.item(selected[0], "values")[0]).lower()
        provider = next((key for key, display, _, _ in doctor.PROVIDERS
                         if name == display.lower() or name == (display + " version").lower()), None)
        if provider is None:
            messagebox.showinfo("Connect agent", "Select a discovered Claude, Codex or Copilot provider row.")
            return
        try:
            message = provider_auth.launch_login(provider)
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Connect agent", str(exc))
        else:
            messagebox.showinfo("Connect agent", message)

    def refresh_connections(self):
        self.discover()

    def run_doctor(self):
        def task(root):
            checks = doctor.run_doctor(root, resolve_state_root(root))
            return [(c.name, c.status, "", c.detail + (" | Fix: " + c.fix if c.fix and c.status != "ok" else ""))
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
                    rows, completion = payload
                    for name, status, connection, detail in rows:
                        self.table.insert("", "end", values=(name, status, connection, detail),
                                          tags=(status,))
                    self.status.set(completion or f"Completed: {len(rows)} checks")
                self.busy = False
                self.discover_button.configure(state="normal")
                self.doctor_button.configure(state="normal")
                self.refresh_button.configure(state="normal")
                self.init_button.configure(state="normal")
                self.chat_button.configure(state="normal")
        except queue.Empty:
            pass
        self.after(100, self.poll)


def main():
    ControlPanel().mainloop()


if __name__ == "__main__":
    main()
