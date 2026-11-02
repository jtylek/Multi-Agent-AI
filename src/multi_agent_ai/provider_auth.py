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
