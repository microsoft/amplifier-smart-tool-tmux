"""Release-backed checks; no fabricated Agent and no paid inference.

Record mapping units are not evidence of model quality or live success.
"""

import asyncio
import json
import os
import subprocess
import sys

import pytest

from tmux_fleet import agent, cli, smart


def test_extract_json_handles_fences_and_prose():
    assert smart._extract_json('{"a": 1}') == {"a": 1}
    assert smart._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert smart._extract_json('here:\n{"a": [1, 2]}\nthanks') == {"a": [1, 2]}
    assert smart._extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_unparseable_is_failure():
    with pytest.raises(agent.AgentError):
        smart._extract_json("not JSON")


def test_select_provider():
    assert agent._select_provider(["openai", "anthropic"], override=None) == "anthropic"
    assert (
        agent._select_provider(["openai", "anthropic"], override="openai") == "openai"
    )
    assert agent._select_provider(["ollama"], override=None) == "ollama"
    with pytest.raises(agent.AgentUnavailable, match="no AI provider"):
        agent._select_provider([], override=None)
    with pytest.raises(agent.AgentUnavailable, match="ANTHROPIC_API_KEY"):
        agent._select_provider(["openai"], override="anthropic")


def test_engine_dependency_missing_is_named_refusal(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "amplifier_agent":
            raise ModuleNotFoundError("No module named 'amplifier_agent'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(agent.AgentUnavailable, match="uv sync"):
        agent._load_engine()


def test_cli_maps_agent_unavailable_to_envelope(monkeypatch, capsys):
    async def refuse(*args, **kwargs):
        raise agent.AgentUnavailable(agent._no_provider_hint())

    monkeypatch.setattr(agent, "prepare_turn", refuse)
    assert cli.main(["triage", "--socket-dir", "/tmp/no-fleet-here"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "agent_unavailable"
    assert payload["error"]["remedy"]


def test_deterministic_paths_do_not_import_agent():
    code = """
import sys
from tmux_fleet import cli
cli.main(["--help"])
cli.main(["doctor", "--socket-dir", "/tmp/no-fleet-here"])
cli.main(["sessions", "--socket-dir", "/tmp/no-fleet-here"])
assert not [m for m in sys.modules if "amplifier_agent" in m]
"""
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_real_discovery_pinned_missing_credentials_refuses_before_tmux(tmp_path):
    # Fresh environment and home remove keys and cached host configuration.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tmux_fleet.cli import main; raise SystemExit(main())",
            "triage",
            "--socket-dir",
            "/tmp/no-fleet-here",
        ],
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "TMUX_FLEET_PROVIDER": "anthropic",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stderr
    result = json.loads(proc.stdout)
    assert result["error"]["code"] == "agent_unavailable"
    assert "ANTHROPIC_API_KEY" in result["error"]["message"]


def test_real_release_records_map_text_and_errors():
    import amplifier_agent as api

    assert api.__version__ == "0.22.0"
    assert (
        agent._result_text(
            api.TurnResult(
                state="success",
                content=[api.TextPart(text='{"a":'), api.TextPart(text="1}")],
            )
        )
        == '{"a":1}'
    )
    with pytest.raises(agent.AgentError, match="empty result"):
        agent._result_text(api.TurnResult(state="success", content=[]))
    error = api.AgentError("example", "validation", "bad input", "repair input")
    for state in ("failure", "cancelled", "rejected"):
        with pytest.raises(agent.AgentError, match=f"turn {state}.*repair input"):
            agent._result_text(api.TurnResult(state=state, error=error))


def test_real_release_startup_is_ephemeral_and_cleanup_owned(monkeypatch, tmp_path):
    # Construction only: no run/start_turn and no paid inference.
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-validation-only-not-a-key")
    monkeypatch.setenv("TMUX_FLEET_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("TMUX_FLEET_REASONING_EFFORT", "low")
    monkeypatch.setenv("HOME", str(tmp_path))

    async def check():
        prepared = await agent.prepare_turn()
        root = prepared._directory
        assert os.path.isdir(os.path.join(root, "working"))
        assert os.path.isdir(os.path.join(root, "sessions"))
        assert prepared._session.info.persistence == "ephemeral"
        assert prepared._session.info.provider == "anthropic"
        assert prepared._session.info.model == "claude-sonnet-4-5"
        assert prepared._session.info.reasoning_effort == "low"
        await prepared.aclose()
        assert not os.path.exists(root)

    asyncio.run(check())


def test_real_release_cancellation_joins_before_cleanup(monkeypatch, tmp_path):
    import socket

    # Transport denial is a safety fence, not a replacement Agent/provider.
    # Even a regression in cancellation cannot contact a paid service.
    def no_network(*args, **kwargs):
        raise AssertionError("provider network forbidden in cancellation check")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-validation-only-not-a-key")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMUX_FLEET_MODEL", "claude-sonnet-4-5")

    async def check():
        prepared = await agent.prepare_turn()
        root = prepared._directory
        task = asyncio.create_task(prepared.submit("Return one JSON object."))
        # start_turn accepts synchronously; this wake observes the submitted turn.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert os.path.isdir(root)
        history = prepared._session.history
        assert len(history) == 1
        assert history[0].result.state == "cancelled"
        with pytest.raises(agent.AgentError, match="do not replay"):
            await prepared.submit("Return one JSON object.")
        assert len(prepared._session.history) == 1
        await prepared.aclose()
        assert not os.path.exists(root)

    asyncio.run(check())


def test_openai_requires_explicit_model_and_preserves_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "local-validation-only-not-a-key")
    monkeypatch.delenv("TMUX_FLEET_MODEL", raising=False)
    monkeypatch.setenv("AMPLIFIER_AGENT_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("AMPLIFIER_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("AMPLIFIER_AGENT_REASONING_EFFORT", "high")
    monkeypatch.setenv("TMUX_FLEET_REASONING_EFFORT", "low")

    async def check():
        with pytest.raises(agent.AgentUnavailable, match="TMUX_FLEET_MODEL"):
            await agent.prepare_turn()
        monkeypatch.setenv("TMUX_FLEET_MODEL", "gpt-5")
        prepared = await agent.prepare_turn()
        try:
            assert prepared._session.info.provider == "openai"
            assert prepared._session.info.model == "gpt-5"
            assert prepared._session.info.reasoning_effort == "low"
        finally:
            await prepared.aclose()

    asyncio.run(check())


def test_anthropic_uses_upstream_default_and_ambient_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-validation-only-not-a-key")
    monkeypatch.delenv("TMUX_FLEET_MODEL", raising=False)
    monkeypatch.delenv("TMUX_FLEET_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AMPLIFIER_AGENT_MODEL", raising=False)
    monkeypatch.delenv("AMPLIFIER_AGENT_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AMPLIFIER_AGENT_CONFIG", raising=False)

    async def check():
        prepared = await agent.prepare_turn()
        try:
            # Public v0.22.0 configuration.md documents these defaults.
            assert prepared._session.info.provider == "anthropic"
            assert prepared._session.info.model == "claude-sonnet-5-5"
            assert prepared._session.info.reasoning_effort == "medium"
        finally:
            await prepared.aclose()
        monkeypatch.setenv("AMPLIFIER_AGENT_MODEL", "claude-sonnet-4-5")
        monkeypatch.setenv("AMPLIFIER_AGENT_REASONING_EFFORT", "low")
        prepared = await agent.prepare_turn()
        try:
            assert prepared._session.info.model == "claude-sonnet-4-5"
            assert prepared._session.info.reasoning_effort == "low"
        finally:
            await prepared.aclose()

    asyncio.run(check())


def test_real_validation_errors_preserve_actionable_contract(monkeypatch, tmp_path):
    import amplifier_agent as api

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "local-validation-only-not-a-key")
    monkeypatch.setenv("TMUX_FLEET_MODEL", "gpt-5")
    monkeypatch.setenv("TMUX_FLEET_REASONING_EFFORT", "invalid-effort")

    async def check():
        with pytest.raises(agent.AgentUnavailable) as caught:
            await agent.prepare_turn()
        original = caught.value.__cause__
        assert isinstance(original, api.AgentError)
        for value in (
            original.code,
            original.category,
            original.message,
            original.remedy,
        ):
            assert value in str(caught.value)
        monkeypatch.setenv("TMUX_FLEET_REASONING_EFFORT", "low")
        prepared = await agent.prepare_turn()
        try:
            # Closed handle is a real public-method rejection before any inference.
            await prepared._session.close()
            with pytest.raises(agent.AgentError) as caught:
                await prepared.submit("JSON please")
            original = caught.value.__cause__
            assert isinstance(original, api.AgentError)
            for value in (
                original.code,
                original.category,
                original.message,
                original.remedy,
            ):
                assert value in str(caught.value)
        finally:
            await prepared.aclose()

    asyncio.run(check())
