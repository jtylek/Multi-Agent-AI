"""Validation, project isolation, and context budget tests."""
import pytest
from multi_agent_ai.memory.policy import validate_content
from multi_agent_ai.memory.context import build_context
from multi_agent_ai.memory import MemoryStore


def test_secret_patterns_rejected():
    with pytest.raises(ValueError):
        validate_content("api_key = example-secret")
    with pytest.raises(ValueError):
        validate_content("-----BEGIN PRIVATE KEY-----")


def test_context_is_project_scoped_and_bounded(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    key = store.add(project_id="one", kind="lesson", title="Windows",
                    content="Windows shim issue", actor="a",
                    source_type="task", source_ref="T1")
    store.promote(key, "one", expected_revision=1, actor="reviewer",
                  reason="checked", evidence="test:T1")
    assert "Windows shim issue" in build_context(store, project_id="one", query="Windows")
    assert "Windows shim issue" not in build_context(store, project_id="two", query="Windows")
    assert len(build_context(store, project_id="one", query="Windows", max_characters=100)) <= 100
