"""Public Agent integration; existing scripted success fixture retained from #17."""

import asyncio
from pathlib import Path

import pytest

from tmux_fleet import agent


@pytest.fixture
def selection_env(tmp_path, monkeypatch):
    """Isolate host configuration before explicitly supplying each case's inputs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "AMPLIFIER_AGENT_PROVIDER",
        "AMPLIFIER_AGENT_MODEL",
        "AMPLIFIER_AGENT_REASONING_EFFORT",
        "AMPLIFIER_AGENT_CONFIG",
        "AMPLIFIER_AGENT_HOME",
        "TMUX_FLEET_PROVIDER",
        "TMUX_FLEET_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "COPILOT_AGENT_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "OLLAMA_HOST",
        "OLLAMA_API_KEY",
        "VLLM_BASE_URL",
        "VLLM_API_KEY",
        "CHAT_COMPLETIONS_BASE_URL",
        "CHAT_COMPLETIONS_API_KEY",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setenv("TMUX_FLEET_PROVIDER", "anthropic")
    monkeypatch.setenv("TMUX_FLEET_MODEL", "fixture-model")


def test_real_agent_022_constructor(selection_env, tmp_path, monkeypatch):
    import amplifier_agent as api

    create_agent = api.create_agent
    observed = []

    async def observe(options):
        observed.append(options)
        return await create_agent(options)

    monkeypatch.setattr(api, "create_agent", observe)

    async def run():
        wrapper = await agent.prepare_turn(cwd=tmp_path / "caller-must-not-be-used")
        storage = Path(wrapper._tmpdir)
        try:
            assert isinstance(wrapper._owner, api.Agent)
            assert isinstance(wrapper._session, api.Session)
            assert wrapper._session.info.persistence == "ephemeral"
            assert wrapper._session.info.provider == "anthropic"
            assert wrapper._session.info.model == "fixture-model"
            assert (storage / "working").is_dir()
            assert (storage / "sessions").is_dir()
            assert len(observed) == 1
            options = observed[0]
            assert options.tools == options.skills == options.mcp_servers == []
            assert options.approvals == "deny"
            assert Path(options.working_directory) == storage / "working"
            assert Path(options.sessions_directory) == storage / "sessions"
            assert options.additional_directories is None
        finally:
            await wrapper.aclose()
            await wrapper.aclose()
        assert not storage.exists()

    asyncio.run(run())


@pytest.mark.parametrize("directory", ["working", "sessions"])
def test_partial_startup_directory_failure_is_cleaned(
    selection_env, monkeypatch, tmp_path, directory
):
    import os

    import amplifier_agent as api

    mkdir = os.mkdir
    mkdtemp = agent.tempfile.mkdtemp
    create_agent = api.create_agent
    roots, calls = [], []

    def observed_mkdtemp(*args, **kwargs):
        root = mkdtemp(*args, **kwargs)
        if kwargs.get("prefix") == "tmux-fleet-agent-":
            roots.append(root)
        return root

    def fail_directory(path, *args, **kwargs):
        if roots and Path(path) == Path(roots[0]) / directory:
            raise OSError("startup directory failure")
        return mkdir(path, *args, **kwargs)

    async def observe(options):
        calls.append(options)
        return await create_agent(options)

    monkeypatch.setattr(agent.tempfile, "mkdtemp", observed_mkdtemp)
    monkeypatch.setattr(os, "mkdir", fail_directory)
    monkeypatch.setattr(api, "create_agent", observe)
    with pytest.raises(agent.AgentError) as caught:
        asyncio.run(agent.prepare_turn())
    assert isinstance(caught.value.__cause__, OSError)
    assert str(caught.value.__cause__) == "startup directory failure"
    assert len(roots) == 1
    assert not Path(roots[0]).exists()
    assert not calls


def test_actual_agent_binding_one_turn_without_tools_or_network(
    tmp_path, monkeypatch, selection_env
):
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


def test_terminal_failure_is_not_success_and_cleanup_is_idempotent(selection_env):
    """A real closed public Session refuses submission, without fabricated handles."""

    async def run():
        wrapper = await agent.prepare_turn()
        await wrapper._session.close()
        with pytest.raises(agent.AgentError, match="turn failed"):
            await wrapper.submit("fixture")
        await wrapper.aclose()
        await wrapper.aclose()

    asyncio.run(run())


def test_real_terminal_failure_keeps_public_error_and_no_replay(
    selection_env, monkeypatch
):
    """A genuine loopback peer disconnect produces the provider's real failure."""
    import socket

    import amplifier_agent as api

    async def check():
        requests = []

        async def disconnect(reader, writer):
            requests.append("connection")
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(disconnect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("TMUX_FLEET_MODEL", "claude-sonnet-4-5")
        connect = socket.socket.connect

        def only_local(sock, address):
            assert address[:2] == ("127.0.0.1", port), address
            return connect(sock, address)

        monkeypatch.setattr(socket.socket, "connect", only_local)
        wrapper = await agent.prepare_turn()
        root = Path(wrapper._tmpdir)
        try:
            with pytest.raises(agent.AgentError, match="did not succeed") as caught:
                await wrapper.submit("No paid network is reachable.", timeout_ms=10000)
            history = wrapper._session.history
            assert len(history) == 1
            assert history[0].result.state == "failure"
            original = caught.value.__cause__
            assert isinstance(original, api.AgentError)
            assert original == history[0].result.error
            for value in (
                original.code,
                original.category,
                original.message,
                original.remedy,
            ):
                assert value in str(caught.value)
            before = len(requests)
            with pytest.raises(agent.AgentError, match="already submitted"):
                await wrapper.submit("no replay")
            assert len(requests) == before
        finally:
            await wrapper.aclose()
            await wrapper.aclose()
            server.close()
            await server.wait_closed()
        assert not root.exists()

    asyncio.run(check())


def test_actual_agent_timeout_joins_owned_turn_without_replay(
    tmp_path, monkeypatch, selection_env
):
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


@pytest.mark.parametrize("interruption", ["timeout", "caller", "caller-twice"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("verb", ["submit", "triage", "interpret"])
def test_real_run_cancellation_terminal_before_cleanup(
    monkeypatch, selection_env, interruption, cleanup_fails, capsys, verb
):
    """Stall a real HTTP connection; no scripted provider responses or paid service."""
    import json
    import socket

    import amplifier_agent as api
    from _helpers import fleet as make_fleet

    from tmux_fleet import smart

    async def check():
        isolated_fleet = make_fleet("composed")
        _server, fleet_options = await isolated_fleet.__aenter__()
        received = asyncio.Event()
        release = asyncio.Event()
        handlers = set()
        requests = []

        async def stall(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                request = json.loads(await reader.readexactly(length))
                requests.append(request)
                assert not request.get("tools")
                received.set()
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(stall, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("TMUX_FLEET_MODEL", "claude-sonnet-4-5")
        # Block every connection except this exact real loopback server.
        connect = socket.socket.connect

        def only_local(sock, address):
            assert address[:2] == ("127.0.0.1", port), address
            return connect(sock, address)

        monkeypatch.setattr(socket.socket, "connect", only_local)
        wrapper = await agent.prepare_turn()
        root = Path(wrapper._tmpdir)
        assert wrapper._owner is not None
        owner_close = wrapper._owner.close
        session_close = wrapper._session.close
        real_run = wrapper._session.run
        real_events = api.Turn.events
        real_cancel = api.Turn.cancel
        closed = []
        terminal = []
        cancelling = asyncio.Event()

        async def observe_run(value):
            result = await real_run(value)
            terminal.append(result)
            return result

        async def observe_events(turn):
            async for event in real_events(turn):
                if event.type == "terminal":
                    terminal.append(event.payload)
                yield event

        async def observed_session_close():
            assert len(terminal) == 1, "terminal must be drained before cleanup"
            assert terminal[0].state == "cancelled"
            if cleanup_fails:
                raise RuntimeError("session cleanup fault after terminal")
            await session_close()

        async def observe_cancel(turn):
            await real_cancel(turn)
            cancelling.set()
            if interruption == "caller-twice":
                await asyncio.sleep(0.05)

        async def observed_close():
            assert len(terminal) == 1
            assert terminal[0].state == "cancelled"
            closed.append("terminal before owner close")
            await owner_close()

        monkeypatch.setattr(wrapper._session, "run", observe_run)
        monkeypatch.setattr(api.Turn, "events", observe_events)
        monkeypatch.setattr(api.Turn, "cancel", observe_cancel)
        monkeypatch.setattr(wrapper._session, "close", observed_session_close)
        monkeypatch.setattr(wrapper._owner, "close", observed_close)
        primary = []
        submit = wrapper.submit

        async def observed_submit(*args, **kwargs):
            try:
                return await submit(*args, **kwargs)
            except BaseException as exc:
                primary.append(exc)
                raise

        async def prepared():
            # This is the actual wrapper from the real factory above, not a
            # replacement Agent/Session/provider; the verb still reads real tmux.
            return wrapper

        monkeypatch.setattr(wrapper, "submit", observed_submit)
        if verb == "submit":
            operation = wrapper.submit("Return one JSON object.", timeout_ms=2000)
        else:
            monkeypatch.setattr(agent, "prepare_turn", prepared)
            operation = (
                smart.triage(
                    timeout_ms=2000,
                    socket_dir=fleet_options["socket_dir"],
                    socket_name=fleet_options["socket_name"],
                )
                if verb == "triage"
                else smart.interpret(
                    "composed",
                    timeout_ms=2000,
                    socket_dir=fleet_options["socket_dir"],
                    socket_name=fleet_options["socket_name"],
                )
            )
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(received.wait(), 5)
            if interruption.startswith("caller"):
                task.cancel()
                if interruption == "caller-twice":
                    await asyncio.wait_for(cancelling.wait(), 5)
                    task.cancel()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(task, 10)
            else:
                with pytest.raises(agent.AgentError, match="budget") as caught:
                    await asyncio.wait_for(task, 10)
                assert isinstance(caught.value.__cause__, TimeoutError)
            assert len(primary) == 1
            assert caught.value is primary[0], "outer cleanup replaced submit's failure"
            if cleanup_fails and verb != "submit":
                assert any(
                    "Smart verb cleanup failed" in note
                    for note in caught.value.__notes__
                )
            assert len(terminal) == 1
            assert terminal[0].state == "cancelled"
            if cleanup_fails:
                with pytest.raises(RuntimeError, match="session cleanup fault"):
                    await wrapper.aclose()
                assert root.is_dir()
                assert wrapper._owner is not None
                if interruption == "timeout":
                    assert "Turn cleanup failed" in capsys.readouterr().err
                elif verb != "submit":
                    assert "Smart verb cleanup failed" in capsys.readouterr().err
            else:
                await wrapper.aclose()
                assert closed == ["terminal before owner close"]
                assert not root.exists()
            with pytest.raises(agent.AgentError, match="already submitted"):
                await wrapper.submit("no replay")
            assert len(requests) == 1
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await session_close()
            await owner_close()
            release.set()
            server.close()
            await server.wait_closed()
            await asyncio.gather(*handlers, return_exceptions=True)
            if root.exists():
                import shutil

                shutil.rmtree(root)
            await isolated_fleet.__aexit__(None, None, None)

    asyncio.run(check())


@pytest.mark.parametrize("session_fails", [False, True])
@pytest.mark.parametrize("owner_fails", [False, True])
def test_real_close_attempts_both_retains_failed_state(
    monkeypatch, selection_env, session_fails, owner_fails
):
    import shutil

    async def check():
        wrapper = await agent.prepare_turn()
        owner = wrapper._owner
        assert owner is not None
        root = Path(wrapper._tmpdir)
        session_close = wrapper._session.close
        owner_close = owner.close
        calls = []

        async def close_session():
            await session_close()
            calls.append("session")
            if session_fails:
                raise RuntimeError("session cleanup fault")

        async def close_owner():
            await owner_close()
            calls.append("owner")
            if owner_fails:
                raise RuntimeError("owner cleanup fault")

        try:
            with monkeypatch.context() as patch:
                patch.setattr(wrapper._session, "close", close_session)
                patch.setattr(owner, "close", close_owner)
                if session_fails or owner_fails:
                    with pytest.raises(RuntimeError, match="cleanup fault"):
                        await wrapper.aclose()
                    assert calls == ["session", "owner"]
                    assert root.is_dir()
                    assert wrapper._owner is owner
                else:
                    await wrapper.aclose()
                    await wrapper.aclose()
                    assert calls == ["session", "owner"]
                    assert not root.exists()
        finally:
            await session_close()
            await owner_close()
            if root.exists():
                shutil.rmtree(root)

    asyncio.run(check())


@pytest.mark.parametrize("verb", ["triage", "interpret"])
@pytest.mark.parametrize(
    "kind",
    [
        "RuntimeError",
        "AgentUnavailable",
        "BaseException",
        "CancelledError",
        "SystemExit",
        "KeyboardInterrupt",
        "GeneratorExit",
    ],
)
def test_smart_context_error_survives_actual_close_fault(
    selection_env, monkeypatch, verb, kind, capsys
):
    """Delegate context collection to real tmux, then fault its return boundary."""
    import shutil

    from _helpers import fleet as make_fleet

    from tmux_fleet import fleet, smart

    primary = {
        "RuntimeError": RuntimeError("context boundary fault"),
        "AgentUnavailable": agent.AgentUnavailable("caller refusal"),
        "BaseException": BaseException("control boundary fault"),
        "CancelledError": asyncio.CancelledError(),
        "SystemExit": SystemExit(23),
        "KeyboardInterrupt": KeyboardInterrupt(),
        "GeneratorExit": GeneratorExit(),
    }[kind]
    prepare = agent.prepare_turn
    wrappers = []
    closes = []

    async def observed_prepare():
        wrapper = await prepare()
        wrappers.append(wrapper)
        assert wrapper._owner is not None
        owner_close = wrapper._owner.close

        async def close_fault():
            await owner_close()
            closes.append("actual owner closed")
            raise RuntimeError("persistent owner cleanup fault")

        monkeypatch.setattr(wrapper._owner, "close", close_fault)
        return wrapper

    method = "list_sessions" if verb == "triage" else "read_session"
    collect = getattr(fleet, method)

    async def context_fault(*args, **kwargs):
        observation = await collect(*args, **kwargs)
        assert (
            observation["server_running"]
            if verb == "triage"
            else observation["session"] == "composed"
        )
        raise primary

    monkeypatch.setattr(agent, "prepare_turn", observed_prepare)
    monkeypatch.setattr(fleet, method, context_fault)

    async def check():
        async with make_fleet("composed") as (_server, options):
            try:
                operation = (
                    smart.triage(
                        socket_dir=options["socket_dir"],
                        socket_name=options["socket_name"],
                    )
                    if verb == "triage"
                    else smart.interpret(
                        "composed",
                        socket_dir=options["socket_dir"],
                        socket_name=options["socket_name"],
                    )
                )
                try:
                    await operation
                except BaseException as caught:  # noqa: BLE001 - check identity of process-control failures
                    assert caught is primary
                    assert closes == ["actual owner closed"]
                    assert len(wrappers) == 1
                    wrapper = wrappers[0]
                    assert Path(wrapper._tmpdir).is_dir()
                    assert wrapper._owner is not None
                    assert not wrapper._submitted
                    assert any(
                        "Smart verb cleanup failed" in note for note in caught.__notes__
                    )
                else:
                    raise AssertionError("primary context fault was suppressed")
            finally:
                for wrapper in wrappers:
                    await wrapper._session.close()
                    # The real owner already closed before the injected fault.
                    shutil.rmtree(wrapper._tmpdir)

    asyncio.run(check())
    stderr = capsys.readouterr().err
    assert "Smart verb cleanup failed" in stderr
    assert "persistent owner cleanup fault" in stderr


def test_real_close_without_primary_is_not_suppressed(selection_env, monkeypatch):
    import shutil

    from tmux_fleet import smart

    async def check():
        wrapper = await agent.prepare_turn()
        assert wrapper._owner is not None
        session_close = wrapper._session.close
        owner_close = wrapper._owner.close
        fault = RuntimeError("close without primary must propagate")

        async def close_fault():
            await owner_close()
            raise fault

        monkeypatch.setattr(wrapper._owner, "close", close_fault)
        try:
            with pytest.raises(RuntimeError) as caught:
                await smart._close_after_work(wrapper, None)
            assert caught.value is fault
            assert Path(wrapper._tmpdir).is_dir()
            assert wrapper._owner is not None
        finally:
            await session_close()
            await owner_close()
            shutil.rmtree(wrapper._tmpdir)

    asyncio.run(check())


@pytest.mark.parametrize(
    "kind",
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
@pytest.mark.parametrize("fault", ["none", "owner", "remove"])
def test_real_startup_preserves_original_exception(tmp_path, kind, fault):
    """Faults follow actual public construction/close; no manufactured handles."""
    import os
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import asyncio, os, shutil, sys, tempfile
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
        fault = sys.argv[2]
        roots, calls = [], []
        mkdtemp = tempfile.mkdtemp
        create_session = api.Agent.create_session
        close_owner = api.Agent.close
        remove = shutil.rmtree

        def observed_mkdtemp(*args, **kwargs):
            root = mkdtemp(*args, **kwargs)
            if kwargs.get("prefix") == "tmux-fleet-agent-":
                roots.append(root)
            return root

        async def rejected(self, options=None):
            session = await create_session(self, options)
            assert isinstance(session, api.Session)
            calls.append("session")
            raise failure

        async def close(self):
            await close_owner(self)
            calls.append("owner")
            if fault == "owner":
                raise RuntimeError("owner cleanup fault")

        def remove_fault(path, *args, **kwargs):
            if path in roots and fault == "remove":
                raise OSError("directory cleanup fault")
            return remove(path, *args, **kwargs)

        tempfile.mkdtemp = observed_mkdtemp
        api.Agent.create_session = rejected
        api.Agent.close = close
        shutil.rmtree = remove_fault

        async def check():
            try:
                await agent.prepare_turn()
            except BaseException as caught:
                assert calls == ["session", "owner"], calls
                if fault != "none":
                    assert os.path.isdir(roots[0])
                    assert any("retained" in n for n in caught.__notes__)
                    assert any("cleanup fault" in n for n in caught.__notes__)
                else:
                    assert not os.path.exists(roots[0])
                if not isinstance(failure, Exception) or isinstance(failure, agent.AgentUnavailable):
                    assert caught is failure, (type(caught).__name__, type(failure).__name__)
                else:
                    assert caught.__cause__ is failure
                    if isinstance(failure, api.AgentError):
                        assert isinstance(caught, agent.AgentUnavailable)
                        for value in (failure.code, failure.category, failure.message, failure.remedy):
                            assert value in str(caught)
                    else:
                        assert isinstance(caught, agent.AgentError)
            else:
                raise AssertionError("startup fault did not propagate")
        try:
            asyncio.run(check())
        finally:
            for root in roots:
                if os.path.exists(root):
                    remove(root)
        print("original preserved")
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code, kind, fault],
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(Path(agent.__file__).parent.parent),
            "ANTHROPIC_API_KEY": "fixture-not-a-real-key",
            "TMUX_FLEET_PROVIDER": "anthropic",
            "TMUX_FLEET_MODEL": "fixture-model",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "original preserved" in proc.stdout
    if fault != "none":
        assert "Startup cleanup failed; directories retained" in proc.stderr
