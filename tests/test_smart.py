"""Public SDK adapter tests without provider requests. Real SDK has its own gate."""
from __future__ import annotations

import asyncio
import builtins
from concurrent.futures import Future, ThreadPoolExecutor
import contextvars
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace as NS

import pytest

from tmux_fleet import agent, cli, smart
from _helpers import run

NO_SOCKET = "/tmp/tmux-fleet-tests-no-such-socket-dir"


def record(provider="anthropic", keys=("ANTHROPIC_API_KEY",), installed=True, credentials="found"):
    return NS(provider=provider, credential_variables=list(keys), installed=installed, credentials=credentials)


def fake_sdk(monkeypatch, *, result=None, raised=None, hang=False, close_error=False, session_error=False):
    """Mock only public symbols/handles, with explicit independent turn lifetime."""
    options, stages = [], []
    release = asyncio.Event()
    async def discover(value):
        assert value.environment == dict(os.environ)
        print("discovery", os.environ["ANTHROPIC_API_KEY"])
        return [record()]
    async def execute(value):
        assert value.content[0].text
        print("turn", os.environ["ANTHROPIC_API_KEY"])
        stages.append("run")
        if hang:
            await release.wait()
        if raised:
            raise raised
        return result or NS(state="success", error=None, content=[NS(text='{"ok":true}')])
    async def close_session():
        stages.append("session-close")
        print("close", os.environ["ANTHROPIC_API_KEY"])
        if close_error:
            raise RuntimeError("synthetic-secret")
        release.set()
    async def session(value):
        assert value.persistence == "ephemeral"
        stages.append("session")
        if session_error:
            raise RuntimeError("synthetic-secret")
        return NS(run=execute, close=close_session)
    async def close_agent():
        stages.append("agent-close")
    async def create(value):
        options.append(value)
        stages.append("agent")
        print("construction", os.environ["ANTHROPIC_API_KEY"])
        return NS(create_session=session, close=close_agent)
    sdk = NS(
        list_providers=discover, create_agent=create, AgentOptions=lambda **kw: NS(**kw),
        DiscoveryOptions=lambda **kw: NS(**kw), SessionOptions=lambda **kw: NS(**kw),
        TurnInput=lambda **kw: NS(**kw), TextPart=lambda text: NS(text=text),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    monkeypatch.delenv("TMUX_FLEET_PROVIDER", raising=False)
    monkeypatch.delenv("AMPLIFIER_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("AMPLIFIER_AGENT_MODEL", raising=False)
    monkeypatch.setattr(agent, "_load_sdk", lambda: sdk)
    return options, stages


def test_extract_json():
    assert smart._extract_json('{"a": 1}') == {"a": 1}
    assert smart._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert smart._extract_json('here:\n{"a": [1]}\nthanks') == {"a": [1]}
    assert smart._extract_json("[1]") == [1]
    with pytest.raises(agent.AgentError, match="parseable JSON"):
        smart._extract_json("synthetic-secret")


def test_missing_binding(monkeypatch):
    real_import = builtins.__import__
    def blocked(name, *a, **kw):
        if name == "amplifier_agent":
            raise ModuleNotFoundError("synthetic-secret")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(agent.AgentUnavailable, match="public Python binding") as exc:
        agent._load_sdk()
    assert "synthetic-secret" not in str(exc.value)


def test_selection_preferences_and_exact_model():
    records = [record("openai", ("OPENAI_API_KEY",)), record()]
    env = {"OPENAI_API_KEY": "fixture", "ANTHROPIC_API_KEY": "fixture"}
    assert agent._select(records, env) == ("anthropic", "claude-sonnet-5-5")
    env.update(TMUX_FLEET_PROVIDER="openai", AMPLIFIER_AGENT_MODEL=" exact-model ")
    assert agent._select(records, env) == ("openai", " exact-model ")


@pytest.mark.parametrize("env,match", [
    ({}, "no AI provider"),
    ({"TMUX_FLEET_PROVIDER": "unknown"}, "unknown"),
    ({"TMUX_FLEET_PROVIDER": "anthropic", "AMPLIFIER_AGENT_PROVIDER": "openai"}, "conflicts"),
    ({"TMUX_FLEET_PROVIDER": "anthropic"}, "ANTHROPIC_API_KEY"),
    ({"OPENAI_API_KEY": "fixture"}, "AMPLIFIER_AGENT_MODEL"),
    ({"ANTHROPIC_API_KEY": "fixture", "AMPLIFIER_AGENT_MODEL": ""}, "empty"),
])
def test_selection_refusals(env, match):
    with pytest.raises(agent.AgentUnavailable, match=match):
        agent._select([record(), record("openai", ("OPENAI_API_KEY",))], env)


def test_selected_missing_install_never_falls_through():
    with pytest.raises(agent.AgentUnavailable, match="not installed"):
        agent._select([record(installed=False), record("openai", ("OPENAI_API_KEY",))],
                      {"ANTHROPIC_API_KEY": "fixture", "OPENAI_API_KEY": "fixture"})


@pytest.mark.parametrize("provider,keys,env,credentials,works", [
    ("github-copilot", ("GITHUB_TOKEN", "GH_TOKEN"), {}, "found", False),
    ("github-copilot", ("GITHUB_TOKEN", "GH_TOKEN"), {"GH_TOKEN": "fixture"}, "found", True),
    ("openai-chatgpt", (), {}, "found", False),
    ("ollama", ("OLLAMA_API_KEY",), {}, "not_required", False),
    ("ollama", ("OLLAMA_API_KEY",), {"OLLAMA_HOST": "http://localhost:11434"}, "not_required", True),
    ("ollama", ("OLLAMA_API_KEY",), {"OLLAMA_HOST": "https://remote.invalid"}, "missing", False),
    ("vllm", ("VLLM_API_KEY",), {"VLLM_BASE_URL": "http://localhost:8000/v1"}, "not_required", True),
    ("gemini", ("GOOGLE_API_KEY", "GEMINI_API_KEY"), {"GEMINI_API_KEY": "fixture"}, "found", True),
    ("azure-openai", ("AZURE_OPENAI_API_KEY",), {"AZURE_OPENAI_API_KEY": "fixture"}, "missing", False),
    ("azure-openai", ("AZURE_OPENAI_API_KEY",), {"AZURE_OPENAI_ENDPOINT": "https://fixture.invalid"}, "missing", False),
    ("azure-openai", ("AZURE_OPENAI_API_KEY",), {
        "AZURE_OPENAI_ENDPOINT": "https://fixture.invalid", "AZURE_CLIENT_ID": "fixture",
        "AZURE_TENANT_ID": "fixture", "AZURE_CLIENT_SECRET": "fixture"}, "missing", True),
])
def test_only_caller_environment_evidence(provider, keys, env, credentials, works):
    rec = record(provider, keys, credentials=credentials)
    assert agent._credential_evidence(rec, env, explicit=False) == works


def test_public_options_owned_paths_environment_and_stdout(monkeypatch, tmp_path, capsys):
    options, stages = fake_sdk(monkeypatch)
    before = dict(os.environ)
    async def scenario():
        owner = await agent.prepare_turn(cwd=tmp_path)
        root = Path(owner._root)
        assert await owner.submit("fixture") == '{"ok":true}'
        await owner.aclose()
        await owner.aclose()
        assert not root.exists()
        assert tmp_path.exists()
    run(scenario())
    opt = options[0]
    assert opt.provider == "anthropic" and opt.model == agent.ANTHROPIC_MODEL
    assert opt.tools == opt.skills == opt.mcp_servers == opt.additional_directories == []
    assert opt.approvals == "deny" and opt.working_directory == str(tmp_path)
    assert not hasattr(opt, "agents")
    assert opt.environment == before == dict(os.environ)
    assert stages[-2:] == ["session-close", "agent-close"]
    output = capsys.readouterr()
    assert output.out == ""
    assert "construction" in output.err and "turn" in output.err and "close" in output.err
    assert "synthetic-secret" not in output.err


@pytest.mark.parametrize("state,error,content", [
    ("failure", None, '{"looks":"successful"}'),
    ("cancelled", None, '{"looks":"successful"}'),
    ("rejected", None, '{"looks":"successful"}'),
    ("success", NS(code="provider_failed", message="synthetic-secret"), '{"ok":true}'),
    ("success", None, ""),
])
def test_terminal_states_not_success(monkeypatch, state, error, content):
    fake_sdk(monkeypatch, result=NS(state=state, error=error, content=[NS(text=content)]))
    async def scenario():
        owner = await agent.prepare_turn()
        root = Path(owner._root)
        try:
            with pytest.raises(agent.AgentError) as exc:
                await owner.submit("fixture")
            assert "synthetic-secret" not in str(exc.value)
        finally:
            await owner.aclose()
        assert not root.exists()
    run(scenario())


def test_transport_error_sanitized(monkeypatch):
    fake_sdk(monkeypatch, raised=RuntimeError("Authorization: synthetic-secret"))
    async def scenario():
        owner = await agent.prepare_turn()
        try:
            with pytest.raises(agent.AgentError, match="sdk_failure") as exc:
                await owner.submit("fixture")
            assert "synthetic-secret" not in str(exc.value)
        finally:
            await owner.aclose()
    run(scenario())


@pytest.mark.parametrize("external", [False, True])
def test_hang_deadline_and_external_cancel_drain(monkeypatch, external):
    _, stages = fake_sdk(monkeypatch, hang=True)
    async def scenario():
        owner = await agent.prepare_turn()
        root = Path(owner._root)
        async def caller():
            try:
                return await owner.submit("fixture", timeout_ms=10)
            finally:
                await owner.aclose()
        task = asyncio.create_task(caller())
        if external:
            while "run" not in stages:
                await asyncio.sleep(0)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if external else agent.AgentError):
            await task
        assert not root.exists() and owner._run.done() and owner._close.done()
        assert stages[-2:] == ["session-close", "agent-close"]
    run(scenario())


def test_partial_construction_and_capture_failure(monkeypatch, tmp_path):
    _, stages = fake_sdk(monkeypatch, session_error=True)
    roots = []
    original = agent.tempfile.mkdtemp
    def temp(**kw):
        root = original(dir=tmp_path, **kw)
        roots.append(Path(root))
        return root
    monkeypatch.setattr(agent.tempfile, "mkdtemp", temp)
    with pytest.raises(agent.AgentUnavailable):
        run(agent.prepare_turn())
    assert stages[-1] == "agent-close" and roots[0].exists()
    # Failed factory returned no session; adapter cannot certify hidden SDK
    # resource shutdown. This mock allocated none, so the test can remove it.
    agent.shutil.rmtree(roots[0])
    fake_sdk(monkeypatch)
    async def broken_capture(**kw):
        raise ValueError("capture failed")
    monkeypatch.setattr(smart.fleet, "list_sessions", broken_capture)
    with pytest.raises(ValueError, match="capture failed"):
        run(smart.triage())
    assert not roots[1].exists()


def test_cleanup_failure_retains_state_and_primary(monkeypatch):
    _, stages = fake_sdk(monkeypatch, close_error=True)
    async def scenario():
        owner = await agent.prepare_turn()
        try:
            try:
                raise ValueError("primary capture failure")
            finally:
                await owner.aclose()
        except ValueError as exc:
            assert "retained" in exc.__notes__[0]
        assert Path(owner._root).exists()
        assert stages[-1] == "agent-close"
        # Mock resources no longer in use; test owns this retained path.
        agent.shutil.rmtree(owner._root)
    run(scenario())


def test_cleanup_total_bound_retains_state(monkeypatch, capsys):
    fake_sdk(monkeypatch)
    monkeypatch.setattr(agent, "CLEANUP_SECONDS", 0.02)
    async def scenario():
        owner = await agent.prepare_turn()
        release = asyncio.Event()
        async def blocked_close():
            await release.wait()
        owner._session.close = blocked_close
        start = asyncio.get_running_loop().time()
        with pytest.raises(agent.AgentError, match="fixed 10s"):
            await owner.aclose()
        assert asyncio.get_running_loop().time() - start < 0.2
        assert Path(owner._root).exists()
        print("caller stdout after bounded return")
        release.set()
        await owner._close
        assert not Path(owner._root).exists()
    run(scenario())
    assert capsys.readouterr().out == "caller stdout after bounded return\n"


def test_split_writes_and_flushes_never_leak(capsys):
    with agent._diagnostics({"ANTHROPIC_API_KEY": "synthetic-secret"}):
        sys.stdout.write("synthetic-")
        sys.stdout.flush()
        sys.stdout.write("secret")
        sys.stdout.flush()
    output = capsys.readouterr()
    assert output.out == "" and output.err == "[redacted]"


def test_stderr_split_writes_never_leak(capsys):
    with agent._diagnostics({"TOKEN": "synthetic-secret"}):
        sys.stderr.write("synthetic-")
        sys.stderr.flush()
        sys.stderr.write("secret\n")
    output = capsys.readouterr()
    assert output.out == "" and output.err == "[redacted]\n"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_logging_prebound_handlers_and_exceptions_are_safe(capsys, stream):
    logger = logging.getLogger("fixture.provider")
    handler = logging.StreamHandler(getattr(sys, stream))  # Before scope entry.
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    original = logging.getLogRecordFactory()
    original_handle = logging.Logger.handle
    try:
        with agent._diagnostics({}):  # Unknown response secret, not an env key.
            try:
                raise RuntimeError("fixture-response-secret")
            except RuntimeError:
                logger.warning("raw response %s", "fixture-response-secret",
                               exc_info=True, stack_info=True)
        logger.warning("caller diagnostic unchanged")
    finally:
        logger.removeHandler(handler)
    assert logging.getLogRecordFactory() is original
    assert logging.Logger.handle is original_handle
    output = capsys.readouterr()
    assert output.out == ("caller diagnostic unchanged\n" if stream == "stdout" else "")
    assert "fixture-response-secret" not in output.out + output.err
    assert "diagnostic omitted" in output.err
    assert "caller diagnostic unchanged" in output.out + output.err


@pytest.mark.parametrize("worker_kind", ["thread", "executor", "to_thread"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_worker_diagnostics_context_boundary(capsys, caplog, worker_kind, stream):
    """Synthetic emission characterizes context routing, not an installed SDK leak."""
    watchdog = 5  # Test wait bound, not a product shutdown deadline.
    env_secret = "synthetic-worker-env-secret"
    response_secret = "synthetic-worker-response-secret"
    caller_secret = "synthetic-caller-response-secret"
    original_stdout, original_stderr = sys.stdout, sys.stderr
    original_handle = logging.Logger.handle
    original_factory = logging.getLogRecordFactory()
    logger = logging.getLogger("fixture.worker_boundary")
    original_level, original_propagate = logger.level, logger.propagate
    handler = logging.StreamHandler(getattr(sys, stream))  # Prebound before scope.
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    logger.addHandler(handler)
    logger.addHandler(caplog.handler)
    release = threading.Event()
    owned_threads = []
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diagnostics-test")

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(pool)
        started = loop.create_future()
        scope_ready = asyncio.Event()
        worker_handle = None
        raw_thread = None

        def emit():
            loop.call_soon_threadsafe(started.set_result, threading.current_thread())
            if not release.wait(watchdog):
                raise TimeoutError("test worker release gate timed out")
            for destination in (sys.stdout, sys.stderr):
                destination.write("synthetic-worker-env-")
                destination.flush()
                destination.write("secret\n")
                destination.flush()
            logger.warning("worker response %s", response_secret)

        async def protected():
            nonlocal worker_handle, raw_thread
            with agent._diagnostics({"TOKEN": env_secret}):
                if worker_kind == "thread":
                    completion = Future()

                    def target():
                        try:
                            emit()
                        except BaseException as exc:
                            completion.set_exception(exc)
                        else:
                            completion.set_result(None)

                    # Explicit empty contexts remain unpropagated even on runtimes
                    # whose raw thread defaults might inherit the starting context.
                    raw_thread = threading.Thread(
                        target=contextvars.Context().run, args=(target,),
                        name="diagnostics-raw-thread",
                    )
                    raw_thread.start()
                    worker_handle = asyncio.wrap_future(completion)
                elif worker_kind == "executor":
                    worker_handle = loop.run_in_executor(
                        pool, contextvars.Context().run, emit,
                    )
                else:
                    worker_handle = asyncio.create_task(asyncio.to_thread(emit))
                try:
                    worker = await asyncio.wait_for(asyncio.shield(started), watchdog)
                    owned_threads.append(worker)
                    assert worker is not threading.current_thread() and worker.is_alive()
                    scope_ready.set()
                    await asyncio.wait_for(asyncio.shield(worker_handle), watchdog)
                finally:
                    # Drain before scope exit even when readiness/assertions fail.
                    release.set()
                    try:
                        await asyncio.wait_for(asyncio.shield(worker_handle), watchdog)
                    finally:
                        if raw_thread is not None:
                            raw_thread.join(watchdog)
                            assert not raw_thread.is_alive()

        task = asyncio.create_task(protected())
        try:
            await asyncio.wait_for(scope_ready.wait(), watchdog)
            # This task has no diagnostics context, while the protected task and
            # its real worker are both active. Even the same env value is unchanged.
            print(f"caller {env_secret}")
            sys.stderr.write(f"caller {env_secret}\n")
            logger.warning("caller response %s", caller_secret)
            release.set()
            await asyncio.wait_for(asyncio.shield(task), watchdog)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.shield(task), watchdog)

    try:
        run(scenario())
    finally:
        release.set()
        pool.shutdown(wait=True)
        logger.removeHandler(handler)
        handler.close()
        logger.removeHandler(caplog.handler)
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        assert sys.stdout is original_stdout and sys.stderr is original_stderr
        assert logging.Logger.handle is original_handle
        assert logging.getLogRecordFactory() is original_factory
        assert all(not worker.is_alive() for worker in owned_threads)

    output = capsys.readouterr()
    caller_log = f"caller response {caller_secret}\n"
    expected_out = f"caller {env_secret}\n" + (caller_log if stream == "stdout" else "")
    expected_err = f"caller {env_secret}\n" + (caller_log if stream == "stderr" else "")
    records = [rec for rec in caplog.records if rec.name == logger.name]
    assert records[0].msg == "caller response %s"
    assert records[0].args == (caller_secret,)
    if worker_kind == "to_thread":
        assert output.out == expected_out
        assert output.err.startswith(expected_err)
        assert output.err[len(expected_err):].startswith("[redacted]\n[redacted]\n")
        assert output.err.count("diagnostic omitted") == 1
        assert response_secret not in output.out + output.err
        assert len(records) == 1  # Omitted before either prebound handler runs.
    else:
        # PASS here proves the boundary exists, NOT that SDK thread secrets are
        # prevented: unpropagated workers intentionally retain caller-side routing.
        worker_log = f"worker response {response_secret}\n"
        assert output.out == expected_out + env_secret + "\n" + (
            worker_log if stream == "stdout" else ""
        )
        assert output.err == expected_err + env_secret + "\n" + (
            worker_log if stream == "stderr" else ""
        )
        assert len(records) == 2
        assert records[1].msg == "worker response %s"
        assert records[1].args == (response_secret,)


@pytest.mark.parametrize("field,message,expected", [
    ("config", "config: the configured file does not exist.", "Create the file"),
    ("config", "config: cannot read valid JSON settings.", "valid JSON object"),
    ("synthetic-secret", "synthetic-secret: unregistered host setting.", "Remove unregistered"),
    ("reasoning_effort", "reasoning_effort: 'synthetic-secret' is invalid.", "AMPLIFIER_AGENT_REASONING_EFFORT"),
    ("synthetic-secret", "synthetic-secret", "public SDK configuration schema"),
])
def test_safe_config_remedies(field, message, expected):
    error = NS(code="invalid_input", message=message, remedy="synthetic-secret",
               details={"field": field, "raw": "synthetic-secret"})
    text = str(agent._safe_error(error, startup=True))
    assert expected in text and "synthetic-secret" not in text


def test_writelines_routes_to_safe_diagnostics(capsys):
    with agent._diagnostics({"TOKEN": "synthetic-secret"}):
        sys.stdout.writelines(["synthetic-", "secret\n"])
    output = capsys.readouterr()
    assert output.out == "" and output.err == "[redacted]\n"


@pytest.mark.parametrize("close_fails", [False, True])
def test_late_sdk_output_after_timeout_or_failed_close(monkeypatch, capsys, close_fails):
    fake_sdk(monkeypatch)
    async def scenario():
        owner = await agent.prepare_turn()
        release = asyncio.Event()
        async def execute(value):
            await release.wait()
            print("late synthetic-secret")
            if close_fails:
                raise RuntimeError("late synthetic-secret")
            return NS(state="success", error=None, content=[NS(text="{}")])
        async def close_session():
            if close_fails:
                raise RuntimeError("synthetic-secret")
            release.set()
        owner._session.run = execute
        owner._session.close = close_session
        with pytest.raises(agent.AgentError, match="budget"):
            await owner.submit("fixture", timeout_ms=1)
        if close_fails:
            with pytest.raises(agent.AgentError, match="retained"):
                await owner.aclose()
            print("caller after failure")
            release.set()
            await asyncio.gather(owner._run, return_exceptions=True)
            # No live mock resource remains; this test owns retained state.
            agent.shutil.rmtree(owner._root)
        else:
            await owner.aclose()
    run(scenario())
    output = capsys.readouterr()
    assert output.out == ("caller after failure\n" if close_fails else "")
    assert "late [redacted]" in output.err
    assert "synthetic-secret" not in output.out + output.err


def test_cancel_during_cleanup_preserves_cancel_even_on_failure(monkeypatch):
    fake_sdk(monkeypatch)
    async def scenario():
        owner = await agent.prepare_turn()
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked_close():
            entered.set()
            await release.wait()
            raise RuntimeError("synthetic-secret")
        owner._session.close = blocked_close
        closing = asyncio.create_task(owner.aclose())
        await entered.wait()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as exc:
            await closing
        assert "retained" in exc.value.__notes__[0]
        assert Path(owner._root).exists()
        agent.shutil.rmtree(owner._root)
    run(scenario())


@pytest.mark.parametrize("stage", ["discovery", "construction"])
def test_cancel_during_startup_drains_owned_sdk_operation(monkeypatch, tmp_path, stage):
    fake_sdk(monkeypatch)
    sdk = agent._load_sdk()
    roots = []
    original = agent.tempfile.mkdtemp
    def temp(**kw):
        root = original(dir=tmp_path, **kw)
        roots.append(Path(root))
        return root
    monkeypatch.setattr(agent.tempfile, "mkdtemp", temp)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original_operation = sdk.list_providers if stage == "discovery" else sdk.create_agent
        async def delayed(options):
            entered.set()
            await release.wait()
            return await original_operation(options)
        if stage == "discovery":
            sdk.list_providers = delayed
        else:
            sdk.create_agent = delayed
        task = asyncio.create_task(agent.prepare_turn())
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(not root.exists() for root in roots)
        assert len(asyncio.all_tasks()) == 1
    run(scenario())


def test_overlapping_sdk_diagnostic_scopes(capsys):
    async def scenario():
        first, second = asyncio.Event(), asyncio.Event()
        async def a():
            with agent._diagnostics({"TOKEN": "synthetic-secret"}):
                first.set()
                await second.wait()
                print("A synthetic-secret")
        async def b():
            await first.wait()
            with agent._diagnostics({"TOKEN": "synthetic-secret"}):
                second.set()
                await asyncio.sleep(0)
                print("B synthetic-secret")
        await asyncio.gather(a(), b())
        print("caller")
    run(scenario())
    output = capsys.readouterr()
    assert output.out == "caller\n"
    assert "synthetic-secret" not in output.err
    assert "A [redacted]" in output.err and "B [redacted]" in output.err


def test_overlapping_diagnostics_restore_stderr_and_logging(capsys):
    original_stream = sys.stderr
    original_factory = logging.getLogRecordFactory()
    original_handle = logging.Logger.handle
    async def scenario():
        first, second = asyncio.Event(), asyncio.Event()
        async def a():
            with agent._diagnostics({"TOKEN": "synthetic-secret"}):
                first.set()
                await second.wait()
                sys.stderr.write("A synthetic-secret\n")
                logging.warning("A fixture-response-secret")
        async def b():
            await first.wait()
            with agent._diagnostics({"TOKEN": "synthetic-secret"}):
                second.set()
                await asyncio.sleep(0)
                sys.stderr.write("B synthetic-secret\n")
                logging.warning("B fixture-response-secret")
        async def caller():
            await first.wait()
            sys.stderr.write("caller stderr\n")
            logging.warning("caller log")
        await asyncio.gather(a(), b(), caller())
    run(scenario())
    assert sys.stderr is original_stream
    assert logging.getLogRecordFactory() is original_factory
    assert logging.Logger.handle is original_handle
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.err
    assert "A [redacted]" in output.err and "B [redacted]" in output.err
    assert "caller stderr" in output.err


def test_cli_refusal_envelope(monkeypatch, capsys):
    async def refused(*a, **kw):
        raise agent.AgentUnavailable(agent._no_provider_hint())
    monkeypatch.setattr(agent, "prepare_turn", refused)
    assert cli.main(["triage", "--socket-dir", NO_SOCKET]) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["error"]["code"] == "agent_unavailable"
    assert result["error"]["remedy"]


def test_deterministic_paths_block_sdk_and_preserve_environment():
    code = textwrap.dedent(f"""
        import builtins, os, sys
        real = builtins.__import__
        def blocked(name, *a, **kw):
            if name.startswith("amplifier_agent"):
                raise AssertionError("SDK imported")
            return real(name, *a, **kw)
        builtins.__import__ = blocked
        before = dict(os.environ)
        from tmux_fleet import cli
        for flag in ("--help", "-V", "--version"):
            try:
                cli.main([flag])
            except SystemExit as exc:
                assert exc.code == 0
        cli.main(["doctor", "--socket-dir", {NO_SOCKET!r}])
        cli.main(["sessions", "--socket-dir", {NO_SOCKET!r}])
        assert before == dict(os.environ)
        assert not any("amplifier_agent" in m for m in sys.modules)
    """)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AMPLIFIER_AGENT_", "ANTHROPIC_", "OPENAI_", "AZURE_", "GOOGLE_", "GEMINI_"))
           and not any(term in k for term in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env["AMPLIFIER_AGENT_CONFIG"] = "/tmp/tmux-fleet-missing-sdk-config.json"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr