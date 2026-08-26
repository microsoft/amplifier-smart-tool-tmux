"""Smart verbs: the model-backed path never lies about itself. With no working
amplifier-agent, triage/interpret fail saying exactly that + the remedy -- never
a silent deterministic fallback."""

from __future__ import annotations

import pytest

from tmux_fleet import agent, cli, smart
from _helpers import run

_NO_SOCKET = "/tmp/tmux-fleet-tests-no-such-socket-dir"


def test_extract_json_handles_fences_and_prose():
    assert smart._extract_json('{"a": 1}') == {"a": 1}
    assert smart._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert smart._extract_json('here you go:\n{"a": [1, 2]}\nthanks') == {"a": [1, 2]}
    assert smart._extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_unparseable_is_a_failure():
    with pytest.raises(agent.AgentError):
        smart._extract_json("I could not do that, sorry.")


def test_triage_refuses_when_agent_absent(monkeypatch):
    monkeypatch.setattr(agent, "resolve_agent_command", lambda: None)
    with pytest.raises(agent.AgentUnavailable) as ei:
        run(smart.triage(socket_dir=_NO_SOCKET))
    msg = str(ei.value)
    assert "amplifier-agent" in msg
    assert "uv tool install" in msg  # the remedy is named


def test_interpret_refuses_when_agent_absent(monkeypatch):
    monkeypatch.setattr(agent, "resolve_agent_command", lambda: None)
    with pytest.raises(agent.AgentUnavailable):
        run(smart.interpret("anything", socket_dir=_NO_SOCKET))


def test_cli_maps_agent_unavailable_to_envelope(monkeypatch, capsys):
    monkeypatch.setattr(agent, "resolve_agent_command", lambda: None)
    rc = cli.main(["triage", "--socket-dir", _NO_SOCKET])
    assert rc == 1
    import json

    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["error"]["code"] == "agent_unavailable"
    assert payload["error"]["remedy"]
    assert "amplifier-agent" in payload["error"]["message"]
