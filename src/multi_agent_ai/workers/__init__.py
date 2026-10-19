"""Managed headless workers: provider adapters (codex, copilot), prompt builder, runner and
failure classification. See docs/protocol/agent-messaging.md, "Managed workers"."""

from .base import FORBIDDEN_FLAGS, MODES, ProfileUnconfigured, forbidden_in
from .failures import FAILURE_CLASSES, classify
from .runner import ADAPTERS, UsageError, WorkerOutcome, WorkerRequest, run_worker

__all__ = [
    "ADAPTERS", "FAILURE_CLASSES", "FORBIDDEN_FLAGS", "MODES", "ProfileUnconfigured", "UsageError",
    "WorkerOutcome", "WorkerRequest", "classify", "forbidden_in", "run_worker",
]
