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
from test_public_agent import (
    selection_env as selection_env,  # noqa: PLC0414 - pytest fixture export
)

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
def _records(environment=None):
    import amplifier_agent as api

    if environment is not None:
        records = run(api.list_providers())
        empty = {name: "" for r in records for name in r.credential_variables}
        empty.update(
            {"OLLAMA_HOST": "", "VLLM_BASE_URL": "", "CHAT_COMPLETIONS_BASE_URL": ""}
        )
        environment = empty | environment
    return run(api.list_providers(api.DiscoveryOptions(environment=environment)))


def test_select_provider_no_provider_configured_is_named_refusal(selection_env):
    with pytest.raises(agent.AgentUnavailable) as ei:
        agent._select_provider(_records({}), override=None)
    msg = str(ei.value)
    assert "no AI provider is configured" in msg
    assert agent.PROVIDER_ENV_VAR in msg  # the pin alternative is named


# case 4: a pinned provider has no credentials in the environment
def test_select_provider_pinned_without_credentials_is_named_refusal(
    selection_env, monkeypatch
):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(agent.AgentUnavailable) as ei:
        run(agent.prepare_turn())
    msg = str(ei.value)
    assert "anthropic" in msg  # the pinned provider is named
    assert "ANTHROPIC_API_KEY" in msg  # the exact env var to set is named
    import amplifier_agent as api

    original = ei.value.__cause__
    assert isinstance(original, api.AgentError)
    for value in (original.code, original.category, original.message, original.remedy):
        assert value in msg


def test_select_provider_prefers_and_returns(selection_env):
    records = _records({"OPENAI_API_KEY": "fixture", "ANTHROPIC_API_KEY": "fixture"})
    # preference order wins over enumeration order
    assert (
        agent._select_provider(list(reversed(records)), override=None).provider
        == "anthropic"
    )
    assert (
        agent._select_provider(
            _records({"OPENAI_API_KEY": "fixture"}), override=None
        ).provider
        == "openai"
    )
    # an explicit, resolvable pin is honoured
    assert agent._select_provider(records, override="openai").provider == "openai"
    # a resolvable provider outside the preference list is still usable
    assert (
        agent._select_provider(
            _records({"OLLAMA_HOST": "http://localhost:11434"}), override="ollama"
        ).provider
        == "ollama"
    )


@pytest.mark.parametrize("configured", [False, True])
def test_real_local_chat_completions_auto_selection(
    selection_env, monkeypatch, configured
):
    """Preserve #17's configured local endpoint, without fabricated records."""
    from pathlib import Path

    import amplifier_agent as api

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv(agent.PROVIDER_ENV_VAR)
    monkeypatch.setenv("TMUX_FLEET_MODEL", "local-model")
    if configured:
        monkeypatch.setenv("CHAT_COMPLETIONS_BASE_URL", "http://127.0.0.1:9876/v1")
    records = _records()
    record = next(r for r in records if r.provider == "chat-completions")
    assert isinstance(record, api.ProviderRecord)
    assert record.installed and record.credentials == "not_required"
    if not configured:
        with pytest.raises(
            agent.AgentUnavailable, match="no AI provider is configured"
        ):
            agent._select_provider(records, override=None)
        with pytest.raises(
            agent.AgentUnavailable, match="no AI provider is configured"
        ):
            run(agent.prepare_turn())
        return
    assert agent._select_provider(records, override=None) is record

    async def check():
        wrapper = await agent.prepare_turn()
        root = Path(wrapper._tmpdir)
        try:
            assert isinstance(wrapper._owner, api.Agent)
            assert isinstance(wrapper._session, api.Session)
            assert wrapper._session.info.provider == "chat-completions"
            assert wrapper._session.info.model == "local-model"
            assert wrapper._session.info.persistence == "ephemeral"
            assert wrapper._session.history == []
        finally:
            await wrapper.aclose()
            await wrapper.aclose()
        assert not root.exists()

    run(check())


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


def test_triage_refuses_when_no_provider_configured(monkeypatch, selection_env):
    monkeypatch.delenv(agent.PROVIDER_ENV_VAR, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(agent.AgentUnavailable, match="no AI provider is configured"):
        run(smart.triage(socket_dir=_NO_SOCKET))


def test_interpret_refuses_when_pinned_provider_lacks_credentials(
    monkeypatch, selection_env
):
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(agent.AgentUnavailable, match="anthropic"):
        run(smart.interpret("anything", socket_dir=_NO_SOCKET))


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_missing_model_refuses_before_agent_construction(
    monkeypatch, selection_env, provider
):
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, provider)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    monkeypatch.delenv("TMUX_FLEET_MODEL", raising=False)
    monkeypatch.delenv("AMPLIFIER_AGENT_MODEL", raising=False)
    with pytest.raises(agent.AgentUnavailable, match="TMUX_FLEET_MODEL"):
        run(smart.triage(socket_dir=_NO_SOCKET))


