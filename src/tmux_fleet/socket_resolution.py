"""Which tmux socket this tool reads -- decided explicitly, stated honestly.

The bug this module exists to kill
----------------------------------
A tmux client that makes no decision about which server it talks to inherits
the ambient process environment, so the answer becomes "whatever
``TMUX_TMPDIR`` (or ``$TMUX``) happened to be set to in the shell that
launched us" -- while a completeness claim simultaneously declares its scope
to be "every session on the default tmux server socket for this user". Those
two are not the same thing: run the same tool from a systemd unit, a cron job,
or any shell without that ambient export, and it reports an empty fleet with
``complete: true`` -- a silent-wrong-fleet lie.

The properties this module holds
--------------------------------
1. **Explicit, never ambient.** The socket directory is resolved from a
   documented, ordered set of sources and INJECTED into tmux-kit via
   ``tmux_kit.proc.set_env_factory()`` -- the library's documented
   host-application seam. The ambient ``TMUX_TMPDIR`` is deliberately NOT one
   of the sources. Auto-detecting it is how a tool works mysteriously on one
   box and fails mysteriously on the next.
2. **Ignoring is not hiding.** An ambient ``TMUX_TMPDIR``/``$TMUX`` that we
   decline to honor is REPORTED as seen-and-ignored, with the exact setting
   that would adopt it.
3. **Say which socket, always.** Every scope string names the resolved
   directory and the concrete server socket path. Where a server is running we
   go further and ask tmux itself (``#{socket_path}``), so the claim is
   CONFIRMED by the server rather than merely asserted.

Why ``$TMUX`` must be popped (load-bearing, not tidiness)
--------------------------------------------------------
tmux gives ``$TMUX`` -- set in every process descended from an *attached* tmux
client -- priority over ``TMUX_TMPDIR``. Any invocation from inside a tmux pane
would therefore silently ignore our resolved directory and talk to that pane's
server instead. ``tmux_kit.proc.tmux_env()`` pops ``TMUX`` for exactly this
reason, which is why we build the environment through it rather than setting
``TMUX_TMPDIR`` ourselves. Belt: every tmux call also carries an explicit
``-S <server_socket_path>`` (see ``run_tmux_scoped``), so the guarantee lives
on the command line where it is checkable, not only in reasoning about
precedence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tmux_kit import proc as tk_proc

#: tmux's own compiled-in default for ``TMUX_TMPDIR``. The server socket then
#: lives at ``<dir>/tmux-<uid>/<socket-name>``.
SYSTEM_DEFAULT_SOCKET_DIR = "/tmp"

#: tmux's default socket name within the per-uid directory. Changing it is
#: ``tmux -L``. Overridable per invocation (``--socket-name`` / the library
#: ``socket_name`` argument) so this tool can be pinned at a uniquely-socketed
#: server -- e.g. one created by ``tmux_kit.isolated_tmux_server()`` for tests.
DEFAULT_SOCKET_NAME = "default"

#: Environment variable naming the socket directory. Deliberately tmux-kit's
#: own variable name (no consumer brand): a co-installed tmux-kit consumer and
#: this tool then point at the same server, and one variable that every
#: consumer agrees on beats two that can silently disagree.
SOCKET_DIR_ENV_VAR = "TMUX_KIT_SOCKET_DIR"

#: Environment variable naming the socket NAME within that directory. Rarely
#: needed; exposed so a deployment (or a test) can pin an exact socket.
SOCKET_NAME_ENV_VAR = "TMUX_FLEET_SOCKET_NAME"

#: Where the deployment may pin the socket directory on disk. Overridable for
#: tests/alternate deployments, mirroring ``TMUX_FLEET_AUDIT_LOG``.
CONFIG_PATH_ENV_VAR = "TMUX_FLEET_CONFIG"
DEFAULT_CONFIG_PATH = "~/.config/tmux-fleet/config.json"

#: The key read from that config file.
CONFIG_KEY = "socket_dir"

SOURCE_ARGUMENT = "explicit --socket-dir argument"
SOURCE_CONFIG = f"config file key {CONFIG_KEY!r}"
SOURCE_ENV = f"{SOCKET_DIR_ENV_VAR} environment variable"
SOURCE_SYSTEM_DEFAULT = "system default (no configuration found)"

RESOLUTION_ORDER = [
    SOURCE_ARGUMENT,
    SOURCE_CONFIG,
    SOURCE_ENV,
    SOURCE_SYSTEM_DEFAULT,
]


class SocketConfigError(RuntimeError):
    """The socket configuration is present but unusable.

    Always fatal. A malformed or ambiguous socket setting must never degrade
    into "fall back to the default and read some other server" -- that is how a
    caller ends up confidently reading the wrong fleet.
    """


@dataclass(frozen=True)
class SocketResolution:
    """The decision: which socket directory, which name, and on whose authority."""

    socket_dir: str
    source: str
    source_detail: str
    config_path: str
    config_path_exists: bool
    ambient_tmux_tmpdir: str | None
    ambient_tmux: str | None
    socket_name: str = DEFAULT_SOCKET_NAME

    @property
    def server_socket_path(self) -> str:
        """Where tmux will look for the server, given this directory + name."""
        return str(Path(self.socket_dir) / f"tmux-{os.getuid()}" / self.socket_name)

    @property
    def ambient_ignored(self) -> bool:
        """True when an ambient setting exists that we declined to honor."""
        if self.ambient_tmux is not None:
            return True
        if self.ambient_tmux_tmpdir is None:
            return False
        return _normalize(self.ambient_tmux_tmpdir) != _normalize(self.socket_dir)


def _normalize(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path.strip()))


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_PATH_ENV_VAR, DEFAULT_CONFIG_PATH)).expanduser()


def _resolve_socket_name(explicit: str | None) -> str:
    """Explicit argument, then ``TMUX_FLEET_SOCKET_NAME``, then the default."""
    for candidate in (explicit, os.environ.get(SOCKET_NAME_ENV_VAR)):
        if candidate is None:
            continue
        name = candidate.strip()
        if not name:
            raise SocketConfigError(
                "REFUSED: a socket NAME was provided but is empty. Either give "
                "a non-empty tmux socket name or omit it to use the default "
                f"({DEFAULT_SOCKET_NAME!r})."
            )
        if "/" in name:
            raise SocketConfigError(
                f"REFUSED: socket name {name!r} contains '/'. A socket name is "
                "the final path component under <dir>/tmux-<uid>/; pass a "
                "directory via --socket-dir, not a path via --socket-name."
            )
        return name
    return DEFAULT_SOCKET_NAME


def _clean_dir_value(raw: str, origin: str) -> str:
    """Validate and normalize a socket-directory value.

    Blank and relative values are refused rather than repaired: a blank value
    would fall through to the ambient environment (the exact bug this module
    exists to kill), and a relative path silently means a different directory
    from every working directory.
    """
    value = raw.strip()
    if not value:
        raise SocketConfigError(
            f"REFUSED: {origin} is set but empty. An empty socket directory "
            "would fall through to whatever TMUX_TMPDIR the ambient environment "
            "happens to carry -- the silent-wrong-fleet bug this resolution "
            "exists to prevent. Either set it to an absolute directory, or "
            f"remove it entirely to get the system default "
            f"({SYSTEM_DEFAULT_SOCKET_DIR})."
        )
    expanded = os.path.expanduser(os.path.expandvars(value))
    if not os.path.isabs(expanded):
        raise SocketConfigError(
            f"REFUSED: {origin} is {value!r}, which is a relative path "
            f"(resolved: {expanded!r}). A relative socket directory names a "
            "different directory from every working directory, so the same "
            "configuration would read a different tmux fleet depending on where "
            "the tool was run from. Use an absolute path (or ~)."
        )
    return os.path.normpath(expanded)


def _from_config_file() -> tuple[str | None, str, Path, bool]:
    """Read the socket dir from the config file.

    Absent, null, and wrong-type are three DIFFERENT things: file/key absent ->
    fall through quietly; key present but ``null`` -> legal "no opinion", fall
    through and say so; key present, wrong type -> FATAL (a broken deployment
    must be told, not silently handed the default).
    """
    path = config_path()
    if not path.exists():
        return None, f"no config file at {path}", path, False

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SocketConfigError(
            f"REFUSED: could not read the socket config at {path}: {exc}. "
            "Refusing to fall back to a default socket, because a caller with a "
            "config file present is entitled to have it honored or to be told "
            "plainly that it is broken."
        ) from exc

    if not isinstance(raw, dict):
        raise SocketConfigError(
            f"REFUSED: the socket config at {path} must be a JSON object, got "
            f"{type(raw).__name__}."
        )

    if CONFIG_KEY not in raw:
        return (
            None,
            f"config file {path} exists but has no {CONFIG_KEY!r} key",
            path,
            True,
        )

    value = raw[CONFIG_KEY]
    if value is None:
        return (
            None,
            (
                f"config file {path} sets {CONFIG_KEY!r} to null -- an explicit "
                "'no opinion' on an optional field, so resolution continues to "
                "the next source"
            ),
            path,
            True,
        )
    if not isinstance(value, str):
        raise SocketConfigError(
            f"REFUSED: {CONFIG_KEY!r} in {path} must be a string or null, got "
            f"{type(value).__name__} ({value!r}). Absent, null and wrong-type "
            "are three different things; this is the third, and it is a broken "
            "deployment rather than an absent setting."
        )
    return (
        _clean_dir_value(value, f"{CONFIG_KEY!r} in {path}"),
        f"read from {path}",
        path,
        True,
    )


def resolve(
    explicit: str | None = None, *, socket_name: str | None = None
) -> SocketResolution:
    """Decide which tmux socket directory (and name) this process will read.

    Order, most explicit first: the *explicit* argument (the CLI's
    ``--socket-dir``), then the config file, then ``TMUX_KIT_SOCKET_DIR``, then
    tmux's system default. The ambient ``TMUX_TMPDIR`` is NOT consulted at any
    tier -- it is only recorded, so the response can say it was seen and
    ignored.

    Raises:
        SocketConfigError: a configured value exists but is unusable (blank,
            relative, wrong JSON type, unreadable file). Never silently
            downgraded to the default.
    """
    ambient_tmpdir = os.environ.get("TMUX_TMPDIR")
    ambient_tmux = os.environ.get("TMUX")
    name = _resolve_socket_name(socket_name)

    def _build(
        socket_dir: str, source: str, detail: str, path: Path, exists: bool
    ) -> SocketResolution:
        return SocketResolution(
            socket_dir=socket_dir,
            source=source,
            source_detail=detail,
            config_path=str(path),
            config_path_exists=exists,
            ambient_tmux_tmpdir=ambient_tmpdir,
            ambient_tmux=ambient_tmux,
            socket_name=name,
        )

    if explicit is not None:
        cleaned = _clean_dir_value(explicit, "--socket-dir")
        path = config_path()
        return _build(
            cleaned,
            SOURCE_ARGUMENT,
            f"passed explicitly on this invocation as {explicit!r}",
            path,
            path.exists(),
        )

    from_file, file_detail, path, exists = _from_config_file()
    if from_file is not None:
        return _build(from_file, SOURCE_CONFIG, file_detail, path, exists)

    env_raw = os.environ.get(SOCKET_DIR_ENV_VAR)
    if env_raw is not None:
        cleaned = _clean_dir_value(env_raw, SOCKET_DIR_ENV_VAR)
        return _build(
            cleaned,
            SOURCE_ENV,
            f"{file_detail}; {SOCKET_DIR_ENV_VAR}={env_raw!r}",
            path,
            exists,
        )

    return _build(
        SYSTEM_DEFAULT_SOCKET_DIR,
        SOURCE_SYSTEM_DEFAULT,
        (
            f"{file_detail}; {SOCKET_DIR_ENV_VAR} is unset -- falling back to "
            f"tmux's compiled-in default TMUX_TMPDIR ({SYSTEM_DEFAULT_SOCKET_DIR})"
        ),
        path,
        exists,
    )


def install(resolution: SocketResolution) -> None:
    """Inject *resolution* into tmux-kit for every subsequent tmux call.

    Uses ``tmux_kit.proc.set_env_factory()``, closed over an ALREADY RESOLVED
    value: tmux-kit calls the factory on every ``run_tmux()``, and re-resolving
    per call would let a config edit land halfway through a listing. Freshness
    lives one level up -- each public verb resolves once, at entry.

    Process-wide, last-writer-wins global state in tmux-kit. Fine for a
    one-shot CLI; tests restore it.
    """
    global _INSTALLED
    socket_dir = resolution.socket_dir
    tk_proc.set_env_factory(lambda: tk_proc.tmux_env(socket_dir))
    _INSTALLED = resolution


#: The resolution most recently passed to ``install()``. Read only through
#: ``installed_socket_path()``, which fails loud when it is unset.
_INSTALLED: SocketResolution | None = None


class SocketNotInstalledError(RuntimeError):
    """A tmux call was attempted before a socket decision was installed."""


def installed_socket_path() -> str:
    """The server socket path every tmux call must be pinned to with ``-S``.

    tmux resolves its server in the order ``-S`` > ``-L`` > ``$TMUX`` >
    ``TMUX_TMPDIR`` > compiled default. Popping ``$TMUX`` is correct, but it is
    a discipline that has to hold on every future edit, in a library this tool
    does not own; hosts have destroyed dozens of live sessions because the flag
    was absent from the argv actually run. ``-S`` moves the guarantee out of
    reasoning and onto the command line.

    Raises:
        SocketNotInstalledError: no resolution has been installed. Never
            returns a default.
    """
    if _INSTALLED is None:
        raise SocketNotInstalledError(
            "no tmux socket decision has been installed, so there is no socket "
            "to pin this call to with -S. Call resolve_and_install() first. "
            "Refusing to fall back to an unscoped `tmux` invocation: without "
            "-S, tmux resolves the server from $TMUX (any parent pane) before "
            "TMUX_TMPDIR, which is how hosts have lost live sessions."
        )
    return _INSTALLED.server_socket_path


async def run_tmux_scoped(*args: str) -> str:
    """``run_tmux`` with an explicit ``-S <socket>`` pinned to the front.

    THE ONLY sanctioned way for this library to invoke tmux. Every verb -- read
    or write -- goes through here, so the question "which server did that
    command actually reach?" has one answer, visible in the argv, for the whole
    codebase.
    """
    return await tk_proc.run_tmux("-S", installed_socket_path(), *args)


def resolve_and_install(
    explicit: str | None = None, *, socket_name: str | None = None
) -> SocketResolution:
    """Resolve, inject, and hand back the decision for reporting."""
    resolution = resolve(explicit, socket_name=socket_name)
    install(resolution)
    return resolution


def describe(
    resolution: SocketResolution,
    *,
    tmux_reported_socket_path: str | None = None,
) -> dict[str, Any]:
    """The socket block that rides along on every response.

    *tmux_reported_socket_path* is tmux's own ``#{socket_path}``, available
    only when a server is actually running. When present it turns the scope
    from a claim we make into a claim the server confirms.
    """
    confirmed: bool | None
    if tmux_reported_socket_path is None:
        confirmed = None
    else:
        confirmed = _normalize(tmux_reported_socket_path) == _normalize(
            resolution.server_socket_path
        )

    block: dict[str, Any] = {
        "socket_dir": resolution.socket_dir,
        "socket_name": resolution.socket_name,
        "server_socket_path": resolution.server_socket_path,
        "resolved_from": resolution.source,
        "resolved_detail": resolution.source_detail,
        "resolution_order": RESOLUTION_ORDER,
        "config_path": resolution.config_path,
        "config_path_exists": resolution.config_path_exists,
        "tmux_reported_socket_path": tmux_reported_socket_path,
        "socket_path_confirmed_by_tmux": confirmed,
        "ambient_tmux_tmpdir": resolution.ambient_tmux_tmpdir,
        "ambient_tmux": resolution.ambient_tmux,
        "ambient_ignored": resolution.ambient_ignored,
        "how_to_change": (
            f"set {SOCKET_DIR_ENV_VAR}=<dir>, or write "
            f'{{"{CONFIG_KEY}": "<dir>"}} to {resolution.config_path}, or pass '
            "--socket-dir <dir>. <dir> is the TMUX_TMPDIR-style PARENT "
            f"directory; the server socket is <dir>/tmux-{os.getuid()}/"
            f"{resolution.socket_name}."
        ),
    }

    if resolution.ambient_ignored:
        parts = []
        if resolution.ambient_tmux_tmpdir is not None:
            parts.append(f"TMUX_TMPDIR={resolution.ambient_tmux_tmpdir!r}")
        if resolution.ambient_tmux is not None:
            parts.append(f"TMUX={resolution.ambient_tmux!r}")
        block["ambient_ignored_note"] = (
            "this process's environment carries "
            + " and ".join(parts)
            + ", and this tool deliberately did NOT use it: the socket was "
            f"resolved from {resolution.source} to {resolution.socket_dir}. "
            "Ambient auto-detection is how a tool works on one box and silently "
            "reads the wrong fleet on the next. If the ambient value is the "
            "fleet you meant, adopt it explicitly -- see how_to_change."
        )
        if resolution.ambient_tmux is not None:
            block["ambient_tmux_note"] = (
                "$TMUX is set, meaning this process is running inside an "
                "attached tmux client. tmux gives $TMUX priority OVER "
                "TMUX_TMPDIR, so it is removed from the environment handed to "
                "every tmux subprocess (tmux_kit.proc.tmux_env does this) -- "
                "otherwise the surrounding pane's server would silently outrank "
                "the resolved socket directory. Every tmux call is also pinned "
                "with an explicit -S."
            )

    return block


def empty_fleet_note(resolution: SocketResolution, detail: str) -> str:
    """The sentence that must appear instead of a bare empty result.

    Zero sessions is a legitimate answer, but only when it is attached to WHERE
    we looked. "No sessions" is unfalsifiable; "pointed at /tmp, saw nothing"
    can be checked, and argued with.
    """
    return (
        f"pointed at {resolution.socket_dir} "
        f"(server socket {resolution.server_socket_path}), saw nothing: "
        f"{detail}. This is a complete read of THAT socket, not a statement "
        "that the machine has no tmux sessions -- sessions on any other socket "
        "directory are out of scope and are not counted as absent. Socket "
        f"resolved from {resolution.source}."
    )
