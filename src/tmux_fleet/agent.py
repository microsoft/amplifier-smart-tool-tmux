"""The amplifier-agent substrate: how the smart verbs execute a model turn.

Contractually (VISION section 3, cli.v1 rule 3), model-backed capabilities
execute through **amplifier-agent's engine library**, imported IN-PROCESS. The
tool's process is per-invocation and single-turn -- it is itself the isolation
boundary, so there is no CLI subprocess and no wrapper SDK between a verb and the
engine. This tool never carries provider credentials and never re-implements an
agent loop: the engine is a regular dependency, a provider SDK arrives via an
install extra (e.g. ``tmux-fleet[anthropic]``), and provider keys arrive from the
caller's environment at runtime.

The engine is imported **lazily**, inside :func:`prepare_turn` only. This is
load-bearing, not stylistic: ``import amplifier_agent_lib`` mutates
``os.environ`` (it sets ``AMPLIFIER_HOME`` unconditionally at import time). The
deterministic verbs and ``--help`` must never trigger that, so nothing at this
module's top level imports the engine; the import happens the first time a smart
verb actually needs a turn.

Refusal taxonomy (cli.v1 rule 3). A smart verb with no usable substrate FAILS
naming exactly which precondition is missing -- **never** a silent fallback to a
deterministic approximation. The four distinct preconditions, each with its own
remedy:

  1. **engine dependency missing** -- ``amplifier_agent_lib`` is not importable
     (the ``amplifier-agent`` package failed to install).
  2. **provider SDK extra not installed** -- the chosen provider's SDK (e.g.
     ``anthropic``) is not importable; install the matching extra.
  3. **no provider configured** -- no provider has resolvable credentials and
     none was pinned; set a provider key or pin ``TMUX_FLEET_PROVIDER``.
  4. **no credentials in the environment** -- a pinned provider
     (``TMUX_FLEET_PROVIDER``) has no credentials resolvable in the environment.

Cases 1/2 surface as :class:`AgentUnavailable`; so do 3/4. A turn that ran and
then failed (or returned nothing usable) is a distinct :class:`AgentError`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any

#: Optional pin: force a specific provider short-name (e.g. ``anthropic``)
#: instead of auto-selecting from whichever providers have resolvable
#: credentials. When set to a provider without resolvable credentials, the smart
#: verbs fail loud (taxonomy case 4) rather than silently falling back.
PROVIDER_ENV_VAR = "TMUX_FLEET_PROVIDER"

#: Auto-selection preference order, applied over the providers that actually
#: have resolvable credentials. A resolvable provider outside this list is still
#: usable (it is picked last, in the engine's own known order).
PROVIDER_PREFERENCE = ("anthropic", "openai", "gemini", "azure-openai")

#: The env var that carries each known provider's credentials, named in the
#: taxonomy-case-4 remedy so the caller knows exactly what to set.
_PROVIDER_CREDENTIAL_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure-openai": "AZURE_OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "github-copilot": "GITHUB_TOKEN",
    "ollama": "OLLAMA_HOST",
}

#: The install extra that ships each provider's SDK, named in the taxonomy-case-2
#: remedy. Providers absent here fall back to a generic hint.
_PROVIDER_EXTRA = {
    "anthropic": "anthropic",
    "openai": "openai",
}

#: Workspace name so the engine's session state is isolated under a stable
#: bucket rather than following whatever directory the tool was launched from.
_WORKSPACE = "tmux-fleet"

#: Client identity reported to the engine at boot.
_CLIENT_NAME = "tmux-fleet"

#: Default single-turn budget for a smart verb, in milliseconds. Triage and
#: interpret are one turn each; generous enough for a real model round-trip,
#: bounded so a hung turn surfaces as a failure rather than a hang. The timeout
#: is OWNED HERE, wrapped around the turn -- there is no subprocess to bound.
DEFAULT_TIMEOUT_MS = 180_000


class AgentUnavailable(RuntimeError):
    """A required substrate precondition is missing (taxonomy cases 1-4).

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
        "the amplifier-agent engine library (amplifier_agent_lib) could not be "
        f"imported{suffix}. The engine is a regular dependency of this tool -- "
        "its absence means the install is broken. Reinstall the tool (e.g. "
        "`uv sync`, or `pip install \"tmux-fleet[anthropic]\"`) so amplifier-agent "
        "is present. The deterministic verbs of this tool need none of this."
    )


