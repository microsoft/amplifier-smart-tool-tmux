"""The deny-by-default write fence and the append-only audit log: both write
verbs (send, create) refuse without --confirmed, and every attempt -- refused or
delivered -- lands in the log."""

from __future__ import annotations

import pytest

from tmux_fleet import audit, creation, fleet
from _helpers import fleet as make_fleet
from _helpers import run


def _audit_records():
    recs, _unparsed = audit.read_records()
    return recs


def test_send_without_confirmed_refuses_and_audits():
    async def scenario():
        async with make_fleet("alpha") as (_srv, kw):
            with pytest.raises(fleet.FleetError) as ei:
                await fleet.send_input("alpha", text="echo x", submit=True, confirmed=False, **kw)
            return str(ei.value)

    msg = run(scenario())
    assert msg.startswith("REFUSED")
    assert "--confirmed" in msg
    recs = _audit_records()
    assert any(r.get("action") == "send" and r.get("outcome") == "refused" for r in recs)


def test_send_with_confirmed_delivers_and_audits_outcome():
    async def scenario():
        async with make_fleet("alpha") as (_srv, kw):
            return await fleet.send_input(
                "alpha", text="echo hello_marker", submit=True, confirmed=True, **kw
            )

    res = run(scenario())
    assert res["delivered"] is True
    assert res["outcome"] in ("submitted", "armed", "uncertain")
    assert res["submitted"] is True  # --submit sends exactly one Enter
    recs = _audit_records()
    delivered = [r for r in recs if r.get("action") == "send" and r.get("outcome") == "delivered"]
    assert delivered and "submission_outcome" in delivered[-1]


def test_send_unknown_session_refuses_with_named_socket():
    async def scenario():
        async with make_fleet("real") as (_srv, kw):
            with pytest.raises(fleet.FleetError) as ei:
                await fleet.send_input("ghost", text="x", confirmed=True, **kw)
            return str(ei.value)

    msg = run(scenario())
    assert "ghost" in msg and "REFUSED" in msg


def test_send_submit_with_key_is_refused():
    async def scenario():
        async with make_fleet("alpha") as (_srv, kw):
            with pytest.raises(fleet.FleetError) as ei:
                await fleet.send_input("alpha", key="Enter", submit=True, confirmed=True, **kw)
            return str(ei.value)

    msg = run(scenario())
    assert "--submit applies to --text" in msg


def test_send_paste_with_key_is_refused_and_audited_before_contact(monkeypatch):
    contacted = False

    async def unexpected_contact(*args: str) -> str:
        nonlocal contacted
        contacted = True
        raise AssertionError("invalid --paste --key must not contact tmux")

    monkeypatch.setattr(fleet, "run_tmux_scoped", unexpected_contact)
    with pytest.raises(fleet.FleetError) as ei:
        run(fleet.send_input("alpha", key="Enter", paste=True, confirmed=True))

    msg = str(ei.value)
    assert "--paste applies to --text" in msg
    assert contacted is False
    recs = _audit_records()
    assert any(
        r.get("action") == "send"
        and r.get("outcome") == "refused"
        and r.get("reason") == "--paste with --key"
        for r in recs
    )


def test_multiline_text_without_paste_refuses_before_input(monkeypatch):
    input_operations: list[tuple[str, ...]] = []

    async def unexpected_input(*args: str) -> str:
        input_operations.append(args)
        raise AssertionError("multiline refusal must not contact tmux")

    monkeypatch.setattr(fleet, "run_tmux_scoped", unexpected_input)

    with pytest.raises(fleet.FleetError) as ei:
        run(
            fleet.send_input(
                "alpha", text="first\r\nsecond\n", confirmed=True, submit=True
            )
        )

    assert "Nothing was delivered" in str(ei.value)
    assert "--paste" in str(ei.value)
    assert input_operations == []
    recs = _audit_records()
    assert any(
        r.get("action") == "send"
        and r.get("outcome") == "refused"
        and r.get("reason") == "multiline --text without --paste"
        for r in recs
    )


def test_create_without_confirmed_refuses_and_audits():
    async def scenario():
        async with make_fleet() as (_srv, kw):
            with pytest.raises(creation.CreateRefused) as ei:
                await creation.create_session("newbie", confirmed=False, **kw)
            return str(ei.value)

    msg = run(scenario())
    assert msg.startswith("REFUSED")
    recs = _audit_records()
    assert any(r.get("action") == "create" and r.get("outcome") == "refused" for r in recs)


def test_create_with_confirmed_creates_then_collision_refuses():
    async def scenario():
        async with make_fleet() as (_srv, kw):
            first = await creation.create_session("scratch", confirmed=True, **kw)
            # a second create under the same name refuses -- never reuses
            with pytest.raises(creation.CreateRefused) as ei:
                await creation.create_session("scratch", confirmed=True, **kw)
            return first, str(ei.value)

    first, collision = run(scenario())
    assert first["created"] is True and first["session"] == "scratch"
    assert "already exists" in collision
    recs = _audit_records()
    assert any(r.get("action") == "create" and r.get("outcome") == "created" for r in recs)


def test_create_rejects_unstable_name():
    async def scenario():
        async with make_fleet() as (_srv, kw):
            with pytest.raises(creation.CreateRefused) as ei:
                await creation.create_session("build.js", confirmed=True, **kw)
            return str(ei.value)

    msg = run(scenario())
    assert "." in msg and "REFUSED" in msg
