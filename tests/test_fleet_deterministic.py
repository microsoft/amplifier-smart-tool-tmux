"""Deterministic verbs against an isolated fixture fleet, with amplifier-agent
present but ALL provider env scrubbed -- proving the deterministic paths need no
AI provider configured at all (cli.v1 core rule 2)."""

from __future__ import annotations

import pytest

from tmux_fleet import diagnostics, fleet
from _helpers import fleet as make_fleet
from _helpers import run, scrub_providers


@pytest.fixture(autouse=True)
def no_providers(monkeypatch):
    scrub_providers(monkeypatch)
    yield


def test_sessions_lists_the_fleet_and_confirms_socket(monkeypatch):
    # Simulate running from inside an attached tmux pane, so the "ambient $TMUX
    # was ignored" assertion is HERMETIC. It must not depend on whether the test
    # runner itself happens to be inside tmux -- in a clean CI/container/sandbox
    # it is not, and TMUX is unset there, which would make ambient_ignored False
    # and fail this assertion for an environment reason rather than a tool
    # defect. Setting $TMUX here exercises exactly what the tool must do: resolve
    # its own socket and report the ambient value as seen-and-ignored.
    monkeypatch.setenv("TMUX", "/tmp/not-our-pane/default,1,0")

    async def scenario():
        async with make_fleet("alpha", "beta") as (_srv, kw):
            return await fleet.list_sessions(**kw)

    result = run(scenario())
    assert result["server_running"] is True
    names = {r["session"] for r in result["sessions"]}
    assert {"alpha", "beta"} <= names
    assert result["counts"]["session_count"] == len(result["sessions"])
    # the scope claim is confirmed by tmux itself
    assert result["socket"]["socket_path_confirmed_by_tmux"] is True
    assert result["_completeness"]["complete"] is True
    # ambient $TMUX (set above) was seen and deliberately ignored
    assert result["socket"]["ambient_ignored"] is True


def test_sessions_sorted_by_recency_not_alphabetical(monkeypatch):
    """The owner's report -- "alphabetical repeats the same long-
    stale sessions" -- sort by most-recent activity instead. `alpha` is
    alphabetically first but the STALEST; `zulu` is alphabetically last but
    the MOST recently active. The listing must lead with `zulu`, never
    `alpha`, proving the order tracks activity and not the name."""

    async def fake_enumerate():
        return (
            ["alpha", "mike", "zulu"],
            {"alpha": 1000.0, "mike": 2000.0, "zulu": 3000.0},
            {"alpha": 1000.0, "mike": 2000.0, "zulu": 3000.0},
            {},
        )

    monkeypatch.setattr(fleet, "_enumerate_sessions_scoped", fake_enumerate)

    async def scenario():
        async with make_fleet("anchor") as (_srv, kw):
            return await fleet.list_sessions(**kw)

    result = run(scenario())
    ordered = [r["session"] for r in result["sessions"]]
    assert ordered == ["zulu", "mike", "alpha"]


def test_sessions_with_unknown_activity_sort_last_by_name(monkeypatch):
    """A session this tool cannot date must never be presented as the most
    recent -- it sorts after every session with real activity data, and the
    unknowns among themselves fall back to the name tiebreak."""

    async def fake_enumerate():
        return (
            ["known", "mystery-b", "mystery-a"],
            {"known": 5000.0},  # the mystery-* sessions have no activity entry
            {},
            {},
        )

    monkeypatch.setattr(fleet, "_enumerate_sessions_scoped", fake_enumerate)

    async def scenario():
        async with make_fleet("anchor") as (_srv, kw):
            return await fleet.list_sessions(**kw)

    result = run(scenario())
    ordered = [r["session"] for r in result["sessions"]]
    assert ordered == ["known", "mystery-a", "mystery-b"]


def test_socket_status_reports_running_and_counts():
    async def scenario():
        async with make_fleet("only") as (_srv, kw):
            return await fleet.socket_status(**kw)

    st = run(scenario())
    assert st["server_running"] is True
    assert st["session_count"] == 1
    assert st["socket"]["socket_path_confirmed_by_tmux"] is True


def test_read_returns_pane_and_completeness_block():
    async def scenario():
        async with make_fleet("work") as (_srv, kw):
            return await fleet.read_session("work", lines=40, **kw)

    r = run(scenario())
    assert r["session"] == "work"
    assert "pane" in r
    assert set(r["at_prompt"] for _ in [0]) <= {r["at_prompt"]}
    assert r["at_prompt"] in ("yes", "no", "uncertain")
    assert "complete" in r["_completeness"]


def test_read_unknown_session_names_the_socket():
    async def scenario():
        async with make_fleet("real") as (_srv, kw):
            with pytest.raises(fleet.FleetError) as ei:
                await fleet.read_session("ghost", **kw)
            return str(ei.value)

    msg = run(scenario())
    assert "ghost" in msg
    assert "socket" in msg.lower()  # names where it looked


def test_attention_carries_socket_and_buckets():
    async def scenario():
        async with make_fleet("a", "b") as (_srv, kw):
            return await fleet.attention(**kw)

    roll = run(scenario())
    assert roll["server_running"] is True
    assert "socket" in roll
    assert set(roll["bucket_counts"]).issuperset({"working", "parked_at_prompt_quiet"})
    assert roll["_completeness"]["ranking_is_heuristic"] is True


def test_exit_code_running_session_is_null_with_reason():
    async def scenario():
        async with make_fleet("live") as (_srv, kw):
            return await diagnostics.exit_code("live", **kw)

    ec = run(scenario())
    assert ec["status"] == "running"
    assert ec["exit_code"] is None
    assert ec["pane_dead"] is False


def test_doctor_reports_ready():
    async def scenario():
        async with make_fleet("x") as (_srv, kw):
            return await diagnostics.doctor(**kw)

    doc = run(scenario())
    assert doc["ok"] is True
    names = {c["name"]: c["ok"] for c in doc["checks"]}
    assert names["tmux_present"] is True
    assert names["socket_dir_writable"] is True
    assert doc["server_running"] is True


def test_empty_fleet_says_where_it_looked():
    async def scenario():
        async with make_fleet() as (_srv, kw):  # server up, no sessions
            return await fleet.list_sessions(**kw)

    result = run(scenario())
    assert result["sessions"] == []
    assert result["saw_nothing"] is True
    assert "saw nothing" in result["saw_nothing_note"]
