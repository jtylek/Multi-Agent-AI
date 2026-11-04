"""Per-user GUI project choices. No project checkout files are changed."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .document import atomic_write

COORDINATORS = ("codex", "claude", "copilot")
MAX_RECENT = 10


def settings_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Multi-Agent-AI" / "projects.json"


@dataclass(frozen=True)
class ProjectLibrary:
    current: str | None
    recent: tuple[str, ...]
    coordinators: dict[str, str]

    def coordinator_for(self, folder: Path | str) -> str:
        return self.coordinators.get(str(Path(folder).expanduser().resolve()), "codex")


def load_projects(path: Path | None = None) -> ProjectLibrary:
    path = path or settings_path()
    if not path.is_file():
        return ProjectLibrary(None, (), {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read project settings at {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"Unsupported project settings format at {path}")
    current = data.get("current")
    recent = data.get("recent")
    coordinators = data.get("coordinators")
    if (current is not None and not isinstance(current, str)
            or not isinstance(recent, list) or not all(isinstance(item, str) for item in recent)
            or not isinstance(coordinators, dict)
            or not all(isinstance(folder, str) and provider in COORDINATORS
                       for folder, provider in coordinators.items())):
        raise ValueError(f"Invalid project settings at {path}")
    return ProjectLibrary(current, tuple(recent[:MAX_RECENT]), coordinators)


def remember_project(folder: Path | str, coordinator: str | None = None,
                     *, path: Path | None = None) -> ProjectLibrary:
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Project folder does not exist: {folder}")
    if coordinator is not None and coordinator not in COORDINATORS:
        raise ValueError(f"Unsupported coordinator: {coordinator}")
    path = path or settings_path()
    previous = load_projects(path)
    key = str(folder)
    coordinators = dict(previous.coordinators)
    coordinators[key] = coordinator or coordinators.get(key, "codex")
    recent = (key, *(item for item in previous.recent if item != key))
    library = ProjectLibrary(key, tuple(recent[:MAX_RECENT]), coordinators)
    atomic_write(path, json.dumps({"version": 1, "current": library.current,
                                   "recent": library.recent, "coordinators": library.coordinators},
                                  indent=2) + "\n")
    return library
