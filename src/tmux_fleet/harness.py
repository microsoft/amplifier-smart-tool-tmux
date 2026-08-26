"""Pane-harness detection: which agent harness runs in a tmux session's active
pane -- amplifier, claude-code, codex -- or honestly ``unknown``.

RE-HOMED HERE ON PURPOSE. This classification used to live in ``tmux_kit.labels``.
tmux-kit deleted that module in 0.3.4 as APP POLICY: identifying which coding
tool a human is running is application judgment, not tmux mechanism, and does
not belong in the plumbing. This tool owns that judgment, so the heuristics
live here (never imported from tmux-kit, which no longer ships them).

**The honesty rule: no signature -> ``unknown``. Never a guess.** A wrong label
is worse than no label -- a steering layer that believes a pane is amplifier
when it is actually a bare shell will type the wrong dialect into a live
terminal.

Two evidence sources, in strength order:

1. **Process tree** (primary). Walk the pane PID's descendants breadth-first;
   match argv TOKEN BASENAMES exactly against the known harness entrypoints.
   Exact-basename matching is load-bearing: ``/.../bin/amplifier`` labels
   amplifier, while ``/.../amplifier-something-manager`` must NOT -- a substring
   match would mislabel it. BFS order means the shallowest match wins.
2. **Snapshot sniff** (fallback). Only consulted when the process tree is
   silent (harness exited; only its screen remains). Patterns are deliberately
   narrow: pane text QUOTING a harness name is a known false-positive class, so
   bare product names never match -- only chrome-shaped signatures (banners,
   version lines). ``source`` is always reported, so a caller can weigh
   ``"snapshot"`` evidence more skeptically than ``"process"`` evidence.

Every tmux invocation goes through ``run_tmux_scoped`` (explicit ``-S``, the one
door); the only non-tmux subprocess is ``ps``, spawned argv-exec with
POSIX-portable flags (``-A -o pid=`` etc. -- the ``=`` header-suppression form
works on both procps/Linux and BSD/macOS ``ps``).

Snapshot capture here deliberately omits ``capture-pane -e``: signature regexes
must match VISIBLE text, and a TUI banner rendered in color would otherwise
carry escape bytes mid-phrase.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tmux_fleet.socket_resolution import run_tmux_scoped

#: The honest label for "no signature found". Exposed so callers compare against
#: a constant, not a magic string.
HARNESS_UNKNOWN = "unknown"

#: Exact argv-token basenames -> label. A closed DEFAULT set -- callers with
#: additional harnesses pass their own mapping to ``label_session()`` (mechanism,
#: not policy). Multiple entrypoints may map to one label.
DEFAULT_PROC_BASENAMES: Mapping[str, str] = {
    "amplifier": "amplifier",
    "amplifier-next": "amplifier",
    "amplifier-app-cli": "amplifier",
    "claude": "claude-code",
    "codex": "codex",
}

#: Narrow, high-precision screen signatures (see module docstring for why narrow
#: is non-negotiable). (label, compiled pattern) pairs, first hit wins in order.
DEFAULT_SNAPSHOT_PATTERNS: Sequence[tuple[str, re.Pattern[str]]] = (
    ("claude-code", re.compile(r"Welcome to Claude Code|Claude Code v\d")),
    ("codex", re.compile(r"OpenAI Codex|Codex CLI v\d")),
    ("amplifier", re.compile(r"Token Usage \(|Welcome to Amplifier|Amplifier v\d")),
)

#: Default snapshot depth for the sniff: enough to include a TUI's footer/banner
#: chrome even with a few blank lines below it.
DEFAULT_SNIFF_LINES = 60

#: Hard ceiling on the sniff depth (mirrors observe.MAX_CAPTURE_LINES' rationale).
MAX_SNIFF_LINES = 2000


@dataclass(frozen=True)
class HarnessLabel:
    """A harness label plus the evidence that earned it.

    ``source`` is part of the contract, not decoration: ``"process"`` evidence
    is live truth, ``"snapshot"`` evidence is screen residue a caller may treat
    more skeptically, ``"none"`` accompanies the honest ``unknown``.
    """

    session: str
    label: str  # a value from the basename/pattern tables, or HARNESS_UNKNOWN
    source: str  # "process" | "snapshot" | "none"
    evidence: str  # matched cmdline / matched snapshot line / why unknown


async def pane_pid(session_name: str) -> int | None:
    """PID of *session_name*'s active pane process (None if unavailable)."""
    try:
        out = await run_tmux_scoped(
            "display-message", "-p", "-t", session_name, "#{pane_pid}"
        )
        return int(out.strip())
    except (RuntimeError, FileNotFoundError, ValueError):
        return None