def _provider_sdk_missing_hint(provider: str, detail: object = "") -> str:
    suffix = f" ({detail})" if detail else ""
    extra = _PROVIDER_EXTRA.get(provider)
    if extra:
        install = (
            f"install the matching extra: `pip install \"tmux-fleet[{extra}]\"` "
            f"(or `uv add \"tmux-fleet[{extra}]\"`)"
        )
    else:
        install = (
            f"install the Python SDK the {provider!r} provider requires into this "
            "tool's environment"
        )
    return (
        f"the {provider!r} provider was selected but its Python SDK is not "
        f"installed{suffix}. Provider SDKs ship as optional extras, not core "
        f"dependencies. To use the model-backed verbs with {provider!r}, {install}."
    )


def _no_provider_hint() -> str:
    return (
        "no AI provider is configured for the model-backed verbs. amplifier-agent "
        "found no provider with resolvable credentials in the environment. Set a "
        "provider credential (e.g. export ANTHROPIC_API_KEY=... and install the "
        "matching extra with `pip install \"tmux-fleet[anthropic]\"`), or pin a "
        f"provider explicitly with {PROVIDER_ENV_VAR}=<provider>. This tool never "
        "stores credentials of its own. The deterministic verbs need none of this."
    )


def _no_credentials_hint(provider: str) -> str:
    env_var = _PROVIDER_CREDENTIAL_ENV.get(provider)
    if env_var:
        set_it = f"set its credentials in the environment (e.g. export {env_var}=...)"
    else:
        set_it = "set its credentials in the environment"
    return (
        f"the provider {provider!r} is pinned via {PROVIDER_ENV_VAR} but has no "
        f"credentials resolvable in the environment. Either {set_it}, or unset "
        f"{PROVIDER_ENV_VAR} to auto-select a provider that does. This tool never "
        "stores credentials of its own."
    )


# --------------------------------------------------------------------------
# Testable seams. Each isolates one import/side-effecting step so the refusal
# taxonomy can be exercised without a live provider (and so a ModuleNotFoundError
# maps to a named AgentUnavailable rather than escaping as a bare import error).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _EngineSyms:
    """The engine symbols an embedder needs, gathered by one lazy import."""

    version: str
    protocol_version: str
    server_default_capabilities: Any
    load_and_prepare_cached: Any
    make_turn_handler: Any
    Engine: Any
    CliApprovalSystem: Any
    CliDisplaySystem: Any
    inject_provider: Any
    inject_routing_matrix: Any
    enumerate_resolvable_providers: Any


def _load_engine() -> _EngineSyms:
    """Lazily import the engine library + provider-injection surface.

    Raises :class:`AgentUnavailable` (taxonomy case 1) when the engine
    dependency is not importable, rather than letting a bare
    ``ModuleNotFoundError`` escape.
    """
    try:
        from amplifier_agent_lib import __version__
        from amplifier_agent_lib._runtime import make_turn_handler
        from amplifier_agent_lib.bundle.cache import load_and_prepare_cached
        from amplifier_agent_lib.engine import Engine
        from amplifier_agent_lib.protocol import (
            PROTOCOL_VERSION,
            server_default_capabilities,
        )
        from amplifier_agent_lib.protocol_points.defaults_cli import (
            CliApprovalSystem,
            CliDisplaySystem,
        )
        from amplifier_agent_cli.provider_sources import (
            enumerate_resolvable_providers,
            inject_provider,
            inject_routing_matrix,
        )
    except ModuleNotFoundError as exc:
        raise AgentUnavailable(_engine_missing_hint(exc)) from exc

    return _EngineSyms(
        version=__version__,
        protocol_version=PROTOCOL_VERSION,
        server_default_capabilities=server_default_capabilities,
        load_and_prepare_cached=load_and_prepare_cached,
        make_turn_handler=make_turn_handler,
        Engine=Engine,
        CliApprovalSystem=CliApprovalSystem,
        CliDisplaySystem=CliDisplaySystem,
        inject_provider=inject_provider,
        inject_routing_matrix=inject_routing_matrix,
        enumerate_resolvable_providers=enumerate_resolvable_providers,
    )


