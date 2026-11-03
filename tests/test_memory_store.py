"""M1 persistent-memory smoke and isolation tests."""
import pytest
from multi_agent_ai.memory import MemoryStore, MemoryConflict


def test_restart_search_and_retract(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    store.initialize()
    key = store.add(project_id="repo-a", kind="lesson", title="Windows shim",
                    content="Copilot npm shim needs a node executable", actor="agent-1",
                    source_type="task", source_ref="T28")
    store = MemoryStore(path)
    store.initialize()
    assert store.get(key, "repo-a")["revision"] == 1
    assert store.get(key, "repo-b") is None
    assert store.search("Copilot", "repo-a") == []  # proposals are not active
    store.change_status(key, "repo-a", expected_revision=1, status="active",
                        actor="reviewer", reason="reviewed evidence")
    assert len(store.search("Copilot", "repo-a")) == 1
    assert store.search("Copilot", "repo-b") == []
    with pytest.raises(MemoryConflict):
        store.change_status(key, "repo-a", expected_revision=1, status="retracted",
                            actor="reviewer", reason="stale")
    store.change_status(key, "repo-a", expected_revision=2, status="retracted",
                        actor="reviewer", reason="obsolete")
    assert store.search("Copilot", "repo-a") == []


def test_invalid_input(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    with pytest.raises(ValueError):
        store.add(project_id="p", kind="unknown", title="t", content="c",
                  actor="a", source_type="task", source_ref="T1")
