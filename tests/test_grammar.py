"""Ids (D6), positive integers, timestamps and staleness (D7)."""

from datetime import datetime

import pytest

from multi_agent_ai.grammar import (
    format_timestamp,
    is_stale,
    is_valid_id,
    parse_positive_int,
    parse_timestamp,
)


@pytest.mark.parametrize("value", ["claude-a7", "codex-a91e", "T6", "20261019-m0-kickoff", "a.b_c", "x" * 128])
def test_valid_ids(value):
    assert is_valid_id(value)


@pytest.mark.parametrize("value", ["", "../x", "a..b", "-a", ".a", "a b", "a/b", "a\\b", "x" * 129, "a:b"])
def test_invalid_ids(value):
    assert not is_valid_id(value)


@pytest.mark.parametrize("value,expected", [("1", 1), ("10", 10), ("v2", None), ("0", None), ("2.5", None), ("-1", None), ("01", None)])
def test_positive_int(value, expected):
    assert parse_positive_int(value) == expected


@pytest.mark.parametrize("value,minute", [
    ("2026-10-19 18:02", True),
    ("2026-10-19", False),
    ("2026-10-19T18:02", True),
    ("2026-10-19T18:02:30", True),
])
def test_accepted_timestamps(value, minute):
    assert parse_timestamp(value).minute_precision is minute


@pytest.mark.parametrize("value", ["2026-10-19T18:02Z", "2026-10-19T18:02+02:00", "19.10.2026", "2026-10-19 18:02:30"])
def test_rejected_timestamps(value):
    with pytest.raises(ValueError):
        parse_timestamp(value)


def test_writer_format_is_local_minute_precision():
    assert format_timestamp(datetime(2026, 10, 19, 7, 5, 59)) == "2026-10-19 07:05"


def test_staleness_about_three_hours_minute_precision_only():
    now = datetime(2026, 10, 19, 21, 3)
    assert is_stale(parse_timestamp("2026-10-19 18:02"), now) is True
    assert is_stale(parse_timestamp("2026-10-19 18:03"), now) is False
    assert is_stale(parse_timestamp("2026-10-18"), now) is None
