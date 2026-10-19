"""Usage-limit detection, reset parsing and the quota blocker rules (T15).

All samples are SYNTHETIC fixtures (no real limit was provoked). The Claude sample follows the
single observed subagent event described in notes/claude-50-quota-input.md; the Codex and Copilot
shapes are the documented field names / plausible messages, to be replaced by recorded real
output when one exists.
"""

import json
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from multi_agent_ai.board import check_status
from multi_agent_ai.models import Status
from multi_agent_ai.workers import failures as fc
from multi_agent_ai.workers import quota

@pytest.fixture(autouse=True)
def versioned_tables(monkeypatch):
    """Pretend every detector table lists version "t1" (the shipped tables list none yet)."""
    for provider, (detector, _) in quota.DETECTORS.items():
        monkeypatch.setitem(quota.DETECTORS, provider, (detector, frozenset({"t1"})))


def detect(provider, stdout, stderr, **kw):
    kw.setdefault("cli_version", "t1")
    return quota.detect(provider, stdout, stderr, **kw)


NOW = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)
BUDAPEST = timezone(timedelta(hours=2), "Europe/Budapest")  # CEST on this date; stands in for the tz database


def lookup(name: str) -> tzinfo | None:
    return {"Europe/Budapest": BUDAPEST, "UTC": timezone.utc}.get(name)


def codex_limit(reached="primary", resets=1792000000, spend=False):
    event = {"type": "token_count", "rate_limits": {
        "rate_limit_reached_type": reached, "spend_control_reached": spend,
        "primary": {"used_percent": 100, "resets_at": resets},
        "secondary": {"used_percent": 40, "resets_at": 1792600000}}}
    return json.dumps(event) + "\n"


CLAUDE_LIMIT = ("Agent terminated early due to an API error: You've hit your session limit · resets 9:10pm "
                "(Europe/Budapest) (error type rate_limit, HTTP 429, request id req_1)")


# --- reset parsing ---------------------------------------------------------------------------


def test_wall_clock_reset_today_and_next_day():
    reset, zone = quota.parse_wall_clock("resets 9:10pm (Europe/Budapest)", NOW, lookup)
    assert zone == "Europe/Budapest" and reset == datetime(2026, 10, 19, 21, 10, tzinfo=BUDAPEST)
    # 6pm UTC is 8pm Budapest: a reset at 9am already passed today, so it is tomorrow
    reset, _ = quota.parse_wall_clock("resets 9am (Europe/Budapest)", NOW, lookup)
    assert reset == datetime(2026, 10, 20, 9, 0, tzinfo=BUDAPEST)


def test_wall_clock_24h_and_noon_midnight():
    assert quota.parse_wall_clock("resets 21:10 (UTC)", NOW, lookup)[0] == datetime(
        2026, 10, 19, 21, 10, tzinfo=timezone.utc)
    assert quota.parse_wall_clock("resets 12am (UTC)", NOW, lookup)[0].hour == 0
    assert quota.parse_wall_clock("resets 12pm (UTC)", NOW, lookup)[0].hour == 12


@pytest.mark.parametrize("text", [
    "no reset here", "resets 13pm (UTC)", "resets 9:75pm (UTC)", "resets 9pm (Nowhere/Land)"])
def test_unparseable_reset_is_unknown_never_guessed(text):
    assert quota.parse_wall_clock(text, NOW, lookup) is None


