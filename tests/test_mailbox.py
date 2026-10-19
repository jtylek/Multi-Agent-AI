"""request.md and ack-<agent-id>.md (formats.md sections 2 and 3), D5 and D8."""

import pytest

from multi_agent_ai.document import OwnershipError, parse_bytes, parse_text
from multi_agent_ai.mailbox import (
    evaluate_agreement,
    parse_ack,
    parse_request,
    read_acks,
    read_request,
    render_ack,
    render_request,
    request_revision,
    write_ack,
    write_request,
)
from multi_agent_ai.models import Ack, Parsed, Request


def req(text, directory=None):
    return parse_request(parse_text(text), directory)


def ack(text, directory=None, file_agent=None):
    return parse_ack(parse_text(text), directory, file_agent)


# --- formats.md section 2: valid requests ----------------------------------------------


def test_request_valid_example_1():
    p = req("id: 20261019-m0-kickoff\nfrom: claude-a7\nto: claude-50, codex, copilot\n\nKickoff text.\n",
            "20261019-m0-kickoff")
    assert p.ok and p.warnings == []
    assert p.value.to == ("claude-50", "codex", "copilot")
    assert p.value.body == "Kickoff text."


def test_request_valid_example_2_broadcast():
    p = req("id: fix-1\nfrom: codex-a91e\nto: broadcast\n\nFYI, test lock released.\n", "fix-1")
    assert p.ok and p.value.is_broadcast and p.warnings == []


def test_request_valid_example_3_bom_crlf_plan_revision():
    raw = "﻿id: q7\r\nfrom: copilot-e19f\r\nto: claude-5e90\r\nplan-revision: 3\r\n\r\nQuestion about plan revision 3.\r\n"
    p = parse_request(parse_bytes(raw.encode("utf-8")), "q7")
    assert p.ok and p.warnings == []
    assert p.value.plan_revision == 3 and p.value.to == ("claude-5e90",)


# --- formats.md section 2: invalid requests --------------------------------------------


def test_request_invalid_1_missing_to():
    p = req("id: x\nfrom: a\n\nbody\n")
    assert not p.ok and "missing required key 'to'" in p.errors


def test_request_invalid_2_empty_recipient_item():
    p = req("id: x\nfrom: a\nto: a,, b\n\nbody\n")
    assert not p.ok and any("empty recipient" in e for e in p.errors)


def test_request_invalid_3_id_mismatch_and_unsafe_id():
    assert any("does not match its directory" in e for e in req("id: m2\nfrom: a\nto: b\n\nx\n", "m1").errors)
    assert any("unsafe" in e for e in req("id: ../x\nfrom: a\nto: b\n\nx\n").errors)


def test_request_other_rules():
    assert not req("id: x\nfrom: a\nto: b\n\n\n").ok  # body required
    assert not req("id: x\nfrom: a\nto: b, broadcast\n\nx\n").ok  # exactly broadcast
    assert not req("id: x\nfrom: a\nto: b\nplan-revision: v2\n\nx\n").ok
    unknown = req("id: x\nfrom: a\nto: b\nx-later: 1\n\nx\n")
    assert unknown.ok and unknown.value.extra == {"x-later": "1"}


def test_broadcast_asking_for_agreement_warns():
    p = req("id: b1\nfrom: a\nto: broadcast\n\nPlease reply agreed.\n")
    assert p.ok and any("broadcast asks for agreement" in w for w in p.warnings)
    agreement = evaluate_agreement(p.value, {"x": ack("id: b1\nagent: x\nstatus: agreed\n\n", "b1", "x")})
    assert agreement.all_agreed is None


# --- formats.md section 3: valid acks --------------------------------------------------


def test_ack_valid_example_1():
    p = ack("id: 20261019-m0-kickoff\nagent: claude-50 (Claude Code, Sonnet 5.5)\nstatus: agreed\n\nTaking T4.\n",
            "20261019-m0-kickoff", "claude-50")
    assert p.ok and p.value.agent_id == "claude-50" and p.value.product == "Claude Code, Sonnet 5.5"


