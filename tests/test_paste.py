"""Regression coverage for explicit buffered paste.

The upstream primitive is mocked here because this lane deliberately does not
wait for the parallel tmux-kit release. The fleet and socket are real, unique
isolated test fixtures.
"""

from __future__ import annotations

import sys
import types

import pytest

from tmux_fleet import fleet
from tmux_fleet import audit
from _helpers import fleet as make_fleet
from _helpers import run


def test_paste_passes_unchanged_unicode_crlf_and_trailing_newline(monkeypatch):
    received = []

    async def fake_paste(pane_id, text, *, socket_path):
        received.append((pane_id, text, socket_path))

    monkeypatch.setattr(fleet, "_paste_text", fake_paste)
    payload = "first\r\ncafé\n最後\r\n"

    async def scenario():
        async with make_fleet("alpha") as (_srv, kw):
            return await fleet.send_input(
                "alpha", text=payload, paste=True, confirmed=True, **kw
            )

    result = run(scenario())
    assert result["outcome"] == "armed"
    assert result["sent"] == {"text": payload, "paste": True}
    assert result["enter_key_events"] == 0
    assert len(received) == 1
    pane_id, pasted, socket_path = received[0]
    assert pane_id == result["pane_id"]
    assert pasted == payload
    assert socket_path == result["socket"]["server_socket_path"]


def test_paste_adapter_calls_the_upstream_native_primitive(monkeypatch):
    received = []
    module = types.ModuleType("tmux_kit.paste")

    async def paste_text(pane_id, text, *, socket_path):
        received.append((pane_id, text, socket_path))

    module.paste_text = paste_text
    monkeypatch.setitem(sys.modules, "tmux_kit.paste", module)
    run(fleet._paste_text("%42", "a\r\nβ\n", socket_path="/tmp/tmux.sock"))
    assert received == [("%42", "a\r\nβ\n", "/tmp/tmux.sock")]


def test_upstream_paste_validation_refuses_and_audits(monkeypatch):
    async def rejected_paste(*_args, **_kwargs):
        raise ValueError("NUL is not allowed")

    monkeypatch.setattr(fleet, "_paste_text", rejected_paste)

    async def scenario():
        async with make_fleet("alpha") as (_srv, kw):
            with pytest.raises(fleet.FleetError) as ei:
                await fleet.send_input(
                    "alpha", text="safe\ntext", paste=True, confirmed=True, **kw
                )
            return str(ei.value)

    assert "tmux-kit rejected" in run(scenario())
    records, _ = audit.read_records()
    assert any(
        record.get("action") == "send"
        and record.get("outcome") == "refused"
        and record.get("reason") == "tmux-kit paste validation"
        for record in records
    )


def test_paste_readback_failure_is_audited_and_emits_a_fleet_error(monkeypatch):
    read_count = 0

    async def fake_paste(*_args, **_kwargs):
        return None

    async def failing_second_read(*_args, **_kwargs):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return ""
        raise RuntimeError("capture-pane disconnected")

    monkeypatch.setattr(fleet, "_paste_text", fake_paste)
    monkeypatch.setattr(fleet, "_read_pane_text", failing_second_read)

    async def scenario():
        async with make_fleet("alpha") as (_srv, kw):
            with pytest.raises(fleet.FleetError) as ei:
                await fleet.send_input(
                    "alpha", text="safe\ntext", paste=True, confirmed=True, **kw
                )
            return str(ei.value)

    assert "paste delivery is uncertain" in run(scenario())
    records, _ = audit.read_records()
    assert any(
        record.get("action") == "send"
        and record.get("outcome") == "uncertain"
        and record.get("paste_called") is True
        and "after paste" in record.get("reason", "")
        for record in records
    )


def test_paste_submit_targets_the_original_pane_after_active_switch(monkeypatch):
    paste_calls = []
    tmux_calls = []
    original_run = fleet.run_tmux_scoped

    async def recording_run(*args):
        tmux_calls.append(args)
        return await original_run(*args)

    async def switch_active_pane(pane_id, text, *, socket_path):
        paste_calls.append((pane_id, text, socket_path))
        # Switch session focus after the paste. The final Enter must still
        # target pane_id, not re-resolve the new active pane.
        await server.run("select-pane", "-t", other_pane_id)

    monkeypatch.setattr(fleet, "run_tmux_scoped", recording_run)
    monkeypatch.setattr(fleet, "_paste_text", switch_active_pane)

    async def scenario():
        nonlocal server, other_pane_id
        async with make_fleet("alpha") as (server, kw):
            await server.run("split-window", "-t", "alpha", "-d")
            pane_rows = await server.run(
                "list-panes", "-t", "=alpha", "-F", "#{pane_active}\t#{pane_id}"
            )
            active_pane_id = next(
                line.split("\t", 1)[1]
                for line in pane_rows.splitlines()
                if line.startswith("1\t")
            )
            other_pane_id = next(
                line.split("\t", 1)[1]
                for line in pane_rows.splitlines()
                if not line.startswith("1\t")
            )
            result = await fleet.send_input(
                "alpha", text="one\r\ntwo\n", paste=True, submit=True,
                confirmed=True, **kw
            )
            return result, active_pane_id

    server = None
    other_pane_id = ""
    result, initially_active = run(scenario())
    assert paste_calls[0][0] == initially_active == result["pane_id"]
    enter_calls = [args for args in tmux_calls if args and args[-1] == "Enter"]
    assert len(enter_calls) == 1
    assert initially_active in enter_calls[0]
    assert result["outcome"] == "uncertain"
    assert result["enter_key_events"] == 1