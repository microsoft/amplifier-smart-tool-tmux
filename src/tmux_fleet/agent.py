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
  4. **no credentials in the environment** -- a pinned provider
     (``TMUX_FLEET_PROVIDER``) has no credentials resolvable in the environment.

  5. **no model selected** -- set ``TMUX_FLEET_MODEL`` (or
     ``AMPLIFIER_AGENT_MODEL``); there is no cross-provider model fallback.

Cases 1-5 surface as :class:`AgentUnavailable`. A turn that ran and
then failed (or returned nothing usable) is a distinct :class:`AgentError`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
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
    "vllm": "VLLM_BASE_URL",
    "chat-completions": "CHAT_COMPLETIONS_BASE_URL",
}

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
        "no AI provider is configured for the model-backed verbs. amplifier-agent "
        "found no provider with resolvable credentials in the environment. Set a "
        "provider credential (e.g. export ANTHROPIC_API_KEY=... and install the "
        'matching extra with `pip install "tmux-fleet[anthropic]"`), or pin a '
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


# The supported Python binding owns engine construction and provider validation.
# No private engine or former CLI modules are imported by this adapter.
def _load_engine():
    try:
        import amplifier_agent
    except (ImportError, OSError) as exc:
        raise AgentUnavailable(_engine_missing_hint(exc)) from exc
    return amplifier_agent


def _resolvable_providers() -> list[str]:
    aliases = {
        "anthropic": ("ANTHROPIC_API_KEY",),
        "openai": ("OPENAI_API_KEY",),
        "azure-openai": ("AZURE_OPENAI_API_KEY",),
        "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "github-copilot": (
            "COPILOT_AGENT_TOKEN",
            "COPILOT_GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ),
        "ollama": ("OLLAMA_HOST", "OLLAMA_API_KEY"),
        "vllm": ("VLLM_BASE_URL", "VLLM_API_KEY"),
        "chat-completions": ("CHAT_COMPLETIONS_BASE_URL",),
    }
    available = [
        name
        for name, keys in aliases.items()
        if any(os.environ.get(key) for key in keys)
    ]
    # Presence is only a selection hint; the public Agent owns validation and refresh.
    if (Path.home() / ".amplifier/openai-chatgpt-oauth.json").is_file():
        available.append("openai-chatgpt")
    return available


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


class AgentSession:
    """One ephemeral public Agent session; no implicit retries or tool authority."""

    def __init__(self, api: Any, owner: Any, session: Any, provider: str, tmpdir: str):
        self._api, self._owner, self._session = api, owner, session
        self.provider, self._tmpdir = provider, tmpdir
        self._submitted = False

    async def submit(self, prompt: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
        import asyncio

        if self._submitted:
            raise AgentError(
                "This single-turn session was already submitted; it cannot replay work."
            )
        self._submitted = True
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = await asyncio.wait_for(
                    self._session.run(
                        self._api.TurnInput([self._api.TextPart(prompt)])
                    ),
                    timeout=max(timeout_ms, 1) / 1000.0,
                )
        except TimeoutError as exc:
            # Closing joins owned sessions, including an interrupted run.
            await self.aclose()
            raise AgentError(
                f"amplifier-agent turn exceeded its {timeout_ms} ms budget and was "
                "cancelled. Inspect the failure; no retry was performed."
            ) from exc
        except Exception as exc:
            raise AgentError(
                f"amplifier-agent turn failed: {type(exc).__name__}: {exc}"
            ) from exc
        if result.state != "success" or result.error is not None:
            error = result.error
            detail = f"{error.message} {error.remedy}" if error else result.state
            raise AgentError(f"amplifier-agent turn did not succeed: {detail}")
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
        owner, self._owner = self._owner, None
        try:
            with contextlib.redirect_stdout(sys.stderr):
                await owner.close()
        finally:
            shutil.rmtree(self._tmpdir, ignore_errors=True)


async def prepare_turn(*, cwd: str | os.PathLike[str] | None = None) -> AgentSession:
    """Construct a credentialed, explicitly selected, tool-free ephemeral agent.

    The optional cwd is retained for API compatibility but grants no filesystem
    access. Storage always lives in a fresh tool-owned temporary directory.
    """
    api = _load_engine()
    provider = _select_provider(
        _resolvable_providers(),
        override=os.environ.get(PROVIDER_ENV_VAR, "").strip() or None,
    )
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
    try:
        with contextlib.redirect_stdout(sys.stderr):
            owner = await api.create_agent(
                api.AgentOptions(
                    provider=provider,
                    model=model,
                    storage=tmpdir,
                    tools=[],
                    skills=[],
                    mcp_servers=[],
                    approvals="deny",
                )
            )
            session = await owner.create_session(
                api.SessionOptions(persistence="ephemeral")
            )
        return AgentSession(api, owner, session, provider, tmpdir)
    except ModuleNotFoundError as exc:
        raise AgentUnavailable(_provider_sdk_missing_hint(provider, exc)) from exc
    except api.AgentError as exc:
        raise AgentUnavailable(f"{exc.message} {exc.remedy}") from exc
    except Exception as exc:
        raise AgentError(
            f"amplifier-agent failed to construct a session ({type(exc).__name__})."
        ) from exc
    finally:
        # The returned wrapper owns cleanup only after both construction steps.
        if "session" not in locals():
            if owner is not None:
                await owner.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
