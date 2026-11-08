"""Non-modal Project AI chat with one output pane per detected provider.

The window does not grab input, so the main panel and the Live Activity window
stay usable beside it.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext, ttk

from .chat_log import MAX_SUMMARY, MAX_TEXT, ChatLog, clip, new_session_id
from .gui_activity import ActivityWindow
from .gui_chat import HELLO_WORLD_REQUEST, ask_provider, detected_chat_providers
from .state import resolve_state_root
from .team_demo import TEAM_TIMEOUT_SECONDS, run_team_task

PROVIDER_LABELS = {"codex": "Codex", "claude": "Claude", "copilot": "Copilot"}
TESTS = {
    "Hello World! (all agents)": "hello",
    "Team task: Python calculator (coordinator dispatches)": "team",
}


class ProjectChatWindow(tk.Toplevel):
    def __init__(self, parent: tk.Misc, project_root: Path, coordinator: tk.StringVar):
        super().__init__(parent)
        self.title("Project AI Chat")
        self.geometry("1200x800")
        self.minsize(850, 600)
        self.project_root = project_root
        self.activity_window: ActivityWindow | None = None
        self.coordinator = coordinator
        self.providers = detected_chat_providers()
        self.history: dict[str, list[tuple[str, str]]] = {name: [] for name in self.providers}
        self.transcripts: dict[str, list[tuple[str, str]]] = {name: [] for name in self.providers}
        self.outputs: dict[str, scrolledtext.ScrolledText] = {}
        self.events: queue.Queue[tuple[str, str, str, str]] = queue.Queue()
        self.pending: set[str] = set()
        self.team_running = False
        self.ui_calls: queue.Queue = queue.Queue()
        """Callables queued by background threads, run on the Tk thread by poll()."""
        self.cancel = threading.Event()

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=2)
        outer.rowconfigure(7, weight=1)
        ttk.Label(outer, text=f"Project: {project_root}").grid(row=0, column=0, sticky="w")
        controls = ttk.Frame(outer)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 10))
        ttk.Label(controls, text="Coordinator:").pack(side="left")
        choices = [PROVIDER_LABELS[name] for name in self.providers]
        current = self.coordinator.get()
        if current not in self.providers and self.providers:
            self.coordinator.set(self.providers[0])
        self.coordinator_choice = ttk.Combobox(controls, values=choices, state="readonly", width=14)
        self.coordinator_choice.pack(side="left", padx=(8, 16))
        if self.providers:
            self.coordinator_choice.set(PROVIDER_LABELS[self.coordinator.get()])
        self.coordinator_choice.bind("<<ComboboxSelected>>", self._select_coordinator)
        ttk.Label(controls, text="Calls use provider quota and read-only project access.").pack(side="left")
        ttk.Button(controls, text="Live Activity", command=self.open_activity).pack(side="right")
        self.recording = tk.StringVar(master=self)
        ttk.Label(controls, textvariable=self.recording).pack(side="right", padx=(0, 8))

        conversation = ttk.LabelFrame(outer, text="Coordinator conversation", padding=5)
        conversation.grid(row=2, column=0, sticky="nsew")
        self.conversation_frame = conversation
        self.coordinator_output = scrolledtext.ScrolledText(conversation, wrap="word", state="disabled")
        self.coordinator_output.pack(fill="both", expand=True)

        ttk.Label(outer, text="Message:").grid(row=3, column=0, sticky="w", pady=(10, 2))
        self.input = tk.Text(outer, height=3, wrap="word")
        self.input.grid(row=4, column=0, sticky="ew")
        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, sticky="ew", pady=(8, 10))
        self.coordinator_button = ttk.Button(actions, text="Send to Coordinator",
                                             command=self.send_to_coordinator)
        self.coordinator_button.pack(side="left")
        self.all_button = ttk.Button(actions, text="Send to All", command=self.send_to_all)
        self.all_button.pack(side="left", padx=(8, 0))
        ttk.Separator(actions, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Label(actions, text="Test:").pack(side="left")
        self.test_choice = ttk.Combobox(actions, values=list(TESTS), state="readonly", width=46)
        self.test_choice.set(next(iter(TESTS)))
        self.test_choice.pack(side="left", padx=(6, 0))
        self.test_button = ttk.Button(actions, text="Run Test", command=self.run_selected_test)
        self.test_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel_requests,
                                        state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))

        ttk.Label(outer, text="AI agent outputs:").grid(row=6, column=0, sticky="w")
        panes = ttk.PanedWindow(outer, orient="horizontal")
        panes.grid(row=7, column=0, sticky="nsew", pady=(4, 0))
        self.agent_panes = panes
        for provider in self.providers:
            panel = ttk.LabelFrame(panes, text=f"{PROVIDER_LABELS[provider]} output", padding=5)
            panes.add(panel, weight=1)
            output = scrolledtext.ScrolledText(panel, wrap="word", state="disabled")
            output.pack(fill="both", expand=True)
            self.outputs[provider] = output

        self.status = tk.StringVar(value=(f"Detected {len(self.providers)} agent(s)."
                                          if self.providers else "No supported agent CLI detected."))
        ttk.Label(outer, textvariable=self.status).grid(row=8, column=0, sticky="w", pady=(8, 0))
        if not self.providers:
            self._set_sending(False)
        self.log = self._open_log()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = self.after(100, self.poll)

    def _open_log(self) -> ChatLog | None:
        """Record this session for Live Activity, but only in an initialized project: the chat
        never creates a state root on its own (that stays the explicit Initialize Project action)."""
        root = resolve_state_root(self.project_root)
        if not root.config_path.is_file():
            self.recording.set("Activity not recorded: project not initialized (use Initialize Project)")
            return None
        log = ChatLog(root.path, new_session_id())
        self.recording.set(f"Activity recorded: {log.session_id}")
        self._record("session_started", f"project {self.project_root}",
                     log=log, project=str(self.project_root), coordinator=self.coordinator.get(),
                     providers=list(self.providers))
        return log

    def _record(self, kind: str, summary: str, *, log: ChatLog | None = None, **fields):
        log = log or getattr(self, "log", None)
        if log is None:
            return
        log.record(kind, summary=clip(summary, MAX_SUMMARY, one_line=True), **fields)
        if log.error:
            self.recording.set("Activity not recorded: write failed (see status)")
            self.status.set(log.error)

    def _provider_event(self, kind: str, fields: dict):
        """Called from provider threads; ChatLog is thread-safe and Tk is not touched here."""
        if self.log is not None:
            self.log.record(kind, **fields)

    def open_activity(self):
        """Open (or raise) the Live Activity window for this chat's project."""
        if self.activity_window is not None and self.activity_window.winfo_exists():
            self.activity_window.show()
            return
        state_root = resolve_state_root(self.project_root).path
        self.activity_window = ActivityWindow(self, state_root)

    def show(self):
        """Bring an already-open chat window to the front."""
        self.deiconify()
        self.lift()
        self.focus_set()

    def _select_coordinator(self, event=None):
        label = self.coordinator_choice.get()
        for provider, display in PROVIDER_LABELS.items():
            if display == label:
                self.coordinator.set(provider)
                self._render_coordinator()
                break

    def _render_coordinator(self):
        output = self.coordinator_output
        output.configure(state="normal")
        output.delete("1.0", "end")
        for speaker, text in self.transcripts.get(self.coordinator.get(), ()):
            output.insert("end", f"{speaker}: {text}\n\n")
        output.see("end")
        output.configure(state="disabled")

    def _append(self, provider: str, speaker: str, text: str):
        self.transcripts[provider].append((speaker, text))
        output = self.outputs[provider]
        output.configure(state="normal")
        output.insert("end", f"{speaker}: {text}\n\n")
        output.see("end")
        output.configure(state="disabled")
        if provider == self.coordinator.get():
            self._render_coordinator()

    def _set_sending(self, sending: bool):
        state = "disabled" if sending or not self.providers else "normal"
        for button in (self.coordinator_button, self.all_button, self.test_button):
            button.configure(state=state)
        self.coordinator_choice.configure(state="disabled" if sending else "readonly")
        self.test_choice.configure(state="disabled" if sending else "readonly")
        self.cancel_button.configure(state="normal" if sending else "disabled")

    def _send(self, providers: tuple[str, ...], message: str):
        if self.pending or self.team_running or not providers:
            return
        message = message.strip()
        if not message:
            self.status.set("Enter a message first.")
            return
        self.cancel = threading.Event()
        self.pending = set(providers)
        self._set_sending(True)
        self.status.set(f"Waiting for {', '.join(PROVIDER_LABELS[name] for name in providers)}…")
        selected = self.coordinator.get()
        self._record("message_sent", f"to {', '.join(PROVIDER_LABELS[name] for name in providers)}: {message}",
                     recipients=list(providers), coordinator=selected, text=clip(message, MAX_TEXT))
        for provider in providers:
            self._append(provider, "You", message)
            history = tuple(self.history[provider])

            def worker(name=provider, previous=history):
                try:
                    answer = ask_provider(name, self.project_root, selected, previous, message,
                                          cancel=self.cancel, on_event=self._provider_event)
                    self.events.put((name, message, answer, ""))
                except Exception as exc:
                    self.events.put((name, message, "", str(exc)))

            threading.Thread(target=worker, daemon=True).start()

    def send_to_coordinator(self):
        if self.coordinator.get() in self.providers:
            message = self.input.get("1.0", "end-1c")
            self.input.delete("1.0", "end")
            self._send((self.coordinator.get(),), message)

    def send_to_all(self):
        message = self.input.get("1.0", "end-1c")
        self.input.delete("1.0", "end")
        self._send(self.providers, message)

    def test_all_agents(self):
        self._send(self.providers, HELLO_WORLD_REQUEST)

    def run_selected_test(self):
        if TESTS.get(self.test_choice.get()) == "team":
            self.run_team_task()
        else:
            self.test_all_agents()

    def run_team_task(self):
        """Coordinator plans, dispatches subtasks to the other agents in parallel and integrates.
        Runs on a background thread; every step is in the chat log, so Live Activity shows it."""
        if self.pending or self.team_running or not self.providers:
            return
        coordinator = self.coordinator.get()
        workers = tuple(name for name in self.providers if name != coordinator)
        self.cancel = threading.Event()
        cancel = self.cancel
        self.team_running = True
        self._set_sending(True)
        self.status.set("Team task running: coordinator is planning… (open Live Activity to follow it)")
        output_dir = self.log.path.parent / "team-task" if self.log is not None else None

        def ask(provider, message):
            return ask_provider(provider, self.project_root, coordinator, (), message, cancel=cancel,
                                on_event=self._provider_event, timeout=TEAM_TIMEOUT_SECONDS)

        def show(provider, speaker, text):
            self.ui_calls.put(lambda: self._append(provider, speaker, text))

        def record(kind, summary, **fields):
            self._provider_event(kind, {"summary": clip(summary, MAX_SUMMARY, one_line=True), **fields})

        def worker():
            try:
                result = run_team_task(coordinator, workers, ask=ask, record=record, show=show,
                                       cancel=cancel, output_dir=output_dir)
                message = {
                    "saved": f"Team task done: {result.path} compiles (not run). "
                             f"Try it: python \"{result.path}\" --self-test",
                    "saved_not_compiling": f"Team task saved {result.path}, but it does not compile: {result.detail}",
                }.get(result.outcome, f"Team task ended: {result.outcome} ({result.detail})")
            except Exception as exc:  # a bug here must not leave the window stuck in "sending"
                record("team_task_finished", f"orchestrator error: {exc}", outcome="error")
                message = f"Team task failed: {exc}"
            self.ui_calls.put(lambda: self._team_finished(message))

        threading.Thread(target=worker, daemon=True).start()

    def _team_finished(self, message: str):
        self.team_running = False
        self._set_sending(False)
        self.status.set(message)
        self._append(self.coordinator.get(), "Orchestrator", message)

    def cancel_requests(self):
        self.cancel.set()
        waiting = sorted(self.pending) + (["team task"] if self.team_running else [])
        self._record("cancel_requested", f"waiting on {', '.join(waiting) or 'nothing'}", pending=waiting)
        self.status.set("Cancelling provider requests…")

    def poll(self):
        try:
            while True:
                provider, message, answer, error = self.events.get_nowait()
                if provider not in self.pending:
                    continue
                self.pending.remove(provider)
                if error:
                    self._append(provider, "Error", error)
                    self._record("chat_error", f"{PROVIDER_LABELS[provider]}: {error}",
                                 provider=provider, error=error)
                else:
                    self._append(provider, PROVIDER_LABELS[provider], answer)
                    self._record("reply_received", f"{PROVIDER_LABELS[provider]}: {answer}",
                                 provider=provider, text=clip(answer, MAX_TEXT))
                    self.history[provider].extend((("User", message), ("Assistant", answer)))
                if not self.pending:
                    self._set_sending(False)
                    self.status.set("Ready.")
        except queue.Empty:
            pass
        try:
            while True:
                self.ui_calls.get_nowait()()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._poll_id = self.after(100, self.poll)

    def close(self):
        self.cancel.set()
        self._record("session_closed", "chat window closed", pending=sorted(self.pending))
        self.after_cancel(self._poll_id)
        self.destroy()
