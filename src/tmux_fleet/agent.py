"""The amplifier-agent substrate: how the smart verbs execute a model turn.

Contractually (VISION section 3), model-backed capabilities execute through
**amplifier-agent** -- an isolated subprocess turn via its maintained SDK
(``amplifier_agent_py``). This tool never carries provider credentials, never
links a provider SDK, never re-implements an agent loop. The provider is
whatever amplifier-agent is configured to use; its credentials flow through
amplifier-agent's own environment resolution, never through this tool.

The SDK is imported LAZILY, inside ``run_agent_turn`` only, so the deterministic
verbs load and run with the SDK importable-but-unused and with no provider
configured. A smart verb with no working amplifier-agent fails saying exactly
that (``AgentUnavailable``) -- never a silent fallback to a deterministic
approximation.

This mirrors drumbeat's runner.py usage of ``spawn_agent_sync``: one
``amplifier-agent run`` process per turn, non-interactive (``-y``), NDJSON event
stream, terminal event captured as the result.
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

_AGENT_COMMAND = "amplifier-agent"

#: Explicit override of the amplifier-agent binary to spawn. For pinning a
#: specific configured engine, or for tests. When set but not executable, the
#: substrate is treated as absent (fail-loud), never silently ignored.
AGENT_BIN_ENV_VAR = "TMUX_FLEET_AGENT_BIN"

#: Default single-turn budget for a smart verb, in milliseconds. Triage and
#: interpret are one turn each; generous enough for a real model round-trip,
#: bounded so a hung engine surfaces as a failure rather than a hang.
DEFAULT_TIMEOUT_MS = 180_000

INSTALL_HINT = (
    "amplifier-agent (the AI substrate this tool's smart verbs run through) is "
    "not available. It is not on PyPI; install it with:\n"
    "  uv tool install git+https://github.com/microsoft/amplifier-agent\n"
    "then configure a provider for it (see `amplifier-agent providers` / "
    "`amplifier-agent auth`). Once configured, every smart tool on this machine "
    "shares it. The deterministic verbs of this tool need none of this."
)


class AgentUnavailable(RuntimeError):
    """The amplifier-agent substrate is not available or not configured.

    This is the fail-loud a smart verb raises INSTEAD of degrading to a
    deterministic answer. Its message names the remedy.
    """


class AgentError(RuntimeError):
    """amplifier-agent ran but the turn failed, or produced no usable output."""


def _sibling_agent_command() -> str | None:
    """``amplifier-agent`` installed alongside this interpreter, if present."""
    candidate = Path(sys.executable).parent / _AGENT_COMMAND
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def resolve_agent_command() -> str | None:
    """Absolute path to spawn ``amplifier-agent`` as, or ``None`` when missing.

    Resolution order:
      1. ``$TMUX_FLEET_AGENT_BIN`` -- an explicit override (pin a specific
         configured engine, or a test). Set-but-not-executable returns ``None``
         (fail-loud), never a silent fall-through.
      2. ``amplifier-agent`` on PATH -- the machine's SHARED, configured
         substrate. VISION section 3: amplifier-agent is configured once and
         every smart tool on the machine shares it. This is why PATH comes
         before a co-installed sibling: a private, under-provisioned copy that
         shadowed the machine's configured engine (e.g. one missing the
         provider SDKs it dynamically imports) would fail turns while a working
         engine sat right there on PATH.
      3. A sibling of this interpreter -- a last-resort fallback.

    Deliberately does NOT fall back to the bare name: returning ``None`` is what
    lets a caller branch on absence and fail loud with the install hint.
    """
    override = os.environ.get(AGENT_BIN_ENV_VAR)
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    return shutil.which(_AGENT_COMMAND) or _sibling_agent_command()


def agent_available() -> bool:
    return resolve_agent_command() is not None


def run_agent_turn(
    prompt: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> str:
    """Run ONE amplifier-agent turn with *prompt* and return its final text.

    Raises:
        AgentUnavailable: amplifier-agent is not installed/resolvable, or the
            SDK reports the binary is missing / failed to spawn. Names the
            remedy. Never returns a degraded deterministic answer.
        AgentError: the turn ran and failed (error event), or finished with no
            terminal result.
    """
    agent_bin = resolve_agent_command()
    if agent_bin is None:
        raise AgentUnavailable(INSTALL_HINT)

    # Lazy import: keeps the deterministic import graph free of the SDK, and
    # turns an unexpected SDK-import problem into a named AgentUnavailable rather
    # than a hard ImportError at tool load.
    try:
        from amplifier_agent_py import (  # type: ignore[import-not-found]
            AaaError,
            ErrorEvent,
            ResultEvent,
            spawn_agent_sync,
        )
    except ImportError as exc:  # pragma: no cover - SDK is a declared dependency
        raise AgentUnavailable(
            "the amplifier-agent-py SDK could not be imported "
            f"({exc}). " + INSTALL_HINT
        ) from exc

    session_id = f"tmux-fleet-{uuid.uuid4().hex}"
    resolved_cwd = str(cwd) if cwd is not None else os.getcwd()

    try:
        handle = spawn_agent_sync(
            session_id=session_id,
            resume=False,
            cwd=resolved_cwd,
            config_path=None,
            display_mode="ndjson",
            env={"allowlist": list(os.environ.keys()), "extra": {}},
            timeout_ms=timeout_ms,
            _binary_resolver=lambda: agent_bin,
        )
    except AaaError as exc:
        # Raised BEFORE the turn ran: binary_not_found / spawn_failed mean the
        # substrate is effectively absent -- route to the same fail-loud path.
        code = getattr(exc, "code", "") or ""
        remediation = getattr(exc, "remediation", None)
        if code in ("binary_not_found", "spawn_failed"):
            raise AgentUnavailable(
                f"amplifier-agent could not be launched ({code}). "
                + (remediation or INSTALL_HINT)
            ) from exc
        raise AgentError(
            f"amplifier-agent failed before the turn started ({code}): "
            + (remediation or code)
        ) from exc

    terminal = None
    try:
        with handle:
            for event in handle.submit(prompt):
                etype = getattr(event, "type", None)
                if etype in ("result", "error"):
                    terminal = event
                # init / activity / notification events are liveness only.
    except Exception as exc:  # noqa: BLE001 - a mid-stream failure is a turn failure
        raise AgentError(f"amplifier-agent turn failed mid-stream: {exc}") from exc

    if isinstance(terminal, ResultEvent):
        text = terminal.text
        if not isinstance(text, str) or not text.strip():
            raise AgentError(
                "amplifier-agent returned an empty result. A smart verb needs a "
                "structured answer; an empty turn is a failure, not a result."
            )
        return text

    if isinstance(terminal, ErrorEvent):
        code = getattr(terminal, "code", "") or ""
        message = getattr(terminal, "message", "") or code
        tail = getattr(terminal, "stderr_tail", None)
        detail = f"{code}: {message}" if code and code not in message else message
        if tail:
            detail = f"{detail}\n{tail}"
        raise AgentError(f"amplifier-agent turn failed ({detail}).")

    raise AgentError(
        "amplifier-agent produced no terminal result or error event. Refusing to "
        "report a smart result the substrate never actually returned."
    )