def _select_provider(resolvable: list[str], *, override: str | None) -> str:
    """Choose the provider to run a turn through, or refuse naming why.

    Pure function -- no imports, no side effects -- so the two credential-side
    refusals (taxonomy cases 3 and 4) are directly testable.

    * ``override`` set (``TMUX_FLEET_PROVIDER``) and NOT resolvable -> case 4.
    * ``override`` unset and nothing resolvable -> case 3.
    * otherwise pick ``override`` if given, else the first provider in
      :data:`PROVIDER_PREFERENCE` that is resolvable, else the first resolvable.
    """
    if override:
        if override not in resolvable:
            raise AgentUnavailable(_no_credentials_hint(override))
        return override
    if not resolvable:
        raise AgentUnavailable(_no_provider_hint())
    for preferred in PROVIDER_PREFERENCE:
        if preferred in resolvable:
            return preferred
    return resolvable[0]


def _mount_provider(syms: _EngineSyms, prepared: Any, provider: str) -> None:
    """Clear the catalog stubs and inject the chosen provider + routing matrix.

    Maps a missing provider SDK (a ``ModuleNotFoundError`` from the engine's
    dynamic provider import) to a named :class:`AgentUnavailable` (taxonomy
    case 2). Clearing ``mount_plan["providers"]`` first is required: the
    vendored bundle declares a catalog stub per provider, and ``inject_provider``
    is a no-op if the list is already non-empty (the injection would be silently
    discarded and the turn would run on the stub).
    """
    prepared.mount_plan["providers"] = []
    try:
        syms.inject_provider(prepared, provider)
        syms.inject_routing_matrix(prepared, provider)
    except ModuleNotFoundError as exc:
        raise AgentUnavailable(_provider_sdk_missing_hint(provider, exc)) from exc


# --------------------------------------------------------------------------
# A booted, single-turn engine session.
# --------------------------------------------------------------------------


