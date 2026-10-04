"""Public Agent 0.20 integration using an actual engine and scripted provider."""

import asyncio
from pathlib import Path

import pytest

from tmux_fleet import agent


def test_actual_agent_binding_one_turn_without_tools_or_network(tmp_path, monkeypatch):
    from amplifier_core.message_models import ChatResponse, TextBlock
    from amplifier_module_provider_anthropic import AnthropicProvider

    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setenv("AMPLIFIER_AGENT_CONFIG", str(config))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setenv("TMUX_FLEET_PROVIDER", "anthropic")
    monkeypatch.setenv("TMUX_FLEET_MODEL", "fixture-model")
    calls = []

    async def complete(self, request, **kwargs):
        calls.append(request)
        assert not request.tools
        return ChatResponse(content=[TextBlock(text='{"ok":true}')])

    monkeypatch.setattr(AnthropicProvider, "complete", complete)

    async def run():
        session = await agent.prepare_turn(cwd=tmp_path)
        storage = Path(session._tmpdir)
        try:
            assert (
                await session.submit("Summarize this supplied fixture.")
                == '{"ok":true}'
            )
            with pytest.raises(agent.AgentError, match="already submitted"):
                await session.submit("do not replay")
        finally:
            await session.aclose()
            await session.aclose()
        assert not storage.exists()

    asyncio.run(run())
    assert len(calls) == 1


def test_terminal_failure_is_not_success_and_cleanup_is_idempotent(tmp_path):
    from types import SimpleNamespace

    from amplifier_agent import AgentError, TextPart, TurnInput, TurnResult

    closed = []

    class Session:
        async def run(self, input):
            return TurnResult(
                state="failure",
                error=AgentError("fixture", "runtime", "No result", "Inspect it"),
            )

    class Owner:
        async def close(self):
            closed.append(True)

    wrapper = agent.AgentSession(
        SimpleNamespace(TurnInput=TurnInput, TextPart=TextPart),
        Owner(),
        Session(),
        "fixture",
        str(tmp_path / "storage"),
    )

    async def run():
        with pytest.raises(agent.AgentError, match="did not succeed"):
            await wrapper.submit("fixture")
        await wrapper.aclose()
        await wrapper.aclose()

    asyncio.run(run())
    assert closed == [True]


def test_actual_agent_timeout_joins_owned_turn_without_replay(tmp_path, monkeypatch):
    from amplifier_module_provider_anthropic import AnthropicProvider

    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setenv("AMPLIFIER_AGENT_CONFIG", str(config))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setenv("TMUX_FLEET_PROVIDER", "anthropic")
    monkeypatch.setenv("TMUX_FLEET_MODEL", "fixture-model")
    calls, cancelled = [], []

    async def complete(self, request, **kwargs):
        calls.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(AnthropicProvider, "complete", complete)

    async def run():
        wrapper = await agent.prepare_turn()
        storage = Path(wrapper._tmpdir)
        with pytest.raises(agent.AgentError, match="budget"):
            await wrapper.submit("one bounded call", timeout_ms=100)
        assert not storage.exists()
        with pytest.raises(agent.AgentError, match="already submitted"):
            await wrapper.submit("do not replay")
        await wrapper.aclose()

    asyncio.run(run())
    assert len(calls) == 1
    assert cancelled == [True]
