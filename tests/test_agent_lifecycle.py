"""Real v0.22 lifecycle regressions mined from PRs #17 and #19.

Brian Krabach's #17 supplies one-shot/repeat-close checks; Diego Colombo's
#19 expands startup, close, context-failure and repeated-cancellation coverage.
Only fault boundaries are wrapped: public factories, handles and HTTP are real.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import shutil

import amplifier_agent as sdk
import pytest

from _helpers import fleet, run
from agent_provider_fixture import ProviderFixture, require_container
from tmux_fleet import agent, smart


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    require_container()
    for name in list(os.environ):
        if name.startswith((
            "AMPLIFIER_AGENT_", "TMUX_FLEET_", "ANTHROPIC_", "OPENAI_",
            "AZURE_", "GOOGLE_", "GEMINI_", "OLLAMA_", "VLLM_",
            "CHAT_COMPLETIONS_", "COPILOT_",
        )) or name in {"GITHUB_TOKEN", "GH_TOKEN"}:
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-api-key")
    monkeypatch.setenv("TMUX_FLEET_PROVIDER", "anthropic")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    original = agent.tempfile.mkdtemp
    roots = []

    def allocate(*args, **kwargs):
        if kwargs.get("prefix") == "tmux-fleet-agent-":
            kwargs["dir"] = tmp_path
            root = original(*args, **kwargs)
            roots.append(Path(root))
            return root
        return original(*args, **kwargs)

    monkeypatch.setattr(agent.tempfile, "mkdtemp", allocate)
    yield
    assert not [root for root in roots if root.exists()]


class StartupControl(BaseException):
    """Synthetic non-Exception control flow safe to raise inside an asyncio task."""


class Handles:
    """Observe real returned handles; certify close before deleting retained state."""

    def __init__(self, patch, *, startup_error=None, failures=()):
        self.owners, self.agents, self.sessions = [], [], []
        self.calls, self.closed = [], []
        self.startup_error, self.failures = startup_error, failures
        self.session_entered = asyncio.Event()
        self.session_release = None
        self.agent_entered = asyncio.Event()
        self.agent_release = None
        self.session_close = sdk.Session.close
        self.agent_close = sdk.Agent.close
        create_agent, create_session = sdk.create_agent, sdk.Agent.create_session
        owner_init = agent.AgentSession.__init__

        def observed_init(owner, *args, **kwargs):
            owner_init(owner, *args, **kwargs)
            self.owners.append(owner)

        async def factory(options):
            native = await create_agent(options)
            assert isinstance(native, sdk.Agent)
            self.agents.append(native)
            return native

        async def session_factory(native, options=None):
            session = await create_session(native, options)
            assert isinstance(session, sdk.Session)
            self.sessions.append(session)
            if self.startup_error is not None:
                # The Agent handle returned, but this allocated Session did not.
                raise self.startup_error
            return session

        async def close_session(session):
            self.calls.append(("session", session))
            self.session_entered.set()
            if self.session_release is not None:
                await self.session_release.wait()
            await self.session_close(session)
            self.closed.append(("session", session))
            if "session" in self.failures:
                raise RuntimeError("fixture-response-secret session close fault")

        async def close_agent(native):
            self.calls.append(("agent", native))
            self.agent_entered.set()
            if self.agent_release is not None:
                await self.agent_release.wait()
            await self.agent_close(native)
            self.closed.append(("agent", native))
            if "agent" in self.failures:
                raise RuntimeError("fixture-response-secret agent close fault")

        patch.setattr(agent.AgentSession, "__init__", observed_init)
        patch.setattr(sdk, "create_agent", factory)
        patch.setattr(sdk.Agent, "create_session", session_factory)
        patch.setattr(sdk.Session, "close", close_session)
        patch.setattr(sdk.Agent, "close", close_agent)

    async def finish(self):
        # Always in the creating event loop, even when a regression fails.
        if self.session_release is not None:
            self.session_release.set()
        if self.agent_release is not None:
            self.agent_release.set()
        for owner in self.owners:
            if owner._close is not None:
                await asyncio.wait_for(asyncio.gather(owner._close, return_exceptions=True), 5)
            if owner._startup is not None:
                await asyncio.gather(owner._startup, return_exceptions=True)
        for session in self.sessions:
            await self.session_close(session)
            with pytest.raises(sdk.AgentError, match="closed"):
                _ = session.history
        for native in self.agents:
            await self.agent_close(native)
        for owner in self.owners:
            if owner._run is not None:
                await asyncio.wait_for(asyncio.gather(owner._run, return_exceptions=True), 5)
            if Path(owner._root).exists():
                shutil.rmtree(owner._root)  # all original public closes succeeded


@contextlib.asynccontextmanager
async def handles(monkeypatch, **kwargs):
    with monkeypatch.context() as patch:
        observed = Handles(patch, **kwargs)
        try:
            yield observed
        finally:
            await observed.finish()


@pytest.mark.parametrize("kind", [GeneratorExit, StartupControl, RuntimeError])
@pytest.mark.parametrize("close_fault", [False, True])
def test_startup_task_control_flow_closes_returned_agent(monkeypatch, kind, close_fault, capsys):
    """Fault inside factory task, never manually close a suspended outer coroutine."""
    primary = kind("fixture-response-secret startup fault")

    async def scenario():
        async with handles(
            monkeypatch, startup_error=primary,
            failures=("agent",) if close_fault else (),
        ) as seen:
            with pytest.raises(agent.AgentUnavailable if kind is RuntimeError else kind) as caught:
                # Python 3.13 wait_for directly awaits a coroutine; GeneratorExit
                # delegation can close it rather than throw into its await.
                # Keep prepare alive as a task so this tests awaited startup
                # failure, not the explicitly excluded outer coroutine.close.
                await asyncio.wait_for(asyncio.create_task(agent.prepare_turn()), 5)
            owner, = seen.owners
            native, = seen.agents
            session, = seen.sessions
            assert owner._startup.done() and owner._startup.exception() is primary
            assert owner._agent is native and owner._session is None
            assert seen.calls == [("agent", native)], seen.calls
            assert seen.closed == [("agent", native)]
            assert owner._uncertain and Path(owner._root).is_dir()
            if kind is RuntimeError:
                assert "sdk_failure" in str(caught.value)
                assert "fixture-response-secret" not in str(caught.value)
                assert caught.value.__cause__ is None
            else:
                assert caught.value is primary
                assert any("owned state retained" in note for note in primary.__notes__)
            # The unreturned session is fixture-owned; not evidence of certified
            # production drain merely because Agent.close returned.
            assert isinstance(session, sdk.Session)
    run(scenario())
    assert "fixture-response-secret" not in capsys.readouterr().err


@pytest.mark.parametrize("failures", [(), ("session",), ("agent",), ("session", "agent")])
def test_real_close_matrix_retains_failed_state(monkeypatch, failures, capsys):
    async def scenario():
        async with handles(monkeypatch, failures=failures) as seen:
            owner = await asyncio.wait_for(agent.prepare_turn(), 5)
            if failures:
                with pytest.raises(agent.AgentError, match="public SDK close"):
                    await owner.aclose()
                assert Path(owner._root).is_dir()
            else:
                await owner.aclose()
                assert not Path(owner._root).exists()
            close_task, deadline = owner._close, owner._close_deadline
            if failures:
                with pytest.raises(agent.AgentError):
                    await owner.aclose()
            else:
                await owner.aclose()
            assert owner._close is close_task and owner._close_deadline == deadline
            assert seen.calls == [("session", owner._session), ("agent", owner._agent)]
            assert seen.closed == seen.calls
    run(scenario())
    assert "fixture-response-secret" not in capsys.readouterr().err


def operation(verb, kw, **options):
    return smart.triage(**kw, **options) if verb == "triage" else smart.interpret("lifecycle", **kw, **options)


@pytest.mark.parametrize("verb", ["triage", "interpret"])
@pytest.mark.parametrize("kind", [RuntimeError, StartupControl, asyncio.CancelledError])
def test_context_primary_survives_real_close_faults(monkeypatch, verb, kind, capsys):
    primary = kind("synthetic context fault")
    method = "list_sessions" if verb == "triage" else "read_session"
    original = getattr(smart.fleet, method)

    async def collected_fault(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result["server_running"] if verb == "triage" else result["session"] == "lifecycle"
        raise primary

    monkeypatch.setattr(smart.fleet, method, collected_fault)

    async def scenario():
        async with handles(monkeypatch, failures=("session", "agent")) as seen, fleet("lifecycle") as (_, kw):
            with pytest.raises(kind) as caught:
                await asyncio.wait_for(operation(verb, kw), 5)
            assert caught.value is primary
            owner, = seen.owners
            assert not owner._submitted and owner._run is None
            assert seen.calls == [("session", owner._session), ("agent", owner._agent)]
            assert seen.closed == seen.calls and Path(owner._root).is_dir()
            assert any("owned state retained" in note for note in primary.__notes__)
    run(scenario())
    assert "fixture-response-secret" not in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["triage", "interpret"])
@pytest.mark.parametrize("close_fault", [False, True])
def test_repeated_cancel_composed_real_turn(monkeypatch, verb, close_fault, capsys):
    assert agent.CLEANUP_SECONDS == 10.0
    monkeypatch.setattr(agent, "CLEANUP_SECONDS", 0.5)

    async def scenario():
        fixture = ProviderFixture(delay=True)
        async with fixture.running() as url, fleet("lifecycle") as (_, kw), handles(
            monkeypatch, failures=("session", "agent") if close_fault else (),
        ) as seen:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
            seen.session_release = asyncio.Event()
            primaries = []
            original_submit = agent.AgentSession.submit

            async def submit(owner, *args, **kwargs):
                try:
                    return await original_submit(owner, *args, **kwargs)
                except asyncio.CancelledError as exc:
                    primaries.append(exc)
                    raise

            monkeypatch.setattr(agent.AgentSession, "submit", submit)
            task = asyncio.create_task(operation(verb, kw))
            try:
                await asyncio.wait_for(fixture.started.wait(), 5)
                task.cancel("first")
                await asyncio.wait_for(seen.session_entered.wait(), 5)
                owner, = seen.owners
                deadline, close_task = owner._close_deadline, owner._close
                task.cancel("second")  # caller now awaiting cleanup, not provider
                assert task.cancelling() == 2
                seen.session_release.set()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(task, 5)
                assert len(primaries) == 1 and caught.value is primaries[0]
                assert caught.value.args == ("first",)
                assert owner._close is close_task and owner._close_deadline == deadline
                assert close_task.done() and owner._run.done()
                assert seen.calls == [("session", owner._session), ("agent", owner._agent)]
                assert seen.closed == seen.calls
                assert Path(owner._root).exists() is close_fault
                if close_fault:
                    assert any("owned state retained" in note for note in caught.value.__notes__)
                with pytest.raises(agent.AgentError, match="one turn only"):
                    await owner.submit("no replay")
                assert len(fixture.requests) == 1
            finally:
                seen.session_release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    run(scenario())
    assert "fixture-response-secret" not in capsys.readouterr().err


def test_total_tail_not_restarted_for_agent_or_repeat_close(monkeypatch):
    assert agent.CLEANUP_SECONDS == 10.0
    monkeypatch.setattr(agent, "CLEANUP_SECONDS", 0.1)

    async def scenario():
        async with handles(monkeypatch) as seen:
            owner = await asyncio.wait_for(agent.prepare_turn(), 5)
            seen.agent_release = asyncio.Event()
            task = asyncio.create_task(owner.aclose())
            await asyncio.wait_for(seen.agent_entered.wait(), 5)
            deadline, close_task = owner._close_deadline, owner._close
            with pytest.raises(agent.AgentError, match="total tail"):
                await asyncio.wait_for(task, 5)
            assert asyncio.get_running_loop().time() >= deadline
            assert not close_task.done() and Path(owner._root).is_dir()
            with pytest.raises(agent.AgentError, match="total tail"):
                await owner.aclose()
            assert owner._close is close_task and owner._close_deadline == deadline
            assert seen.calls == [("session", owner._session), ("agent", owner._agent)]
            seen.agent_release.set()
            await asyncio.wait_for(close_task, 5)
            assert not Path(owner._root).exists()
            await owner.aclose()
    run(scenario())


@pytest.mark.parametrize("endpoint", [False, True])
def test_real_keyless_local_discovery(monkeypatch, endpoint):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("TMUX_FLEET_PROVIDER")
    monkeypatch.setenv("AMPLIFIER_AGENT_MODEL", "fixture-local-model")
    if endpoint:
        monkeypatch.setenv("CHAT_COMPLETIONS_BASE_URL", "http://127.0.0.1:9876/v1")

    async def scenario():
        records = await asyncio.wait_for(sdk.list_providers(sdk.DiscoveryOptions(environment=dict(os.environ))), 5)
        record = next(r for r in records if r.provider == "chat-completions")
        assert isinstance(record, sdk.ProviderRecord)
        assert record.installed and record.credentials == "not_required"
        async with handles(monkeypatch) as seen:
            if not endpoint:
                with pytest.raises(agent.AgentUnavailable, match="no AI provider"):
                    await asyncio.wait_for(agent.prepare_turn(), 5)
                assert not seen.agents and not seen.sessions
                return
            assert agent._select(records, dict(os.environ)) == ("chat-completions", "fixture-local-model")
            owner = await asyncio.wait_for(agent.prepare_turn(), 5)
            assert owner._agent is seen.agents[0] and owner._session is seen.sessions[0]
            assert owner._session.info.provider == "chat-completions"
            assert owner._session.info.model == "fixture-local-model"
            assert owner._session.info.persistence == "ephemeral"
            assert owner._session.history == []
            await owner.aclose()
            await owner.aclose()
            assert not Path(owner._root).exists()
    run(scenario())


@pytest.mark.parametrize("outcome", ["success", "provider-failure", "timeout"])
def test_no_replay_and_repeat_close_real_wire(monkeypatch, outcome, capsys):
    async def scenario():
        fixture = ProviderFixture(failure=401 if outcome == "provider-failure" else None, delay=outcome == "timeout")
        async with fixture.running() as url, handles(monkeypatch) as seen:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
            owner = await asyncio.wait_for(agent.prepare_turn(), 5)
            submitted = asyncio.create_task(owner.submit("JSON fixture", timeout_ms=500 if outcome == "timeout" else 10_000))
            await asyncio.wait_for(fixture.started.wait(), 5)
            if outcome == "success":
                assert await submitted == '{"ok":true}'
            else:
                with pytest.raises(agent.AgentError, match="budget" if outcome == "timeout" else "provider_failed") as caught:
                    await submitted
                assert "fixture-api-key" not in str(caught.value)
                assert "fixture-response-secret" not in str(caught.value)
            count = len(fixture.requests)
            with pytest.raises(agent.AgentError, match="one turn only"):
                await owner.submit("no replay before close")
            await owner.aclose()
            close_task, deadline = owner._close, owner._close_deadline
            await owner.aclose()
            with pytest.raises(agent.AgentError, match="one turn only"):
                await owner.submit("no replay after close")
            assert len(fixture.requests) == count == 1
            assert owner._close is close_task and owner._close_deadline == deadline
            assert owner._run.done() and not Path(owner._root).exists()
            assert seen.calls == [("session", owner._session), ("agent", owner._agent)]
            assert seen.closed == seen.calls
    run(scenario())
    output = capsys.readouterr()
    assert not output.out
    assert "fixture-api-key" not in output.err and "fixture-response-secret" not in output.err