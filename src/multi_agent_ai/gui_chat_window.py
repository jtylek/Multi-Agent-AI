"""Modal Project AI chat with one output pane per detected provider."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext, ttk

from .gui_chat import HELLO_WORLD_REQUEST, ask_provider, detected_chat_providers

PROVIDER_LABELS = {"codex": "Codex", "claude": "Claude", "copilot": "Copilot"}


class ProjectChatWindow(tk.Toplevel):
    def __init__(self, parent: tk.Misc, project_root: Path, coordinator: tk.StringVar):
        super().__init__(parent)
        self.title("Project AI chat")
        self.geometry("1200x680")
        self.minsize(850, 500)
        self.transient(parent)
        self.project_root = project_root
        self.coordinator = coordinator
        self.providers = detected_chat_providers()
        self.history: dict[str, list[tuple[str, str]]] = {name: [] for name in self.providers}
        self.outputs: dict[str, scrolledtext.ScrolledText] = {}
        self.events: queue.Queue[tuple[str, str, str, str]] = queue.Queue()
        self.pending: set[str] = set()
        self.cancel = threading.Event()

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"Project: {project_root}").pack(anchor="w")
        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(8, 10))
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

        panes = ttk.PanedWindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)
        for provider in self.providers:
            panel = ttk.LabelFrame(panes, text=f"{PROVIDER_LABELS[provider]} output", padding=5)
            panes.add(panel, weight=1)
            output = scrolledtext.ScrolledText(panel, wrap="word", state="disabled")
            output.pack(fill="both", expand=True)
            self.outputs[provider] = output

        ttk.Label(outer, text="Message:").pack(anchor="w", pady=(10, 2))
        self.input = tk.Text(outer, height=3, wrap="word")
        self.input.pack(fill="x")
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(8, 0))
        self.coordinator_button = ttk.Button(actions, text="Send to Coordinator",
                                             command=self.send_to_coordinator)
        self.coordinator_button.pack(side="left")
        self.all_button = ttk.Button(actions, text="Send to All", command=self.send_to_all)
        self.all_button.pack(side="left", padx=(8, 0))
        self.test_button = ttk.Button(actions, text="Test All Agents: Hello World!",
                                      command=self.test_all_agents)
        self.test_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel_requests,
                                        state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value=(f"Detected {len(self.providers)} agent(s)."
                                          if self.providers else "No supported agent CLI detected."))
        ttk.Label(outer, textvariable=self.status).pack(anchor="w", pady=(8, 0))
        if not self.providers:
            self._set_sending(False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.grab_set()
        self.after(100, self.poll)

    def _select_coordinator(self, event=None):
        label = self.coordinator_choice.get()
        for provider, display in PROVIDER_LABELS.items():
            if display == label:
                self.coordinator.set(provider)
                break

    def _append(self, provider: str, speaker: str, text: str):
        output = self.outputs[provider]
        output.configure(state="normal")
        output.insert("end", f"{speaker}: {text}\n\n")
        output.see("end")
        output.configure(state="disabled")

    def _set_sending(self, sending: bool):
        state = "disabled" if sending or not self.providers else "normal"
        for button in (self.coordinator_button, self.all_button, self.test_button):
            button.configure(state=state)
        self.coordinator_choice.configure(state="disabled" if sending else "readonly")
        self.cancel_button.configure(state="normal" if sending else "disabled")

    def _send(self, providers: tuple[str, ...], message: str):
        if self.pending or not providers:
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
        for provider in providers:
            self._append(provider, "You", message)
            history = tuple(self.history[provider])

            def worker(name=provider, previous=history):
                try:
                    answer = ask_provider(name, self.project_root, selected, previous, message,
                                          cancel=self.cancel)
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

    def cancel_requests(self):
        self.cancel.set()
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
                else:
                    self._append(provider, PROVIDER_LABELS[provider], answer)
                    self.history[provider].extend((("User", message), ("Assistant", answer)))
                if not self.pending:
                    self._set_sending(False)
                    self.status.set("Ready.")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self.poll)

    def close(self):
        self.cancel.set()
        self.destroy()
