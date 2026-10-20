"""Print one line whenever a status, review or ack file of a joint task changes.

Coordinator helper for a background watch (for example Claude Code's Monitor tool). Read-only,
standard library only, Windows-native. The first pass is a silent baseline. Whenever the set of
agents whose status file says ``safe-to-clear: yes`` changes, one extra ``SAFE TO CLEAR`` line is
printed (the field is the agent's own claim, see docs/protocol/formats.md section 5).

    py -3.12 tools/watch_board.py --project-root C:\\path\\to\\project --task m1-real-execution
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

KEYS = {
    "status": ("state", "last-commit", "blockers", "safe-to-clear"),
    "review": ("status", "reviewed"),
    "ack": ("status",),
}


def read_headers(path: Path) -> dict[str, str]:
    """Header block of a coordination file as a dict (first occurrence wins, BOM tolerated)."""
    headers: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return headers
    for line in text.splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() not in headers:
            headers[key.strip()] = value.strip()
    return headers


def header_values(path: Path, keys: tuple[str, ...]) -> str:
    headers = read_headers(path)
    if not headers:
        return "(unreadable)"
    return " | ".join(f"{k}: {headers[k][:90]}" for k in keys if k in headers)


def safe_to_clear(state_root: Path, task: str) -> list[str]:
    """Agent ids whose status file claims ``safe-to-clear: yes`` (sorted)."""
    agents = []
    for path in (state_root / "tasks" / task).glob("status-*.md"):
        headers = read_headers(path)
        if headers.get("safe-to-clear", "").strip().lower() == "yes":
            agents.append(headers.get("agent") or path.stem[len("status-"):])
    return sorted(agents)


def watched(state_root: Path, task: str) -> list[tuple[Path, str]]:
    task_dir = state_root / "tasks" / task
    files = [(p, "status") for p in sorted(task_dir.glob("status-*.md"))]
    files += [(p, "review") for p in sorted(task_dir.glob("review-*.md"))]
    files += [(p, "ack") for p in sorted((state_root / "messages").glob("*/ack-*.md"))]
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--state-root", default=".multi-agent-ai")
    parser.add_argument("--task", required=True)
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    parser.add_argument("--once", action="store_true", help="baseline pass only, then exit")
    args = parser.parse_args(argv)

    state_root = Path(args.state_root)
    if not state_root.is_absolute():
        state_root = Path(args.project_root) / state_root
    seen: dict[Path, int] = {}
    previous_safe: list[str] | None = None
    first = True
    while True:
        for path, kind in watched(state_root, args.task):
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if seen.get(path) != mtime:
                seen[path] = mtime
                if not first:
                    label = f"{path.parent.name}/{path.name}"
                    print(f"{label} :: {header_values(path, KEYS[kind])}", flush=True)
        safe = safe_to_clear(state_root, args.task)
        if not first and safe != previous_safe:
            print("SAFE TO CLEAR now: " + (", ".join(safe) if safe else "(nobody)"), flush=True)
        previous_safe = safe
        first = False
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