class AgentSession:
    """A booted engine, ready for exactly one turn, plus its cleanup.

    Built by :func:`prepare_turn`. All four substrate preconditions are already
    satisfied by the time this exists, so :meth:`submit` only raises
    :class:`AgentError` (the turn ran and failed), never :class:`AgentUnavailable`.
    """

    def __init__(self, syms: _EngineSyms, engine: Any, session_id: str, provider: str, _tmpdir: str | None) -> None:
        self._syms = syms
        self._engine = engine
        self._session_id = session_id
        self.provider = provider
        self._tmpdir = _tmpdir

    async def submit(self, prompt: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
        """Run the one turn and return its final text.

        The timeout is owned here: the turn is bounded by ``asyncio.wait_for``,
        and a turn that overruns raises :class:`AgentError` rather than hanging.
        Any stray stdout a bundle module might emit during the turn is diverted
        to stderr so the CLI's stdout stays pure JSON.
        """
        import asyncio

        submit_params = {
            "sessionId": self._session_id,
            "turnId": "turn-1",
            "prompt": prompt,
        }
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = await asyncio.wait_for(
                    self._engine.submit_turn(submit_params),
                    timeout=max(timeout_ms, 1) / 1000.0,
                )
        except TimeoutError as exc:
            raise AgentError(
                f"amplifier-agent turn exceeded its {timeout_ms} ms budget and was "
                "aborted. Increase --timeout-ms, or check the provider is reachable."
            ) from exc
        except (AgentUnavailable, AgentError):
            raise
        except Exception as exc:  # noqa: BLE001 - a mid-turn failure is a turn failure
            raise AgentError(f"amplifier-agent turn failed: {type(exc).__name__}: {exc}") from exc

        reply = result.get("reply") if isinstance(result, dict) else None
        if not isinstance(reply, str) or not reply.strip():
            raise AgentError(
                "amplifier-agent returned an empty result. A smart verb needs a "
                "structured answer; an empty turn is a failure, not a result."
            )
        return reply

    async def aclose(self) -> None:
        """Shut the engine down and remove the temp working directory.

        ``Engine.shutdown()`` is idempotent and never raises.
        """
        try:
            with contextlib.redirect_stdout(sys.stderr):
                await self._engine.shutdown()
        finally:
            if self._tmpdir is not None:
                shutil.rmtree(self._tmpdir, ignore_errors=True)


async def prepare_turn(*, cwd: str | os.PathLike[str] | None = None) -> AgentSession:
    """Resolve the substrate and boot a one-turn engine, or refuse naming why.

    Runs the full taxonomy check UP FRONT (before any tmux work), so a smart
    verb's refusal never depends on there being a live tmux server to assemble
    context from first:

      1. import the engine library (case 1),
      2. select a provider from resolvable credentials / the pin (cases 3, 4),
      3. prepare the bundle and inject the provider (case 2 on a missing SDK),
      4. build and boot the engine.

    The returned :class:`AgentSession` is ready for exactly one
    :meth:`AgentSession.submit`; the caller MUST ``aclose()`` it.
    """
    syms = _load_engine()

    override = os.environ.get(PROVIDER_ENV_VAR, "").strip() or None
    provider = _select_provider(list(syms.enumerate_resolvable_providers()), override=override)

    owns_tmpdir = cwd is None
    cwd_dir = str(cwd) if cwd is not None else tempfile.mkdtemp(prefix="tmux-fleet-agent-")

    try:
        with contextlib.redirect_stdout(sys.stderr):
            prepared = await syms.load_and_prepare_cached(aaa_version=syms.version)
            _mount_provider(syms, prepared, provider)

            handler = syms.make_turn_handler(
                prepared,
                cwd=cwd_dir,
                is_resumed=False,
                workspace=_WORKSPACE,
            )
            engine = syms.Engine(
                turn_handler=handler,
                protocol_points={
                    # decline: a triage/interpret turn reasons ONLY over context
                    # this library assembled mechanically (VISION principle 4). It
                    # must not run tools that could inspect the live fleet or the
                    # filesystem, so tool requests are refused rather than approved.
                    "approval": syms.CliApprovalSystem(mode="no"),
                    "display": syms.CliDisplaySystem(stream=sys.stderr, verbosity="quiet"),
                },
            )
            session_id = f"tmux-fleet-{uuid.uuid4().hex}"
            init_params: dict[str, Any] = {
                "protocolVersion": syms.protocol_version,
                "clientInfo": {"name": _CLIENT_NAME, "version": _tool_version()},
                "capabilities": dict(syms.server_default_capabilities()),
                "sessionId": session_id,
                "resume": False,
                "cwd": cwd_dir,
            }
            await engine.boot(init_params, bundle_override=prepared)
    except AgentUnavailable:
        if owns_tmpdir:
            shutil.rmtree(cwd_dir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - boot/prepare failure is a turn failure
        if owns_tmpdir:
            shutil.rmtree(cwd_dir, ignore_errors=True)
        raise AgentError(
            f"amplifier-agent failed to start a turn: {type(exc).__name__}: {exc}"
        ) from exc

    return AgentSession(syms, engine, session_id, provider, cwd_dir if owns_tmpdir else None)


def _tool_version() -> str:
    """This tool's own version, for the engine's ``clientInfo``. Best-effort."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("tmux-fleet")
        except PackageNotFoundError:
            return "0.2.0"
    except Exception:  # noqa: BLE001 - clientInfo version is cosmetic
        return "0.2.0"
