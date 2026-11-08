"""Chat sessions leave an append-only event log that Live Activity reads while providers run."""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from multi_agent_ai.activity import ActivityWatcher
from multi_agent_ai.chat_log import (
    ChatLog, StreamTail, describe_claude_event, describe_codex_event, new_session_id,
)
from multi_agent_ai.gui_chat import ChatError, ask_provider, stream_claude_args
from multi_agent_ai.job import read_events

CLAUDE_STREAM = [
    {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-test"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "# Project", "is_error": False}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello World!"}]}},
    {"type": "result", "result": "Hello World!", "usage": {"input_tokens": 12, "output_tokens": 3},
     "total_cost_usd": 0.001},
]
CODEX_STREAM = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "item.started", "item": {"type": "command_execution", "command": "git status"}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "git status", "exit_code": 0}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello World!"}},
    {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 4}},
]


def test_log_lines_are_ordered_job_style_events_from_many_threads(tmp_path):
    log = ChatLog(tmp_path, new_session_id())
    threads = [threading.Thread(target=lambda n=n: log.record("tool_use", summary=f"call {n}"))
               for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    events, warnings = read_events(log.path)
    assert warnings == []
    assert [event["seq"] for event in events] == list(range(1, 21))
    assert all(event["session"] == log.session_id for event in events)
    assert log.path.parent.parent == tmp_path / "chat"


def test_unwritable_log_reports_instead_of_raising(tmp_path):
    blocker = tmp_path / "chat"
    blocker.write_text("not a directory", encoding="utf-8")
    log = ChatLog(tmp_path, "chat-x")
    assert log.record("message_sent", summary="hi") is None
    assert "not being recorded" in log.error


def test_provider_events_are_summarized():
    claude = [kind for event in CLAUDE_STREAM for kind, _, _ in describe_claude_event(event)]
    assert claude == ["provider_session", "tool_use", "tool_result", "assistant_text"]
    assert describe_claude_event(CLAUDE_STREAM[1])[0][1] == "Read README.md"
    codex = [(kind, summary) for event in CODEX_STREAM for kind, summary, _ in describe_codex_event(event)]
    assert codex == [("provider_session", "thread t1"), ("command", "git status"),
                     ("command_finished", "exit 0: git status"), ("assistant_text", "Hello World!")]


def test_stream_tail_follows_a_growing_file_and_partial_lines(tmp_path):
    path = tmp_path / "stdout.txt"
    seen = []
    tail = StreamTail(path, seen.append).start()
    with open(path, "wb") as out:
        out.write(b'not json\n{"type": "a"}\n{"type"')
        out.flush()
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)
        assert seen == [{"type": "a"}]  # seen while the "process" is still writing
        out.write(b': "b"}\n')
    tail.stop()
    assert seen == [{"type": "a"}, {"type": "b"}]


def _runner(provider, stdout_lines, exit_code=0):
    calls = []

    def run(command, args, **kwargs):
        calls.append(list(args))
        kwargs["stderr_path"].write_text("boom" if exit_code else "", encoding="utf-8")
        kwargs["stdout_path"].write_text("".join(json.dumps(e) + "\n" for e in stdout_lines),
                                         encoding="utf-8")
        if provider == "codex":
            Path(args[args.index("-o") + 1]).write_text("Hello World!", encoding="utf-8")
        return SimpleNamespace(exit_code=exit_code, timed_out=False, cancelled=False, launch_error=None,
                               duration_seconds=1.5)

    return run, calls


@pytest.mark.parametrize("provider, stream, live", [
    ("claude", CLAUDE_STREAM, ["provider_session", "tool_use", "tool_result", "assistant_text"]),
    ("codex", CODEX_STREAM, ["provider_session", "command", "command_finished", "assistant_text"]),
])
def test_ask_provider_reports_live_events(tmp_path, provider, stream, live):
    events = []
    run, calls = _runner(provider, stream)
    answer = ask_provider(provider, tmp_path, "claude", [], "hi", resolver=lambda n: n, runner=run,
                          temp_dir=tmp_path, on_event=lambda kind, fields: events.append((kind, fields)))
    assert answer == "Hello World!"
    kinds = [kind for kind, _ in events]
    assert kinds == ["provider_started", *live, "provider_finished"]
    assert all(fields["provider"] == provider for _, fields in events)
    finished = events[-1][1]
    assert finished["outcome"] == "ok" and finished["duration_seconds"] == 1.5
    assert finished["usage"] != "unknown"
    if provider == "claude":
        assert "--output-format=stream-json" in calls[0] and "--verbose" in calls[0]
        assert "--output-format=json" not in calls[0]
        assert finished["provider_reported_cost_usd"] == 0.001


