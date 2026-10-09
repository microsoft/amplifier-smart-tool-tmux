"""Release-backed checks; no fabricated Agent and no paid inference.

Record mapping units are not evidence of model quality or live success.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from typing import Any, cast

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


def test_select_provider(monkeypatch, selection_env):
    import amplifier_agent as api

    monkeypatch.setenv("OPENAI_API_KEY", "local-validation-only-not-a-key")
    records = asyncio.run(api.list_providers())
    anthropic = next(r for r in records if r.provider == "anthropic")
    openai = next(r for r in records if r.provider == "openai")
    assert agent._select_provider(records, override=None) is anthropic
    assert agent._select_provider(records, override="openai") is openai
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("OPENAI_API_KEY")
    records = asyncio.run(api.list_providers())
    # Explicit installed pins reach public construction; discovery is advisory.
    openai = next(r for r in records if r.provider == "openai")
    assert openai.credentials == "missing"
    assert agent._select_provider(records, override="openai") is openai
    with pytest.raises(agent.AgentUnavailable, match="no AI provider") as caught:
        agent._select_provider(records, override=None)
    assert "ANTHROPIC_API_KEY" not in str(caught.value)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    records = asyncio.run(api.list_providers())
    assert agent._select_provider(records, override=None).provider == "ollama"
    monkeypatch.delenv("OLLAMA_HOST")
    monkeypatch.setenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1")
    records = asyncio.run(api.list_providers())
    assert agent._select_provider(records, override=None).provider == "vllm"


def test_provider_sdk_missing_hint_only_includes_nonempty_detail():
    hint = agent._provider_sdk_missing_hint("anthropic")
    assert "not installed ()." not in hint
    assert "not installed." in hint

    hint = agent._provider_sdk_missing_hint("anthropic", "missing module")
    assert "not installed (missing module)." in hint


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


def test_real_release_startup_is_ephemeral_and_cleanup_owned(
    monkeypatch, tmp_path, selection_env
):
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


@pytest.fixture
def selection_env(monkeypatch, tmp_path):
    import socket

    import amplifier_agent as api

    monkeypatch.setenv("HOME", str(tmp_path))
    # No ambient gh login, host configuration or provider secrets participate.
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable))
    for name in list(os.environ):
        if name.startswith("AMPLIFIER_AGENT_") or name in (
            agent.PROVIDER_ENV_VAR,
            "TMUX_FLEET_MODEL",
            "TMUX_FLEET_REASONING_EFFORT",
            "OLLAMA_HOST",
            "VLLM_BASE_URL",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
        ):
            monkeypatch.delenv(name)
    for record in asyncio.run(api.list_providers()):
        for name in record.credential_variables:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-validation-only-not-a-key")

    def no_network(*args, **kwargs):
        raise AssertionError("provider network forbidden in selection check")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)


@pytest.fixture
def observed_startup(monkeypatch, selection_env):
    import amplifier_agent as api

    roots, owners, sessions, options, pairs, calls = [], [], [], [], [], []
    mkdtemp = agent.tempfile.mkdtemp
    create_agent = api.create_agent
    create_session = api.Agent.create_session
    session_close = api.Session.close
    owner_close = api.Agent.close
    rmtree = shutil.rmtree

    def directory(suffix=None, prefix=None, dir=None):
        root = mkdtemp(suffix, prefix, dir)
        if prefix == "tmux-fleet-agent-":
            roots.append(root)
        return root

    async def create(value):
        options.append(value)
        owner = await create_agent(value)
        assert isinstance(owner, api.Agent)
        owners.append(owner)
        return owner

    async def session(self, value=None):
        result = await create_session(self, value)
        assert isinstance(result, api.Session)
        assert result.info.persistence == "ephemeral"
        pairs.append(
            (result.info.provider, result.info.model, result.info.reasoning_effort)
        )
        sessions.append(result)
        return result

    async def close_session(self):
        assert self.history == []  # Refusal must precede any turn.
        await session_close(self)
        calls.append("session closed")

    async def close_owner(self):
        await owner_close(self)
        calls.append("owner closed")

    def remove(path, *args, **kwargs):
        if path in roots:
            calls.append("removed")
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(agent.tempfile, "mkdtemp", directory)
    monkeypatch.setattr(api, "create_agent", create)
    monkeypatch.setattr(api.Agent, "create_session", session)
    monkeypatch.setattr(api.Session, "close", close_session)
    monkeypatch.setattr(api.Agent, "close", close_owner)
    monkeypatch.setattr(shutil, "rmtree", remove)
    yield roots, owners, sessions, options, pairs, calls
    # Clean up even while demonstrating RED or testing retained failed state.
    for owner in owners:
        asyncio.run(owner_close(owner))
    for root in roots:
        if os.path.exists(root):
            rmtree(root)


def _ambient_pair(monkeypatch, tmp_path, source, provider, model):
    if source == "environment":
        monkeypatch.setenv("AMPLIFIER_AGENT_PROVIDER", provider)
        monkeypatch.setenv("AMPLIFIER_AGENT_MODEL", model)
        monkeypatch.setenv("AMPLIFIER_AGENT_REASONING_EFFORT", "low")
    else:
        path = tmp_path / "host.json"
        path.write_text(
            json.dumps(
                {"provider": provider, "model": model, "reasoning_effort": "low"}
            )
        )
        monkeypatch.setenv("AMPLIFIER_AGENT_CONFIG", str(path))


@pytest.mark.parametrize("source", ["environment", "host-json"])
@pytest.mark.parametrize("failed_close", [None, "session", "owner"])
def test_real_ambient_pair_mismatch_refuses_and_cleans(
    monkeypatch, tmp_path, observed_startup, source, failed_close
):
    import amplifier_agent as api

    roots, owners, sessions, options, pairs, calls = observed_startup
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "local-validation-only-not-a-key")
    _ambient_pair(monkeypatch, tmp_path, source, "openai", "gpt-5")
    if failed_close:
        cls = api.Session if failed_close == "session" else api.Agent
        real_close = cls.close

        async def fault(self):
            await real_close(self)
            raise RuntimeError(f"{failed_close} cleanup fault")

        monkeypatch.setattr(cls, "close", fault)

    async def check():
        with pytest.raises(agent.AgentUnavailable) as caught:
            await agent.prepare_turn()
        message = str(caught.value)
        for text in ("openai", "gpt-5", "anthropic", "TMUX_FLEET_MODEL"):
            assert text in message
        assert len(owners) == len(sessions) == len(roots) == 1
        assert options[0].provider is None and options[0].model is None
        assert pairs == [("openai", "gpt-5", "low")]
        assert calls[:2] == ["session closed", "owner closed"]
        if failed_close:
            assert os.path.isdir(roots[0])
            assert calls == ["session closed", "owner closed"]
            assert any("retained" in note for note in caught.value.__notes__)
            assert any(
                f"{failed_close} cleanup fault" in note
                for note in caught.value.__notes__
            )
        else:
            assert calls == ["session closed", "owner closed", "removed"]
            assert not os.path.exists(roots[0])

    asyncio.run(check())


@pytest.mark.parametrize("source", ["environment", "host-json"])
def test_real_compatible_ambient_pair_and_explicit_tool_remedy(
    monkeypatch, tmp_path, observed_startup, source
):
    roots, owners, sessions, options, pairs, calls = observed_startup
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    _ambient_pair(monkeypatch, tmp_path, source, "anthropic", "claude-sonnet-4-5")

    async def check():
        prepared = await agent.prepare_turn()
        assert pairs == [("anthropic", "claude-sonnet-4-5", "low")]
        assert options[0].provider is None and options[0].model is None
        await prepared.aclose()
        # An explicit tool model fixes both provider and model, overriding a
        # conflicting host pair without implementing config precedence here.
        _ambient_pair(monkeypatch, tmp_path, source, "openai", "gpt-5")
        monkeypatch.setenv("TMUX_FLEET_MODEL", "claude-sonnet-4-5")
        prepared = await agent.prepare_turn()
        assert pairs[-1] == ("anthropic", "claude-sonnet-4-5", "low")
        assert options[-1].provider == "anthropic"
        assert options[-1].model == "claude-sonnet-4-5"
        await prepared.aclose()
        assert all(not os.path.exists(root) for root in roots)
        assert len(owners) == len(sessions) == 2
        assert calls == ["session closed", "owner closed", "removed"] * 2

    asyncio.run(check())


def test_real_ambient_construction_refusal_keeps_remedy_and_escape_hatch(
    monkeypatch, tmp_path, observed_startup
):
    import amplifier_agent as api

    roots, owners, sessions, options, pairs, calls = observed_startup
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    _ambient_pair(monkeypatch, tmp_path, "host-json", "openai", "gpt-5")

    async def check():
        with pytest.raises(agent.AgentUnavailable) as caught:
            await agent.prepare_turn()
        original = caught.value.__cause__
        assert isinstance(original, api.AgentError)
        assert original.remedy in str(caught.value)
        assert "OPENAI_API_KEY" in str(caught.value)
        assert "TMUX_FLEET_MODEL" in str(caught.value)
        assert "observed" not in str(caught.value).lower()
        assert len(options) == 1
        assert owners == []  # Public construction refuses before returning a handle.
        assert sessions == pairs == []
        assert calls == ["removed"]
        assert not os.path.exists(roots[0])

    asyncio.run(check())


@pytest.mark.parametrize(
    ("provider", "model", "remedy"),
    [("openai", "gpt-5", "OPENAI_API_KEY"), ("openai-chatgpt", "gpt-5", "OAuth")],
)
def test_real_pinned_missing_credentials_uses_public_construction_remedy(
    monkeypatch, observed_startup, provider, model, remedy
):
    import amplifier_agent as api

    roots, owners, sessions, options, pairs, calls = observed_startup
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, provider)
    monkeypatch.setenv("TMUX_FLEET_MODEL", model)

    async def check():
        records = await api.list_providers()
        record = next(r for r in records if r.provider == provider)
        assert record.installed and record.credentials == "missing"
        with pytest.raises(agent.AgentUnavailable) as caught:
            await agent.prepare_turn()
        original = caught.value.__cause__
        assert isinstance(original, api.AgentError)
        assert original.remedy in str(caught.value)
        assert remedy in str(caught.value)
        assert "ANTHROPIC_API_KEY" not in str(caught.value)
        assert "TMUX_FLEET_MODEL" not in str(caught.value)  # Already explicit.
        assert len(options) == 1 and options[0].provider == provider
        assert owners == []  # No owned handle exists when construction itself refuses.
        assert sessions == pairs == []
        assert calls == ["removed"]
        assert not os.path.exists(roots[0])

    asyncio.run(check())


def test_real_unknown_pin_and_missing_sdk_are_distinct(monkeypatch, selection_env):
    import amplifier_agent as api

    async def check():
        records = await api.list_providers()
        monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropci")
        with pytest.raises(agent.AgentUnavailable) as caught:
            await agent.prepare_turn()
        assert "not registered" in str(caught.value)
        assert "anthropci" in str(caught.value)
        assert all(record.provider in str(caught.value) for record in records)
        assert "credentials" not in str(caught.value).lower()
        # The pinned environment really lacks this optional SDK; no import fake.
        missing = next(r for r in records if not r.installed)
        monkeypatch.setenv(agent.PROVIDER_ENV_VAR, missing.provider)
        monkeypatch.delenv("TMUX_FLEET_MODEL", raising=False)
        with pytest.raises(
            agent.AgentUnavailable, match="SDK is not installed"
        ) as caught:
            await agent.prepare_turn()
        assert missing.provider in str(caught.value)
        assert "ANTHROPIC_API_KEY" not in str(caught.value)

    asyncio.run(check())


def test_real_azure_identity_construction_despite_advisory_discovery(
    monkeypatch, observed_startup
):
    import amplifier_agent as api

    roots, owners, sessions, options, pairs, calls = observed_startup
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "azure-openai")
    monkeypatch.setenv("TMUX_FLEET_MODEL", "gpt-5")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")

    async def check():
        record = next(
            r for r in await api.list_providers() if r.provider == "azure-openai"
        )
        assert record.installed and record.credentials == "missing"
        prepared = await agent.prepare_turn()
        assert prepared._session.history == []
        assert prepared._session.info.provider == "azure-openai"
        assert prepared._session.info.model == "gpt-5"
        assert len(owners) == len(sessions) == len(pairs) == 1
        assert options[0].provider == "azure-openai"
        await prepared.aclose()
        assert calls == ["session closed", "owner closed", "removed"]
        assert not os.path.exists(roots[0])

    asyncio.run(check())


def test_real_explicit_model_mismatch_refuses_before_turn(
    monkeypatch, observed_startup
):
    import amplifier_agent as api

    roots, owners, sessions, options, pairs, calls = observed_startup
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "local-validation-only-not-a-key")
    monkeypatch.setenv("TMUX_FLEET_MODEL", "gpt-5")
    create_session = api.Agent.create_session

    async def changed_session(self, value=None):
        session = await create_session(self, value)
        # Controlled mutation through the real public API, not forged SessionInfo.
        await session.set_model("openai", "gpt-5-mini")
        return session

    monkeypatch.setattr(api.Agent, "create_session", changed_session)

    async def check():
        with pytest.raises(agent.AgentUnavailable) as caught:
            await agent.prepare_turn()
        assert "gpt-5-mini" in str(caught.value)
        assert "gpt-5" in str(caught.value)
        assert "TMUX_FLEET_MODEL" in str(caught.value)
        assert len(owners) == len(sessions) == len(options) == len(pairs) == 1
        assert calls == ["session closed", "owner closed", "removed"]
        assert not os.path.exists(roots[0])

    asyncio.run(check())


def test_real_mismatch_cli_refusal_before_tmux(
    monkeypatch, tmp_path, selection_env, capsys
):
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "local-validation-only-not-a-key")
    _ambient_pair(monkeypatch, tmp_path, "environment", "openai", "gpt-5")
    assert cli.main(["triage", "--socket-dir", str(tmp_path / "absent")]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "agent_unavailable"
    assert "TMUX_FLEET_MODEL" in result["error"]["message"]
    assert "openai" in result["error"]["message"]
    assert "gpt-5" in result["error"]["message"]


def test_real_release_cancellation_joins_before_cleanup(
    monkeypatch, tmp_path, selection_env
):
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


def test_openai_requires_explicit_model_and_preserves_selection(
    monkeypatch, tmp_path, selection_env
):
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


def test_anthropic_uses_upstream_default_and_ambient_selection(
    monkeypatch, tmp_path, selection_env
):
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


def test_real_validation_errors_preserve_actionable_contract(
    monkeypatch, tmp_path, selection_env
):
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


@pytest.fixture
def real_agent_env(monkeypatch, tmp_path, selection_env):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(agent.PROVIDER_ENV_VAR, "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-validation-only-not-a-key")
    monkeypatch.setenv("TMUX_FLEET_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("TMUX_FLEET_REASONING_EFFORT", "low")


@pytest.mark.parametrize(
    ("session_fails", "owner_fails"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_real_close_attempts_both_and_retains_failed_state(
    monkeypatch, real_agent_env, session_fails, owner_fails
):
    import amplifier_agent as api

    async def check():
        prepared = await agent.prepare_turn()
        assert isinstance(prepared._owner, api.Agent)
        assert isinstance(prepared._session, api.Session)
        root = prepared._directory
        calls = []
        session_close = prepared._session.close
        owner_close = prepared._owner.close
        rmtree = shutil.rmtree
        session_error = RuntimeError("session close fault")
        owner_error = RuntimeError("owner close fault")

        async def close_session():
            assert os.path.isdir(root)
            await session_close()
            calls.append("session")
            if session_fails:
                raise session_error

        async def close_owner():
            assert os.path.isdir(root)
            await owner_close()
            calls.append("owner")
            if owner_fails:
                raise owner_error

        def remove(path, *args, **kwargs):
            if path == root:
                assert calls == ["session", "owner"]
                calls.append("remove")
            return rmtree(path, *args, **kwargs)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(prepared._session, "close", close_session)
                patch.setattr(prepared._owner, "close", close_owner)
                patch.setattr(shutil, "rmtree", remove)
                if session_fails or owner_fails:
                    with pytest.raises(RuntimeError) as caught:
                        await prepared.aclose()
                    assert caught.value is (
                        owner_error if owner_fails else session_error
                    )
                    assert calls == ["session", "owner"]
                    assert os.path.isdir(root)
                else:
                    await prepared.aclose()
                    assert calls == ["session", "owner", "remove"]
                    assert not os.path.exists(root)
        finally:
            # Faults occur AFTER the actual closes, but clean up on RED too.
            await session_close()
            await owner_close()
            if os.path.exists(root):
                rmtree(root)

    asyncio.run(check())


@pytest.mark.parametrize(
    "error_kind",
    [
        "RuntimeError",
        "AgentError",
        "AgentUnavailable",
        "BaseException",
        "CancelledError",
        "SystemExit",
        "KeyboardInterrupt",
        "GeneratorExit",
    ],
)
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_real_startup_cleanup_preserves_exception_kind(
    tmp_path, error_kind, cleanup_fails
):
    # Process-control exceptions must not escape into pytest's asyncio runner.
    code = """
