"""`create` -- the one verb that adds a session, fenced so it can only add.

Every other verb in this tool is read-or-observe. This verb touches the owner's
live working environment, so the question that shapes every decision here is not
"what should this do when used correctly" but **"what could a confused agent do
with it, and is that outcome impossible or merely unlikely?"**

The failure modes, and what makes each impossible
-------------------------------------------------
1. **Landing on somebody else's session.** A name that already exists is REFUSED
   -- never attached to, never reused, never renamed around. Creation is
   deliberately NOT idempotent. Two independent fences: our own pre-check, and
   tmux's own atomic ``duplicate session`` refusal, which closes the race.
2. **Creating a session under a name the caller did not ask for.** tmux 3.4
   silently rewrites ``.`` to ``_`` at creation time and still exits 0. Such
   names are refused up front, and the created name is read BACK and compared.
3. **Flooding the fleet.** A rolling rate guard, computed from the tool's own
   audit log, refuses past ``MAX_CREATES_PER_WINDOW``.
4. **Killing the whole fleet later.** ``tmux new-session`` starts a SERVER if
   none is running; a server that inherits a systemd --user unit's cgroup dies
   at that unit's next restart. The spawn is wrapped in tmux-kit's cgroup escape.
5. **Working in a directory nobody chose.** The default is ``$HOME``,
   explicitly, and it is reported -- never this process's ambient cwd.

IDEMPOTENCE -- asked, answered, refused
---------------------------------------
Idempotent creation requires deciding an existing session is "close enough" to
the one requested. This tool cannot know that: the session wearing the requested
name may be the owner's live work. A collision refuses instead -- with a refusal
that is INFORMATIVE (working directory, age, harness label, prompt state, and
whether this tool's own audit log shows WE created it).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from tmux_kit import cgroup as tk_cgroup
from tmux_kit import keys as tk_keys
from tmux_kit import names as tk_names
from tmux_kit.proc import default_env

from tmux_fleet import audit, harness, socket_resolution, submission
from tmux_fleet.socket_resolution import run_tmux_scoped

# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------

#: Creations permitted per rolling window. A human-directed batch is a handful
#: and passes; an agent looping on a retry is hundreds and is stopped within
#: seconds. There is intentionally NO environment override -- a guard an agent
#: can switch off from the same shell it is looping in is decoration.
MAX_CREATES_PER_WINDOW = 10
CREATE_RATE_WINDOW_SECONDS = 600

#: How long to wait for the new pane's shell to draw a prompt before giving up
#: on delivering an initial command. Keystrokes sent to a shell that has not
#: finished starting are simply lost.
SHELL_READY_TIMEOUT_SECONDS = 8.0
SHELL_READY_POLL_SECONDS = 0.15

#: Lines captured when reporting the settled state of a new session.
SETTLE_SNAPSHOT_LINES = 30

#: How long to poll for a newly created session to appear on a re-enumeration
#: before giving up. The cgroup-escape wrapper can return before the tmux server
#: it wraps has bound its socket, so an immediate read can legitimately see
#: nothing yet even though tmux already reported success.
POST_CREATE_POLL_TIMEOUT_SECONDS = 5.0
POST_CREATE_POLL_INTERVAL_SECONDS = 0.1


class CreateRefused(RuntimeError):
    """The creation was refused. Nothing was created.

    Messages start with ``REFUSED`` so the CLI's exit-code contract classifies
    them as ``2`` with no special case.
    """


class CreateFailed(RuntimeError):
    """The creation was attempted and did not complete cleanly.

    The message always says whether a session exists, because "did it happen?"
    is the only question that matters after a failed write.
    """


# --------------------------------------------------------------------------
# Validation -- everything checkable is checked BEFORE anything is created
# --------------------------------------------------------------------------


def validate_name(name: str) -> None:
    """Refuse any name that is unsafe, or that tmux would not honor exactly.

    * ``is_valid_session_name`` is the SECURITY boundary -- excludes shell
      metacharacters, whitespace, tmux's ``:`` target separator, and a leading
      ``-`` (parsed as an option by tmux itself, which quoting does NOT close).
    * ``is_tmux_stable_name`` is the HONESTY boundary -- tmux silently rewrites
      ``.`` to ``_``, so without this a caller could ask for one name, get
      another, and be told it succeeded.
    """
    if not tk_names.is_valid_session_name(name):
        raise CreateRefused(
            f"REFUSED: {name!r} is not a valid tmux session name for this tool. "
            "Allowed: 1-64 characters of ASCII letters, digits, underscore, dot "
            "and hyphen, starting with a letter, digit or underscore. Excluded "
            "on purpose: whitespace and shell metacharacters (injection), ':' "
            "(tmux's session:window.pane target separator), and a leading '-' "
            "(which tmux would parse as an option -- quoting does not prevent "
            "that) or leading '.' (path traversal)."
        )
    if not tk_names.is_tmux_stable_name(name):
        raise CreateRefused(
            f"REFUSED: {name!r} contains '.', which tmux silently rewrites to "
            "'_' at creation time while still reporting success -- so you would "
            f"get {name.replace('.', '_')!r} and this tool would have no honest "
            "way to tell you. Refusing rather than predicting tmux's mangling "
            f"rule. Use {name.replace('.', '-')!r} or {name.replace('.', '_')!r}."
        )


def resolve_cwd(explicit: str | None) -> tuple[str, str]:
    """Decide the new session's working directory. Returns ``(path, source)``.

    Defaulting to ``$HOME`` rather than this process's own working directory is
    the same decision the socket resolution made and for the same reason: a
    value inherited from ambient context produces a tool that behaves one way in
    a shell and another under a service, with nothing in the output to say which.
    """
    if explicit is None:
        home = os.path.expanduser("~")
        source = (
            "default ($HOME) -- deliberately NOT this process's current working "
            "directory, which would make the same command land somewhere "
            "different depending on where it was invoked from"
        )
        candidate = home
    else:
        candidate = os.path.abspath(os.path.expanduser(os.path.expandvars(explicit)))
        source = f"--cwd {explicit!r}"

    path = Path(candidate)
    if not path.exists():
        raise CreateRefused(
            f"REFUSED: working directory {candidate!r} does not exist (from "
            f"{source}). Refusing rather than letting tmux fall back to some "
            "other directory and silently start the work in the wrong place."
        )
    if not path.is_dir():
        raise CreateRefused(
            f"REFUSED: working directory {candidate!r} exists but is not a "
            f"directory (from {source})."
        )
    return str(path), source


# --------------------------------------------------------------------------
# The rate guard -- the fence against a loop, read from our own audit log
# --------------------------------------------------------------------------


def recent_creations(now: float | None = None) -> tuple[list[dict[str, Any]], int]:
    """Sessions this tool recorded creating inside the rolling window.

    Reads the tool's own audit log. Records whose timestamp cannot be parsed are
    counted and reported rather than dropped in silence -- an audit log that has
    partly gone unreadable must not quietly relax the guard it is the basis for.
    """
    now = time.time() if now is None else now
    records, unparsed = audit.read_records()
    cutoff = now - CREATE_RATE_WINDOW_SECONDS

    recent: list[dict[str, Any]] = []
    for record in records:
        if record.get("action") != "create" or record.get("outcome") != "created":
            continue
        stamp = record.get("time")
        if not isinstance(stamp, str):
            unparsed += 1
            continue
        try:
            when = audit.parse_iso(stamp)
        except ValueError:
            unparsed += 1
            continue
        if when >= cutoff:
            recent.append({"session": record.get("session"), "time": stamp})
    return recent, unparsed


def check_rate_limit(now: float | None = None) -> dict[str, Any]:
    """Refuse if this tool has created too many sessions too recently."""
    recent, unparsed = recent_creations(now)
    block = {
        "creations_in_window": len(recent),
        "window_seconds": CREATE_RATE_WINDOW_SECONDS,
        "limit": MAX_CREATES_PER_WINDOW,
        "recent": recent,
        "audit_lines_unparsed": unparsed,
        "source": str(audit.audit_log_path()),
    }
    if len(recent) >= MAX_CREATES_PER_WINDOW:
        raise CreateRefused(
            f"REFUSED: this tool has already created {len(recent)} sessions in "
            f"the last {CREATE_RATE_WINDOW_SECONDS // 60} minutes, which is its "
            f"ceiling of {MAX_CREATES_PER_WINDOW}. This guard exists because an "
            "agent retrying in a loop can create sessions far faster than a "
            "human notices, and this tool has no verb that removes one. Recent: "
            + ", ".join(f"{r['session']} at {r['time']}" for r in recent)
            + ". Wait for the window to roll, or create the session by hand with "
            "tmux if this is genuinely intended. There is deliberately no flag "
            "or environment variable that raises the ceiling."
        )
    return block


# --------------------------------------------------------------------------
# Collision reporting -- a refusal that is worth reading
# --------------------------------------------------------------------------


def _created_by_us(name: str) -> dict[str, Any]:
    """Does our own audit log show that WE created *name*?

    Best-effort and labelled as such. The log can be rotated, redirected
    (``TMUX_FLEET_AUDIT_LOG``) or predate the session, so a "no" here is
    genuinely "no record", never "definitely someone else's".
    """
    try:
        records, _ = audit.read_records()
    except audit.AuditError:
        return {
            "we_have_a_record": None,
            "note": "this tool's audit log could not be read, so ownership is unknown",
        }
    hits = [
        r
        for r in records
        if r.get("action") == "create"
        and r.get("outcome") == "created"
        and r.get("session") == name
    ]
    if not hits:
        return {
            "we_have_a_record": False,
            "note": (
                "no record in this tool's audit log of creating a session by "
                "this name. That means NO RECORD -- not proof the session "
                "belongs to someone else. The log may have been rotated, "
                "redirected, or the session may simply predate it."
            ),
        }
    return {
        "we_have_a_record": True,
        "created_at_per_our_log": hits[-1].get("time"),
        "note": (
            "this tool's own audit log shows it created a session by this name. "
            "The live session may still be a different one that reused the name "
            "after ours ended -- compare created_at."
        ),
    }


async def _describe_existing(name: str) -> dict[str, Any]:
    """Everything cheap and true about the session already wearing *name*."""
    # Imported lazily to keep the module import graph one-directional: fleet
    # imports creation for the verb, so creation must not import fleet at module
    # scope.
    from tmux_fleet import fleet

    _, activity, created, cwds = await fleet._enumerate_sessions_scoped()

    raw = await fleet._capture_pane_scoped(name, SETTLE_SNAPSHOT_LINES)
    raw = "" if raw == fleet._PANE_CAPTURE_UNAVAILABLE else raw
    text = fleet.strip_ansi(raw)
    last = fleet.last_nonblank_line(text)

    labels, _label_reason = await fleet._label_sessions_safely([name])
    label = labels[0] if labels else None

    now = time.time()
    act = activity.get(name)
    return {
        "session": name,
        "cwd": cwds.get(name),
        "created_at": audit.iso(created.get(name)),
        "last_activity_at": audit.iso(act),
        "idle_seconds": (round(now - act) if act is not None else None),
        "last_line": last,
        **fleet.classify_prompt(last),
        "harness": label.label if label else harness.HARNESS_UNKNOWN,
        "harness_evidence": label.evidence if label else "not labeled",
        "our_records": _created_by_us(name),
    }


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


async def _wait_for_prompt(name: str) -> dict[str, Any]:
    """Poll until the new pane has drawn something, or time out.

    Readiness is "the pane is no longer blank", NOT "the prompt matches a known
    pattern": the classifier is a heuristic over prompt TEXT, and a themed
    prompt it does not recognize would make a healthy shell look permanently
    unready. Pane-has-rendered-something is falsifiable and theme-independent.
    """
    from tmux_fleet import fleet

    deadline = time.monotonic() + SHELL_READY_TIMEOUT_SECONDS
    text = ""
    while time.monotonic() < deadline:
        raw = await fleet._capture_pane_scoped(name, SETTLE_SNAPSHOT_LINES)
        raw = "" if raw == fleet._PANE_CAPTURE_UNAVAILABLE else raw
        text = fleet.strip_ansi(raw)
        if text.strip():
            last = fleet.last_nonblank_line(text)
            return {
                "ready": True,
                "waited_seconds": round(
                    SHELL_READY_TIMEOUT_SECONDS - (deadline - time.monotonic()), 2
                ),
                "last_line": last,
                **fleet.classify_prompt(last),
                "readiness_test": (
                    "the pane rendered output, which means the shell has started "
                    "and is drawing. at_prompt above is the classifier's "
                    "separate opinion about the last line and does NOT gate "
                    "delivery -- a themed prompt it cannot recognize is still a "
                    "working shell."
                ),
            }
        await asyncio.sleep(SHELL_READY_POLL_SECONDS)

    return {
        "ready": False,
        "waited_seconds": SHELL_READY_TIMEOUT_SECONDS,
        "last_line": None,
        "readiness_test": (
            f"the pane was still blank after {SHELL_READY_TIMEOUT_SECONDS}s, so "
            "the shell has not drawn a prompt. Keystrokes sent to a shell that "
            "has not finished starting are silently lost, so nothing was typed."
        ),
    }


async def _spawn(name: str, cwd: str) -> None:
    """Run ``tmux new-session -d`` as argv, escaping this process's cgroup.

    argv, never a shell string. The cgroup escape matters even for a session
    create: if no server is running yet, THIS call forks one, and a server that
    inherits a systemd --user unit's cgroup dies -- taking every session on the
    host with it -- at that unit's next restart.

    Creates the socket's parent directory first, because ``-S`` does not (under
    ``TMUX_TMPDIR`` tmux makes ``<dir>/tmux-<uid>/`` itself; under an explicit
    ``-S <path>`` it only binds). 0700 because tmux refuses a socket directory
    other users can reach.
    """
    socket_path = socket_resolution.installed_socket_path()
    Path(socket_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    argv = [
        "tmux",
        "-S",
        socket_path,
        "new-session",
        "-d",
        "-s",
        name,
        "-c",
        cwd,
    ]
    if await tk_cgroup.should_escape():
        argv = tk_cgroup.wrap_exec_argv(argv)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=default_env(),
    )
    _out, err = await proc.communicate()
    if proc.returncode == 0:
        return

    stderr_text = err.decode("utf-8", errors="replace").strip()
    if "duplicate session" in stderr_text.lower():
        raise CreateRefused(
            f"REFUSED: tmux refused to create {name!r} because a session by that "
            f"name already exists ({stderr_text}). This is tmux's own atomic "
            "check firing after this tool's pre-check passed, which means the "
            "session appeared in between -- something else created it just now. "
            "NOTHING was created or modified here; the existing session was not "
            "touched."
        )
    raise CreateFailed(
        f"tmux refused to create session {name!r}: {stderr_text or 'no stderr'} "
        f"(exit {proc.returncode}). No session was created."
    )


async def create_session(
    name: str,
    *,
    cwd: str | None = None,
    command: str | None = None,
    confirmed: bool = False,
    socket_dir: str | None = None,
    socket_name: str | None = None,
) -> dict[str, Any]:
    """Create a NEW detached tmux session. Refuses unless *confirmed*.

    The order of operations is the safety design: every cheap, checkable refusal
    happens BEFORE anything in the world changes. Consent, then name, then
    working directory, then rate ceiling, then socket, then collision -- and
    only then a spawn.

    Raises:
        CreateRefused: nothing was created, and nothing was touched.
        CreateFailed: the spawn itself failed; the message says whether a
            session exists.
    """
    requested = {"name": name, "cwd": cwd, "command": command}

    if not confirmed:
        audit.append(
            {
                "action": "create",
                "session": name,
                "outcome": "refused",
                "reason": "not confirmed",
                "requested_cwd": cwd,
                "command_preview": (
                    tk_keys.redact_preview(command) if command else None
                ),
            }
        )
        raise CreateRefused(
            "REFUSED: creating a session is deny-by-default and this call did "
            "not pass --confirmed.\n"
            f"  name: {name}\n"
            f"  cwd: {cwd if cwd is not None else '(default: $HOME)'}\n"
            f"  would have run: "
            f"{tk_keys.redact_preview(command) if command else '(nothing -- bare shell)'}\n"
            "Nothing was created. This is the only verb in this tool that adds "
            "to the owner's live working environment, so consent is required per "
            "invocation -- the same fence `send` carries. Re-run with --confirmed "
            "if you genuinely intend it."
        )

    # Cheap, world-unchanging refusals first.
    validate_name(name)
    resolved_cwd, cwd_source = resolve_cwd(cwd)

    try:
        rate = check_rate_limit()
    except audit.AuditError as exc:
        raise CreateRefused(str(exc)) from exc

    resolution = socket_resolution.resolve_and_install(socket_dir, socket_name=socket_name)

    from tmux_fleet import fleet

    probe = await fleet.probe_tmux()
    # Collision check -- read through the pinned socket (fleet._enumerate raises
    # on an unreadable socket instead of swallowing it into an empty roster that
    # would let `create` proceed under a name that may collide with the owner's
    # live work).
    existing: list[str] = []
    if probe.server_running:
        existing, _activity, _created, _cwds = await fleet._enumerate_sessions_scoped()

    if name in existing:
        detail = await _describe_existing(name)
        audit.append(
            {
                "action": "create",
                "session": name,
                "outcome": "refused",
                "reason": "name already in use",
                "socket_dir": resolution.socket_dir,
            }
        )
        raise CreateRefused(
            f"REFUSED: a session named {name!r} already exists on "
            f"{resolution.server_socket_path}. Nothing was created, attached to, "
            "renamed or modified.\n"
            f"  working directory: {detail.get('cwd')}\n"
            f"  created: {detail.get('created_at')}\n"
            f"  last activity: {detail.get('last_activity_at')} "
            f"({detail.get('idle_seconds')}s ago)\n"
            f"  harness: {detail.get('harness')}\n"
            f"  at a prompt: {detail.get('at_prompt')} "
            f"({detail.get('at_prompt_reason')})\n"
            f"  last line: {detail.get('last_line')!r}\n"
            f"  our records: {detail['our_records']['note']}\n"
            "Creation is deliberately NOT idempotent. Reusing a session that "
            "merely shares a name would mean handing back work that may belong "
            "to someone else as though this tool had made it -- the one failure "
            "here with no undo. Choose a different name, or decide deliberately "
            "to use the existing session (read it first, and `send --confirmed` "
            "if you mean to type into it)."
        )

    # World changes from here.
    await _spawn(name, resolved_cwd)

    # Verify by reading BACK from tmux rather than trusting exit 0. A bounded
    # POLL, not a single read: the cgroup-escape wrapper returns before the tmux
    # server it wraps has necessarily bound its socket.
    deadline = time.monotonic() + POST_CREATE_POLL_TIMEOUT_SECONDS
    after: list[str] = []
    poll_created: dict[str, float] = {}
    poll_cwds: dict[str, str] = {}
    while True:
        (
            after,
            _poll_activity,
            poll_created,
            poll_cwds,
        ) = await fleet._enumerate_sessions_scoped()
        if name in after:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(POST_CREATE_POLL_INTERVAL_SECONDS)

    if name not in after:
        audit.append(
            {
                "action": "create",
                "session": name,
                "outcome": "failed",
                "reason": "session absent after apparently successful create",
                "socket_dir": resolution.socket_dir,
            }
        )
        raise CreateFailed(
            f"tmux reported success creating {name!r} but no session by that "
            f"exact name is present on {resolution.server_socket_path} "
            f"afterwards. Present: {sorted(after)}. Refusing to report a success "
            "this tool cannot confirm -- if a session was created under a "
            "mangled name it is in that list, and this tool will not guess which."
        )

    audit.append(
        {
            "action": "create",
            "session": name,
            "outcome": "created",
            "cwd": resolved_cwd,
            "command_preview": tk_keys.redact_preview(command) if command else None,
            "socket_dir": resolution.socket_dir,
            "server_socket_path": resolution.server_socket_path,
        }
    )

    observed_cwd = poll_cwds.get(name)
    observed_created = poll_created.get(name)

    initial: dict[str, Any] = {"requested": command, "delivered": False}
    status = "created"
    warnings: list[str] = []

    if command is not None:
        readiness = await _wait_for_prompt(name)
        initial["shell_readiness"] = readiness
        if readiness["ready"]:
            encoded = command.encode("utf-8")
            if len(encoded) > tk_keys.MAX_TEXT_BYTES:
                audit.append(
                    {
                        "action": "create-command",
                        "session": name,
                        "outcome": "refused",
                        "reason": "over MAX_TEXT_BYTES",
                        "bytes": len(encoded),
                    }
                )
                status = "created_command_not_delivered"
                initial["reason"] = (
                    f"the command is {len(encoded)} bytes, over the "
                    f"{tk_keys.MAX_TEXT_BYTES}-byte cap on a single send"
                )
                warnings.append(
                    f"session {name!r} EXISTS but the initial command was NOT "
                    "run: it exceeds the single-send byte cap."
                )
            else:
                # Same submission discipline as `send`: any newline INSIDE the
                # command is an Enter key event too, not a literal LF byte. A
                # trailing newline is stripped first so the submit stays exactly
                # ONE Enter.
                argvs, _ = submission.build_send_argvs(name, command.rstrip("\r\n"))
                for argv in argvs:
                    await run_tmux_scoped(*argv)
                await run_tmux_scoped(*tk_keys.build_send_key_argv(name, "Enter"))
                audit.append(
                    {
                        "action": "create-command",
                        "session": name,
                        "outcome": "delivered",
                        "preview": tk_keys.redact_preview(command),
                    }
                )
                initial["delivered"] = True
                initial["how"] = (
                    "TYPED into the session's shell (send-keys -l, then Enter), "
                    "not exec'd as the pane's process. This is deliberate: a "
                    "command given to `new-session` becomes the pane process, so "
                    "the session DIES the moment that command exits. Typed, the "
                    "shell survives, the output stays on screen, and the owner "
                    "can keep working in it. The consequence is that the command "
                    "IS shell-interpreted and runs under the shell's own rc "
                    "environment."
                )
        else:
            status = "created_command_not_delivered"
            initial["reason"] = readiness["readiness_test"]
            warnings.append(
                f"session {name!r} EXISTS but the initial command was NOT run -- "
                f"the shell never drew a prompt within "
                f"{SHELL_READY_TIMEOUT_SECONDS}s. Nothing was typed. Deliver it "
                f"yourself once the session is up: tmux-fleet send {name} --text "
                "'<command>' --confirmed, then --key Enter."
            )

    settled_raw = await fleet._capture_pane_scoped(name, SETTLE_SNAPSHOT_LINES)
    settled_raw = "" if settled_raw == fleet._PANE_CAPTURE_UNAVAILABLE else settled_raw
    settled = fleet.strip_ansi(settled_raw)

    return {
        "socket": socket_resolution.describe(
            resolution, tmux_reported_socket_path=probe.socket_path
        ),
        "status": status,
        "created": True,
        "session": name,
        "requested": requested,
        "cwd": resolved_cwd,
        "cwd_source": cwd_source,
        "cwd_observed": observed_cwd,
        "cwd_confirmed_by_tmux": (
            None
            if observed_cwd is None
            else os.path.normpath(observed_cwd) == os.path.normpath(resolved_cwd)
        ),
        "created_at": audit.iso(observed_created),
        "server_was_already_running": probe.server_running,
        "initial_command": initial,
        "warnings": warnings,
        "attach_command": _attach_command(resolution, name),
        "audit_log": str(audit.audit_log_path()),
        "rate_guard": rate,
        "settled_last_line": fleet.last_nonblank_line(settled),
        "_completeness": {
            "verified_by_reenumeration": True,
            "name_is_exact": True,
            "scope": (
                f"this session was created on the tmux server socket "
                f"{resolution.server_socket_path} (socket directory "
                f"{resolution.socket_dir}, resolved from {resolution.source}) -- "
                "the same socket every other verb in this tool reads"
            ),
            "sessions_not_touched": (
                "no existing session was attached to, renamed, killed, resized "
                "or typed into. This verb only adds; a name collision refuses "
                "rather than reusing."
            ),
            "note": (
                "the snapshot above was taken immediately after creation. A "
                "command that takes time to produce output will not have "
                "produced it yet -- this is NOT a completion signal, and "
                "delivered=true means the keystrokes were sent, never that the "
                "command succeeded. Poll with `read` to see what actually "
                "happened."
            ),
        },
    }


def _attach_command(
    resolution: socket_resolution.SocketResolution, name: str
) -> dict[str, Any]:
    """How a human actually gets to the session that was just made.

    Carries the socket directory explicitly. A bare ``tmux attach -t <name>`` is
    wrong for anyone whose shell points at a different ``TMUX_TMPDIR`` than this
    tool resolved.
    """
    default_dir = resolution.socket_dir == socket_resolution.SYSTEM_DEFAULT_SOCKET_DIR
    plain = f"tmux attach -t ={name}"
    explicit = f"TMUX_TMPDIR={resolution.socket_dir} tmux attach -t ={name}"
    return {
        "command": plain if default_dir else explicit,
        "always_correct": explicit,
        "note": (
            "=<name> is tmux's exact-match target form, so this cannot attach to "
            "a differently-named neighbour by prefix. "
            + (
                "This tool read the system default socket directory, so a plain "
                "`tmux attach` works from any shell."
                if default_dir
                else "TMUX_TMPDIR is set explicitly because this tool was "
                f"pointed at {resolution.socket_dir}; a shell without that "
                "variable would look on a different socket and report that the "
                "session does not exist."
            )
        ),
    }