def test_ack_valid_example_2_changes_requested_with_revision():
    p = ack("id: 20261019-m0-rev2\nagent: codex (OpenAI Codex)\nstatus: changes requested\nplan-revision: 2\n\n"
            "Revision 2 conflicts with my owned files.\n", "20261019-m0-rev2", "codex")
    assert p.ok and p.value.status == "changes requested" and p.value.plan_revision == 2


def test_ack_valid_example_3_status_case_insensitive_without_product():
    p = ack("id: m9\nagent: copilot-e19f\nstatus: RECEIVED\n", "m9", "copilot-e19f")
    assert p.ok and p.value.status == "received" and p.value.product is None
    assert p.warnings == []  # a body-less file needs no blank separator
    assert ack("id: m9\nagent: copilot-e19f\nstatus: RECEIVED\n\n", "m9", "copilot-e19f").warnings == []


# --- formats.md section 3: invalid acks ------------------------------------------------


def test_ack_invalid_1_unknown_status():
    assert not ack("id: m1\nagent: a\nstatus: ok\n\n").ok


def test_ack_invalid_2_file_identity_mismatch():
    p = ack("id: m1\nagent: claude-50\nstatus: agreed\n\n", "m1", "codex")
    assert not p.ok and any("possible forged ack" in e for e in p.errors)


def test_ack_invalid_3a_id_mismatch():
    assert not ack("id: m1\nagent: a\nstatus: agreed\n\n", "m2", "a").ok


def test_ack_invalid_3b_revision_request_ack_without_plan_revision():
    request = req("id: r\nfrom: c\nto: a\nplan-revision: 2\n\nPlease agree.\n").value
    state = evaluate_agreement(request, {"a": ack("id: r\nagent: a\nstatus: agreed\n\n", "r", "a")})
    assert state.recipients[0].state == "invalid" and not state.recipients[0].counts
    assert state.all_agreed is False


def test_changes_requested_without_body_warns():
    assert ack("id: m\nagent: a\nstatus: changes requested\n\n").warnings


# --- agreement (D5, D8) ----------------------------------------------------------------


def _acks(**status_by_agent):
    out = {}
    for agent, (status, revision) in status_by_agent.items():
        rev = f"plan-revision: {revision}\n" if revision else ""
        out[agent] = ack(f"id: r\nagent: {agent}\nstatus: {status}\n{rev}\n", "r", agent)
    return out


REV_REQUEST = Request(id="r", sender="c", to=("a", "b"), body="Plan revision 3 published.", plan_revision=3)


def test_all_agreed_requires_every_named_recipient_agreed_on_same_revision():
    assert evaluate_agreement(REV_REQUEST, _acks(a=("agreed", 3), b=("agreed", 3))).all_agreed is True
    assert evaluate_agreement(REV_REQUEST, _acks(a=("agreed", 3))).all_agreed is False  # b missing
    assert evaluate_agreement(REV_REQUEST, _acks(a=("agreed", 3), b=("received", 3))).all_agreed is False
    assert evaluate_agreement(REV_REQUEST, _acks(a=("agreed", 3), b=("changes requested", 3))).all_agreed is False


def test_older_revision_is_stale_and_newer_is_mismatch():
    state = evaluate_agreement(REV_REQUEST, _acks(a=("agreed", 2), b=("agreed", 4)))
    assert [r.state for r in state.recipients] == ["stale", "mismatch"]
    assert state.all_agreed is False


def test_non_revision_request_still_needs_same_revision_among_agreed_acks():
    request = Request(id="r", sender="c", to=("a", "b"), body="Hello.")
    assert evaluate_agreement(request, _acks(a=("agreed", None), b=("agreed", None))).all_agreed is True
    assert evaluate_agreement(request, _acks(a=("agreed", 2), b=("agreed", None))).all_agreed is False


def test_ack_from_unaddressed_agent_is_recorded_but_does_not_count():
    state = evaluate_agreement(REV_REQUEST, _acks(a=("agreed", 3), b=("agreed", 3), z=("agreed", 3)))
    assert state.all_agreed is True
    assert [(r.agent_id, r.counts) for r in state.unaddressed] == [("z", False)]


