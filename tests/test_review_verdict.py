"""The orchestrator-written ``status`` header of a review file (T27, Q12): parsed from the verdict line of the
reviewer's message, and the only thing the integration gate trusts."""

import pytest

from multi_agent_ai import workspace as ws
from multi_agent_ai.workers.runner import review_verdict, write_review


@pytest.mark.parametrize("message, expected", [
    ("verdict: agreed\ncommit reviewed: abc\n", "agreed"),
    ("verdict: agreed\r\ncommit reviewed: x\r\n", "agreed"),
    ("  Verdict: Changes Requested\n1. a.py:3 bug", "changes requested"),
    ("**verdict:** agreed", "unparsed"),
    ("> verdict: changes requested", "unparsed"),
    ("verdict: agreed\nverdict: agreed", "agreed"),
    ("verdict: agreed with remarks", "unparsed"),
    ("I agree with this change.", "unparsed"),
    ("verdict: maybe", "unparsed"),
    ("no verdict line here\nfindings: verdict: agreed is mentioned mid-line", "unparsed"),
    ("", "unparsed"),
])
def test_review_verdict(message, expected):
    assert review_verdict(message) == expected


def test_conflicting_verdict_lines_are_unparsed():
    assert review_verdict("verdict: changes requested\nverdict: agreed") == "unparsed"


def test_integration_gate_reads_the_status_header(tmp_path):
    sha = "a" * 40
    for verdict, gate in (("agreed", "agreed"), ("changes requested", "changes requested"), ("unparsed", "unparsed")):
        path = tmp_path / f"review-{verdict.replace(' ', '-')}.md"
        write_review(path, [("status", verdict), ("subtask", "A"), ("agent", "codex-w1"), ("commit", sha)],
                     f"verdict: {verdict}\n", "codex-w1")
        assert ws.review_commit(path) == (gate, sha)


def test_status_is_the_first_header_line(tmp_path):
    path = tmp_path / "review-A-codex-w1.md"
    write_review(path, [("status", "agreed"), ("subtask", "A"), ("agent", "codex-w1")], "verdict: agreed\n", "codex-w1")
    assert path.read_text(encoding="utf-8").splitlines()[0] == "status: agreed"
