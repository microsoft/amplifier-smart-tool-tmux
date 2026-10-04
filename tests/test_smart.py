"""Smart verbs: the model-backed path never lies about itself.

With no usable substrate, triage/interpret fail saying exactly WHICH precondition
is missing (the cli.v1 rule-3 refusal taxonomy) -- never a silent deterministic
fallback. The engine is imported lazily by the smart verbs only, so the
deterministic verbs and --help never touch it (an import-tracking test proves it).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
from _helpers import run

from tmux_fleet import agent, cli, smart

_NO_SOCKET = "/tmp/tmux-fleet-tests-no-such-socket-dir"


# --------------------------------------------------------------------------
# JSON extraction (structured-or-it-did-not-happen)
# --------------------------------------------------------------------------


def test_extract_json_handles_fences_and_prose():
    assert smart._extract_json('{"a": 1}') == {"a": 1}
    assert smart._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert smart._extract_json('here you go:\n{"a": [1, 2]}\nthanks') == {"a": [1, 2]}
    assert smart._extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_unparseable_is_a_failure():
    with pytest.raises(agent.AgentError):
        smart._extract_json("I could not do that, sorry.")


# --------------------------------------------------------------------------
# Refusal taxonomy (cli.v1 rule 3) -- each of the four preconditions named,
# proven both at the mapping-unit level and end-to-end through a smart verb.
# --------------------------------------------------------------------------


# case 3: no provider configured (nothing resolvable, none pinned)
def test_select_provider_no_provider_configured_is_named_refusal():
    with pytest.raises(agent.AgentUnavailable) as ei:
        agent._select_provider([], override=None)
    msg = str(ei.value)
    assert "no AI provider is configured" in msg
    assert "ANTHROPIC_API_KEY" in msg  # a concrete credential remedy is named
    assert agent.PROVIDER_ENV_VAR in msg  # the pin alternative is named


# case 4: a pinned provider has no credentials in the environment
def test_select_provider_pinned_without_credentials_is_named_refusal():
    with pytest.raises(agent.AgentUnavailable) as ei:
        agent._select_provider(["openai"], override="anthropic")
    msg = str(ei.value)
    assert "anthropic" in msg  # the pinned provider is named
    assert agent.PROVIDER_ENV_VAR in msg
    assert "ANTHROPIC_API_KEY" in msg  # the exact env var to set is named


def test_select_provider_prefers_and_returns():
    # preference order wins over enumeration order
    assert agent._select_provider(["openai", "anthropic"], override=None) == "anthropic"
    assert agent._select_provider(["openai"], override=None) == "openai"
    # an explicit, resolvable pin is honoured
    assert (
        agent._select_provider(["anthropic", "openai"], override="openai") == "openai"
    )
    # a resolvable provider outside the preference list is still usable
    assert agent._select_provider(["ollama"], override=None) == "ollama"


# case 1: the engine dependency is not importable
def test_engine_dependency_missing_is_named_refusal(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "amplifier_agent" or name.startswith("amplifier_agent."):
            raise ModuleNotFoundError("No module named 'amplifier_agent'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(agent.AgentUnavailable) as ei:
        agent._load_engine()
    msg = str(ei.value)
    assert "amplifier_agent" in msg
    assert "uv sync" in msg or "tmux-fleet[" in msg  # a reinstall remedy is named


# Provider selection stays independent from runtime construction.
def test_triage_refuses_when_engine_dependency_missing(monkeypatch):
    def boom():
        raise agent.AgentUnavailable(
            agent._engine_missing_hint("missing amplifier_agent")
        )

    monkeypatch.setattr(agent, "_load_engine", boom)
    with pytest.raises(agent.AgentUnavailable, match="amplifier_agent"):
        run(smart.triage(socket_dir=_NO_SOCKET))


def test_triage_refuses_when_no_provider_configured(monkeypatch):
    monkeypatch.delenv(agent.PROVIDER_ENV_VAR, raising=False)
    monkeypatch.setattr(agent, "_resolvable_providers", lambda: [])
    with pytest.raises(agent.AgentUnavailable, match="no AI provider is configured"):
        run(smart.triage(socket_dir=_NO_SOCKET))


def test_interpret_refuses_when_pinned_provider_lacks_credentials(monkeypatch):
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    monkeypatch.setattr(agent, "_resolvable_providers", lambda: ["openai"])
    with pytest.raises(agent.AgentUnavailable, match="anthropic"):
        run(smart.interpret("anything", socket_dir=_NO_SOCKET))


def test_missing_model_refuses_before_agent_construction(monkeypatch):
    monkeypatch.setattr(agent, "_resolvable_providers", lambda: ["openai"])
    monkeypatch.delenv(agent.PROVIDER_ENV_VAR, raising=False)
    monkeypatch.delenv("TMUX_FLEET_MODEL", raising=False)
    monkeypatch.delenv("AMPLIFIER_AGENT_MODEL", raising=False)
    with pytest.raises(agent.AgentUnavailable, match="TMUX_FLEET_MODEL"):
        run(smart.triage(socket_dir=_NO_SOCKET))


# --------------------------------------------------------------------------
# The CLI maps a substrate refusal onto the error envelope + a non-zero exit.
# --------------------------------------------------------------------------


def test_cli_maps_agent_unavailable_to_envelope(monkeypatch, capsys):
    async def _boom(*a, **k):
        raise agent.AgentUnavailable(agent._no_provider_hint())

    monkeypatch.setattr(agent, "prepare_turn", _boom)
    rc = cli.main(["triage", "--socket-dir", _NO_SOCKET])
    assert rc == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "agent_unavailable"
    assert payload["error"]["remedy"]
    assert "provider" in payload["error"]["message"]


# --------------------------------------------------------------------------
# Lazy import: the deterministic verbs and --help never import the engine
# (its import mutates os.environ). Proven in a fresh subprocess so no other
# test's engine import can contaminate the observation.
# --------------------------------------------------------------------------


def test_deterministic_paths_do_not_import_the_engine():
    code = textwrap.dedent(
        f"""
        import sys
        from tmux_fleet import cli

        # importing the package + CLI must not have pulled the engine in
        assert "amplifier_agent" not in sys.modules, "engine imported at module load"

        # --help must not import the engine
        try:
            cli.main(["--help"])
        except SystemExit:
            pass
        # a deterministic verb must not import the engine
        cli.main(["doctor", "--socket-dir", {_NO_SOCKET!r}])
        cli.main(["sessions", "--socket-dir", {_NO_SOCKET!r}])

        leaked = sorted(m for m in sys.modules if "amplifier_agent" in m)
        assert not leaked, f"engine leaked into deterministic path: {{leaked}}"
        print("CLEAN")
        """
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "CLEAN" in proc.stdout