def test_missing_tz_database_means_unknown(monkeypatch):
    import zoneinfo

    def boom(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", boom)
    assert quota.parse_wall_clock("resets 9:10pm (Europe/Budapest)", NOW) is None


def test_epoch_seconds_and_milliseconds():
    assert quota.from_epoch(1792000000)[0] == datetime.fromtimestamp(1792000000, timezone.utc)
    assert quota.from_epoch(1792000000000)[0] == datetime.fromtimestamp(1792000000, timezone.utc)
    assert quota.from_epoch("soon") is None and quota.from_epoch(True) is None


# --- detectors -------------------------------------------------------------------------------


def test_codex_structured_limit_with_reported_reset():
    found = detect("codex", codex_limit(), "", now=NOW)
    assert found.kind == quota.QUOTA_EXHAUSTED and found.reason == "rate_limit_reached_type=primary"
    assert found.reset == datetime.fromtimestamp(1792000000, timezone.utc)
    assert found.reset_text.startswith("resets 2026-") and found.reset_text.endswith("UTC (reported)")


def test_codex_spend_control_without_a_window_has_unknown_reset():
    found = detect("codex", codex_limit(reached=None, spend=True), "", now=NOW)
    assert found.kind == quota.QUOTA_EXHAUSTED and found.reset is None and found.reset_text == "reset unknown"


def test_codex_rate_limits_below_the_limit_are_not_a_limit():
    assert detect("codex", codex_limit(reached=None), "", now=NOW) is None


def test_codex_prose_without_structured_fields_is_not_quota():
    assert detect("codex", "", "usage limit hit, try later", now=NOW) is None


def test_claude_requires_all_three_signals():
    found = detect("claude", "", CLAUDE_LIMIT, now=NOW, zone_lookup=lookup)
    assert found.kind == quota.QUOTA_EXHAUSTED and found.reset == datetime(2026, 10, 19, 21, 10, tzinfo=BUDAPEST)
    assert "session limit" in found.raw_message
    bare = detect("claude", "", "API error 429 rate_limit: slow down", now=NOW)
    assert bare.kind == quota.PROVIDER_ERROR  # 429 alone is throttling, not exhaustion


def test_copilot_cap_vs_quota_vs_throttle():
    cap = detect("copilot", "", "Stopped: --max-ai-credits 5 reached", now=NOW)
    assert cap.kind == quota.CAP_REACHED and cap.reset is None
    exhausted = detect("copilot", "", "You've reached your monthly premium requests quota.", now=NOW)
    assert exhausted.kind == quota.QUOTA_EXHAUSTED
    assert detect("copilot", "", "HTTP 429 Too Many Requests", now=NOW).kind == quota.PROVIDER_ERROR
    assert detect("copilot", "", "something broke", now=NOW) is None


def test_unlisted_or_unknown_cli_version_falls_back_to_provider_error():
    listed = detect("codex", codex_limit(), "", now=NOW)
    assert listed.kind == quota.QUOTA_EXHAUSTED
    odd = detect("codex", codex_limit(), "", now=NOW, cli_version="9.9.9")
    assert odd.kind == quota.PROVIDER_ERROR and "9.9.9" in odd.reason and odd.raw_message
    unknown = detect("codex", codex_limit(), "", now=NOW, cli_version=None)
    assert unknown.kind == quota.PROVIDER_ERROR and "unknown" in unknown.reason and unknown.reset is None
    assert detect("claude", "", CLAUDE_LIMIT, now=NOW, cli_version=None).kind == quota.PROVIDER_ERROR
    assert detect("copilot", "", "--max-ai-credits 5 reached", now=NOW, cli_version=None).kind == quota.PROVIDER_ERROR


def test_shipped_tables_trust_no_version_yet(monkeypatch):
    monkeypatch.undo()
    assert all(versions is None for _, versions in quota.DETECTORS.values())
    assert quota.detect("codex", codex_limit(), "", now=NOW, cli_version="0.163.0").kind == quota.PROVIDER_ERROR


def test_unknown_provider_still_reports_throttling_only():
    assert detect("future", "", "429 Too Many Requests", now=NOW).kind == quota.PROVIDER_ERROR
    assert detect("future", "", "plain failure", now=NOW) is None


# --- classification -----------------------------------------------------------------------------


@pytest.mark.parametrize("limit, exit_code, stderr, expected", [
    (quota.QUOTA_EXHAUSTED, 1, "", fc.QUOTA_EXHAUSTED),
    (quota.CAP_REACHED, 1, "", fc.CAP_REACHED),
    (quota.QUOTA_EXHAUSTED, 1, "401 Unauthorized", fc.QUOTA_EXHAUSTED),  # positive limit evidence wins
    (quota.PROVIDER_ERROR, 1, "", fc.PROVIDER_ERROR),
    (quota.PROVIDER_ERROR, 1, "401 Unauthorized", fc.AUTH),  # a refined class beats a generic throttle
    (None, 1, "something", fc.NONZERO_EXIT),
    (quota.QUOTA_EXHAUSTED, 0, "", None),  # exit 0 is never reclassified
])
def test_classify_with_limit(limit, exit_code, stderr, expected):
    assert fc.classify(fc.Evidence(mode="review", exit_code=exit_code, stderr=stderr, limit=limit)) == expected


def test_timeout_beats_limit():
    evidence = fc.Evidence(mode="review", exit_code=1, timed_out=True, limit=quota.QUOTA_EXHAUSTED)
    assert fc.classify(evidence) == fc.TIMEOUT


# --- blocker text and the stale-worker suspension ---------------------------------------------


def test_blocker_text_matches_formats_md():
    finding = quota.LimitFinding(quota.QUOTA_EXHAUSTED, "claude", "raw",
                                 datetime(2026, 10, 19, 21, 10, tzinfo=BUDAPEST), "Europe/Budapest")
    assert quota.blocker_text(finding, "claude-opus-5-5") == (
        "quota: claude (claude-opus-5-5), resets 2026-10-19 21:10 Europe/Budapest (reported)")
    unknown = quota.LimitFinding(quota.QUOTA_EXHAUSTED, "codex", "raw")
    assert quota.blocker_text(unknown, None) == "quota: codex (provider default), reset unknown"
    cap = quota.LimitFinding(quota.CAP_REACHED, "copilot", "raw")
    assert quota.blocker_text(cap, "m").startswith("cap: copilot (m), ")


def test_quota_block_active_until_reported_reset():
    blockers = "quota: claude (m), resets 2026-10-19 21:10 Europe/Budapest (reported)"
    updated = datetime(2026, 10, 19, 14, 0)
    before_reset = datetime(2026, 10, 19, 20, 0, tzinfo=BUDAPEST)
    after_reset = datetime(2026, 10, 19, 21, 30, tzinfo=BUDAPEST)
    assert quota.quota_block_active(blockers, updated, before_reset, lookup)
    assert not quota.quota_block_active(blockers, updated, after_reset, lookup)


def test_quota_block_with_unknown_reset_holds_24_hours():
    blockers = "quota: codex (m), reset unknown"
    updated = datetime(2026, 10, 19, 10, 0)
    assert quota.quota_block_active(blockers, updated, datetime(2026, 10, 20, 9, 59))
    assert not quota.quota_block_active(blockers, updated, datetime(2026, 10, 20, 10, 1))


def test_cap_and_plain_blockers_never_suspend_staleness():
    updated, now = datetime(2026, 10, 19, 10, 0), datetime(2026, 10, 19, 11, 0)
    assert not quota.quota_block_active("cap: copilot (m), reset unknown", updated, now)
    assert not quota.quota_block_active("waiting for review", updated, now)


def status(blockers, state="blocked", updated="2026-10-19 10:00"):
    return Status(agent_id="codex-w1", plan_revision=1, subtask="T1", state=state, last_commit="none",
                  tests="none", blockers=blockers, updated=updated)


def test_check_status_does_not_call_a_quota_blocked_worker_stale():
    now = datetime(2026, 10, 19, 15, 0)  # five hours after the update: normally stale
    _, warnings = check_status(status("quota: codex (m), reset unknown"), None, now)
    assert any("not stale" in w for w in warnings) and not any("stale worker" in w for w in warnings)
    _, warnings = check_status(status("quota: codex (m), reset unknown"), None, datetime(2026, 10, 20, 11, 0))
    assert any("stale worker" in w for w in warnings)
    _, warnings = check_status(status("cap: copilot (m), reset unknown"), None, now)
    assert any("stale worker" in w for w in warnings)
    _, warnings = check_status(status("other blocker"), None, now)
    assert any("stale worker" in w for w in warnings)
