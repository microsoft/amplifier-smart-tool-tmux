"""The two gap verbs added on top of the ported observation set: `doctor`
(preflight) and `exit-code` (tmux-native ground truth for a finished session).

Both are deterministic -- no AI substrate is consulted. Both name their socket
explicitly, the same discipline every other verb holds.
"""

from __future__ import annotations

from typing import Any

import tmux_kit

from tmux_fleet import fleet, socket_resolution
from tmux_fleet.socket_resolution import run_tmux_scoped


async def doctor(
    *, socket_dir: str | None = None, socket_name: str | None = None
) -> dict[str, Any]:
    """Preflight: is this host ready to run the tool at all?

    Built on ``tmux_kit.doctor()`` for the mechanism checks (tmux present +
    version, socket directory resolvable + writable, cgroup-escape readiness),
    wrapped with this tool's own explicit socket block and a live server probe.

    Reporting a problem IS this verb's success: it always emits a JSON report
    (exit 0) with an ``ok`` boolean and a ``remedy`` for anything that failed,
    rather than a bare error -- a preflight that crashed on the first problem
    would be a worse preflight.
    """
    resolution = socket_resolution.resolve_and_install(socket_dir, socket_name=socket_name)
    # Point tmux-kit's own facade at the SAME resolved directory, so its
    # socket_dir / socket_dir_writable check describes the socket this tool
    # actually reads -- not tmux-kit's private default.
    tmux_kit.configure(socket_dir=resolution.socket_dir)
    report = await tmux_kit.doctor()

    checks: list[dict[str, Any]] = []

    checks.append(
        {
            "name": "tmux_present",
            "ok": bool(report.tmux_found),
            "detail": (
                f"tmux found (version {report.tmux_version})"
                if report.tmux_found
                else "tmux is not on PATH"
            ),
            "remedy": (
                None
                if report.tmux_found
                else "install tmux and ensure it is on PATH; this tool is a tmux "
                "client and cannot work without it"
            ),
        }
    )

    checks.append(
        {
            "name": "socket_dir_writable",
            "ok": bool(report.socket_dir_writable),
            "detail": (
                f"socket directory {report.socket_dir} is writable"
                if report.socket_dir_writable
                else f"socket directory {report.socket_dir} is not writable"
            ),
            "remedy": (
                None
                if report.socket_dir_writable
                else f"create {report.socket_dir} or make it writable, or point "
                "the tool at a writable directory via --socket-dir / "
                f"{socket_resolution.SOCKET_DIR_ENV_VAR}"
            ),
        }
    )

    # A live server probe. Its ABSENCE is not a failure -- a resolvable, writable
    # socket with no server yet is a perfectly healthy preflight -- but a socket
    # that is unreadable for some OTHER reason surfaces here as a fatal probe.
    server_running: bool | None
    probe_detail: str
    probe_error: str | None = None
    try:
        probe = await fleet.probe_tmux()
        server_running = probe.server_running
        probe_detail = probe.detail
    except fleet.FleetError as exc:
        server_running = None
        probe_detail = "probe failed"
        probe_error = str(exc)

    checks.append(
        {
            "name": "server_reachable",
            "ok": server_running is not False,  # None (unreadable) is the only fail
            "detail": (
                probe_detail
                if server_running is not None
                else f"could not probe the server: {probe_error}"
            ),
            "remedy": (
                None
                if server_running is not None
                else "the resolved socket is present but unreadable for a reason "
                "other than 'no server running' (e.g. permissions); inspect it "
                "with `tmux-fleet socket`"
            ),
            "note": (
                "a server that is simply not running yet is NOT a failure -- this "
                "check only fails when the socket is unreadable for another reason"
            ),
        }
    )

    ok = all(c["ok"] for c in checks)

    return {
        "socket": socket_resolution.describe(
            resolution,
            tmux_reported_socket_path=(
                None if server_running in (False, None) else probe_detail
            ),
        ),
        "ok": ok,
        "checks": checks,
        "tmux": {"found": report.tmux_found, "version": report.tmux_version},
        "server_running": server_running,
        "socket_dir_writable": report.socket_dir_writable,
        "cgroup": {
            "mode": report.cgroup_mode,
            "escape_ready": report.cgroup_escape_ready,
            "note": (
                "cgroup escape lets `create` fork a tmux SERVER that survives a "
                "systemd --user restart. escape_ready=false is only a risk for "
                "`create` when no server is already running; every read verb is "
                "unaffected."
            ),
        },
        "tmux_kit_notes": list(report.notes),
        "remedy": (
            None
            if ok
            else "one or more preflight checks failed; see each check's `remedy`"
        ),
        "note": (
            "reporting a problem IS this verb's success -- exit 0 with ok=false "
            "means the host is not ready and the remedies say why. This is a "
            "deterministic verb: no AI substrate is consulted."
        ),
    }


