"""Additional headless CLI adapters for managed review attempts.

All five are REVIEW-ONLY until their least-privilege implement profiles have
been validated. Runner checks git changes after execution, but that is not a
filesystem sandbox; run only in a dedicated worktree with trusted inputs.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import Invocation, LaunchRequest, ParsedOutput, ProfileUnconfigured, ProviderAdapter, token_counts


class _ReviewAdapter(ProviderAdapter):
    def build_invocation(self, request: LaunchRequest) -> Invocation:
        if request.mode != "review":
            raise ProfileUnconfigured(
                f"{self.name}: implement mode disabled until a tested least-privilege profile exists"
            )
        return Invocation(tuple(self.review_args(request)), f"{self.name}-review")

    def review_args(self, request: LaunchRequest) -> list[str]:
        raise NotImplementedError

    def parse_output(self, stdout_path: Path, invocation: Invocation) -> ParsedOutput:
        try:
            raw = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            return ParsedOutput(None, "unknown", f"cannot read stdout: {exc}")
        if not raw:
            return ParsedOutput(None, "unknown", "empty stdout")
        return ParsedOutput(raw, "unknown")


class GeminiAdapter(_ReviewAdapter):
    name = command = "gemini"
    env_names = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_CLI_HOME")

    def review_args(self, request: LaunchRequest) -> list[str]:
        args = ["-p", request.prompt, "--output-format", "json", "--approval-mode", "plan"]
        if request.model:
            args += ["--model", request.model]
        return args

    def parse_output(self, stdout_path: Path, invocation: Invocation) -> ParsedOutput:
        try:
            obj = json.loads(stdout_path.read_text(encoding="utf-8"))
            if not isinstance(obj, dict) or not isinstance(obj.get("response"), str):
                raise ValueError("missing response")
            return ParsedOutput(obj["response"], token_counts(obj.get("stats")) or "unknown")
        except (OSError, ValueError) as exc:
            return ParsedOutput(None, "unknown", f"invalid Gemini JSON: {exc}")


class QwenAdapter(_ReviewAdapter):
    name = command = "qwen"
    env_names = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "QWEN_CODE_HOME")

    def review_args(self, request: LaunchRequest) -> list[str]:
        args = ["-p", request.prompt, "--output-format", "json", "--approval-mode", "plan"]
        if request.model:
            args += ["--model", request.model]
        return args

    def parse_output(self, stdout_path: Path, invocation: Invocation) -> ParsedOutput:
        try:
            obj = json.loads(stdout_path.read_text(encoding="utf-8"))
            if not isinstance(obj, list):
                raise ValueError("expected a JSON array")
            results = [x.get("result") for x in obj if isinstance(x, dict) and x.get("type") == "result"]
            message = next((x for x in reversed(results) if isinstance(x, str) and x.strip()), None)
            return ParsedOutput(message, "unknown", None if message else "missing result message")
        except (OSError, ValueError) as exc:
            return ParsedOutput(None, "unknown", f"invalid Qwen JSON: {exc}")


class OpenCodeAdapter(_ReviewAdapter):
    name = command = "opencode"
    env_names = ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")

    def review_args(self, request: LaunchRequest) -> list[str]:
        args = ["run", "--agent", "plan"]
        if request.model:
            args += ["--model", request.model]
        return [*args, request.prompt]


class AiderAdapter(_ReviewAdapter):
    name = command = "aider"
    env_names = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OLLAMA_API_BASE", "AIDER_MODEL")

    def review_args(self, request: LaunchRequest) -> list[str]:
        args = ["--message", request.prompt, "--chat-mode", "ask", "--no-auto-commits",
                "--no-dirty-commits", "--no-stream", "--no-auto-test", "--no-auto-lint"]
        if request.model:
            args += ["--model", request.model]
        return args


class GooseAdapter(_ReviewAdapter):
    name = command = "goose"
    env_names = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOSE_PROVIDER", "GOOSE_MODEL")

    def review_args(self, request: LaunchRequest) -> list[str]:
        # Goose extensions/configuration are user-controlled. No filesystem
        # isolation is claimed here; orchestrator validates git state afterwards.
        args = ["run", "--no-session", "-t", request.prompt]
        if request.model:
            args += ["--model", request.model]
        return args
