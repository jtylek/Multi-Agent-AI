"""CLI integration and evidence promotion tests for persistent memory M1."""
import json
from multi_agent_ai.cli import main
from multi_agent_ai.memory import MemoryStore, MemoryConflict
import pytest


def test_memory_cli_lifecycle(tmp_path, capsys):
    root = tmp_path / ".multi-agent-ai"
    root.mkdir()
    assert main(["memory", "init", "--state-root", str(root)]) == 0
    content = tmp_path / "note.txt"
    content.write_text("Windows subprocess behavior", encoding="utf-8")
    assert main(["memory", "add", "--state-root", str(root), "--project", "repo",
                 "--kind", "lesson", "--title", "Process handling",
                 "--content-file", str(content), "--actor", "agent",
                 "--source-type", "task", "--source-ref", "T1"]) == 0
    key = json.loads(capsys.readouterr().out.split("}\n")[-2] + "}")["id"] if False else None
    store = MemoryStore(root / "memory.sqlite3")
    with store._connect() as db:
        key = db.execute("SELECT id FROM memory_records").fetchone()[0]
    assert main(["memory", "promote", "--state-root", str(root), "--project", "repo",
                 key, "--expected-revision", "1", "--actor", "reviewer",
                 "--reason", "verified test", "--evidence", "test:T1"]) == 0
    assert main(["memory", "search", "--state-root", str(root), "--project", "repo",
                 "Windows"]) == 0
    assert main(["memory", "search", "--state-root", str(root), "--project", "other",
                 "Windows"]) == 0
    assert main(["memory", "doctor", "--state-root", str(root)]) == 0
    assert main(["memory", "retract", "--state-root", str(root), "--project", "repo",
                 key, "--expected-revision", "2", "--actor", "reviewer",
                 "--reason", "obsolete"]) == 0
    assert store.search("Windows", "repo") == []


def test_atomic_promotion_rejects_stale_revision(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    key = store.add(project_id="repo", kind="fact", title="Fact", content="Evidence",
                    actor="worker", source_type="task", source_ref="T2")
    with pytest.raises(MemoryConflict):
        store.promote(key, "repo", expected_revision=2, actor="reviewer",
                      reason="checked", evidence="test:T2")
    assert store.get(key, "repo")["status"] == "proposed"
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_sources WHERE source_type='evidence'").fetchone()[0] == 0