async def exit_code(
    session: str,
    *,
    socket_dir: str | None = None,
    socket_name: str | None = None,
) -> dict[str, Any]:
    """The tmux-native exit status of a finished session's active pane.

    Ground truth from tmux's own pane primitives (``#{pane_dead}`` /
    ``#{pane_dead_status}``), read through the pinned socket -- the same
    semantics as ``tmux_kit.exit_code``, but scoped with an explicit ``-S`` like
    every other call this tool makes.

    ``exit_code`` is ``null`` unless the pane is DEAD and tmux retained its
    status (which requires ``remain-on-exit on``; tmux's factory default tears a
    dead pane down immediately). "still running", "session gone", and "status
    not retained" are three different facts, each named in ``status`` / ``note``
    rather than collapsed into a bare ``null``.
    """
    resolution = socket_resolution.resolve_and_install(socket_dir, socket_name=socket_name)
    probe = await fleet.probe_tmux()
    if not probe.server_running:
        raise fleet.FleetError(
            f"no tmux server is running on {resolution.server_socket_path}, so "
            f"the exit status of session {session!r} cannot be read "
            f"({probe.detail}). "
            + socket_resolution.empty_fleet_note(resolution, "no server there")
        )

    names, _activity, _created, _cwds = await fleet._enumerate_sessions_scoped()
    if session not in names:
        raise fleet.FleetError(
            f"no session named {session!r} on the tmux server at "
            f"{resolution.server_socket_path} (socket directory "
            f"{resolution.socket_dir}, resolved from {resolution.source}). "
            f"Present: {sorted(names)}. NOTE: a session whose command finished "
            "may have VANISHED already -- tmux tears a dead pane/session down "
            "immediately unless it was started with `remain-on-exit on`. An "
            "absent session is therefore not the same as a zero exit."
        )

    try:
        output = await run_tmux_scoped(
            "list-panes",
            "-t",
            f"={session}",
            "-F",
            "#{pane_active}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_pid}",
        )
    except RuntimeError as exc:
        raise fleet.FleetError(
            f"could not read pane state for {session!r} on "
            f"{resolution.server_socket_path}: {exc}. Refusing to report an exit "
            "status this tool cannot establish."
        ) from exc

    rows = [ln for ln in output.splitlines() if ln.strip()]
    if not rows:
        raise fleet.FleetError(
            f"tmux returned no panes for session {session!r} even though it was "
            "just enumerated; refusing to report an exit status without a pane."
        )
    chosen = next((ln for ln in rows if ln.startswith("1\t")), rows[0])
    parts = chosen.split("\t")
    # pane_active, pane_dead, pane_dead_status, pane_pid
    pane_dead = parts[1].strip() if len(parts) > 1 else ""
    dead_status_raw = parts[2].strip() if len(parts) > 2 else ""
    pane_pid = parts[3].strip() if len(parts) > 3 else ""

    is_dead = pane_dead == "1"
    code: int | None = None
    if is_dead and dead_status_raw:
        try:
            code = int(dead_status_raw)
        except ValueError:
            code = None

    if not is_dead:
        status = "running"
        note = (
            "the session's active pane is ALIVE (its foreground process has not "
            "exited), so there is no exit status yet. exit_code is null because "
            "the question 'did it succeed?' has no answer for a running pane."
        )
    elif code is not None:
        status = "finished"
        note = (
            f"the pane is DEAD and tmux retained its exit status ({code}). 0 is "
            "typically success, nonzero failure, program-specific beyond that. "
            "This is tmux's own ground truth, not a heuristic over screen text."
        )
    else:
        status = "finished"
        note = (
            "the pane is DEAD but tmux did not retain an exit status (the session "
            "was not started with `remain-on-exit on`, or the status is "
            "otherwise unavailable). exit_code is null: the pane finished, but "
            "WITH WHAT is not knowable now -- and guessing 0 would be a lie."
        )

    return {
        "socket": socket_resolution.describe(
            resolution, tmux_reported_socket_path=probe.socket_path
        ),
        "session": session,
        "status": status,
        "exit_code": code,
        "pane_dead": is_dead,
        "pane_pid": int(pane_pid) if pane_pid.isdigit() else None,
        "note": note,
        "source": (
            "tmux pane primitives #{pane_dead} / #{pane_dead_status}, read "
            "through an explicit -S; deterministic, no AI substrate consulted"
        ),
    }
