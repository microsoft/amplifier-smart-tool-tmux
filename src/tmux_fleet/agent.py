"""Single-turn, read-only judgment through the public Amplifier Agent API.

Imports remain lazy: deterministic verbs require neither discovery nor an agent.
Each invocation owns separate working and session directories. A cancelled turn
is joined before closing the agent or deleting those directories; never replay it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import amplifier_agent

PROVIDER_ENV_VAR = "TMUX_FLEET_PROVIDER"
PROVIDER_PREFERENCE = ("anthropic", "openai", "gemini", "azure-openai")
DEFAULT_TIMEOUT_MS = 180_000


class AgentUnavailable(RuntimeError):
    """A substrate precondition is missing."""


class AgentError(RuntimeError):
    """A turn failed, timed out, or produced no usable output."""


def _engine_missing_hint(detail: object = "") -> str:
    return (
        f"the amplifier-agent public library (amplifier_agent) could not be imported ({detail}). "
        "Reinstall the tool with `uv sync`; deterministic verbs need none of this."
    )


def _provider_sdk_missing_hint(provider: str, detail: object = "") -> str:
    detail_suffix = f" ({detail})" if detail else ""
    install = (
        f'Install the matching extra with `pip install "tmux-fleet[{provider}]"`.'
        if provider in ("anthropic", "openai")
        else f"Install the SDK required by {provider!r} into the tool environment."
    )
    return f"the {provider!r} provider SDK is not installed{detail_suffix}. {install}"


def _no_provider_hint() -> str:
    return (
        "no AI provider is eligible for automatic selection. Configure a provider "
        f"and install its SDK, or pin a registered {PROVIDER_ENV_VAR} and set "
        "TMUX_FLEET_MODEL. Discovery alone does not establish authentication. "
        "This tool never stores credentials."
    )


def _load_engine():
    try:
        import amplifier_agent
    except ImportError as exc:
        raise AgentUnavailable(_engine_missing_hint(exc)) from exc
    return amplifier_agent


def _select_provider(
    records: list[amplifier_agent.ProviderRecord], *, override: str | None
) -> amplifier_agent.ProviderRecord:
    if override:
        record = next((r for r in records if r.provider == override), None)
        if record is None:
            raise AgentUnavailable(
                f"Provider {override!r} pinned via {PROVIDER_ENV_VAR} is not registered. "
                "Select one of the registered provider IDs: "
                + ", ".join(r.provider for r in records)
                + "."
            )
        # OAuth and identity construction can know more than advisory discovery.
        return record
    resolvable = [
        r
        for r in records
        if r.credentials == "found"
        or (
            r.credentials == "not_required"
            and (
                (r.provider == "ollama" and os.environ.get("OLLAMA_HOST"))
                or (r.provider == "vllm" and os.environ.get("VLLM_BASE_URL"))
            )
        )
    ]
    if not resolvable:
        raise AgentUnavailable(_no_provider_hint())
    return next(
        (r for p in PROVIDER_PREFERENCE for r in resolvable if r.provider == p),
        resolvable[0],
    )


def _result_text(result: amplifier_agent.TurnResult) -> str:
    if result.state != "success":
        error = result.error
        detail = _public_error_detail(error) if error is not None else "no error detail"
        raise AgentError(f"amplifier-agent turn {result.state}: {detail}")
    text = "".join(part.text for part in result.content or [])
    if not text.strip():
        raise AgentError(
            "amplifier-agent returned an empty result, not a structured answer."
        )
    return text


def _public_error_detail(error: amplifier_agent.AgentError) -> str:
    """Keep the public actionable error contract, not merely Exception.__str__."""
    return f"{error.code} [{error.category}]: {error.message} Remedy: {error.remedy}"


class AgentSession:
    """An ephemeral session, owned agent and directories; exactly one submission."""

    def __init__(
        self,
        api,
        owner: amplifier_agent.Agent,
        session: amplifier_agent.Session,
        provider: str,
        directory: str,
    ) -> None:
        self._api = api
        self._owner = owner
        self._session = session
        self.provider = provider
        self._directory = directory
        self._submitted = False

    async def submit(self, prompt: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
        if self._submitted:
            raise AgentError(
                "This single-turn session has already been submitted; do not replay."
            )
        self._submitted = True
        turn = None
        collector = None

        async def collect(
            active_turn: amplifier_agent.Turn,
        ) -> amplifier_agent.TurnResult:
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
                    self._api.TurnInput(content=[self._api.TextPart(text=prompt)])
                )
                collector = asyncio.create_task(collect(turn))
                result = await asyncio.wait_for(
                    asyncio.shield(collector), max(timeout_ms, 1) / 1000
                )
                return _result_text(result)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if turn is not None:
                # cancel() requests cancellation; consuming terminal joins it.
                async def settle() -> None:
                    await turn.cancel()
                    if collector is not None:
                        await collector

                draining = asyncio.create_task(settle())
                while not draining.done():
                    try:
                        await asyncio.shield(draining)
                    except asyncio.CancelledError:
                        # A second caller cancellation cannot orphan the drain.
                        continue
                draining.result()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AgentError(
                f"amplifier-agent turn exceeded its {timeout_ms} ms budget; cancelled."
            ) from exc
        except AgentError:
            raise
        except self._api.AgentError as exc:
            raise AgentError(
                f"amplifier-agent turn failed: {_public_error_detail(exc)}"
            ) from exc
        except Exception as exc:
            raise AgentError(f"amplifier-agent turn failed: {exc}") from exc

    async def aclose(self) -> None:
        # A failed close retains the directories rather than deleting live state.
        with contextlib.redirect_stdout(sys.stderr):
            try:
                await self._session.close()
            finally:
                await self._owner.close()
        shutil.rmtree(self._directory)


async def prepare_turn(*, cwd: str | os.PathLike[str] | None = None) -> AgentSession:
    api = _load_engine()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            records = await api.list_providers()
    except api.AgentError as exc:
        raise AgentUnavailable(
            f"amplifier-agent discovery failed: {_public_error_detail(exc)}"
        ) from exc
    override = os.environ.get(PROVIDER_ENV_VAR, "").strip() or None
    record = _select_provider(records, override=override)
    provider = record.provider
    if not record.installed:
        raise AgentUnavailable(_provider_sdk_missing_hint(provider))
    model = os.environ.get("TMUX_FLEET_MODEL", "").strip()
    if not model and provider != "anthropic":
        raise AgentUnavailable(
            f"Provider {provider!r} requires an explicit compatible model: set "
            "TMUX_FLEET_MODEL to a model supported by that provider. Agent v0.22.0 "
            "does not guarantee its ambient/default model matches the selected provider; "
            "tmux-fleet will not guess or silently use a different provider's model."
        )
    directory = tempfile.mkdtemp(prefix="tmux-fleet-agent-")
    root = directory
    working = os.path.join(root, "working")
    sessions = os.path.join(root, "sessions")
    os.mkdir(working)
    os.mkdir(sessions)
    owner = None
    session = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            owner = await api.create_agent(
                api.AgentOptions(
                    # Inherit the ambient pair intact unless the tool names both.
                    provider=provider if model else None,
                    model=model or None,
                    reasoning_effort=os.environ.get("TMUX_FLEET_REASONING_EFFORT")
                    or None,
                    working_directory=str(cwd) if cwd is not None else working,
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
            if info.provider != provider or (model and info.model != model):
                requested_model = f" and model {model!r}" if model else ""
                raise AgentUnavailable(
                    f"amplifier-agent observed provider/model {info.provider!r}/{info.model!r}, "
                    f"but tmux-fleet requested provider {provider!r}{requested_model}. "
                    f"Set TMUX_FLEET_MODEL to a compatible model for {provider!r}; "
                    "no turn was submitted."
                )
    except BaseException as exc:
        cleanup_note = None
        try:
            with contextlib.redirect_stdout(sys.stderr):
                try:
                    if session is not None:
                        await session.close()
                finally:
                    if owner is not None:
                        await owner.close()
            shutil.rmtree(directory)
        except BaseException as cleanup_exc:  # noqa: BLE001 - preserve the original exception
            cleanup_note = (
                f"Startup cleanup failed; directories retained at {directory}: "
                f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            )
            exc.add_note(cleanup_note)
            print(cleanup_note, file=sys.stderr)
        if not isinstance(exc, Exception) or isinstance(exc, AgentUnavailable):
            raise
        if isinstance(exc, api.AgentError):
            escape_hatch = (
                f" If you intended to override the ambient provider/model, set "
                f"TMUX_FLEET_MODEL to a compatible model for {provider!r}."
                if not model
                else ""
            )
            refusal = AgentUnavailable(
                f"amplifier-agent startup refused: {_public_error_detail(exc)}{escape_hatch}"
            )
        else:
            refusal = AgentError(f"amplifier-agent failed to start: {exc}")
        if cleanup_note is not None:
            refusal.add_note(cleanup_note)
        raise refusal from exc
    return AgentSession(api, owner, session, provider, directory)
