"""Curated free-access agent catalog. Free availability and limits change by provider.

Catalog entries are discovery links, not proof of free API credits or
orchestrator integration. Only providers with a supported adapter can run jobs; some adapters are review-only.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    name: str
    access: str
    url: str
    cli: str = ""
    adapter: bool = False


CATALOG = (
    CatalogEntry("codex", "OpenAI Codex CLI", "Plan-dependent access; verify entitlement", "https://github.com/openai/codex", "codex", True),
    CatalogEntry("copilot", "GitHub Copilot CLI", "Limited free plan; limits vary", "https://github.com/github/copilot-cli", "copilot", True),
    CatalogEntry("claude", "Claude Code", "Account/subscription-dependent; no guaranteed free CLI quota", "https://code.claude.com/docs/en/overview", "claude", True),
    CatalogEntry("gemini", "Google Gemini CLI", "Free individual usage subject to quotas", "https://github.com/google-gemini/gemini-cli", "gemini", True),
    CatalogEntry("qwen", "Qwen Code", "Open-source CLI; provider usage may be limited or billed", "https://github.com/QwenLM/qwen-code", "qwen", True),
    CatalogEntry("opencode", "OpenCode", "Open-source CLI; model access varies", "https://github.com/anomalyco/opencode", "opencode", True),
    CatalogEntry("aider", "Aider", "Open-source CLI; local models or separately billed APIs", "https://github.com/Aider-AI/aider", "aider", True),
    CatalogEntry("goose", "Goose", "Open-source agent; local models or separately billed APIs", "https://github.com/block/goose", "goose", True),
    CatalogEntry("openhands", "OpenHands", "Open-source agent; compute/model costs may apply", "https://github.com/All-Hands-AI/OpenHands"),
    CatalogEntry("continue", "Continue", "Open-source coding agent; model costs may apply", "https://github.com/continuedev/continue"),
    CatalogEntry("cline", "Cline", "Open-source agent; model costs may apply", "https://github.com/cline/cline"),
    CatalogEntry("roo", "Roo Code", "Open-source agent; model costs may apply", "https://github.com/RooCodeInc/Roo-Code"),
)


def get_entry(key: str) -> CatalogEntry:
    return next(entry for entry in CATALOG if entry.key == key)