def test_unknown_pin_and_absent_sdk_precede_model(monkeypatch, selection_env):
    records = _records({})
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthorpic")
    monkeypatch.delenv("TMUX_FLEET_MODEL")
    with pytest.raises(agent.AgentUnavailable, match="not registered") as caught:
        run(agent.prepare_turn())
    assert all(r.provider in str(caught.value) for r in records)
    assert "credentials" not in str(caught.value).lower()
    missing = next(r for r in records if not r.installed)
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, missing.provider)
    with pytest.raises(agent.AgentUnavailable, match="SDK") as caught:
        run(agent.prepare_turn())
    assert missing.provider in str(caught.value)


@pytest.mark.parametrize("tool_model", [None, "tool-model"])
def test_real_model_sources_precedence_and_inherited_effort(
    selection_env, monkeypatch, tool_model
):
    monkeypatch.setenv("AMPLIFIER_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("AMPLIFIER_AGENT_MODEL", "ambient-model")
    monkeypatch.setenv("AMPLIFIER_AGENT_REASONING_EFFORT", "high")
    # This is deliberately NOT a new tmux-fleet setting.
    monkeypatch.setenv("TMUX_FLEET_REASONING_EFFORT", "low")
    if tool_model:
        monkeypatch.setenv("TMUX_FLEET_MODEL", tool_model)
    else:
        monkeypatch.delenv("TMUX_FLEET_MODEL")

    async def check():
        wrapper = await agent.prepare_turn()
        try:
            info = wrapper._session.info
            assert info.provider == "anthropic"
            assert info.model == (tool_model or "ambient-model")
            assert info.reasoning_effort == "high"
            assert wrapper._session.history == []
        finally:
            await wrapper.aclose()

    run(check())


def test_real_azure_identity_pin_reaches_public_construction(
    selection_env, monkeypatch
):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "azure-openai")
    monkeypatch.setenv("TMUX_FLEET_MODEL", "gpt-5")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    record = next(r for r in _records() if r.provider == "azure-openai")
    assert record.installed and record.credentials == "missing"

    async def check():
        wrapper = await agent.prepare_turn()
        try:
            assert wrapper._session.info.provider == "azure-openai"
            assert wrapper._session.info.model == "gpt-5"
            assert wrapper._session.history == []
        finally:
            await wrapper.aclose()

    run(check())


@pytest.mark.parametrize("provider", ["openai", "openai-chatgpt"])
def test_public_auth_remedy_without_anthropic_guess(
    selection_env, monkeypatch, provider
):
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, provider)
    with pytest.raises(agent.AgentUnavailable) as caught:
        run(agent.prepare_turn())
    assert "ANTHROPIC_API_KEY" not in str(caught.value)
    import amplifier_agent as api

    original = caught.value.__cause__
    assert isinstance(original, api.AgentError)
    for value in (original.code, original.category, original.message, original.remedy):
        assert value in str(caught.value)


def test_real_mismatch_checked_before_submission(selection_env, monkeypatch, tmp_path):
    import amplifier_agent as api

    create_session = api.Agent.create_session
    sessions, owners, roots = [], [], []
    mkdtemp = agent.tempfile.mkdtemp

    def observed_mkdtemp(*args, **kwargs):
        root = mkdtemp(*args, dir=tmp_path, **kwargs)
        if kwargs.get("prefix") == "tmux-fleet-agent-":
            roots.append(root)
        return root

    async def changed_session(owner, options=None):
        session = await create_session(owner, options)
        owners.append(owner)
        sessions.append(session)
        await session.set_model("anthropic", "unexpected-model")
        assert session.history == []
        return session

    monkeypatch.setattr(agent.tempfile, "mkdtemp", observed_mkdtemp)
    monkeypatch.setattr(api.Agent, "create_session", changed_session)
    with pytest.raises(agent.AgentUnavailable, match="no turn was submitted"):
        run(agent.prepare_turn())
    assert len(sessions) == len(owners) == len(roots) == 1
    from pathlib import Path

    assert not Path(roots[0]).exists()
    with pytest.raises(api.AgentError, match="closed"):
        _ = sessions[0].history


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
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "CLEAN" in proc.stdout
