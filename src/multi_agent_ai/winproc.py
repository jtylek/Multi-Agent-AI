"""Native Windows process launching for provider CLIs (no shell, no PowerShell).

Decisions (documented in docs/protocol/agent-messaging.md, "Managed workers"):

* **Resolution.** A command name is looked up on ``PATH`` directory by directory; inside a
  directory ``.exe`` wins over ``.com``, then ``.cmd``, then ``.bat``. ``.ps1`` files and
  extensionless shell shims are never used.
* **npm shims.** A ``.cmd`` written by npm (``"%_prog%" "%dp0%\\...\\x.js" %*``) is resolved
  to ``node.exe <script>``: ``node.exe`` next to the shim, else ``node.exe`` on ``PATH``. The
  arguments then reach node through ``CreateProcess`` with standard MSVCRT quoting
  (``subprocess.list2cmdline``) and never pass through ``cmd.exe``, so quotes, ``%``, ``^``,
  ``&``, newlines and non-ASCII text arrive unchanged.
* **Other batch files** run as ``cmd.exe /d /v:off /s /c ""<file>" "arg" ..."``. ``cmd.exe``
  cannot pass every string safely (``%`` expands, ``"`` and newlines end arguments), so an
  argument containing ``"``, ``%``, CR, LF or NUL is refused with :class:`UnsafeArgument`
  instead of being mangled.
* **Process tree.** The child is assigned to a Windows Job Object created with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``; on timeout the job is terminated, which kills every
  descendant even after an intermediate process exited. When closing the job after a normal
  exit, descendants that are still running are killed too: a worker never outlives its run. If
  a job cannot be created or assigned, ``taskkill /T /F /PID`` is the fallback.
* **I/O.** stdin is ``DEVNULL`` or a file opened for reading (EOF at its end); stdout and
  stderr are written straight to files, never buffered in memory. ``shell=True`` is never used.
* **Environment.** Passed explicitly: a small allowlist (:data:`BASE_ENV_NAMES`) plus names a
  provider adapter needs; everything else (API keys, tokens) is dropped unless named by the
  caller. :data:`NEVER_PASS_ENV` is removed even when named.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

IS_WINDOWS = sys.platform == "win32"
EXTENSION_PREFERENCE = (".exe", ".com", ".cmd", ".bat")
BATCH_EXTENSIONS = (".cmd", ".bat")
MAX_COMMAND_LINE = 32_000
"""CreateProcess allows 32,767 characters; stay below with a margin."""

BASE_ENV_NAMES = (
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "ALLUSERSPROFILE", "PUBLIC", "USERNAME",
    "USERDOMAIN", "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER", "OS", "LANG", "TZ",
    # network configuration (not credentials by design; a proxy URL with embedded credentials
    # is passed on, see docs)
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)
NEVER_PASS_ENV = ("COPILOT_ALLOW_ALL",)
"""Environment variables that grant blanket tool permission; stripped unconditionally."""


class LaunchError(OSError):
    """The command cannot be resolved or started."""


class UnsafeArgument(ValueError):
    """An argument cannot be passed through ``cmd.exe`` without being altered."""


@dataclass(frozen=True)
class ResolvedCommand:
    """A command name resolved to something ``CreateProcess`` can run without a shell."""

    name: str
    path: Path
    """The file found (the ``.exe``, or the ``.cmd``/``.bat`` shim)."""
    kind: str
    """``exe``, ``npm-shim`` (run as node + script) or ``batch`` (run through cmd.exe)."""
    prefix: tuple[str, ...]
    """argv prefix for ``exe`` and ``npm-shim``; for ``batch`` the cmd.exe path."""

    def command_line(self, args: Sequence[str]) -> str | list[str]:
        """The value handed to ``subprocess.Popen``: an argv list, or for batch files the
        exact command-line string. Raises :class:`UnsafeArgument` for unsafe batch args."""
        if self.kind != "batch":
            return [*self.prefix, *args]
        for arg in (str(self.path), *args):
            if any(c in arg for c in '"%\r\n\0'):
                raise UnsafeArgument(f"argument {arg!r} cannot be passed safely through cmd.exe")
        inner = " ".join(f'"{a}"' for a in (str(self.path), *args))
        return f'"{self.prefix[0]}" /d /v:off /s /c "{inner}"'

    def command_line_length(self, args: Sequence[str]) -> int:
        line = self.command_line(args)
        return len(line) if isinstance(line, str) else len(subprocess.list2cmdline(line))


_NPM_SHIM = re.compile(r'"%_prog%"\s+"%dp0%\\(?P<script>[^"\r\n]+?\.(?:js|cjs|mjs))"\s+%\*')


def _path_dirs(path_value: str | None) -> list[Path]:
    return [Path(p.strip('"')) for p in (path_value or "").split(os.pathsep) if p.strip()]


def _find(name: str, dirs: Iterable[Path], extensions: Sequence[str]) -> Path | None:
    for directory in dirs:
        for ext in extensions:
            candidate = directory / f"{name}{ext}"
            if candidate.is_file():
                return candidate
    return None


def cmd_exe_path(env: Mapping[str, str] | None = None) -> str:
    """``%SystemRoot%\\System32\\cmd.exe``; ``COMSPEC`` is deliberately not trusted."""
    env = env if env is not None else os.environ
    root = next((v for k, v in env.items() if k.upper() == "SYSTEMROOT"), None) or r"C:\Windows"
    return str(Path(root) / "System32" / "cmd.exe")


def npm_shim_script(shim: Path) -> Path | None:
    """The node script an npm-generated ``.cmd`` shim runs, or None if it is not one."""
    try:
        text = shim.read_bytes()[:65536].decode("utf-8", "replace")
    except OSError:
        return None
    match = _NPM_SHIM.search(text)
    return shim.parent / match.group("script") if match else None


def resolve_command(name: str, path: str | None = None, env: Mapping[str, str] | None = None) -> ResolvedCommand:
    """Resolve ``name`` (or an absolute path) to an exe, an npm shim or a batch file.

    ``path`` defaults to ``PATH`` from ``env`` (default ``os.environ``). Raises
    :class:`LaunchError` when nothing usable is found.
    """
    env = env if env is not None else os.environ
    if path is None:
        path = next((v for k, v in env.items() if k.upper() == "PATH"), "")
    dirs = _path_dirs(path)
    given = Path(name)
    if given.is_absolute() or given.parent != Path("."):
        if given.suffix.lower() not in EXTENSION_PREFERENCE:
            raise LaunchError(f"{name}: only {', '.join(EXTENSION_PREFERENCE)} files can be run (never .ps1)")
        if not given.is_file():
            raise LaunchError(f"{name}: file not found")
        found: Path | None = given
    elif given.suffix.lower() in EXTENSION_PREFERENCE:
        found = _find(given.stem, dirs, (given.suffix.lower(),))
    else:
        found = _find(name, dirs, EXTENSION_PREFERENCE)
    if found is None:
        raise LaunchError(f"{name}: no {'/'.join(EXTENSION_PREFERENCE)} found on PATH")

    suffix = found.suffix.lower()
    if suffix not in BATCH_EXTENSIONS:
        return ResolvedCommand(name, found, "exe", (str(found),))
    script = npm_shim_script(found)
    if script is not None:
        if not script.is_file():
            raise LaunchError(f"{found}: npm shim target {script} does not exist")
        node = found.parent / "node.exe"
        if not node.is_file():
            node_found = _find("node", dirs, (".exe",))
            if node_found is None:
                raise LaunchError(f"{found}: npm shim needs node.exe, not found next to it or on PATH")
            node = node_found
        return ResolvedCommand(name, found, "npm-shim", (str(node), str(script)))
    return ResolvedCommand(name, found, "batch", (cmd_exe_path(env),))


def build_env(
    base: Mapping[str, str] | None = None,
    extra_names: Iterable[str] = (),
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """A minimal child environment: :data:`BASE_ENV_NAMES` plus ``extra_names`` copied from
    ``base`` (case-insensitive), then ``overrides``. :data:`NEVER_PASS_ENV` is always removed."""
    base = base if base is not None else os.environ
    wanted = {n.upper() for n in (*BASE_ENV_NAMES, *extra_names)}
    never = {n.upper() for n in NEVER_PASS_ENV}
    env = {k: v for k, v in base.items() if k.upper() in wanted and k.upper() not in never}
    for k, v in (overrides or {}).items():
        if k.upper() not in never:
            env[k] = v
    return env


# --- process tree control -----------------------------------------------------------------

if IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
TIMEOUT_EXIT_CODE = 0xC000013A & 0xFFFFFFFF
"""Exit code given to processes killed by job termination (STATUS_CONTROL_C_EXIT)."""


class _Job:
    """A kill-on-close Job Object holding one process tree."""

    def __init__(self) -> None:
        self.handle = None
        if not IS_WINDOWS:
            return
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            _kernel32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, pid: int) -> bool:
        if not self.handle:
            return False
        process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            return False
        try:
            return bool(_kernel32.AssignProcessToJobObject(self.handle, process))
        finally:
            _kernel32.CloseHandle(process)

    def terminate(self) -> bool:
        return bool(self.handle) and bool(_kernel32.TerminateJobObject(self.handle, TIMEOUT_EXIT_CODE))

    def close(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)  # kill-on-close: remaining descendants die
            self.handle = None


def process_alive(pid: int) -> bool:
    """True while the process ``pid`` is running (Windows only)."""
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(_kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def _taskkill_tree(pid: int, env: Mapping[str, str] | None = None) -> bool:
    exe = str(Path(cmd_exe_path(env)).parent / "taskkill.exe")
    try:
        result = subprocess.run([exe, "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass
class RunResult:
    exit_code: int | None
    timed_out: bool
    started: datetime
    finished: datetime
    duration_seconds: float
    pid: int | None = None
    launch_error: str | None = None
    tree_control: str = "none"
    """``job-object``, ``taskkill`` (fallback) or ``none`` (launch failed)."""
    kill_method: str | None = None
    notes: list[str] = field(default_factory=list)
    cancelled: bool = False
    """The caller's cancel event was set and the process tree was killed."""


