"""Fleet observation and (deny-by-default) input.

Plumbing from tmux-kit; the judgment stays here.

Properties this module exists to hold, each paid for in a predecessor's blood:

1. **A boolean would lie about `at_prompt`.** It is ``"yes"``/``"no"``/
   ``"uncertain"``, never a bare bool. An honest "not sure" beats a confident
   wrong answer.
2. **Every list answers "is this everything?"** via a ``_completeness`` block in
   the response itself. A tool that cannot tell its caller whether they saw the
   whole world lets a partial view be mistaken for a complete one.
3. **An empty read and a broken read must never share an observable.** tmux-kit's
   own enumeration returns ``[]`` when tmux is unavailable -- indistinguishable
   from a genuinely empty fleet. This module probes first and fails loud.
4. **A scope claim names the socket it actually read.** The socket directory is
   resolved explicitly (see ``socket_resolution``), injected into tmux-kit, and
   named verbatim in the scope string -- and where a server is running,
   confirmed by tmux itself. Zero sessions is reported as "pointed at <dir>,
   saw nothing", never as a bare empty list with ``complete: true``.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

from tmux_kit import keys as tk_keys
from tmux_kit import observe as tk_observe

from tmux_fleet import audit, harness, socket_resolution, submission
from tmux_fleet.socket_resolution import (
    SocketResolution,
    run_tmux_scoped,
)

# --------------------------------------------------------------------------
# Bounds. Proven field values ported from the fleet connector this is based on,
# not fresh guesses.
# --------------------------------------------------------------------------

#: Lines captured per session on a `sessions` listing. Enough to see the last
#: line and classify a prompt; NOT enough to characterize a session.
LIST_SNAPSHOT_LINES = 30

#: Ceiling on a single `read`. tmux-kit's own MAX_CAPTURE_LINES.
MAX_READ_LINES = tk_observe.MAX_CAPTURE_LINES  # 2000

#: Default depth of a `read` when the caller passes nothing.
DEFAULT_READ_LINES = 200

#: A session with no pane output for this long is "quiet" in the rollup. A prior
#: for ordering, never a verdict about any one session.
DEFAULT_QUIET_SECONDS = 900  # 15 minutes


class FleetError(RuntimeError):
    """A read or write failed. Always fatal -- never a partial answer."""


def check_quiet_seconds(quiet_seconds: int) -> None:
    """Refuse a quiet threshold that cannot mean what it says.

    A negative threshold makes ``idle >= quiet_seconds`` true for every session,
    so the whole fleet silently lands in a "quiet" bucket and the ordering the
    caller asked for is not the ordering it gets. Two verbs take this argument
    (``attention`` and ``triage``) and both must refuse it identically, hence
    one check rather than two messages that can drift apart.
    """
    if quiet_seconds < 0:
        raise FleetError(
            f"REFUSED: --quiet-seconds must be >= 0 (got {quiet_seconds}). A "
            "negative threshold marks every session quiet regardless of its "
            "idle time, which would reorder the whole result without saying so."
        )


def _absent_server_reason(stderr: str) -> str | None:
    """Is this tmux error "nothing is there", or "I cannot tell"?

    "Nothing is there" is a real, reportable observation -- zero sessions on the
    socket we were pointed at. "I cannot tell" must stay fatal, because
    reporting an empty fleet when the truth is unreadable is precisely the
    silent-empty-fleet lie this tool exists to refuse.

    tmux says "nothing is there" in more than one dialect:

    * ``no server running on <path>``    -- socket dir exists, no server.
    * ``error connecting to <path> (No such file or directory)`` -- a socket
      directory that has never hosted a server (a freshly configured deployment
      on its first run).
    * ``error connecting to <path> (Connection refused)`` -- a stale socket file
      left behind by a server that is gone.

    Everything else -- notably ``Permission denied`` -- stays fatal. Returns the
    reason when the server is genuinely absent, else ``None``.
    """
    text = stderr.lower()
    if "no server running" in text or "no sessions" in text:
        return stderr.strip() or "no server running"
    if "error connecting to" in text:
        if "no such file or directory" in text:
            return (
                f"{stderr.strip()} -- nothing has ever run on this socket path "
                "(the per-uid socket directory does not exist yet)"
            )
        if "connection refused" in text:
            return (
                f"{stderr.strip()} -- a socket file is present but no server is "
                "listening on it (stale socket from an exited server)"
            )
    return None


# --------------------------------------------------------------------------
# ANSI + prompt classification (proven grammar/token table, kept exactly)
# --------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1B
    (?:
        \[ [0-?]* [ -/]* [@-~]   # CSI: ESC [ ... final byte
      | \] .*? (?:\x07|\x1B\\)   # OSC: ESC ] ... BEL or ST
      | [@-Z\\-_]                # simple two-byte escape
    )
    """,
    re.VERBOSE,
)

_PROMPT_TAIL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:>>>|\.\.\.)\s*$"), "Python REPL (>>> / ...)"),
    (re.compile(r"In\s*\[\d+\]:\s*$"), "IPython (In [n]:)"),
    (re.compile(r"\((?:i?pdb)\)\s*$", re.IGNORECASE), "Python debugger ((Pdb)/(ipdb))"),
    (re.compile(r"[\$#%>]\s*$"), "shell/generic prompt ($ / # / % / >)"),
]

