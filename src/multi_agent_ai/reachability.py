"""Read-only discovery of reachable Claude and Codex sessions on Windows."""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Sequence


class ReachabilityError(RuntimeError):
    """A provider could not be queried or returned malformed discovery data."""


@dataclass(frozen=True)
class AgentEndpoint:
    id: str
    name: str
    cwd: str
    product: str
    status: str | None = None
    session_id: str | None = None
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryResult:
    agents: tuple[AgentEndpoint, ...]
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"agents": [a.as_dict() for a in self.agents], "errors": list(self.errors)}


def resolve_executable(command: str) -> str:
    """Resolve a Windows command, preferring executable shims over PowerShell shims."""
    candidates = (command, f"{command}.cmd", f"{command}.exe", f"{command}.bat")
    found: list[str] = []
    for candidate in candidates:
        path = shutil.which(candidate)
        if path and path not in found:
            found.append(path)
    if not found:
        raise ReachabilityError(f"executable not found: {command}")
    # npm installs commonly expose both .ps1 and .cmd. Python subprocess cannot reliably
    # execute the former on Windows; choose a native shim when one exists.
    for path in found:
        if Path(path).suffix.lower() in {".cmd", ".exe", ".bat"}:
            return path
    return found[0]


def _same_cwd(value: object, project_root: Path) -> bool:
    if not isinstance(value, str):
        return False
    try:
        left = Path(value).resolve()
        right = project_root.resolve()
    except OSError:
        return False
    return str(left).casefold() == str(right).casefold()


def _run_json(command: Sequence[str], cwd: Path, timeout: float) -> Any:
    try:
        result = subprocess.run(
            list(command), cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReachabilityError(f"{' '.join(command[:2])}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReachabilityError(f"{' '.join(command[:2])} exited {result.returncode}: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReachabilityError(f"{' '.join(command[:2])} returned invalid JSON: {exc}") from exc


def discover_claude_sessions(
    project_root: Path,
    *,
    runner: Callable[[Sequence[str], Path, float], Any] = _run_json,
    timeout: float = 15,
) -> tuple[AgentEndpoint, ...]:
    executable = resolve_executable("claude")
    value = runner((executable, "agents", "--json"), project_root, timeout)
    if not isinstance(value, list):
        raise ReachabilityError("claude agents --json returned a non-list")
    result: list[AgentEndpoint] = []
    for item in value:
        if not isinstance(item, dict) or not _same_cwd(item.get("cwd"), project_root):
            continue
        session_id = item.get("sessionId")
        name = item.get("name") or session_id
        if not isinstance(name, str) or not name:
            continue
        result.append(
            AgentEndpoint(
                id=name,
                name=name,
                cwd=str(item.get("cwd", "")),
                product="claude",
                status=item.get("status") if isinstance(item.get("status"), str) else None,
                session_id=session_id if isinstance(session_id, str) else None,
                source="claude agents",
            )
        )
    return tuple(result)


def _read_rpc_lines(stream: Any, output: queue.Queue[dict[str, Any]]) -> None:
    for line in stream:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            output.put(value)


def _query_codex_app_server(project_root: Path, timeout: float) -> list[dict[str, Any]]:
    executable = resolve_executable("codex")
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise ReachabilityError(f"codex app-server: {exc}") from exc
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise ReachabilityError("codex app-server did not expose stdio")
    output: queue.Queue[dict[str, Any]] = queue.Queue()
    threading.Thread(target=_read_rpc_lines, args=(process.stdout, output), daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def response(request_id: int) -> dict[str, Any]:
        # Queue.get uses a monotonic timeout; a single bounded wait is enough because the
        # app-server returns one response for each request and notifications are ignored.
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReachabilityError(f"codex app-server timed out waiting for request {request_id}")
            try:
                item = output.get(timeout=remaining)
            except queue.Empty as exc:
                raise ReachabilityError(f"codex app-server timed out waiting for request {request_id}") from exc
            if item.get("id") != request_id:
                continue
            if "error" in item:
                raise ReachabilityError(f"codex app-server request {request_id} failed: {item['error']}")
            return item

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "multi-agent-ai", "version": "0.1.0"}
        }})
        response(1)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "thread/list", "params": {
            "cwd": [str(project_root.resolve())], "limit": 100, "useStateDbOnly": True
        }})
        value = response(2).get("result", {}).get("data", [])
        if not isinstance(value, list):
            raise ReachabilityError("codex thread/list returned a non-list")
        return [item for item in value if isinstance(item, dict) and _same_cwd(item.get("cwd"), project_root)]
    finally:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


def discover_codex_threads(
    project_root: Path,
    *,
    query: Callable[[Path, float], list[dict[str, Any]]] = _query_codex_app_server,
    timeout: float = 15,
) -> tuple[AgentEndpoint, ...]:
    result: list[AgentEndpoint] = []
    for item in query(project_root, timeout):
        if not _same_cwd(item.get("cwd"), project_root):
            continue
        thread_id = item.get("id") or item.get("sessionId")
        name = item.get("name") or thread_id
        if not isinstance(thread_id, str) or not isinstance(name, str):
            continue
        result.append(
            AgentEndpoint(
                id=thread_id,
                name=name,
                cwd=str(item.get("cwd", "")),
                product="codex",
                status=(item.get("status") or {}).get("type") if isinstance(item.get("status"), dict) else None,
                session_id=thread_id,
                source="codex thread/list",
            )
        )
    return tuple(result)


def discover_agents(project_root: Path, *, timeout: float = 15) -> DiscoveryResult:
    """Discover supported sessions; one provider failure does not hide the other."""
    agents: list[AgentEndpoint] = []
    errors: list[str] = []
    for discover in (discover_claude_sessions, discover_codex_threads):
        try:
            agents.extend(discover(project_root, timeout=timeout))
        except ReachabilityError as exc:
            errors.append(str(exc))
    return DiscoveryResult(tuple(agents), tuple(errors))