CANCEL_POLL_SECONDS = 0.1


def _wait(proc: subprocess.Popen, timeout: float | None, cancel: threading.Event | None, result: RunResult) -> int | None:
    """``proc.wait(timeout)``, also returning when ``cancel`` is set (then ``result.cancelled`` is True and
    the caller's kill path runs). Raises :class:`subprocess.TimeoutExpired` like ``proc.wait``."""
    if cancel is None:
        return proc.wait(timeout=timeout)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if cancel.is_set():
            result.cancelled = True
            raise subprocess.TimeoutExpired(proc.args, timeout or 0)
        step = CANCEL_POLL_SECONDS if deadline is None else max(0.0, min(CANCEL_POLL_SECONDS, deadline - time.monotonic()))
        try:
            return proc.wait(timeout=step)
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                raise


def run(
    command: ResolvedCommand,
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
    stdin_path: Path | None = None,
    timeout: float | None = None,
    cancel: threading.Event | None = None,
) -> RunResult:
    """Run ``command args`` without a shell, streaming output to files; kill the whole process
    tree on timeout, or as soon as ``cancel`` (optional) is set. Never retries. Launch failures are
    returned, not raised. Without ``cancel`` the behaviour is exactly as before."""
    started = datetime.now()
    clock = time.monotonic()
    creationflags = 0
    if IS_WINDOWS:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        line = command.command_line(args)
    except UnsafeArgument as exc:
        now = datetime.now()
        return RunResult(None, False, started, now, 0.0, launch_error=f"unsafe argument: {exc}")
    executable = command.prefix[0] if command.kind == "batch" else None

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    job = _Job()
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        stdin = open(stdin_path, "rb") if stdin_path is not None else subprocess.DEVNULL
        try:
            try:
                proc = subprocess.Popen(
                    line, executable=executable, cwd=str(cwd), env=dict(env), stdin=stdin,
                    stdout=out, stderr=err, shell=False, creationflags=creationflags,
                )
            except OSError as exc:
                job.close()
                now = datetime.now()
                return RunResult(None, False, started, now, time.monotonic() - clock,
                                 launch_error=f"{type(exc).__name__}: {exc}")
        finally:
            if stdin is not subprocess.DEVNULL:
                stdin.close()
        tree_control = "job-object" if job.assign(proc.pid) else "taskkill"
        result = RunResult(None, False, started, started, 0.0, pid=proc.pid, tree_control=tree_control)
        try:
            result.exit_code = _wait(proc, timeout, cancel, result)
        except subprocess.TimeoutExpired:
            result.timed_out = not result.cancelled
            if tree_control == "job-object" and job.terminate():
                result.kill_method = "job-object"
            else:
                result.kill_method = "taskkill" if _taskkill_tree(proc.pid, env) else "kill"
                if result.kill_method == "kill":
                    proc.kill()
            try:
                result.exit_code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                result.notes.append("process did not exit within 30 s after the kill")
        finally:
            job.close()
    result.finished = datetime.now()
    result.duration_seconds = round(time.monotonic() - clock, 3)
    return result
