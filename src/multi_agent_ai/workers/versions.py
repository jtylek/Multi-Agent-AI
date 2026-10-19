"""Provider CLI version detection through the CLI's own ``--version`` (T18).

``<cli> --version`` is a local flag every supported CLI answers without contacting the
provider, so it needs no sign-in and uses no quota (the T5/T12 surveys collected the installed
codex/copilot ``--version`` and ``--help`` output the same way). The probe is still bounded: no
shell, a short timeout, the process tree killed on expiry, stdin closed, and the same allowlisted
environment as the real run. A failed or unparseable probe yields ``version=None`` with the reason;
it never blocks a run and never guesses a version.

The version keys the quota detector tables (:mod:`multi_agent_ai.workers.quota`): a version no
table lists falls back to ``provider_error``.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import winproc

PROBE_TIMEOUT = 15.0
RAW_LIMIT = 300

_VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?(?:\+[0-9A-Za-z.]+)?)")


@dataclass(frozen=True)
class CliVersion:
    version: str | None
    """``x.y.z`` (optionally with a pre-release/build suffix), or None when unknown."""
    raw: str = ""
    """What the CLI printed, flattened and truncated (evidence)."""
    error: str | None = None
    """Why the version is unknown (launch failure, timeout, nonzero exit, unparseable output)."""

    def as_record(self) -> dict[str, str | None]:
        return {"version": self.version, "raw": self.raw, "error": self.error}


def parse_version(text: str) -> str | None:
    """The first ``x.y.z`` token in ``text`` (``codex-cli 0.163.0`` -> ``0.163.0``), else None."""
    match = _VERSION.search(text)
    return match.group(1) if match else None


def _flat(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= RAW_LIMIT else flat[: RAW_LIMIT - 3] + "..."


def read_cli_version(
    command: winproc.ResolvedCommand,
    env: Mapping[str, str],
    *,
    timeout: float = PROBE_TIMEOUT,
    runner: Callable[..., winproc.RunResult] = winproc.run,
) -> CliVersion:
    """Run ``<command> --version`` and parse the answer. Never raises, never retries."""
    with tempfile.TemporaryDirectory(prefix="maai-version-") as scratch:
        directory = Path(scratch)
        out, err = directory / "stdout.log", directory / "stderr.log"
        try:
            result = runner(command, ["--version"], cwd=directory, env=env, stdout_path=out, stderr_path=err,
                            stdin_path=None, timeout=timeout)
        except OSError as exc:  # runner contract: launch failures are returned; belt and braces
            return CliVersion(None, "", f"version probe could not start: {exc}")
        stdout = out.read_bytes().decode("utf-8", "replace") if out.is_file() else ""
        stderr = err.read_bytes().decode("utf-8", "replace") if err.is_file() else ""
    raw = _flat(stdout or stderr)
    if result.launch_error:
        return CliVersion(None, raw, f"version probe could not start: {result.launch_error}")
    if result.timed_out:
        return CliVersion(None, raw, f"version probe timed out after {timeout:g}s")
    if result.exit_code != 0:
        return CliVersion(None, raw, f"version probe exited {result.exit_code}")
    version = parse_version(stdout) or parse_version(stderr)
    if version is None:
        return CliVersion(None, raw, "no x.y.z version in the output")
    return CliVersion(version, raw)
