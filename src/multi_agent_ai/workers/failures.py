"""Deterministic failure classification for one worker attempt (AGENTS.md section 18).

:func:`classify` is a pure function of the evidence in :class:`Evidence`: launch result, exit
code, stdout/stderr text, and the orchestrator's own checks. It returns the primary class
(``None`` = success) and never triggers a retry; what to do next is the coordinator's call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PROFILE_UNCONFIGURED = "profile_unconfigured"
ENVIRONMENT = "environment"
LAUNCH_ERROR = "launch_error"
REVIEW_MODIFIED_FILES = "review_modified_files"
GIT_POLICY_VIOLATION = "git_policy_violation"
TIMEOUT = "timeout"
UNSUPPORTED_MODEL = "unsupported_model"
AUTH = "auth"
PERMISSION_DENIED = "permission_denied"
INVALID_ARGUMENTS = "invalid_arguments"
NONZERO_EXIT = "nonzero_exit"
MALFORMED_OUTPUT = "malformed_output"
NO_CHANGES = "no_changes"
COMMIT_FAILED = "commit_failed"

FAILURE_CLASSES = (
    PROFILE_UNCONFIGURED, ENVIRONMENT, LAUNCH_ERROR, REVIEW_MODIFIED_FILES, GIT_POLICY_VIOLATION,
    TIMEOUT, UNSUPPORTED_MODEL, AUTH, PERMISSION_DENIED, INVALID_ARGUMENTS, NONZERO_EXIT,
    MALFORMED_OUTPUT, NO_CHANGES, COMMIT_FAILED,
)
"""In precedence order: the first that applies is the primary class."""

# Patterns are matched case-insensitively against stderr + stdout of a nonzero exit, in this
# order: model, auth, permission, arguments.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (UNSUPPORTED_MODEL, re.compile(
        r"model[^\n]{0,80}(not supported|not found|does not exist|is not available|unavailable|unknown|invalid)"
        r"|(unknown|unsupported|invalid) model|model_not_found|no access to model"
        r"|do(es)? not have access to (the )?model", re.I)),
    (AUTH, re.compile(
        r"not logged in|not authenticated|authentication (failed|required|error)|unauthori[sz]ed|\b401\b"
        r"|please (log ?in|login|sign in|authenticate)|login required|invalid api key|no (valid )?credentials"
        r"|token (has )?expired|copilot login|codex login", re.I)),
    (PERMISSION_DENIED, re.compile(
        r"permission (was |is )?denied|denied by (the )?(user|policy|permission)|tool[^\n]{0,60}(was |is )?"
        r"(denied|not (allowed|permitted))|not permitted to (run|use)|requires? (user )?approval"
        r"|rejected by (the )?sandbox|blocked by (the )?sandbox", re.I)),
    (INVALID_ARGUMENTS, re.compile(
        r"unexpected argument|unknown (option|argument|flag)|invalid value .* for|unrecognized (option|argument)"
        r"|error: the argument|missing required argument|for more information, try '--help'", re.I)),
)


@dataclass(frozen=True)
class Evidence:
    mode: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    profile_unconfigured: bool = False
    environment_error: bool = False
    launch_error: bool = False
    arguments_rejected: bool = False
    """Refused before launch: argv too long, or not passable through cmd.exe unaltered."""
    timed_out: bool = False
    review_modified: bool = False
    git_violation: bool = False
    malformed: bool = False
    changed_files: int = 0
    commit_failed: bool = False


def match_output(stdout: str, stderr: str) -> str | None:
    """The first output pattern class matching ``stderr`` then ``stdout``, or None."""
    text = f"{stderr}\n{stdout}"
    return next((name for name, pattern in PATTERNS if pattern.search(text)), None)


def classify(e: Evidence) -> str | None:
    """Primary failure class, or None for a successful attempt.

    Rules, in order: profile unconfigured; environment (pre-launch checks); launch error
    (executable missing/not startable); arguments refused before launch; review run that changed the worktree; a Git operation
    by the worker (HEAD/branch moved, new ref at HEAD); timeout; nonzero exit refined by
    output patterns (unsupported model, auth, permission denied, invalid arguments, else
    nonzero exit); exit 0 with output not in the expected format; implement run without
    changes; the orchestrator's commit failed.
    """
    if e.profile_unconfigured:
        return PROFILE_UNCONFIGURED
    if e.environment_error:
        return ENVIRONMENT
    if e.launch_error:
        return LAUNCH_ERROR
    if e.arguments_rejected:
        return INVALID_ARGUMENTS
    if e.mode == "review" and e.review_modified:
        return REVIEW_MODIFIED_FILES
    if e.git_violation:
        return GIT_POLICY_VIOLATION
    if e.timed_out:
        return TIMEOUT
    if e.exit_code != 0:
        return match_output(e.stdout, e.stderr) or NONZERO_EXIT
    if e.malformed:
        return MALFORMED_OUTPUT
    if e.mode == "implement" and e.changed_files == 0:
        return NO_CHANGES
    if e.commit_failed:
        return COMMIT_FAILED
    return None