import asyncio
import os
import shutil
import tempfile
import sys
import amplifier_agent as api
from tmux_fleet import agent

failure = {
    "RuntimeError": RuntimeError("startup fault"),
    "AgentError": api.AgentError("startup_fault", "validation", "bad setup", "repair setup"),
    "AgentUnavailable": agent.AgentUnavailable("tool startup refusal"),
    "BaseException": BaseException("control fault"),
    "CancelledError": asyncio.CancelledError(),
    "SystemExit": SystemExit(23),
    "KeyboardInterrupt": KeyboardInterrupt(),
    "GeneratorExit": GeneratorExit(),
}[sys.argv[1]]
roots, calls = [], []
cleanup_fails = sys.argv[3] == "True"
real_mkdtemp = tempfile.mkdtemp
real_create_session = api.Agent.create_session
real_close = api.Agent.close

def mkdtemp(suffix=None, prefix=None, dir=None):
    owned = prefix == "tmux-fleet-agent-"
    root = real_mkdtemp(suffix, prefix, sys.argv[2] if owned else dir)
    if owned:
        roots.append(root)
    return root

async def create_session(self, options=None):
    assert isinstance(self, api.Agent)
    session = await real_create_session(self, options)
    assert isinstance(session, api.Session)
    calls.append("session")
    raise failure