def test_forged_ack_does_not_count():
    forged = {"b": ack("id: r\nagent: a\nstatus: agreed\nplan-revision: 3\n\n", "r", "b")}
    state = evaluate_agreement(REV_REQUEST, {**_acks(a=("agreed", 3)), **forged})
    assert state.recipients[1].state == "invalid" and state.all_agreed is False


def test_request_concerns_a_revision_only_through_its_header():
    body = "Plan revision 3. Second review (plan revision 4)."
    assert request_revision(Request("r", "c", ("a",), body)) is None
    assert request_revision(Request("r", "c", ("a",), body, plan_revision=5)) == 5


def test_ack_to_request_without_header_is_valid_even_if_body_names_a_revision():
    # Real case: the kickoff request said "(revision 1)" in prose and had no header, so its
    # acks without plan-revision are valid (plan revision 11, decision 1).
    request = req("id: r\nfrom: c\nto: a\n\nKickoff of plan (revision 1).\n").value
    state = evaluate_agreement(request, {"a": ack("id: r\nagent: a\nstatus: agreed\n\n", "r", "a")})
    assert state.required_revision is None
    assert state.recipients[0].state == "agreed" and state.recipients[0].counts
    assert state.all_agreed is True


# --- files and writers -----------------------------------------------------------------


def test_round_trip_write_and_read(tmp_path):
    request = Request(id="m1", sender="lead", to=("w1", "w2"), body="Review please.", plan_revision=2)
    path = write_request(tmp_path, request, author="lead")
    assert path.read_bytes() == b"id: m1\nfrom: lead\nto: w1, w2\nplan-revision: 2\n\nReview please.\n"
    assert read_request(path.parent).value == request
    write_ack(tmp_path, Ack("m1", "w1", "agreed", product="Codex", plan_revision=2), author="w1")
    acks = read_acks(path.parent)
    assert acks["w1"].ok and acks["w1"].value.agent_field == "w1 (Codex)"
    assert (path.parent / "ack-w1.md").read_text(encoding="utf-8").splitlines()[2:4] == ["status: agreed", "plan-revision: 2"]


def test_writers_never_write_for_another_agent_or_overwrite(tmp_path):
    request = Request(id="m1", sender="lead", to=("w1",), body="x")
    with pytest.raises(OwnershipError):
        write_request(tmp_path, request, author="w1")
    write_request(tmp_path, request, author="lead")
    with pytest.raises(FileExistsError):
        write_request(tmp_path, request, author="lead")
    with pytest.raises(OwnershipError):  # the coordinator cannot ack for w1
        write_ack(tmp_path, Ack("m1", "w1", "agreed"), author="lead")
    with pytest.raises(OwnershipError):  # not addressed
        write_ack(tmp_path, Ack("m1", "w9", "agreed"), author="w9")
    write_ack(tmp_path, Ack("m1", "w1", "received"), author="w1")
    with pytest.raises(FileExistsError):
        write_ack(tmp_path, Ack("m1", "w1", "agreed"), author="w1")


def test_ack_writer_requires_the_request_revision(tmp_path):
    write_request(tmp_path, Request(id="m2", sender="lead", to=("w1",), body="x", plan_revision=5), author="lead")
    with pytest.raises(ValueError):
        write_ack(tmp_path, Ack("m2", "w1", "agreed"), author="w1")
    with pytest.raises(ValueError):
        write_ack(tmp_path, Ack("m2", "w1", "agreed", plan_revision=4), author="w1")
    write_ack(tmp_path, Ack("m2", "w1", "agreed", plan_revision=5), author="w1")


def test_render_rejects_invalid_records(tmp_path):
    with pytest.raises(ValueError):
        write_request(tmp_path, Request(id="../x", sender="a", to=("b",), body="x"), author="a")
    assert render_ack(Ack("m", "a", "received")) == "id: m\nagent: a\nstatus: received\n\n"
    assert render_request(Request("m", "a", ("broadcast",), "hi")).startswith("id: m\nfrom: a\nto: broadcast\n")


def test_parsed_ok_requires_value():
    assert not Parsed(None).ok
