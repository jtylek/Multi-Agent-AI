"""Common grammar (formats.md section 1) and atomic writes."""

import os

import pytest

from multi_agent_ai import document
from multi_agent_ai.document import DocumentError, atomic_write, parse_bytes, parse_text, render


def test_bom_is_stripped_and_crlf_and_lone_cr_are_normalised():
    doc = parse_bytes(b"\xef\xbb\xbfid: q7\r\nfrom: a\rto: b\r\n\r\nline 1\r\nline 2\r\n")
    assert doc.headers == {"id": "q7", "from": "a", "to": "b"}
    assert doc.body == "line 1\nline 2"
    assert doc.warnings == []


def test_invalid_utf8_is_an_error_never_guessed():
    with pytest.raises(DocumentError):
        parse_bytes(b"id: x\nfrom: \xff\n\nbody\n")


def test_header_ends_at_first_blank_line():
    doc = parse_text("id: a\n\nbody: not a header\n\nmore\n")
    assert doc.headers == {"id": "a"}
    assert doc.body == "body: not a header\n\nmore"
    assert doc.warnings == []


def test_non_matching_line_ends_header_with_warning():
    doc = parse_text("id: a\nthis is text\nfrom: b\n\nrest\n")
    assert doc.headers == {"id": "a"}
    assert doc.body.startswith("this is text\nfrom: b")
    assert len(doc.warnings) == 1 and "line 2" in doc.warnings[0]


def test_formats_example_missing_blank_separator_parses_note_as_header():
    doc = parse_text("id: a\nfrom: b\nto: c\nNote: please ack\n")
    assert doc.headers["note"] == "please ack"
    assert doc.body == ""
    assert any("missing blank separator" in w for w in doc.warnings)


def test_column_aligned_values_and_trailing_whitespace():
    doc = parse_text("agent:    codex   \nstatus:\tagreed\n\n")
    assert doc.headers == {"agent": "codex", "status": "agreed"}


def test_keys_are_case_insensitive():
    assert parse_text("ID: m1\nFrom: a\n\nx\n").headers == {"id": "m1", "from": "a"}


def test_repeated_key_first_wins_with_warning():
    doc = parse_text("id: first\nid: second\n\nx\n")
    assert doc.headers["id"] == "first"
    assert doc.repeated == {"id": ["second"]}
    assert any("repeated key 'id'" in w for w in doc.warnings)


def test_unknown_keys_are_preserved():
    assert parse_text("id: a\nx-future: 1\n\nb\n").headers["x-future"] == "1"


def test_render_is_canonical():
    text = render([("id", "m1"), ("from", "a")], "Body\r\nline\n\n")
    assert text == "id: m1\nfrom: a\n\nBody\nline\n"
    assert render([("id", "m1")]) == "id: m1\n\n"
    with pytest.raises(ValueError):
        render([("id", "a\nb")])
    with pytest.raises(ValueError):
        render([("ID", "a")])


def test_atomic_write_is_utf8_without_bom_and_leaves_no_temp(tmp_path):
    target = tmp_path / "sub" / "f.md"
    atomic_write(target, "id: é\n\n")
    assert target.read_bytes() == "id: é\n\n".encode("utf-8")
    assert [p.name for p in target.parent.iterdir()] == ["f.md"]


def test_atomic_write_retries_when_windows_locks_the_target(tmp_path, monkeypatch):
    real_replace, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError("locked")
        real_replace(src, dst)

    monkeypatch.setattr(document.os, "replace", flaky)
    monkeypatch.setattr(document.time, "sleep", lambda s: None)
    atomic_write(tmp_path / "f.md", "x\n")
    assert len(calls) == 3 and (tmp_path / "f.md").read_text() == "x\n"


def test_atomic_write_gives_up_and_cleans_up(tmp_path, monkeypatch):
    def locked(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(document.os, "replace", locked)
    monkeypatch.setattr(document.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        atomic_write(tmp_path / "f.md", "x\n")
    assert list(tmp_path.iterdir()) == []