async def close(self):
    assert all(os.path.isdir(root) for root in roots)
    await real_close(self)
    calls.append("owner")
    if cleanup_fails:
        raise RuntimeError("owner cleanup fault")

agent.tempfile.mkdtemp = mkdtemp
api.Agent.create_session = create_session
api.Agent.close = close

async def check():
    try:
        await agent.prepare_turn()
    except BaseException as caught:
        assert calls == ["session", "owner"], calls
        assert len(roots) == 1
        if cleanup_fails:
            assert os.path.isdir(roots[0])
            assert any("retained" in note for note in caught.__notes__)
            assert any("owner cleanup fault" in note for note in caught.__notes__)
            shutil.rmtree(roots[0])
        else:
            assert not os.path.exists(roots[0])
        if not isinstance(failure, Exception) or isinstance(failure, agent.AgentUnavailable):
            assert caught is failure, (type(caught).__name__, type(failure).__name__)
        else:
            assert caught.__cause__ is failure
            if isinstance(failure, api.AgentError):
                assert isinstance(caught, agent.AgentUnavailable)
                for detail in (failure.code, failure.category, failure.message, failure.remedy):
                    assert detail in str(caught)
            else:
                assert isinstance(caught, agent.AgentError)
                assert str(caught) == "amplifier-agent failed to start: startup fault"
    else:
        raise AssertionError("startup fault did not propagate")

