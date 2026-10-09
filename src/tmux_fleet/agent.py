"""The amplifier-agent substrate: how the smart verbs execute a model turn.

Contractually (VISION section 3, cli.v1 rule 3), model-backed capabilities
execute through **amplifier-agent's public Python binding**, imported IN-PROCESS. The
tool's process is per-invocation and single-turn -- it is itself the isolation
boundary, so there is no CLI subprocess between a verb and its embedded
engine. This tool never carries provider credentials and never re-implements an
agent loop. The binding and its declared engine/provider dependencies install
together; provider keys arrive from the caller's environment at runtime.

The public Python binding is imported lazily inside :func:`prepare_turn`.
Deterministic verbs and help never import or initialize the agent. Explicit
provider/model selection, an empty tool set and denied approvals bound execution.

Refusal taxonomy (cli.v1 rule 3). A smart verb with no usable substrate FAILS
naming exactly which precondition is missing -- **never** a silent fallback to a
deterministic approximation. The five distinct preconditions, each with its own
remedy:

  1. **engine dependency missing** -- ``amplifier_agent`` is not importable
     (the ``amplifier-agent`` package failed to install).
  2. **provider SDK extra not installed** -- the chosen provider's SDK (e.g.
     ``anthropic``) is not importable; repair the declared runtime dependencies.
  3. **no provider configured** -- no provider has resolvable credentials and
     none was pinned; set a provider key or pin ``TMUX_FLEET_PROVIDER``.
  4. **public construction refusal** -- the pinned provider's public Agent
     reports an authentication or configuration failure, with its own remedy.

  5. **no model selected** -- set ``TMUX_FLEET_MODEL`` (or
     ``AMPLIFIER_AGENT_MODEL``); there is no cross-provider model fallback.

Cases 1-5 surface as :class:`AgentUnavailable`. A turn that ran and
then failed (or returned nothing usable) is a distinct :class:`AgentError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import amplifier_agent

#: Optional pin: force a specific provider short-name (e.g. ``anthropic``)
#: instead of auto-selecting from whichever providers have resolvable
#: credentials. Registered pins reach public construction even when discovery's
#: credential observation is advisory (OAuth or Azure identity).
PROVIDER_ENV_VAR = "TMUX_FLEET_PROVIDER"

#: Auto-selection preference order, applied over the providers that actually
#: have resolvable credentials. A resolvable provider outside this list is still
#: usable (it is picked last, in the engine's own known order).
PROVIDER_PREFERENCE = ("anthropic", "openai", "gemini", "azure-openai")

#: The install extra that ships each provider's SDK, named in the taxonomy-case-2
#: remedy. Providers absent here fall back to a generic hint.
_PROVIDER_EXTRA = {
    "anthropic": "anthropic",
    "openai": "openai",
}

#: Default single-turn budget for a smart verb, in milliseconds. Triage and
#: interpret are one turn each; generous enough for a real model round-trip,
#: bounded so a hung turn surfaces as a failure rather than a hang. The timeout
#: is OWNED HERE, wrapped around the turn -- there is no subprocess to bound.
DEFAULT_TIMEOUT_MS = 180_000


class AgentUnavailable(RuntimeError):
    """A required substrate precondition is missing (taxonomy cases 1-5).

    This is the fail-loud a smart verb raises INSTEAD of degrading to a
    deterministic answer. Its message names the missing precondition and the
    remedy.
    """


class AgentError(RuntimeError):
    """The engine ran but the turn failed, timed out, or produced no output."""


# --------------------------------------------------------------------------
# Refusal-hint builders -- one per taxonomy case. Each names the precondition
# AND a concrete remedy (cli.v1 rule 3 / VISION principle 6).
# --------------------------------------------------------------------------


def _engine_missing_hint(detail: object = "") -> str:
    suffix = f" ({detail})" if detail else ""
    return (
        "the amplifier-agent public Python binding (amplifier_agent) could not be "
        f"imported{suffix}. The engine is a regular dependency of this tool -- "
        "its absence means the install is broken. Reinstall the tool (e.g. "
        '`uv sync`, or `pip install "tmux-fleet[anthropic]"`) so amplifier-agent '
        "is present. The deterministic verbs of this tool need none of this."
    )


def _provider_sdk_missing_hint(provider: str, detail: object = "") -> str:
    suffix = f" ({detail})" if detail else ""
    extra = _PROVIDER_EXTRA.get(provider)
    if extra:
        install = (
            f'install the matching extra: `pip install "tmux-fleet[{extra}]"` '
            f'(or `uv add "tmux-fleet[{extra}]"`)'
        )
    else:
        install = f"install the Python SDK the {provider!r} provider requires into this tool's environment"
    return (
        f"the {provider!r} provider was selected but its Python SDK is not "
        f"installed{suffix}. Repair the declared runtime dependencies or {install}."
    )


def _no_provider_hint() -> str:
    return (
        "no AI provider is configured for automatic selection. Configure a provider "
        f"and its SDK, or pin a registered {PROVIDER_ENV_VAR} and set "
        "TMUX_FLEET_MODEL. Discovery alone does not establish authentication. This tool never "
        "stores credentials of its own. The deterministic verbs need none of this."
    )


# The supported Python binding owns engine construction and provider validation.
# No private engine or former CLI modules are imported by this adapter.
def _load_engine():
    try:
        import amplifier_agent
    except (ImportError, OSError) as exc:
        raise AgentUnavailable(_engine_missing_hint(exc)) from exc
    return amplifier_agent


def _select_provider(
    records: list[amplifier_agent.ProviderRecord], *, override: str | None
) -> amplifier_agent.ProviderRecord:
    """Choose the provider to run a turn through, or refuse naming why.

    Discovery is advisory for explicit pins. Automatic selection requires an
    installed SDK and found credentials, or an explicitly configured local endpoint.
    The preference order is unchanged; the Agent owns authentication.
    """
    if override:
        record = next((r for r in records if r.provider == override), None)
        if record is None:
            raise AgentUnavailable(
                f"Provider {override!r} pinned via {PROVIDER_ENV_VAR} is not registered. "
                "Select one of the registered provider IDs: "
                + ", ".join(r.provider for r in records)
                + "."
            )
        return record
    resolvable = [
        r
        for r in records
        if r.installed
        and (
            r.credentials == "found"
            or (
                r.credentials == "not_required"
                and (
                    (r.provider == "ollama" and os.environ.get("OLLAMA_HOST"))
                    or (r.provider == "vllm" and os.environ.get("VLLM_BASE_URL"))
                    or (
                        r.provider == "chat-completions"
                        and os.environ.get("CHAT_COMPLETIONS_BASE_URL")
                    )
                )
            )
        )
    ]
    if not resolvable:
        raise AgentUnavailable(_no_provider_hint())
    for preferred in PROVIDER_PREFERENCE:
        for record in resolvable:
            if record.provider == preferred:
                return record
    return resolvable[0]


def _public_error_detail(error: amplifier_agent.AgentError) -> str:
    return f"{error.code} [{error.category}]: {error.message} Remedy: {error.remedy}"


def _cleanup_note(
    error: BaseException,
    directory: str,
    fault: BaseException,
    *,
    phase: str = "Startup",
) -> str:
    note = (
        f"{phase} cleanup failed; directories retained at {directory}: "
        f"{type(fault).__name__}: {fault}"
    )
    error.add_note(note)
    print(note, file=sys.stderr)
    return note


class AgentSession:
    """One ephemeral public Agent session; no implicit retries or tool authority."""

    def __init__(self, api: Any, owner: Any, session: Any, provider: str, tmpdir: str):
        self._api, self._owner, self._session = api, owner, session
        self.provider, self._tmpdir = provider, tmpdir
        self._submitted = False

    async def submit(self, prompt: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
        if self._submitted:
            raise AgentError(
                "This single-turn session was already submitted; it cannot replay work."
            )
        self._submitted = True
        turn = None
        collector = None

        async def collect(active_turn):
            async for event in active_turn.events():
                if event.type == "terminal":
                    if not isinstance(event.payload, self._api.TurnResult):
                        raise AgentError(
                            "amplifier-agent returned an invalid terminal payload."
                        )
                    return event.payload
            raise AgentError("amplifier-agent stream ended without a terminal result.")

        try:
            with contextlib.redirect_stdout(sys.stderr):
                turn = await self._session.start_turn(
                    self._api.TurnInput([self._api.TextPart(prompt)])
                )
                collector = asyncio.create_task(collect(turn))
                result = await asyncio.wait_for(
                    asyncio.shield(collector),
                    timeout=max(timeout_ms, 1) / 1000.0,
                )
        except (TimeoutError, asyncio.CancelledError) as exc:

            async def settle():
                if turn is not None:
                    await turn.cancel()
                    if collector is not None:
                        await collector

            draining = asyncio.create_task(settle())
            while not draining.done():
                try:
                    await asyncio.shield(draining)
                except asyncio.CancelledError:
                    continue
            draining.result()
            if isinstance(exc, asyncio.CancelledError):
                raise
            failure = AgentError(
                f"amplifier-agent turn exceeded its {timeout_ms} ms budget and was "
                "cancelled. Inspect the failure; no retry was performed."
            )
            try:
                await self.aclose()
            except BaseException as fault:  # noqa: BLE001 - preserve the timeout and retain failed cleanup state
                _cleanup_note(failure, self._tmpdir, fault, phase="Turn")
            raise failure from exc
        except Exception as exc:
            detail = (
                _public_error_detail(cast("amplifier_agent.AgentError", exc))
                if isinstance(exc, self._api.AgentError)
                else f"{type(exc).__name__}: {exc}"
            )
            raise AgentError(f"amplifier-agent turn failed: {detail}") from exc
        if result.state != "success" or result.error is not None:
            error = result.error
            detail = _public_error_detail(error) if error else result.state
            raise AgentError(
                f"amplifier-agent turn did not succeed: {detail}"
            ) from error
        reply = "".join(
            part.text
            for part in result.content or []
            if isinstance(part, self._api.TextPart)
        )
        if not reply.strip():
            raise AgentError(
                "amplifier-agent returned an empty result; no structured answer was produced."
            )
        return reply

    async def aclose(self) -> None:
        if self._owner is None:
            return
        owner = self._owner
        with contextlib.redirect_stdout(sys.stderr):
            try:
                await self._session.close()
            finally:
                await owner.close()
        shutil.rmtree(self._tmpdir)
        self._owner = None


async def prepare_turn(*, cwd: str | os.PathLike[str] | None = None) -> AgentSession:
    """Construct a credentialed, explicitly selected, tool-free ephemeral agent.

    The optional cwd is retained for API compatibility but grants no filesystem
    access. Storage always lives in a fresh tool-owned temporary directory.
    """
    api = _load_engine()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            records = await api.list_providers()
    except api.AgentError as exc:
        raise AgentUnavailable(
            f"amplifier-agent discovery failed: {_public_error_detail(exc)}"
        ) from exc
    record = _select_provider(
        records,
        override=os.environ.get(PROVIDER_ENV_VAR, "").strip() or None,
    )
    provider = record.provider
    if not record.installed:
        raise AgentUnavailable(_provider_sdk_missing_hint(provider))
    model = (
        os.environ.get("TMUX_FLEET_MODEL")
        or os.environ.get("AMPLIFIER_AGENT_MODEL")
        or ""
    ).strip()
    if not model:
        raise AgentUnavailable(
            "No model is selected. Set TMUX_FLEET_MODEL (or AMPLIFIER_AGENT_MODEL) "
            f"to an exact model available through {provider!r}. Selecting a provider "
            "does not select a matching model; no fallback was used."
        )
    tmpdir = tempfile.mkdtemp(prefix="tmux-fleet-agent-")
    owner = None
    session = None
    try:
        working = os.path.join(tmpdir, "working")
        sessions = os.path.join(tmpdir, "sessions")
        os.mkdir(working)
        os.mkdir(sessions)
        with contextlib.redirect_stdout(sys.stderr):
            owner = await api.create_agent(
                api.AgentOptions(
                    provider=provider,
                    model=model,
                    working_directory=working,
                    sessions_directory=sessions,
                    tools=[],
                    skills=[],
                    mcp_servers=[],
                    approvals="deny",
                )
            )
            session = await owner.create_session(
                api.SessionOptions(persistence="ephemeral")
            )
            info = session.info
            if info.provider != provider or info.model != model:
                raise AgentUnavailable(
                    f"amplifier-agent observed provider/model {info.provider!r}/{info.model!r}, "
                    f"but tmux-fleet requested {provider!r}/{model!r}. "
                    "Set TMUX_FLEET_MODEL to a compatible model; no turn was submitted."
                )
        return AgentSession(api, owner, session, provider, tmpdir)
    except BaseException as exc:
        note = None
        try:
            with contextlib.redirect_stdout(sys.stderr):
                try:
                    if session is not None:
                        await session.close()
                finally:
                    if owner is not None:
                        await owner.close()
            shutil.rmtree(tmpdir)
        except BaseException as fault:  # noqa: BLE001 - secondary cleanup must not replace the original
            note = _cleanup_note(exc, tmpdir, fault)
        if not isinstance(exc, Exception) or isinstance(exc, AgentUnavailable):
            raise
        if isinstance(exc, ModuleNotFoundError):
            refusal = AgentUnavailable(_provider_sdk_missing_hint(provider, exc))
        elif isinstance(exc, api.AgentError):
            refusal = AgentUnavailable(
                f"amplifier-agent startup refused: {_public_error_detail(exc)}"
            )
        else:
            refusal = AgentError(
                f"amplifier-agent failed to construct a session ({type(exc).__name__})."
            )
        if note is not None:
            refusal.add_note(note)
        raise refusal from exc
