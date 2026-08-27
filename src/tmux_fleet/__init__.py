"""tmux-fleet -- a smart tool for tmux fleets.

Every capability is reachable from this library; the ``tmux-fleet`` CLI is a
thin adapter over it. Observation plumbing comes from tmux-kit (a dependency,
never vendored). What lives here is the judgment: tri-state prompt
classification, completeness contracts on every list, an attention rollup that
refuses to present a heuristic as a verdict, deny-by-default input, harness
classification (re-homed from the deleted ``tmux_kit.labels``), and the
model-backed ``triage``/``interpret`` verbs executed through amplifier-agent's
engine library, embedded in-process.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Deterministic verbs (the eight cli.v1 core verbs, plus their helpers).
from tmux_fleet.creation import create_session
from tmux_fleet.diagnostics import doctor, exit_code
from tmux_fleet.fleet import (
    FleetError,
    attention,
    list_sessions,
    read_session,
    send_input,
    socket_status,
)

# The manifest accessor (spec: reachable from the library as structured data).
from tmux_fleet.manifest import Manifest, ManifestError, manifest, manifest_dict

# Model-backed verbs and their substrate.
from tmux_fleet.agent import AgentError, AgentUnavailable
from tmux_fleet.smart import interpret, triage

try:
    __version__ = _pkg_version("tmux-fleet")
except PackageNotFoundError:  # pragma: no cover - source checkout without metadata
    __version__ = "0.2.0"

__all__ = [
    "__version__",
    # deterministic verbs
    "socket_status",
    "list_sessions",
    "attention",
    "read_session",
    "send_input",
    "create_session",
    "doctor",
    "exit_code",
    "FleetError",
    # model-backed verbs
    "triage",
    "interpret",
    "AgentUnavailable",
    "AgentError",
    # manifest
    "manifest",
    "manifest_dict",
    "Manifest",
    "ManifestError",
]
