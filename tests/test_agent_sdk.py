"""Required isolated installed-v0.22 SDK gate: script only the provider wire."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from amplifier_agent import __version__, AgentOptions

from _helpers import fleet, run
from agent_provider_fixture import ProviderFixture, require_container
from tmux_fleet import agent, cli, smart
from check_package import check_sdk_provenance


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path, request):
    require_container()
    # This container has no owner home or live secrets. Tests still fence all
    # credential/config selectors so inherited test-runner settings cannot route.
    for name in list(os.environ):
        if name.startswith(("AMPLIFIER_AGENT_", "TMUX_FLEET_", "ANTHROPIC_", "OPENAI_", "AZURE_", "GOOGLE_", "GEMINI_", "OLLAMA_", "VLLM_", "CHAT_COMPLETIONS_", "COPILOT_")) or name in {"GITHUB_TOKEN", "GH_TOKEN"}:
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-api-key")
    monkeypatch.setenv("TMUX_FLEET_PROVIDER", "anthropic")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    # Track only adapter-owned allocations, not SDK internals. Factories,
    # engine and provider adapters remain real and unmodified.
    roots = []
    original_temp = agent.tempfile.mkdtemp
    def allocate(*args, **kwargs):
        if kwargs.get("prefix") == "tmux-fleet-agent-":
            kwargs["dir"] = tmp_path
            root = original_temp(*args, **kwargs)
            roots.append(Path(root))
            return root
        return original_temp(*args, **kwargs)
    monkeypatch.setattr(agent.tempfile, "mkdtemp", allocate)
    yield
    for root in roots:
        if root.exists():
            # The deliberate invalid-config test is pre-runtime construction.
            # Its asyncio.run already settled; only an empty work directory
            # exists. Never sweep or rmtree potentially live SDK sessions.
            assert request.node.originalname == "test_real_refusals_and_config_validation"
            assert list(root.iterdir()) == [root / "work"]
            (root / "work").rmdir()
            root.rmdir()


def assert_clean(owner):
    assert owner._close.done() and owner._close.exception() is None
    assert not Path(owner._root).exists()
    assert owner._run is None or owner._run.done()


def test_installed_release_and_default_agreement(tmp_path, monkeypatch):
    assert __version__ == "0.22.0"
    check_sdk_provenance()
    # Public default configuration value through a real public handle, rather
    # than application imports of the private engine config implementation.
    import amplifier_agent as sdk
    async def scenario():
        native = await sdk.create_agent(AgentOptions(
            tools=[], skills=[], mcp_servers=[], approvals="deny",
            working_directory=tmp_path, sessions_directory=tmp_path / "native",
        ))
        try:
            session = await native.create_session(sdk.SessionOptions(persistence="ephemeral"))
            try:
                assert session.info.model == agent.ANTHROPIC_MODEL
            finally:
                await session.close()
        finally:
            await native.close()
    run(scenario())


def test_real_triage_interpret_no_tools_context(monkeypatch):
    async def scenario():
        fixture = ProviderFixture(replies=['{"summary":"fleet fixture"}', '{"summary":"session fixture"}'])
        async with fixture.running() as url, fleet("fixture-session") as (server, kw):
            monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
            before = dict(os.environ)
            result = await smart.triage(**kw)
            interpret = await smart.interpret("fixture-session", **kw)
            assert dict(os.environ) == before
            assert result["triage"] == {"summary": "fleet fixture"}
            assert interpret["interpretation"] == {"summary": "session fixture"}
            assert result["provider"] == "anthropic"
            assert "public Python binding" in result["executed_through"]
            assert len(fixture.requests) == 2
            for request in fixture.requests:
                assert request["model"] == agent.ANTHROPIC_MODEL
                assert not request.get("tools")
            triage_prompt = json.dumps(fixture.requests[0]["messages"])
            assert "FLEET SNAPSHOT" in triage_prompt and "fixture-session" in triage_prompt
            assert "heuristic_attention_order" in triage_prompt
            assert "SESSION CAPTURE" in json.dumps(fixture.requests[1]["messages"])
    run(scenario())


@pytest.mark.parametrize("reply,match", [
    ("not json", "parseable JSON"), ("[]", "JSON object"),
    ('{"unfinished":', "parseable JSON"), ("", "empty"),
])
def test_real_invalid_results(monkeypatch, reply, match):
    async def scenario():
        fixture = ProviderFixture(replies=[reply])
        async with fixture.running() as url, fleet("fixture-session") as (_, kw):
            monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
            with pytest.raises(agent.AgentError, match=match):
                await smart.triage(**kw)
    run(scenario())


@pytest.mark.parametrize("failure,partial", [(401, False), (503, False), (None, True)])
def test_real_wire_failures_safe_and_drained(monkeypatch, failure, partial, capsys):
    async def scenario():
        fixture = ProviderFixture(failure=failure, partial_eof=partial)
        async with fixture.running() as url:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
            owner = await agent.prepare_turn()
            try:
                with pytest.raises(agent.AgentError) as exc:
                    await owner.submit("JSON fixture", timeout_ms=10_000)
                assert "fixture-api-key" not in str(exc.value)
            finally:
                await owner.aclose()
            assert_clean(owner)
    run(scenario())
    output = capsys.readouterr()
    assert not output.out and "fixture-api-key" not in output.err


@pytest.mark.parametrize("external", [False, True])
def test_real_delayed_deadline_and_external_cancel(monkeypatch, external):
    async def scenario():
        fixture = ProviderFixture(delay=True)
        async with fixture.running() as url:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
            owner = await agent.prepare_turn()
            baseline = set(asyncio.all_tasks())
            start = asyncio.get_running_loop().time()
            async def caller():
                try:
                    await owner.submit("JSON fixture", timeout_ms=300)
                finally:
                    await owner.aclose()
            task = asyncio.create_task(caller())
            if external:
                await asyncio.wait_for(fixture.started.wait(), 5)
                task.cancel()
            with pytest.raises(asyncio.CancelledError if external else agent.AgentError):
                await task
            assert asyncio.get_running_loop().time() - start < 10.5
            assert_clean(owner)
            for _ in range(20):
                if not fixture.tasks:
                    break
                await asyncio.sleep(0.01)
            assert not fixture.tasks and not fixture.connections
            assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
    run(scenario())


def test_real_explicit_non_anthropic_pair_and_file_override(monkeypatch, tmp_path):
    async def scenario():
        fixture = ProviderFixture()
        async with fixture.running() as url:
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"provider": "anthropic", "model": "not-the-selected-model"}))
            monkeypatch.setenv("AMPLIFIER_AGENT_CONFIG", str(config))
            monkeypatch.setenv("TMUX_FLEET_PROVIDER", "chat-completions")
            monkeypatch.setenv("AMPLIFIER_AGENT_MODEL", "fixture-exact-model")
            monkeypatch.setenv("CHAT_COMPLETIONS_API_KEY", "fixture-api-key")
            monkeypatch.setenv("CHAT_COMPLETIONS_BASE_URL", url + "/v1")
            owner = await agent.prepare_turn()
            try:
                assert owner._session.info.provider == "chat-completions"
                assert owner._session.info.model == "fixture-exact-model"
                assert json.loads(await owner.submit("JSON fixture")) == {"ok": True}
                assert fixture.requests[0]["model"] == "fixture-exact-model"
                assert not fixture.requests[0].get("tools")
            finally:
                await owner.aclose()
            assert_clean(owner)
    run(scenario())


@pytest.mark.parametrize("config_text,cwd_case,match", [
    (None, None, "Create the file selected by AMPLIFIER_AGENT_CONFIG"),
    ("{broken fixture-api-key", None, "valid JSON object"),
    ("[]", None, "valid JSON object"),
    ('{"fixture-api-key": true}', None, "Remove unregistered top-level settings"),
    ('{"reasoning_effort": "fixture-api-key"}', None, "AMPLIFIER_AGENT_REASONING_EFFORT"),
    ("{}", "missing", "existing directory via cwd"),
    ("{}", "file", "existing directory via cwd"),
])
def test_real_refusals_and_config_validation(monkeypatch, tmp_path, config_text, cwd_case, match):
    async def scenario():
        monkeypatch.setenv("AMPLIFIER_AGENT_PROVIDER", "openai")
        with pytest.raises(agent.AgentUnavailable, match="conflicts"):
            await agent.prepare_turn()
        monkeypatch.delenv("AMPLIFIER_AGENT_PROVIDER")
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        with pytest.raises(agent.AgentUnavailable, match="ANTHROPIC_API_KEY"):
            await agent.prepare_turn()
        monkeypatch.delenv("TMUX_FLEET_PROVIDER")
        with pytest.raises(agent.AgentUnavailable, match="no AI provider"):
            await agent.prepare_turn()  # default local not_required is not evidence
        monkeypatch.setenv("TMUX_FLEET_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-api-key")
        config = tmp_path / "invalid.json"
        if config_text is not None:
            config.write_text(config_text)
        monkeypatch.setenv("AMPLIFIER_AGENT_CONFIG", str(config))
        cwd = tmp_path / "fixture-api-key" if cwd_case else None
        if cwd_case == "file":
            cwd.write_text("not a directory")
        with pytest.raises(agent.AgentUnavailable, match=match) as exc:
            await agent.prepare_turn(cwd=cwd)
        assert "fixture-api-key" not in str(exc.value)
        assert str(config) not in str(exc.value)
    run(scenario())


def test_real_cli_one_json_and_no_secret(monkeypatch, capsys):
    # CLI's asyncio.run is driven in its own thread so the fixture event loop
    # can serve the request; no SDK patching or inference outside loopback.
    async def scenario():
        fixture = ProviderFixture()
        async with fixture.running() as url, fleet("fixture-session") as (_, kw):
            monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
            rc = await asyncio.to_thread(cli.main, [
                "triage", "--socket-dir", kw["socket_dir"], "--socket-name", kw["socket_name"],
            ])
            assert rc == 0
    run(scenario())
    output = capsys.readouterr()
    assert json.loads(output.out)["model_backed"] is True
    assert "fixture-api-key" not in output.out + output.err


@pytest.mark.parametrize("failure,delay", [(401, False), (503, False), (None, True)])
def test_standalone_cli_error_secrets_and_delayed_shutdown(monkeypatch, tmp_path, failure, delay):
    """Real CLI process: no pytest logging handler can hide stderr leaks."""
    async def scenario():
        fixture = ProviderFixture(failure=failure, delay=delay)
        async with fixture.running() as url, fleet("fixture-session") as (_, kw):
            environment = dict(os.environ, ANTHROPIC_BASE_URL=url, TMPDIR=str(tmp_path))
            start = asyncio.get_running_loop().time()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tmux_fleet.cli", "triage",
                "--socket-dir", kw["socket_dir"], "--socket-name", kw["socket_name"],
                "--timeout-ms", "1500" if delay else "10000",
                env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            # This is a test watchdog, not a product kill/exit workaround.
            # On failure release only our scripted provider so the child can drain.
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), 20)
            except TimeoutError:
                fixture.release.set()
                await asyncio.wait_for(proc.communicate(), 20)
                pytest.fail("standalone CLI exceeded watchdog; scripted provider release required")
            elapsed = asyncio.get_running_loop().time() - start
            assert proc.returncode == 1, stderr.decode()
            result = json.loads(stdout)
            assert result["error"]["code"] == "agent_error"
            assert fixture.started.is_set() and fixture.requests
            assert b"fixture-api-key" not in stdout + stderr
            assert b"fixture-response-secret" not in stdout + stderr
            if delay:
                assert "budget" in result["error"]["message"]
                assert elapsed < 13
            else:
                assert "diagnostic omitted" in stderr.decode()
            assert not list(tmp_path.glob("tmux-fleet-agent-*"))
            for _ in range(20):
                if not fixture.tasks:
                    break
                await asyncio.sleep(0.01)
            assert not fixture.tasks and not fixture.connections
    run(scenario())