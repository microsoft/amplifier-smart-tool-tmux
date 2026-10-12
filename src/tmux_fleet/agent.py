"""Lazy one-shot public Python binding; no private engine access or tool authority.

Caller environment selects the provider/model. Normal SDK config still validates
and may deliberately forward captures. Owned state is removed after public close.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import io
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

PROVIDER_ENV_VAR = "TMUX_FLEET_PROVIDER"
PROVIDER_PREFERENCE = ("anthropic", "openai", "gemini", "azure-openai")
DEFAULT_TIMEOUT_MS = 180_000
CLEANUP_SECONDS = 10.0
ANTHROPIC_MODEL = "claude-sonnet-5-5"
_LOCAL_ENDPOINTS = {
    "ollama": "OLLAMA_HOST", "vllm": "VLLM_BASE_URL",
    "chat-completions": "CHAT_COMPLETIONS_BASE_URL",
}


class AgentUnavailable(RuntimeError):
    """A required binding, provider, model or credential is unavailable."""


class AgentError(RuntimeError):
    """A turn or its safe shutdown failed."""


def _load_sdk() -> Any:
    try:
        import amplifier_agent
    except ImportError:
        raise AgentUnavailable(
            "the amplifier-agent public Python binding (amplifier_agent) could not "
            "be imported. Reinstall tmux-fleet or run `uv sync --locked`; its "
            "tagged binding and provider dependencies are regular dependencies."
        ) from None
    return amplifier_agent


def _no_provider_hint() -> str:
    return (
        "no AI provider is configured in the caller environment. Set "
        "ANTHROPIC_API_KEY, or set TMUX_FLEET_PROVIDER and AMPLIFIER_AGENT_MODEL "
        "with that provider's environment credentials. Saved logins are not used."
    )


def _credential_evidence(record: Any, env: dict[str, str], *, explicit: bool) -> bool:
    provider = record.provider
    if provider == "openai-chatgpt":
        return False  # Only cached OAuth is supported; no caller-env credential.
    if provider == "azure-openai":
        return bool(env.get("AZURE_OPENAI_ENDPOINT")) and bool(
            env.get("AZURE_OPENAI_API_KEY")
            or all(env.get(k) for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"))
        )
    key = any(env.get(k) for k in record.credential_variables)
    if provider in _LOCAL_ENDPOINTS:
        endpoint = env.get(_LOCAL_ENDPOINTS[provider])
        # Discovery distinguishes local unauthenticated from remote servers;
        # default localhost alone must not create an automatic candidate.
        return bool(key or (record.credentials == "not_required" and (explicit or endpoint)))
    return key  # Discovery's cached gh login cannot satisfy this check.


def _select(records: list[Any], env: dict[str, str]) -> tuple[str, str]:
    fleet_pin = env.get(PROVIDER_ENV_VAR, "").strip()
    sdk_pin = env.get("AMPLIFIER_AGENT_PROVIDER", "").strip()
    if fleet_pin and sdk_pin and fleet_pin != sdk_pin:
        raise AgentUnavailable(
            "TMUX_FLEET_PROVIDER conflicts with AMPLIFIER_AGENT_PROVIDER. "
            "Set both to the same provider or unset one."
        )
    pin = fleet_pin or sdk_pin
    by_name = {r.provider: r for r in records}
    if pin:
        if pin not in by_name:
            raise AgentUnavailable(
                "the pinned provider is unknown. Set TMUX_FLEET_PROVIDER / "
                "AMPLIFIER_AGENT_PROVIDER to a provider from public list_providers()."
            )
        selected = by_name[pin]
    else:
        eligible = [r for r in records if _credential_evidence(r, env, explicit=False)]
        if not eligible:
            raise AgentUnavailable(_no_provider_hint())
        selected = next(
            (by_name[p] for p in PROVIDER_PREFERENCE if any(r.provider == p for r in eligible)),
            eligible[0],
        )
    provider = selected.provider
    if not selected.installed:
        raise AgentUnavailable(
            f"the selected {provider} provider is not installed. Repair the "
            "tmux-fleet / amplifier-agent installation (Copilot needs the SDK's "
            "github-copilot extra). Selection never falls through."
        )
    if not _credential_evidence(selected, env, explicit=bool(pin)):
        variables = " or ".join(selected.credential_variables) or "supported environment credentials"
        if provider == "azure-openai":
            variables = (
                "AZURE_OPENAI_ENDPOINT plus AZURE_OPENAI_API_KEY, or all of "
                "AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET"
            )
        raise AgentUnavailable(
            f"the selected {provider} provider has no usable caller-environment "
            f"credentials. Set {variables}; saved logins and implicit managed "
            "identity are not used by tmux-fleet."
        )
    model = env.get("AMPLIFIER_AGENT_MODEL")
    if model is None:
        if provider != "anthropic":
            raise AgentUnavailable(
                f"the selected {provider} provider requires AMPLIFIER_AGENT_MODEL. "
                "Set its exact matching model ID (Azure: deployment ID); no "
                "first-model or Anthropic default is guessed."
            )
        model = ANTHROPIC_MODEL
    if not model.strip():
        raise AgentUnavailable("AMPLIFIER_AGENT_MODEL is empty. Set an exact matching model ID.")
    return provider, model


def _input_remedy(error: Any) -> str:
    """Translate public error discriminators, never echo arbitrary values/paths."""
    details = getattr(error, "details", None)
    field = details.get("field") if isinstance(details, dict) else None
    message = getattr(error, "message", "")
    location = "AMPLIFIER_AGENT_CONFIG (otherwise ~/.amplifier-agent/config.json)"
    if field == "config":
        if message == "config: the configured file does not exist.":
            return "Create the file selected by AMPLIFIER_AGENT_CONFIG or unset AMPLIFIER_AGENT_CONFIG."
        return f"Correct {location}: use a readable, valid JSON object."
    if isinstance(message, str) and message.endswith(": unregistered host setting."):
        return (
            f"Remove unregistered top-level settings from {location}. Allowed: provider, "
            "model, reasoning_effort, sessions_directory, approvals, extra_request_params, "
            "context_intelligence."
        )
    # Only fixed field names and fixed remedies leave this boundary. An SDK
    # error's unknown field name can itself contain credentials.
    fields = {
        "reasoning_effort": "Set reasoning_effort / AMPLIFIER_AGENT_REASONING_EFFORT to none, minimal, low, medium, high, xhigh or max, or omit it; use an effort supported by the selected model.",
        "provider": "Set TMUX_FLEET_PROVIDER / AMPLIFIER_AGENT_PROVIDER to a registered provider.",
        "model": "Set AMPLIFIER_AGENT_MODEL to one nonempty matching model/deployment ID.",
        "working_directory": "Pass an existing directory via cwd (not a file) when preparing the turn.",
        "sessions_directory": "Set sessions_directory / AMPLIFIER_AGENT_SESSIONS_DIRECTORY to a nonempty writable path.",
        "approvals": "Set host approvals / AMPLIFIER_AGENT_APPROVALS to allow or deny; this tool still explicitly denies approvals.",
        "extra_request_params": "Correct extra_request_params in the SDK config to a JSON object of supported request settings.",
        "context_intelligence": "Correct context_intelligence.destinations in the SDK config using the public SDK configuration schema.",
    }
    if isinstance(field, str) and field in fields:
        return fields[field]
    return f"Check {location} and supported AMPLIFIER_AGENT_* variables against the public SDK configuration schema."


def _safe_error(error: Any, *, startup: bool = False) -> RuntimeError:
    # SDK messages/remedies/details and raw transport exceptions may contain
    # response bodies, endpoint credentials or authorization headers.
    code = getattr(error, "code", "")
    remedies = {
        "invalid_configuration": "Check the SDK host config and explicit provider/model settings.",
        "invalid_input": _input_remedy(error),
        "selector_rejected": "Set AMPLIFIER_AGENT_MODEL to a model/deployment this provider accepts.",
        "engine_unavailable": "Repair the binding/provider install and caller environment credentials.",
        "provider_failed": "Check caller credentials, provider availability and model compatibility.",
        "turn_cancelled": "Start a new invocation when the provider is ready.",
    }
    remedy = remedies.get(code, "Check SDK configuration, provider credentials and connectivity.")
    label = code if code in remedies else "sdk_failure"
    cls = AgentUnavailable if startup else AgentError
    return cls(f"amplifier-agent {label}. {remedy}")


class _Diagnostics(io.TextIOBase):
    """Buffer incidental stdout so even split writes cannot expose a key."""

    def __init__(self, env: dict[str, str]) -> None:
        self._buffer = ""
        self._secrets = sorted(
            {v for k, v in env.items() if v and any(s in k for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))},
            key=len, reverse=True,
        )

    def write(self, text: str) -> int:
        self._buffer += text
        return len(text)

    def flush(self) -> None:
        # SDK flush boundaries must not split a secret across stderr writes.
        # One-shot diagnostics are emitted only at the lifecycle boundary.
        pass

    def finish(self, destination) -> None:
        text, self._buffer = self._buffer, ""
        for value in self._secrets:
            text = text.replace(value, "[redacted]")
        destination.write(text)
        destination.flush()


_stdout_sink = contextvars.ContextVar("_tmux_fleet_sdk_stdout", default=None)


class _StdoutRouter(io.TextIOBase):
    """Overlapping SDK scopes restore once; unrelated tasks keep their stdout."""

    def __init__(self, original):
        self.original = original
        self.users = 0

    def write(self, text):
        target = _stdout_sink.get()
        return (target if target is not None else self.original).write(text)

    def flush(self):
        target = _stdout_sink.get()
        return (target if target is not None else self.original).flush()

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    @property
    def encoding(self):
        return self.original.encoding

    @property
    def errors(self):
        return self.original.errors

    def isatty(self):
        return self.original.isatty()

    def writable(self):
        return True

    # Raw descriptors/binary streams are deliberately not delegated: SDK
    # lifecycle diagnostics use the text stream, not an interception bypass.


class _SdkLogRouter:
    """Omit payloads before handlers, even those holding old stdout/stderr.

    Raw SDK/provider response bodies are not useful safe diagnostics. Suppress
    their message/exception payload entirely, not just known environment keys.
    Caller tasks outside the SDK scope retain their original records.
    """

    def __init__(self, original):
        self.original = original
        self.users = 0

    def __get__(self, instance, owner):
        return self if instance is None else functools.partial(self, instance)

    def __call__(self, logger, record):
        sink = _stdout_sink.get()
        if sink is not None:
            sink.write("amplifier-agent diagnostic omitted (raw SDK/provider logs may contain secrets).\n")
            return
        return self.original(logger, record)


@contextlib.contextmanager
def _diagnostics(env: dict[str, str]):
    sink = _Diagnostics(env)
    router = sys.stdout if isinstance(sys.stdout, _StdoutRouter) else _StdoutRouter(sys.stdout)
    sys.stdout = router
    router.users += 1
    stderr = sys.stderr if isinstance(sys.stderr, _StdoutRouter) else _StdoutRouter(sys.stderr)
    sys.stderr = stderr
    stderr.users += 1
    logs = logging.Logger.handle
    logs = logs if isinstance(logs, _SdkLogRouter) else _SdkLogRouter(logs)
    logging.Logger.handle = logs
    logs.users += 1
    token = _stdout_sink.set(sink)
    try:
        yield
    finally:
        _stdout_sink.reset(token)
        router.users -= 1
        if not router.users and sys.stdout is router:
            sys.stdout = router.original
        stderr.users -= 1
        if not stderr.users and sys.stderr is stderr:
            sys.stderr = stderr.original
        logs.users -= 1
        if not logs.users and logging.Logger.handle is logs:
            logging.Logger.handle = logs.original
        # Diagnostics failure is secondary, never a replacement exception.
        with contextlib.suppress(OSError, ValueError):
            sink.finish(stderr.original)


class AgentSession:
    """Caller-owned session with a fixed total cleanup tail, not per-step budgets."""

    def __init__(self, sdk: Any, env: dict[str, str], root: str, provider: str) -> None:
        self._sdk, self._env, self._root = sdk, env, root
        self.provider = provider
        self._agent: Any = None
        self._session: Any = None
        self._run: asyncio.Task | None = None
        self._close: asyncio.Task | None = None
        self._close_deadline: float | None = None
        self._submitted = False
        self._startup: asyncio.Task | None = None
        self._uncertain = False

    async def _construct(self, options: Any) -> None:
        with _diagnostics(self._env):
            await self._construct_resources(options)

    async def _construct_resources(self, options: Any) -> None:
        try:
            self._agent = await self._sdk.create_agent(options)
            self._session = await self._agent.create_session(self._sdk.SessionOptions(persistence="ephemeral"))
        except BaseException:
            # A failed public factory can allocate a runtime but return no handle.
            # Agent.close alone cannot certify that unreturned runtime is drained.
            self._uncertain = True
            raise

    async def submit(self, prompt: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
        if self._submitted or self._close is not None:
            raise AgentError("This adapter accepts one turn only; prepare a new invocation.")
        self._submitted = True
        try:
            with _diagnostics(self._env):
                self._run = asyncio.create_task(self._execute(prompt))
                self._run.add_done_callback(lambda task: None if task.cancelled() else task.exception())
                done, _ = await asyncio.wait({self._run}, timeout=max(timeout_ms, 1) / 1000)
                if not done:
                    raise AgentError(
                        f"amplifier-agent turn exceeded its {timeout_ms} ms budget. "
                        "Check provider connectivity or increase --timeout-ms."
                    )
                result = self._run.result()
            if result.state != "success" or result.error is not None:
                raise _safe_error(result.error)
            text = "".join(part.text for part in result.content or [])
            if not text.strip():
                raise AgentError("amplifier-agent returned empty content; a structured answer is required.")
            return text
        except (AgentError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise _safe_error(exc) from None

    async def _execute(self, prompt: str):
        # This scope belongs to the operation, not the waiting caller. It stays
        # alive through late SDK output after timeout or failed public close.
        with _diagnostics(self._env):
            return await self._session.run(
                self._sdk.TurnInput(content=[self._sdk.TextPart(prompt)])
            )

    async def _drain(self) -> None:
        with _diagnostics(self._env):
            await self._drain_resources()

    async def _drain_resources(self) -> None:
        failed = False
        if self._startup is not None:
            await asyncio.gather(self._startup, return_exceptions=True)
        # Public session.close cancels/drains the independent SDK turn. Attempt
        # agent close even if session close raises; never remove uncertain state.
        if self._session is not None:
            try:
                await self._session.close()
            except BaseException:
                failed = True
        if self._agent is not None:
            try:
                await self._agent.close()
            except BaseException:
                failed = True
        if failed or self._uncertain:
            raise AgentError("Public SDK shutdown of partial/failed resources cannot be certified.")
        if self._run is not None:
            await asyncio.gather(self._run, return_exceptions=True)
        # Safe drain established; deletion must not block the event-loop deadline.
        await asyncio.to_thread(shutil.rmtree, self._root)

    async def aclose(self) -> None:
        # Task-local routing means bounded return never captures unrelated
        # caller stdout, even when an uncooperative SDK close is still active.
        with _diagnostics(self._env):
            await self._aclose()

    async def _aclose(self) -> None:
        primary = sys.exception()
        loop = asyncio.get_running_loop()
        if self._close is None:
            self._close_deadline = loop.time() + CLEANUP_SECONDS
            self._close = asyncio.create_task(self._drain())
            self._close.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        failure = None
        interrupted = None
        while not self._close.done():
            remaining = max(0.0, self._close_deadline - loop.time())
            if not remaining:
                failure = "cleanup exceeded its fixed 10s total tail"
                break
            try:
                await asyncio.wait({self._close}, timeout=remaining)
            except asyncio.CancelledError as exc:
                interrupted = exc  # Drain without resetting the budget.
        if self._close.done():
            if self._close.cancelled() or self._close.exception() is not None:
                failure = "public SDK close or local cleanup failed"
        if failure:
            message = (
                f"amplifier-agent {failure}; owned state retained at {self._root}. "
                "Do not remove it while SDK resources remain in use."
            )
            existing = primary or interrupted
            if existing is not None:
                existing.add_note(message)
                with contextlib.suppress(OSError, ValueError):
                    sys.stderr.write(message + "\n")
            else:
                raise AgentError(message) from None
        if interrupted is not None and primary is None:
            raise interrupted


async def _discover(sdk: Any, env: dict[str, str]):
    with _diagnostics(env):
        return await sdk.list_providers(sdk.DiscoveryOptions(environment=env))


async def prepare_turn(*, cwd: str | os.PathLike[str] | None = None) -> AgentSession:
    """Select and construct before fleet capture; caller must always aclose."""
    env = dict(os.environ)
    with _diagnostics(env):
        sdk = _load_sdk()
        try:
            discovery = asyncio.create_task(_discover(sdk, env))
            discovery.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            try:
                records = await asyncio.shield(discovery)
            except asyncio.CancelledError as primary:
                # Let discovery's own subprocess timeout reap a cached-gh probe.
                # Do not cancel an SDK operation whose partial ownership is hidden.
                deadline = asyncio.get_running_loop().time() + CLEANUP_SECONDS
                while not discovery.done():
                    remaining = max(0, deadline - asyncio.get_running_loop().time())
                    if not remaining:
                        primary.add_note("Public SDK discovery did not drain within the fixed 10s tail.")
                        break
                    try:
                        await asyncio.wait({discovery}, timeout=remaining)
                    except asyncio.CancelledError:
                        pass
                raise
        except Exception as exc:
            raise _safe_error(exc, startup=True) from None
    provider, model = _select(records, env)
    try:
        root = tempfile.mkdtemp(prefix="tmux-fleet-agent-")
    except OSError:
        raise AgentUnavailable(
            "Could not allocate owned temporary SDK state. Check the temporary directory is writable."
        ) from None
    owner = AgentSession(sdk, env, root, provider)
    work = Path(root) / "work"
    try:
        work.mkdir()
        with _diagnostics(env):
            options = sdk.AgentOptions(
                provider=provider, model=model, tools=[], skills=[], mcp_servers=[],
                approvals="deny", additional_directories=[],
                working_directory=str(cwd) if cwd is not None else work,
                sessions_directory=Path(root) / "sessions", environment=env,
            )
            owner._startup = asyncio.create_task(owner._construct(options))
            await asyncio.shield(owner._startup)
        return owner
    except asyncio.CancelledError:
        await owner.aclose()
        raise
    except Exception as exc:
        await owner.aclose()
        raise _safe_error(exc, startup=True) from None