_TRAILING_BRACKET_RE = re.compile(r"\[([^\[\]]{1,200})\]\s*$")


def strip_ansi(text: str) -> str:
    """Strip ANSI/VT100 escapes. tmux captures with ``-e`` (colour kept for a
    real terminal); for anything reading the text they are pure token overhead."""
    return _ANSI_ESCAPE_RE.sub("", text)


def last_nonblank_line(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.rstrip()
    return None


def _extract_annotation(line: str) -> tuple[str | None, str]:
    """Split a trailing ``[note]`` off *line*.

    The operator convention: a note typed directly at a parked prompt and never
    submitted. Stripping it back off is what lets the prompt-token check still
    recognize ``[triage]>`` as a prompt ending in ``>``.
    """
    m = _TRAILING_BRACKET_RE.search(line)
    if not m:
        return None, line
    return m.group(1), line[: m.start()].rstrip()


def classify_prompt(line: str | None) -> dict[str, Any]:
    """Classify a pane's last line as a parked prompt. Tri-state, always.

    Known limitations, documented rather than papered over:
      - Only line-based shell/REPL prompts are recognized. A full-screen TUI
        (vim, htop, a pager, a menu picker) blocked on a keypress comes back
        ``"no"`` even though it may badly need a human.
      - A command's own output ending in ``$``/``#``/``%``/``>`` is a possible
        false positive. This is a heuristic over pane text, not a readback of
        the shell's own state.
    """
    if line is None or not line.strip():
        return {
            "at_prompt": "uncertain",
            "at_prompt_reason": "pane has no readable output (empty, or snapshot unavailable)",
            "annotation": None,
        }

    annotation, remainder = _extract_annotation(line)
    for pattern, label in _PROMPT_TAIL_PATTERNS:
        if pattern.search(remainder):
            reason = f"last line ends with {label}"
            if annotation:
                reason += f"; trailing note {annotation!r} suggests parked deliberately"
            return {
                "at_prompt": "yes",
                "at_prompt_reason": reason,
                "annotation": annotation,
            }

    if annotation is not None:
        return {
            "at_prompt": "uncertain",
            "at_prompt_reason": (
                f"last line ends with a bracketed note {annotation!r} but no "
                "recognized prompt token precedes it -- could be a parked prompt "
                "this heuristic does not recognize, or ordinary bracketed output "
                "(e.g. a log-level tag)"
            ),
            "annotation": annotation,
        }

    return {
        "at_prompt": "no",
        "at_prompt_reason": "last line does not match any known prompt pattern",
        "annotation": None,
    }


# --------------------------------------------------------------------------
# The tmux probe -- the reason an empty fleet and a broken read differ here
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """What we know about the tmux server before we trust any enumeration."""

    server_running: bool
    tmux_version: str
    detail: str
    #: tmux's own ``#{socket_path}``, when a server is up -- the server
    #: CONFIRMING which socket we reached. ``None`` when no server is running or
    #: the query itself failed (recorded, not guessed).
    socket_path: str | None = None


async def probe_tmux() -> Probe:
    """Establish whether tmux exists and whether a server is up.

    Callers must have resolved and installed the socket directory first; this
    probe deliberately speaks to whatever socket that decision selected, never
    to an ambient one.

    Raises:
        FleetError: tmux is not on PATH, or the server errored in a way that is
            NOT "no server running". Never returns a value that a caller could
            mistake for "the fleet is empty".
    """
    try:
        version = (await run_tmux_scoped("-V")).strip()
    except FileNotFoundError as exc:
        raise FleetError(
            "tmux is not on PATH. This tool is a tmux client and cannot work "
            "without it; install tmux or fix PATH."
        ) from exc
    except RuntimeError as exc:
        raise FleetError(f"`tmux -V` failed: {exc}") from exc

    try:
        await run_tmux_scoped("list-sessions", "-F", "#{session_name}")
    except RuntimeError as exc:
        absent = _absent_server_reason(str(exc))
        if absent is not None:
            return Probe(False, version, absent)
        raise FleetError(
            f"`tmux list-sessions` failed in a way that is NOT 'no server "
            f"running': {exc}. Refusing to report an empty fleet, because this "
            "is an unreadable socket rather than an empty one."
        ) from exc

    socket_path: str | None
    try:
        socket_path = (
            await run_tmux_scoped("display-message", "-p", "#{socket_path}")
        ).strip() or None
    except RuntimeError:
        socket_path = None
    return Probe(True, version, "server running", socket_path)


# --------------------------------------------------------------------------
# Scoped reads -- the only sanctioned way this module learns what tmux knows.
# Every call is pinned through run_tmux_scoped() (explicit -S), and a genuinely
# unreadable socket is a FleetError, not an empty list.
# --------------------------------------------------------------------------

#: Sentinel returned by _capture_pane_scoped() on failure. A real pane can
#: legitimately be blank (""), so this string is not, letting a caller tell
#: "nothing captured" apart from "capture failed" without a second signal.
_PANE_CAPTURE_UNAVAILABLE = "<pane capture unavailable -- capture-pane failed>"


async def _enumerate_sessions_scoped() -> tuple[
    list[str], dict[str, float], dict[str, float], dict[str, str]
]:
    """Session roster, activity/created/cwd -- read through the pinned socket.

    Same tab-separated field order and per-field tolerance as tmux-kit's own
    enumeration; what is NOT mirrored is the failure path: an unreadable socket
    surfaces as a FleetError instead of an empty roster.

    Raises:
        FleetError: the read failed in a way that is not a recognized
            absent-server dialect.
    """
    try:
        output = await run_tmux_scoped(
            "list-sessions",
            "-F",
            "#{session_name}\t#{window_activity}\t#{session_created}\t#{pane_current_path}",
        )
    except RuntimeError as exc:
        absent = _absent_server_reason(str(exc))
        if absent is not None:
            return [], {}, {}, {}
        raise FleetError(
            "`tmux list-sessions` failed on "
            f"{socket_resolution.installed_socket_path()!r} in a way that is "
            f"NOT 'no server running': {exc}. Refusing to report an empty fleet, "
            "because this is an unreadable socket rather than an empty one."
        ) from exc

    names: list[str] = []
    activity: dict[str, float] = {}
    created: dict[str, float] = {}
    cwds: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name, _, rest = line.partition("\t")
        name = name.strip()
        if not name:
            continue
        names.append(name)
        activity_field, _, rest2 = rest.partition("\t")
        created_field, _, cwd_field = rest2.partition("\t")
        activity_field = activity_field.strip()
        if activity_field:
            try:
                activity[name] = float(activity_field)
            except ValueError:
                pass
        cwd_field = cwd_field.strip()
        if cwd_field:
            cwds[name] = cwd_field
        created_field = created_field.strip()
        if created_field:
            try:
                created[name] = float(created_field)
            except ValueError:
                pass
    return names, activity, created, cwds


async def _capture_pane_scoped(session: str, lines: int) -> str:
    """``capture-pane`` for *session*, read through the pinned socket.

    The target is resolved exactly (``=session`` via list-panes to the active
    pane's immutable %id), so a capture can never silently prefix-match a
    session the caller did not name; a failure returns _PANE_CAPTURE_UNAVAILABLE
    rather than '' so "nothing to see" and "capture failed" stay distinct.
    """
    try:
        # capture-pane's -t is a target-PANE and rejects the '=session'
        # exact-match form on tmux 3.4; list-panes DOES honor '=session'.
        # Resolve the active pane's immutable %id, then capture that pane.
        panes = await run_tmux_scoped(
            "list-panes",
            "-t",
            f"={session}",
            "-F",
            "#{pane_active}\t#{pane_id}",
        )
        pane_rows = [ln for ln in panes.splitlines() if ln.strip()]
        if not pane_rows:
            return _PANE_CAPTURE_UNAVAILABLE
        chosen = next((ln for ln in pane_rows if ln.startswith("1\t")), pane_rows[0])
        pane_id = chosen.split("\t", 1)[1].strip() if "\t" in chosen else ""
        if not pane_id.startswith("%"):
            return _PANE_CAPTURE_UNAVAILABLE
        return await run_tmux_scoped(
            "capture-pane",
            "-e",  # preserve ANSI escapes; strip_ansi() removes them later
            "-p",
            "-t",
            pane_id,
            "-S",
            f"-{lines}",
        )
    except RuntimeError:
        return _PANE_CAPTURE_UNAVAILABLE


async def _capture_pane_metadata_scoped(session: str) -> tuple[int, int, int]:
    """``(history_size, pane_height, history_limit)``, read through the pinned
    socket. Raises RuntimeError on failure; read_session translates it."""
    # list-panes evaluates formats in a real pane context, so the numbers exist
    # regardless of attachment (display-message would expand them to empty
    # strings for a detached session).
    output = await run_tmux_scoped(
        "list-panes",
        "-t",
        f"={session}",
        "-F",
        "#{pane_active}\t#{history_size}\t#{pane_height}\t#{history_limit}",
    )
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(
            f"tmux list-panes returned no panes for session '{session}' -- raw output: {output!r}"
        )
    chosen = next((ln for ln in lines if ln.startswith("1\t")), lines[0])
    parts = chosen.split("\t")
    if len(parts) != 4 or any(not p.strip() for p in parts[1:]):
        raise RuntimeError(
            f"tmux pane metadata for session '{session}' came back malformed -- "
            f"expected 'active\\thistory\\theight\\tlimit', got: {chosen!r}"
        )
    return int(parts[1].strip()), int(parts[2].strip()), int(parts[3].strip())


async def _label_sessions_safely(names: list[str]) -> tuple[list[Any], str | None]:
    """Harness labels for *names*, or an honest reason they are missing.

    A labeling OUTAGE and a labeling RESULT of all-unknown are different facts;
    the second element of the returned tuple is how a caller tells them apart,
    and list_sessions surfaces it as ``harness_labels_unavailable`` rather than
    silently reporting every session as unknown.
    """
    try:
        return await harness.label_sessions(names), None
    except (RuntimeError, OSError, ValueError) as exc:
        return [], f"harness.label_sessions failed: {exc}"


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


async def list_sessions(
    *,
    snapshot_lines: int = LIST_SNAPSHOT_LINES,
    socket_dir: str | None = None,
    socket_name: str | None = None,
) -> dict[str, Any]:
    """Every session on the RESOLVED tmux socket, each with a shallow read.

    The socket directory is resolved and injected here, once per call, so every
    row and the scope string that describes them come from the same server. The
    snapshot here is a SLIVER -- `read` before you characterize anything.
    """
    if snapshot_lines < 1:
        raise FleetError(
            f"REFUSED: --snapshot-lines must be >= 1 (got {snapshot_lines}). "
            "tmux reads a depth below one as some other depth entirely, so the "
            "capture would silently differ from the one the `_completeness` "
            "block reports -- and that block is the only honest bound a caller "
            "has on what came back."
        )
    resolution = socket_resolution.resolve_and_install(socket_dir, socket_name=socket_name)
    probe = await probe_tmux()
    now = time.time()
    socket_block = socket_resolution.describe(
        resolution, tmux_reported_socket_path=probe.socket_path
    )

    if not probe.server_running:
        return {
            "socket": socket_block,
            "server_running": False,
            "tmux_version": probe.tmux_version,
            "detail": probe.detail,
            "saw_nothing": True,
            "saw_nothing_note": socket_resolution.empty_fleet_note(
                resolution, f"no tmux server is running there ({probe.detail})"
            ),
            "sessions": [],
            "counts": {
                "session_count": 0,
                "at_prompt_yes": 0,
                "at_prompt_no": 0,
                "at_prompt_uncertain": 0,
            },
            "_completeness": _sessions_completeness(
                0, snapshot_lines, True, resolution, probe
            ),
        }

    names, activity, created, cwds = await _enumerate_sessions_scoped()
    label_list, labels_unavailable = await _label_sessions_safely(names)
    labels = {lab.session: lab for lab in label_list}

    rows: list[dict[str, Any]] = []
    for name in names:
        raw = await _capture_pane_scoped(name, snapshot_lines)
        capture_failed = raw == _PANE_CAPTURE_UNAVAILABLE
        text = "" if capture_failed else strip_ansi(raw)
        last = last_nonblank_line(text)
        verdict = classify_prompt(last)
        act = activity.get(name)
        lab = labels.get(name)
        rows.append(
            {
                "session": name,
                "last_line": last,
                **verdict,
                "harness": lab.label if lab else harness.HARNESS_UNKNOWN,
                "harness_evidence_source": lab.source if lab else "none",
                "harness_evidence": lab.evidence if lab else "not labeled",
                "last_activity_at": _iso(act),
                "idle_seconds": (round(now - act) if act is not None else None),
                "created_at": _iso(created.get(name)),
                "cwd": cwds.get(name),
                "snapshot_readable": bool(text.strip()),
            }
        )

    # Most-recently-active first. Before this, rows carried
    # whatever order `tmux list-sessions` returned them in -- which repeats
    # the same long-stale sessions at the top of every listing instead of
    # surfacing what the owner is actually looking at right now. Missing
    # activity (no window_activity parsed) sorts LAST, never first: a session
    # this tool cannot date is not "most recent," and must not be presented
    # as if it were. `session` name is the tiebreak, purely for a stable,
    # reproducible order when two sessions share a timestamp.
    rows.sort(
        key=lambda r: (-(activity.get(r["session"], float("-inf"))), r["session"])
    )

    counts = {
        "session_count": len(rows),
        "at_prompt_yes": sum(1 for r in rows if r["at_prompt"] == "yes"),
        "at_prompt_no": sum(1 for r in rows if r["at_prompt"] == "no"),
        "at_prompt_uncertain": sum(1 for r in rows if r["at_prompt"] == "uncertain"),
    }

    payload: dict[str, Any] = {
        "socket": socket_block,
        "server_running": True,
        "tmux_version": probe.tmux_version,
        "sessions": rows,
        "counts": counts,
        "_completeness": _sessions_completeness(
            len(rows), snapshot_lines, True, resolution, probe
        ),
    }
    if labels_unavailable is not None:
        payload["harness_labels_unavailable"] = labels_unavailable
    if not rows:
        payload["saw_nothing"] = True
        payload["saw_nothing_note"] = socket_resolution.empty_fleet_note(
            resolution,
            "a tmux server IS running there, but it currently owns no sessions",
        )
    return payload


def _sessions_completeness(
    count: int,
    snapshot_lines: int,
    enumerated: bool,
    resolution: SocketResolution,
    probe: Probe,
) -> dict[str, Any]:
    scope = (
        "every session on the tmux server socket "
        f"{resolution.server_socket_path} (socket directory "
        f"{resolution.socket_dir}, resolved from {resolution.source})"
    )
    if probe.socket_path:
        scope += f"; tmux itself reports its socket as {probe.socket_path}"

    block: dict[str, Any] = {
        "session_count": count,
        "complete": enumerated,
        "scope": scope,
        "socket_dir": resolution.socket_dir,
        "server_socket_path": resolution.server_socket_path,
        "socket_resolved_from": resolution.source,
        "not_covered": (
            "sessions on ANY OTHER socket directory (including the ambient "
            "TMUX_TMPDIR, which is deliberately not auto-detected), on a "
            "different socket name (-L/-S), under a different user, or inside a "
            "container are NOT visible here and are not counted as absent -- "
            "they are simply out of scope"
        ),
        "snapshot_lines_per_session": snapshot_lines,
        "snapshot_is_a_sliver": True,
        "note": (
            f"each session's snapshot is only the last {snapshot_lines} lines -- "
            "enough to classify the last line, NOT enough to describe what a "
            "session has been doing. Use `tmux-fleet read <session> --lines N` "
            "before characterizing any session."
        ),
    }

    if count == 0:
        block["saw_nothing"] = True
        block["saw_nothing_note"] = socket_resolution.empty_fleet_note(
            resolution,
            (
                "no tmux server is running there"
                if not probe.server_running
                else "a server is running there but owns no sessions"
            ),
        )
        block["complete_means"] = (
            "complete=true here means 'this is the whole of THAT socket', NOT "
            "'this machine has no tmux sessions'. Zero sessions on a resolved "
            "socket is a fact about the socket, and the socket was chosen by "
            "configuration -- check `socket.resolved_from` and "
            "`socket.ambient_ignored` before concluding the fleet is empty."
        )

    return block


# --------------------------------------------------------------------------
# socket -- the diagnostic that answers "where are you even looking?"
# --------------------------------------------------------------------------


async def socket_status(
    *, socket_dir: str | None = None, socket_name: str | None = None
) -> dict[str, Any]:
    """Report the resolved socket and whether a server answers on it.

    Deliberately its own verb. When a fleet comes back empty the first question
    is not "why are there no sessions" but "which socket did you read, and who
    told you to read it" -- and that must be answerable without running a
    listing whose emptiness is the thing in doubt.
    """
    resolution = socket_resolution.resolve_and_install(socket_dir, socket_name=socket_name)
    probe = await probe_tmux()
    block = socket_resolution.describe(
        resolution, tmux_reported_socket_path=probe.socket_path
    )
    session_count: int | None = None
    if probe.server_running:
        names, _activity, _created, _cwds = await _enumerate_sessions_scoped()
        session_count = len(names)

    return {
        "socket": block,
        "tmux_version": probe.tmux_version,
        "server_running": probe.server_running,
        "detail": probe.detail,
        "session_count": session_count,
        "saw_nothing": session_count in (0, None),
        "saw_nothing_note": (
            socket_resolution.empty_fleet_note(
                resolution,
                (
                    f"no tmux server is running there ({probe.detail})"
                    if not probe.server_running
                    else "a server is running there but owns no sessions"
                ),
            )
            if session_count in (0, None)
            else None
        ),
    }


# --------------------------------------------------------------------------
# attention rollup
# --------------------------------------------------------------------------


async def attention(
    *,
    quiet_seconds: int = DEFAULT_QUIET_SECONDS,
    socket_dir: str | None = None,
    socket_name: str | None = None,
) -> dict[str, Any]:
    """Which sessions plausibly want a human -- a triage ORDER, not a verdict.

    Counts are computed over every session BEFORE any filter, so the filtered
    view can always be compared against the whole fleet. The socket block is
    carried through verbatim from the underlying listing: a rollup over the
    wrong socket is worse than no rollup, so the provenance travels with it.
    """
    check_quiet_seconds(quiet_seconds)
    listing = await list_sessions(socket_dir=socket_dir, socket_name=socket_name)
    rows = listing["sessions"]
    now = time.time()

    def _bucket(row: dict[str, Any]) -> str:
        idle = row.get("idle_seconds")
        quiet = idle is not None and idle >= quiet_seconds
        if row["at_prompt"] == "yes":
            return "parked_at_prompt_quiet" if quiet else "parked_at_prompt_recent"
        if row["at_prompt"] == "uncertain":
            return "unreadable_or_ambiguous"
        return "working_quiet" if quiet else "working"

    for row in rows:
        row["bucket"] = _bucket(row)

    order = [
        "parked_at_prompt_quiet",
        "unreadable_or_ambiguous",
        "parked_at_prompt_recent",
        "working_quiet",
        "working",
    ]
    candidates = [r for r in rows if r["bucket"] != "working"]
    candidates.sort(
        key=lambda r: (order.index(r["bucket"]), -(r.get("idle_seconds") or 0))
    )

    return {
        "socket": listing["socket"],
        "server_running": listing["server_running"],
        "generated_at": _iso(now),
        "quiet_threshold_seconds": quiet_seconds,
        "fleet_counts": listing["counts"],
        "bucket_counts": {b: sum(1 for r in rows if r["bucket"] == b) for b in order},
        "candidates": candidates,
        "how_to_read_this": (
            "This is a prior for WHERE TO LOOK FIRST, not a conclusion about any "
            "one session. A session ranked low here can still need a human, and "
            "a session ranked high may be one the operator parked on purpose -- "
            "a populated `annotation` means exactly that, and should be led with "
            "rather than re-derived. Nothing in this rollup substitutes for "
            "reading a session."
        ),
        "_completeness": {
            **listing["_completeness"],
            "candidates_returned": len(candidates),
            "counts_computed_over": "every session, before any filter",
            "ranking_is_heuristic": True,
            "blind_spot": (
                "a full-screen TUI blocked on a keypress classifies as "
                "at_prompt=no and will NOT appear as a candidate; this rollup "
                "cannot see it"
            ),
        },
    }


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------


async def read_session(
    session: str,
    *,
    lines: int = DEFAULT_READ_LINES,
    keep_ansi: bool = False,
    socket_dir: str | None = None,
    socket_name: str | None = None,
) -> dict[str, Any]:
    """Read a session's pane with an honest bound on what came back.

    Every failure path names the socket that was searched: "no session named X"
    is a different problem from "no session named X *on the socket you
    configured*", and only the second one is actionable.
    """
    if lines < 1:
        raise FleetError(f"REFUSED: --lines must be >= 1 (got {lines})")
    if lines > MAX_READ_LINES:
        raise FleetError(
            f"REFUSED: --lines {lines} exceeds the ceiling of {MAX_READ_LINES}. "
            "Refusing rather than silently capping: a caller who asked for more "
            "than they got must be told."
        )

    resolution = socket_resolution.resolve_and_install(socket_dir, socket_name=socket_name)
    probe = await probe_tmux()
    if not probe.server_running:
        raise FleetError(
            f"no tmux server is running on {resolution.server_socket_path}, so "
            f"session {session!r} cannot be read ({probe.detail}). "
            + socket_resolution.empty_fleet_note(resolution, "no server there")
        )

    names, _activity, _created, _cwds = await _enumerate_sessions_scoped()
    if session not in names:
        raise FleetError(
            f"no session named {session!r} on the tmux server at "
            f"{resolution.server_socket_path} (socket directory "
            f"{resolution.socket_dir}, resolved from {resolution.source}). "
            f"Present: {sorted(names)}. Refusing rather than letting tmux "
            "prefix-match a name you did not mean. If the session you meant "
            "lives on a different socket, this tool is pointed elsewhere -- see "
            "`tmux-fleet socket`."
        )

    try:
        (
            history_size,
            pane_height,
            history_limit,
        ) = await _capture_pane_metadata_scoped(session)
    except RuntimeError as exc:
        raise FleetError(
            f"could not read scrollback metadata for {session!r}: {exc}. "
            "Refusing to return a pane read whose completeness cannot be "
            "established."
        ) from exc

    raw = await _capture_pane_scoped(session, lines)
    if raw == _PANE_CAPTURE_UNAVAILABLE:
        raise FleetError(
            f"could not capture the pane for {session!r} on "
            f"{resolution.server_socket_path}: capture-pane failed even though "
            "the session was just confirmed present. Refusing to return a "
            "fabricated empty read."
        )
    text = raw if keep_ansi else strip_ansi(raw)
    returned = len(text.splitlines())

    # `--lines N` is tmux's `-S -N`: N lines of HISTORY *before* the visible
    # screen, and the visible screen always comes along. The honest question is
    # whether the requested depth reached the beginning of retained scrollback,
    # which is `history_size`.
    complete = lines >= history_size
    return {
        "socket": socket_resolution.describe(
            resolution, tmux_reported_socket_path=probe.socket_path
        ),
        "session": session,
        "pane": text,
        "raw_bytes": len(raw.encode("utf-8")),
        "returned_bytes": len(text.encode("utf-8")),
        "ansi_stripped": not keep_ansi,
        "last_line": last_nonblank_line(strip_ansi(raw)),
        **classify_prompt(last_nonblank_line(strip_ansi(raw))),
        "_completeness": {
            "history_lines_requested": lines,
            "lines_returned": returned,
            "complete": complete,
            "history_size": history_size,
            "pane_height": pane_height,
            "session_history_limit": history_limit,
            "used_default_depth": lines == DEFAULT_READ_LINES,
            "max_read_lines": MAX_READ_LINES,
            "what_lines_means": (
                "--lines N is tmux's `-S -N`: N lines of history BEFORE the "
                "visible screen. The visible screen always comes along, so "
                "lines_returned is about N + pane_height and must NOT be "
                "compared against N to judge completeness."
            ),
            "note": (
                f"read is COMPLETE: {lines} requested >= history_size "
                f"{history_size}, so the whole retained scrollback for this "
                "session is in this response."
                if complete
                else (
                    f"read is INCOMPLETE: {history_size - lines} lines of "
                    f"retained history sit beyond the window (history_size "
                    f"{history_size} > requested {lines}). Widen --lines (up to "
                    f"{MAX_READ_LINES}) rather than presenting this slice as the "
                    "whole history."
                )
            ),
        },
    }


# --------------------------------------------------------------------------
# send -- deny by default
# --------------------------------------------------------------------------


# The audit log lives in its own module: there are TWO verbs that change the
# world (`send` and `create`) and they must write to the SAME log.
audit_log_path = audit.audit_log_path
_audit = audit.append


async def send_input(
    session: str,
    *,
    text: str | None = None,
    key: str | None = None,
    submit: bool = False,
    confirmed: bool = False,
    socket_dir: str | None = None,
    socket_name: str | None = None,
) -> dict[str, Any]:
    """Type into a session. Refuses unless *confirmed* is explicitly true.

    Deny-by-default is the whole point: this tool observes a fleet it did not
    create, and an unconfirmed keystroke into somebody else's session is
    indistinguishable from sabotage.

    Two shapes:

    * **raw keystrokes** (default) -- ``--text`` types and does not submit
      unless the text itself carries a newline. This is what a caller wants when
      filling a field it is not ready to send.
    * **a command** (``submit=True``) -- types *text* and submits it with
      exactly ONE Enter, in ONE call. A relayed command must never sit ARMED
      while the owner is told it was sent.

    Either way the response NAMES what happened -- ``outcome`` is
    ``submitted`` / ``armed`` / ``uncertain``, backed by a readback of the pane,
    never a bare "delivered".

    Raises:
        FleetError: on refusal (unconfirmed, unknown session, bad argument, over
            cap). Every refusal is audited before it is raised.
    """
    if (text is None) == (key is None):
        raise FleetError("pass exactly one of --text or --key")

    if not confirmed:
        _audit(
            {
                "action": "send",
                "session": session,
                "outcome": "refused",
                "reason": "not confirmed",
                "preview": tk_keys.redact_preview(text) if text else key,
            }
        )
        raise FleetError(
            "REFUSED: sending input is deny-by-default and this call did not "
            "pass --confirmed.\n"
            f"  session: {session}\n"
            f"  would have sent: {tk_keys.redact_preview(text) if text else key!r}\n"
            "Nothing was sent. This tool observes a fleet it did not create; "
            "typing into someone else's session is an action with a real blast "
            "radius, so it requires an explicit, per-invocation --confirmed. "
            "Re-run with --confirmed if you genuinely intend it."
        )

    # AFTER the consent gate, deliberately: a caller who forgot --confirmed must
    # be told about the fence that always applies, not handed an argument-shape
    # lecture that implies consent was fine.
    if submit and key is not None:
        _audit(
            {
                "action": "send",
                "session": session,
                "outcome": "refused",
                "reason": "--submit with --key",
                "key": key,
            }
        )
        raise FleetError(
            "REFUSED: --submit applies to --text, not --key. --submit means "
            "'type this command and press Enter once'; a named key is already a "
            f"single key event, and {key!r} plus an Enter would be two. Send "
            "`--key Enter` on its own to submit what is already sitting on the "
            "input line."
        )

    if text is not None:
        encoded = text.encode("utf-8")
        if len(encoded) > tk_keys.MAX_TEXT_BYTES:
            _audit(
                {
                    "action": "send",
                    "session": session,
                    "outcome": "refused",
                    "reason": "over MAX_TEXT_BYTES",
                    "bytes": len(encoded),
                }
            )
            raise FleetError(
                f"REFUSED: {len(encoded)} bytes exceeds the "
                f"{tk_keys.MAX_TEXT_BYTES}-byte cap on a single send."
            )
        # Each newline becomes its own `send-keys Enter` subprocess, so the
        # newline count is a fork count. Counted BEFORE the cap so a
        # capped-plus-one send cannot half-submit a multi-line send.
        if submit:
            _, _interior = submission.split_for_submission(text.rstrip("\r\n"))
            _enters = _interior + 1
        else:
            _, _enters = submission.split_for_submission(text)
        if _enters > submission.MAX_SUBMIT_KEYS:
            _audit(
                {
                    "action": "send",
                    "session": session,
                    "outcome": "refused",
                    "reason": "over MAX_SUBMIT_KEYS",
                    "newlines": _enters,
                }
            )
            raise FleetError(
                f"REFUSED: {_enters} newlines exceeds the "
                f"{submission.MAX_SUBMIT_KEYS}-Enter cap on a single send. Each "
                "newline is delivered as a real Enter key event (one tmux call "
                "each), so an unbounded count is a fork amplifier. Split the send."
            )
    else:
        assert key is not None
        if key not in tk_keys.ALLOWED_KEYS:
            _audit(
                {
                    "action": "send",
                    "session": session,
                    "outcome": "refused",
                    "reason": "key not in allowed set",
                    "key": key,
                }
            )
            raise FleetError(
                f"REFUSED: {key!r} is not in the allowed key set. Allowed: "
                f"{sorted(tk_keys.ALLOWED_KEYS)}"
            )

    # Resolved AFTER the deny-by-default gate on purpose: a broken socket config
    # must never preempt the refusal path, because "refused" is the safe outcome
    # and must be reachable even on a misconfigured box.
    resolution = socket_resolution.resolve_and_install(socket_dir, socket_name=socket_name)
    probe = await probe_tmux()
    if not probe.server_running:
        _audit(
            {
                "action": "send",
                "session": session,
                "outcome": "refused",
                "reason": "no tmux server",
                "socket_dir": resolution.socket_dir,
                "server_socket_path": resolution.server_socket_path,
            }
        )
        raise FleetError(
            f"REFUSED: no tmux server is running on "
            f"{resolution.server_socket_path} ({probe.detail})"
        )

    names, _activity, _created, _cwds = await _enumerate_sessions_scoped()
    if session not in names:
        _audit(
            {
                "action": "send",
                "session": session,
                "outcome": "refused",
                "reason": "unknown session",
                "socket_dir": resolution.socket_dir,
                "server_socket_path": resolution.server_socket_path,
            }
        )
        raise FleetError(
            f"REFUSED: no session named {session!r} on the tmux server at "
            f"{resolution.server_socket_path} (socket directory "
            f"{resolution.socket_dir}, resolved from {resolution.source}). "
            f"Present: {sorted(names)}. Refusing rather than letting tmux "
            "prefix-match into a session you did not name."
        )

    # A newline inside --text is NOT sent as a byte (LF, which a raw-mode TUI
    # reads as Ctrl+J and silently drops). Each newline is delivered as its own
    # Enter KEY EVENT (CR), which is what a real keypress produces.
    before_line = last_nonblank_line(await _read_pane_text(session))

    if text is not None:
        if submit:
            argvs, interior = submission.build_send_argvs(session, text.rstrip("\r\n"))
            argvs.append(tk_keys.build_send_key_argv(session, "Enter"))
            enter_count = interior + 1
        else:
            argvs, enter_count = submission.build_send_argvs(session, text)
    else:
        assert key is not None
        argvs = [tk_keys.build_send_key_argv(session, key)]
        enter_count = 1 if key == "Enter" else 0

    # READ THE PANE BACK, in two halves, before saying anything about what
    # happened: type, watch it APPEAR, submit, watch it GO. Absence alone proves
    # nothing -- text that never rendered is also absent.
    readback = await _send_and_read_back(session, argvs, text=text)
    settled_text = readback["pane"]
    outcome, confirmed = submission.classify_submission(
        enter_count=enter_count,
        appeared=readback["appeared"],
        cleared=readback["cleared"],
    )

    _audit(
        {
            "action": "send",
            "session": session,
            "outcome": "delivered",
            "preview": tk_keys.redact_preview(text) if text else key,
            "enter_key_events": enter_count,
            "submitted": enter_count > 0,
            "submission_outcome": outcome,
            "submission_confirmed": confirmed,
            "submit_requested": submit,
            "socket_dir": resolution.socket_dir,
            "server_socket_path": resolution.server_socket_path,
        }
    )

    return {
        "socket": socket_resolution.describe(
            resolution, tmux_reported_socket_path=probe.socket_path
        ),
        "session": session,
        # THE answer to "did it run or is it sitting there?", first and by name.
        "outcome": outcome,
        "delivered": True,
        # `delivered` and `submitted` are DIFFERENT facts, reported separately.
        "submitted": enter_count > 0,
        "enter_key_events": enter_count,
        "submission_confirmed": confirmed,
        "submit_requested": submit,
        "sent": {"text": text} if text is not None else {"key": key},
        "readback": {
            "input_line_before": before_line,
            "input_line_after": readback["after_line"],
            "typed_text_appeared_on_input_line": readback["appeared"],
            "typed_text_still_on_input_line": readback["holds_after"],
            "input_line_cleared_after_enter": readback["cleared"],
            "waited_seconds": round(readback["waited_seconds"], 3),
            "method": (
                "two halves, both required for a confirmed submission: the typed "
                "text is first watched APPEAR on the pane's last non-blank line "
                "(proof the target received the keystrokes), and only then, "
                "after the Enter, watched GO (proof it consumed the line). "
                "Absence alone proves nothing. This observes the INPUT LINE; it "
                "is not a claim that the command succeeded. null means the "
                "question could not be judged."
            ),
        },
        "audit_log": str(audit_log_path()),
        "settled_last_line": last_nonblank_line(settled_text),
        "note": (
            submission.outcome_note(outcome, submit=submit)
            + " The snapshot above was taken immediately after the send; a "
            "session that takes time to react may not have reacted yet. This is "
            "not a completion signal."
            + submission.submission_note(enter_count=enter_count, key=key)
        ),
    }


async def _read_pane_text(session: str) -> str:
    """One scoped pane capture, ANSI stripped, '' when unavailable."""
    raw = await _capture_pane_scoped(session, LIST_SNAPSHOT_LINES)
    return "" if raw == _PANE_CAPTURE_UNAVAILABLE else strip_ansi(raw)


async def _poll_input_line(
    session: str, text: str, *, want_present: bool
) -> tuple[bool | None, str, str | None]:
    """Re-read the pane until the input line reaches *want_present*, or time out.

    Returns ``(observed, pane_text, last_non_blank_line)`` where ``observed`` is
    the FINAL judgement -- True/False as read, or None when the pane could not
    answer at all. Polls rather than sleeping a fixed interval.
    """
    deadline = time.monotonic() + submission.SUBMIT_CONFIRM_TIMEOUT_S
    while True:
        pane = await _read_pane_text(session)
        line = last_nonblank_line(pane)
        holds = submission.input_line_holds_text(line, text)
        if holds is want_present:
            return holds, pane, line
        if time.monotonic() >= deadline:
            return holds, pane, line
        await asyncio.sleep(submission.SUBMIT_CONFIRM_POLL_S)


async def _send_and_read_back(
    session: str, argvs: list[list[str]], *, text: str | None
) -> dict[str, Any]:
    """Run the send, watching the input line on both sides of the submit.

    When the send ENDS with an Enter, that Enter is held back so the two halves
    can be observed separately: type everything else -> watch the text APPEAR;
    send the final Enter -> watch the text GO. Both halves are required to call
    a submission confirmed.
    """
    submit_argv: list[str] | None = None
    if argvs and argvs[-1][-1] == "Enter" and len(argvs) > 1:
        submit_argv = argvs[-1]
        argvs = argvs[:-1]

    started = time.monotonic()
    for argv in argvs:
        await run_tmux_scoped(*argv)

    appeared: bool | None = None
    pane = ""
    line: str | None = None
    if text is not None:
        appeared, pane, line = await _poll_input_line(session, text, want_present=True)
    else:
        pane = await _read_pane_text(session)
        line = last_nonblank_line(pane)

    cleared: bool | None = None
    if submit_argv is not None:
        await run_tmux_scoped(*submit_argv)
        if text is not None:
            holds, pane, line = await _poll_input_line(
                session, text, want_present=False
            )
            cleared = None if holds is None else not holds
        else:
            pane = await _read_pane_text(session)
            line = last_nonblank_line(pane)

    return {
        "pane": pane,
        "after_line": line,
        "appeared": appeared,
        "cleared": cleared,
        "holds_after": (
            submission.input_line_holds_text(line, text) if text is not None else None
        ),
        "waited_seconds": time.monotonic() - started,
    }


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