async def process_table() -> tuple[dict[int, list[int]], dict[int, str]]:
    """One ``ps`` pass -> ``(children-by-ppid, cmdline-by-pid)``.

    Spawned argv-exec (never a shell), with POSIX-portable flags: ``-A`` (all
    processes) and per-column ``-o pid=`` header suppression, which both procps
    (Linux) and BSD (macOS) ``ps`` honor. Returns empty tables on failure --
    callers fall back to the snapshot sniff rather than raising.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-A",
            "-o",
            "pid=",
            "-o",
            "ppid=",
            "-o",
            "args=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _stderr = await proc.communicate()
        out = stdout_bytes.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return {}, {}
    children: dict[int, list[int]] = defaultdict(list)
    cmds: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children[ppid].append(pid)
        cmds[pid] = parts[2].strip()
    return children, cmds


def _match_cmdline(cmdline: str, basenames: Mapping[str, str]) -> str | None:
    """Label for *cmdline* if any argv token's basename is a known harness
    entrypoint -- EXACT basename match only (see module docstring)."""
    for token in cmdline.split():
        base = os.path.basename(token)
        if base in basenames:
            return basenames[base]
    return None


def _label_from_process_tree(
    root_pid: int,
    children: dict[int, list[int]],
    cmds: dict[int, str],
    basenames: Mapping[str, str],
) -> tuple[str, str] | None:
    """(label, evidence-cmdline) from BFS over *root_pid* AND its descendants.

    The pane process itself is checked first (a pane whose root process IS the
    harness would otherwise be invisible to a descendants-only walk). BFS by
    generation means the shallowest match wins.
    """
    root_cmdline = cmds.get(root_pid, "")
    root_label = _match_cmdline(root_cmdline, basenames)
    if root_label is not None:
        return root_label, root_cmdline[:160]
    queue = list(children.get(root_pid, []))
    while queue:
        next_queue: list[int] = []
        for pid in queue:
            cmdline = cmds.get(pid, "")
            label = _match_cmdline(cmdline, basenames)
            if label is not None:
                return label, cmdline[:160]
            next_queue.extend(children.get(pid, []))
        queue = next_queue
    return None


def _label_from_snapshot(
    snapshot: str, patterns: Sequence[tuple[str, re.Pattern[str]]]
) -> tuple[str, str] | None:
    """(label, evidence-line) from the first snapshot pattern that hits."""
    for label, pattern in patterns:
        match = pattern.search(snapshot)
        if match:
            line_start = snapshot.rfind("\n", 0, match.start()) + 1
            line_end = snapshot.find("\n", match.end())
            if line_end == -1:
                line_end = len(snapshot)
            return label, snapshot[line_start:line_end].strip()[:160]
    return None


async def _sniff_snapshot(session_name: str, lines: int) -> str:
    """Plain-text pane capture for signature matching ('' on error).

    Deliberately WITHOUT ``-e``: escape sequences interleaved through a colored
    banner would break mid-phrase signature regexes.
    """
    lines = max(1, min(int(lines), MAX_SNIFF_LINES))
    try:
        return await run_tmux_scoped(
            "capture-pane", "-p", "-t", session_name, "-S", f"-{lines}"
        )
    except (RuntimeError, FileNotFoundError):
        return ""


async def label_session(
    session_name: str,
    *,
    basenames: Mapping[str, str] = DEFAULT_PROC_BASENAMES,
    patterns: Sequence[tuple[str, re.Pattern[str]]] = DEFAULT_SNAPSHOT_PATTERNS,
    sniff_lines: int = DEFAULT_SNIFF_LINES,
    table: tuple[dict[int, list[int]], dict[int, str]] | None = None,
) -> HarnessLabel:
    """Label the harness in *session_name*'s active pane, with evidence.

    Process tree first (live truth), snapshot sniff second (screen residue),
    else honestly ``unknown`` -- never a guess.
    """
    resolved_table = table if table is not None else await process_table()
    pid = await pane_pid(session_name)
    if pid is not None:
        hit = _label_from_process_tree(pid, *resolved_table, basenames)
        if hit is not None:
            return HarnessLabel(session_name, hit[0], "process", hit[1])
    snapshot = await _sniff_snapshot(session_name, sniff_lines)
    hit = _label_from_snapshot(snapshot, patterns)
    if hit is not None:
        return HarnessLabel(session_name, hit[0], "snapshot", hit[1])
    return HarnessLabel(
        session_name,
        HARNESS_UNKNOWN,
        "none",
        "no harness signature in process tree or pane snapshot",
    )


async def label_sessions(
    session_names: Sequence[str],
    *,
    basenames: Mapping[str, str] = DEFAULT_PROC_BASENAMES,
    patterns: Sequence[tuple[str, re.Pattern[str]]] = DEFAULT_SNAPSHOT_PATTERNS,
    sniff_lines: int = DEFAULT_SNIFF_LINES,
) -> list[HarnessLabel]:
    """Label many sessions off ONE ``ps`` pass.

    The expensive shared input -- the full-system process table -- is read once.
    """
    table = await process_table()
    return [
        await label_session(
            name,
            basenames=basenames,
            patterns=patterns,
            sniff_lines=sniff_lines,
            table=table,
        )
        for name in session_names
    ]
