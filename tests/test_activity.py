"""The live view reports evidence without inferring delivery or completion."""

from multi_agent_ai.activity import ActivityWatcher
from multi_agent_ai.job import append_event


def test_mailbox_and_job_updates_are_incremental(tmp_path):
    watcher = ActivityWatcher(tmp_path)
    request = tmp_path / "messages" / "m1" / "request.md"
    request.parent.mkdir(parents=True)
    request.write_text("from: codex-a\nto: claude-b\n\nPlease review.\n", encoding="utf-8")
    first = watcher.poll()
    assert len(first) == 1
    assert first[0].event == "Request recorded"
    assert "Please review" in first[0].detail
    assert watcher.poll() == []

    ack = request.parent / "ack-claude-b.md"
    ack.write_text("agent: claude-b\nstatus: received\n", encoding="utf-8")
    append_event(tmp_path / "jobs" / "job1", "attempt_started", 1, subtask="T1", provider="codex")
    events = watcher.poll()
    assert {event.event for event in events} == {"Acknowledgment recorded", "Job event"}
    assert any("received" in event.summary for event in events)
    assert watcher.poll() == []

    ack.write_text("agent: claude-b\nstatus: agreed\n", encoding="utf-8")
    assert [event.summary for event in watcher.poll()] == ["claude-b: agreed"]

    result = tmp_path / "runs" / "attempt1" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text('{"provider":"codex","mode":"implement","usage":{"input_tokens":70,"output_tokens":10}}', encoding="utf-8")
    usage = watcher.poll()
    assert len(usage) == 1 and usage[0].event == "Provider usage"
    assert "input_tokens" in usage[0].summary
