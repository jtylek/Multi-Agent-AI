"""User-initiated provider login in an interactive terminal.

The provider CLI owns browser OAuth/device-code authentication. This module
never receives passwords, tokens, or browser callbacks.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

LOGIN_COMMANDS = {
    "codex": ("login",),
    "copilot": ("login",),
    "claude": (),
}


def login_command(provider: str) -> list[str]:
    if provider not in LOGIN_COMMANDS:
        raise ValueError("Unsupported provider")
    executable = shutil.which(provider + ".cmd") if sys.platform == "win32" else None
    executable = executable or shutil.which(provider)
    if not executable:
        raise FileNotFoundError(f"{provider} CLI not installed or not on PATH")
    return [executable, *LOGIN_COMMANDS[provider]]


def launch_login(provider: str) -> str:
    """Open a dedicated interactive terminal; no shell interpolation of input."""
    command = login_command(provider)
    if sys.platform == "win32":
        # Windows Terminal is optional. cmd /k keeps interactive login visible.
        subprocess.Popen(["cmd.exe", "/d", "/k", *command],
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    elif sys.platform.startswith("linux"):
        terminals = (
            ("x-terminal-emulator", ["-e"]),
            ("gnome-terminal", ["--"]),
            ("konsole", ["-e"]),
            ("xfce4-terminal", ["-x"]),
            ("xterm", ["-e"]),
        )
        for name, args in terminals:
            terminal = shutil.which(name)
            if terminal:
                subprocess.Popen([terminal, *args, *command], env=os.environ.copy())
                break
        else:
            raise RuntimeError("No graphical terminal found; run " + provider + " login manually")
    else:
        raise RuntimeError("Unsupported operating system")
    return "Opened interactive login terminal. Complete provider sign-in there, then run Doctor."

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class ConnectionStatus:
    provider: str
    status: str
    detail: str


def check_connection(provider: str, timeout: float = 8.0) -> ConnectionStatus:
    """Read-only authentication probe; never initiates a login or stores secrets.

    Unknown is intentionally distinct from disconnected: provider CLIs may
    change their auth-status commands, or be temporarily unavailable.
    """
    if provider not in LOGIN_COMMANDS:
        raise ValueError("Unsupported provider")
    try:
        command = login_command(provider)
    except FileNotFoundError:
        return ConnectionStatus(provider, "not installed", "Install the provider CLI first")
    executable = command[0]
    probes = {
        "codex": [executable, "login", "status"],
        "claude": [executable, "auth", "status", "--json"],
    }
    if provider not in probes:
        return ConnectionStatus(provider, "unknown", "No reliable read-only auth probe configured; check sign-in in the provider CLI")
    try:
        result = subprocess.run(
            probes[provider], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ConnectionStatus(provider, "unknown", "Authentication status check unavailable or timed out")
    output = (result.stdout + "\n" + result.stderr).lower()
    if provider == "codex":
        if "not logged in" in output or "not authenticated" in output:
            return ConnectionStatus(provider, "not connected", "Codex reports no active login")
        if result.returncode == 0 and ("logged in" in output or "authenticated" in output):
            return ConnectionStatus(provider, "connected", "Codex reports an active login")
    if provider == "claude":
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("loggedIn"), bool):
            return ConnectionStatus(provider, "connected" if data["loggedIn"] else "not connected",
                                    "Claude CLI authentication status")
    return ConnectionStatus(provider, "unknown", "Provider did not return a recognized authentication status")
