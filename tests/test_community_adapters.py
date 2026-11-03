import json
from pathlib import Path

import pytest

from multi_agent_ai.workers.base import LaunchRequest, ProfileUnconfigured, forbidden_in
from multi_agent_ai.workers.community import (
    AiderAdapter, GeminiAdapter, GooseAdapter, OpenCodeAdapter, QwenAdapter,
)
from multi_agent_ai.workers.runner import ADAPTERS


ADDITIONAL = (GeminiAdapter, QwenAdapter, OpenCodeAdapter, AiderAdapter, GooseAdapter)


def request(tmp_path, mode="review"):
    return LaunchRequest(
        worktree=tmp_path, mode=mode, prompt="Review the changes; do not modify files.",
        prompt_path=tmp_path / "prompt.md", run_dir=tmp_path,
    )


@pytest.mark.parametrize("adapter_type", ADDITIONAL)
def test_review_invocation(adapter_type, tmp_path):
    adapter = adapter_type()
    invocation = adapter.build_invocation(request(tmp_path))
    assert invocation.profile == adapter.name + "-review"
    assert not forbidden_in(invocation.args)
    assert "Review the changes" in " ".join(invocation.args)
    assert adapter.name in ADAPTERS


@pytest.mark.parametrize("adapter_type", ADDITIONAL)
def test_implement_fails_closed(adapter_type, tmp_path):
    with pytest.raises(ProfileUnconfigured):
        adapter_type().build_invocation(request(tmp_path, "implement"))


def test_gemini_json(tmp_path):
    file = tmp_path / "out.json"
    file.write_text(json.dumps({"response": "verdict: agreed", "stats": {}}))
    output = GeminiAdapter().parse_output(file, GeminiAdapter().build_invocation(request(tmp_path)))
    assert output.final_message == "verdict: agreed"


def test_qwen_json(tmp_path):
    file = tmp_path / "out.json"
    file.write_text(json.dumps([{"type": "result", "result": "verdict: changes requested"}]))
    output = QwenAdapter().parse_output(file, QwenAdapter().build_invocation(request(tmp_path)))
    assert output.final_message == "verdict: changes requested"


def test_malformed_output_fails_closed(tmp_path):
    file = tmp_path / "out.json"
    file.write_text("not-json")
    assert GeminiAdapter().parse_output(file, GeminiAdapter().build_invocation(request(tmp_path))).malformed
    assert QwenAdapter().parse_output(file, QwenAdapter().build_invocation(request(tmp_path))).malformed
