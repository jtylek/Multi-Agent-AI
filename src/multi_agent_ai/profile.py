"""Project-profile bootstrap.

The profile is created inside the configured coordination state root so it is excluded from
worker Git snapshots and commits, including when a caller uses a custom state-root path. This
module only creates the starter document; profile validation and execution belong to later
milestones.
"""

from __future__ import annotations

import os
from pathlib import Path

PROFILE_FILE = "project-profile.toml"

_PROFILE_TEMPLATE = '''# Multi-Agent-AI project profile (T21 starter)
# Fill in project-specific values before using verification or deployment automation.
# This file is kept with the configured state root so worker commits never include it.
# The engine creates this starter but does not read, validate, or enforce it yet.

[profile]
schema_version = 1
id = "project"
root = "__PROJECT_ROOT__"
description = "Project profile; replace placeholders with confirmed project facts."

[state]
state_root = "__STATE_ROOT__"

# Add one [[tooling]] table for each runtime or package manager used by the project.
# Use minimum_version = "unspecified" when no minimum is known; do not guess.

[worktree]
dependency_install = []
environment_files = []
copy_from_live_checkout = false

[database]
test_isolation = "unspecified"
destructive_setup_requires_approval = true

[verification]
# Add [[verification.commands]] entries with exact argv lists; do not use shell strings.
commands = []

[formatting]
formatter_argv = []
syntax_exceptions = []

[modules]
required = []
optional = []
dependent_test_commands = []

[safety]
# An empty list means "not declared" until confirmed, not "no live branches".
live_branches = []
protected_branches = []
deployment_requires_user_approval = true
deployment_source = "clean-verified-artifact"
deployment_requires_clean_tree = true
'''


def _render_profile(project_root: Path, state_root: Path) -> str:
    project_root = project_root.resolve()
    state_root = state_root.resolve()
    project_relative = os.path.relpath(project_root, state_root).replace("\\", "/") or "."
    state_relative = os.path.relpath(state_root, project_root).replace("\\", "/") or "."
    if not state_relative.endswith("/"):
        state_relative += "/"
    return _PROFILE_TEMPLATE.replace("__PROJECT_ROOT__", project_relative).replace("__STATE_ROOT__", state_relative)


STARTER_PROFILE = _render_profile(Path("."), Path(".") / ".multi-agent-ai")


def profile_path(project_root: Path | str = ".", state_root: Path | str | None = None) -> Path:
    root = Path(state_root) if state_root is not None else Path(project_root).resolve() / ".multi-agent-ai"
    if not root.is_absolute():
        root = Path(project_root).resolve() / root
    return root.resolve() / PROFILE_FILE


def ensure_starter_profile(project_root: Path | str = ".", state_root: Path | str | None = None) -> Path:
    """Create the starter profile once and never overwrite an existing profile."""
    path = profile_path(project_root, state_root)
    project = Path(project_root).resolve()
    state = path.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(_render_profile(project, state))
    except FileExistsError:
        pass
    return path