try:
    asyncio.run(check())
finally:
    for root in roots:
        if os.path.exists(root):
            shutil.rmtree(root)
print("startup cleanup and exception kind preserved: " + sys.argv[1])
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, error_kind, str(tmp_path), str(cleanup_fails)],
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            agent.PROVIDER_ENV_VAR: "anthropic",
            "ANTHROPIC_API_KEY": "local-validation-only-not-a-key",
            "TMUX_FLEET_MODEL": "claude-sonnet-4-5",
            "TMUX_FLEET_REASONING_EFFORT": "low",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"exception kind preserved: {error_kind}" in proc.stdout
    if cleanup_fails:
        assert "Startup cleanup failed; directories retained at " in proc.stderr
        assert "RuntimeError: owner cleanup fault" in proc.stderr


def test_real_wait_for_expiry_cancels_and_drains_before_cleanup(
    monkeypatch, real_agent_env
):
    import socket

    import amplifier_agent as api

    network_attempts = []

    def no_network(*args, **kwargs):
        network_attempts.append("connect")
        raise AssertionError("provider network forbidden in timeout check")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)

    async def check():
        prepared = await agent.prepare_turn()
        root = prepared._directory
        calls = []
        real_cancel = api.Turn.cancel
        real_events = api.Turn.events
        session_close = prepared._session.close
        owner_close = prepared._owner.close
        rmtree = shutil.rmtree
        hook_tasks = []

        async def slow_hook(event, data):
            hook_tasks.append(asyncio.current_task())
            calls.append("hook entered")
            await asyncio.sleep(0.5)

        # v0.22 has no public hook option. This inspected engine path installs a
        # real hook, not a substitute Session/Turn/provider or canned result.
        engine_session = cast(Any, prepared._session._port)._target
        hooks = engine_session.runtime.core.coordinator.hooks
        hooks.register("prompt:submit", slow_hook, priority=-1000, name="timeout-check")

        async def cancel(turn):
            assert isinstance(turn, api.Turn)
            assert "hook entered" in calls
            calls.append("cancel requested")
            await real_cancel(turn)
            calls.append("cancel joined")

        async def events(turn):
            async for event in real_events(turn):
                if event.type == "terminal":
                    assert isinstance(event.payload, api.TurnResult)
                    assert event.payload.state == "cancelled"
                    assert os.path.isdir(root)
                    # Make cancellation's join distinct from consumption of the
                    # real terminal event; the collector must survive wait_for.
                    await asyncio.sleep(0.03)
                    calls.append("terminal drained")
                yield event

        async def close_session():
            assert calls[-1] == "terminal drained"
            assert os.path.isdir(root)
            await session_close()
            calls.append("session closed")

        async def close_owner():
            assert calls[-1] == "session closed"
            assert os.path.isdir(root)
            await owner_close()
            calls.append("owner closed")

        def remove(path, *args, **kwargs):
            if path == root:
                assert calls[-1] == "owner closed"
                calls.append("removed")
            return rmtree(path, *args, **kwargs)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(api.Turn, "cancel", cancel)
                patch.setattr(api.Turn, "events", events)
                patch.setattr(prepared._session, "close", close_session)
                patch.setattr(prepared._owner, "close", close_owner)
                patch.setattr(shutil, "rmtree", remove)
                with pytest.raises(agent.AgentError) as caught:
                    await asyncio.wait_for(
                        prepared.submit("Return one JSON object.", timeout_ms=100), 10
                    )
                assert str(caught.value) == (
                    "amplifier-agent turn exceeded its 100 ms budget; cancelled."
                )
                assert isinstance(caught.value.__cause__, TimeoutError)
                assert calls == [
                    "hook entered",
                    "cancel requested",
                    "cancel joined",
                    "terminal drained",
                ]
                assert os.path.isdir(root)
                assert len(prepared._session.history) == 1
                assert prepared._session.history[0].result.state == "cancelled"
                with pytest.raises(agent.AgentError, match="do not replay"):
                    await prepared.submit("Return one JSON object.")
                assert len(prepared._session.history) == 1
                assert not network_attempts
                await prepared.aclose()
                assert calls[-3:] == ["session closed", "owner closed", "removed"]
                assert not os.path.exists(root)
        finally:
            await session_close()
            await owner_close()
            if os.path.exists(root):
                rmtree(root)
            # The Rust hook registry may outlive its cancelled emit. Join this
            # finite test hook too; it owns no Agent state or provider work.
            await asyncio.gather(*hook_tasks)

    asyncio.run(check())