COPILOT_STREAM = [
    {"type": "session.start", "data": {"sessionId": "c1"}},
    {"type": "assistant.message_delta", "data": {"deltaContent": "Hel"}},
    {"type": "tool.execution_start", "data": {"toolName": "view", "arguments": {"path": "README.md"}}},
    {"type": "tool.execution_complete", "data": {"toolName": "view"}},
    {"type": "assistant.message", "data": {"content": "Hello World!"}},
]


def _copilot_runner(lines):
    def run(command, args, **kwargs):
        kwargs["stderr_path"].write_text("", encoding="utf-8")
        kwargs["stdout_path"].write_text(lines, encoding="utf-8")
        return SimpleNamespace(exit_code=0, timed_out=False, cancelled=False, launch_error=None)
    return run


def test_copilot_json_events_are_shown_generically(tmp_path):
    events = []
    lines = "log line\n" + "".join(json.dumps(e) + "\n" for e in COPILOT_STREAM)
    answer = ask_provider("copilot", tmp_path, "claude", [], "hi", resolver=lambda n: n,
                          runner=_copilot_runner(lines), temp_dir=tmp_path,
                          on_event=lambda kind, fields: events.append((kind, fields)))
    assert answer == "Hello World!"
    assert [(kind, fields["summary"]) for kind, fields in events[1:-1]] == [
        ("provider_event", "session.start"),
        ("tool_use", "tool.execution_start: view README.md"),
        ("tool_result", "tool.execution_complete: view"),
        ("assistant_text", "Hello World!"),
    ]
    assert events[0][1]["live_events"] is True
    assert events[-1][1]["event_types"][0] == "session.start"
    assert "hi" not in json.dumps(events)  # the prompt itself is not logged here


def test_copilot_without_a_recognisable_message_names_the_event_types(tmp_path):
    lines = json.dumps({"type": "answer.final", "body": "Hello"}) + "\n"
    with pytest.raises(ChatError, match="answer.final"):
        ask_provider("copilot", tmp_path, "claude", [], "hi", resolver=lambda n: n,
                     runner=_copilot_runner(lines), temp_dir=tmp_path)


def test_failed_call_is_logged_before_the_error(tmp_path):
    events = []
    run, _ = _runner("codex", [], exit_code=2)
    with pytest.raises(ChatError, match="boom"):
        ask_provider("codex", tmp_path, "codex", [], "hi", resolver=lambda n: n, runner=run,
                     temp_dir=tmp_path, on_event=lambda kind, fields: events.append((kind, fields)))
    assert events[-1][0] == "provider_finished"
    assert events[-1][1]["outcome"] == "exit_nonzero" and events[-1][1]["stderr_tail"] == "boom"


def test_stream_args_refuse_an_unexpected_adapter_format():
    with pytest.raises(ValueError):
        stream_claude_args(("-p", "--output-format=text"))


def test_live_activity_shows_chat_events_incrementally(tmp_path):
    watcher = ActivityWatcher(tmp_path)
    log = ChatLog(tmp_path, "chat-20261108-120000-abcd")
    log.record("message_sent", summary="to Claude: hello")
    log.record("tool_use", provider="claude", summary="Read README.md")
    first = watcher.poll()
    assert [(a.event, a.summary) for a in first] == [
        ("Chat message sent", "to Claude: hello"), ("Agent tool use (claude)", "Read README.md")]
    assert watcher.poll() == []
    log.record("provider_finished", provider="claude", summary="ok")
    assert [a.event for a in watcher.poll()] == ["Agent call finished (claude)"]
