"""`tmux-fleet` -- the thin CLI adapter over the library.

Contract (cli.v1):

* **One binary, non-interactive.** A run with stdin closed never hangs.
* **stdout is JSON, and only JSON.** Success emits one JSON document; failure
  emits a JSON error envelope ``{"error": {"code", "message", "remedy"}}``.
  Both go to stdout. Progress/diagnostics (there are none in normal operation)
  would go to stderr, never stdout.
* **Exit codes:** ``0`` success · ``2`` refusal (deny-by-default write, unknown
  session, bad argument, usage) · ``1`` read/agent failure.
* **-h is a terse human summary; --help is the complete agent-facing listing**,
  including WHICH verbs are model-backed. They are not aliases, and both are
  rendered from the same library-level verb registry.

Every capability lives in the library; this module only parses arguments, calls
the library, and formats the result. No domain logic lives here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, NoReturn

from tmux_fleet import (
    agent,
    audit,
    creation,
    diagnostics,
    fleet,
    smart,
    socket_resolution,
)
# Imported by symbol (not `import tmux_fleet.manifest as ...`) because the
# package `__init__` binds the ATTRIBUTE `tmux_fleet.manifest` to the accessor
# FUNCTION, which IMPORT_FROM's getattr would return instead of the submodule.
from tmux_fleet.manifest import ManifestError as _ManifestError
from tmux_fleet.manifest import manifest_dict as _manifest_dict

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


# --------------------------------------------------------------------------
# The verb registry -- the single source of truth both -h and --help render
# from, so a terse summary and the complete listing can never disagree.
# --------------------------------------------------------------------------

#: (name, one-line summary, model_backed, argument help, returns help)
VERBS: list[dict[str, Any]] = [
    {
        "name": "socket",
        "summary": "which tmux socket this tool reads, and on whose authority",
        "model_backed": False,
        "args": "[--socket-dir DIR] [--socket-name NAME]",
        "returns": "the resolved socket, its source, whether a server answers, and any ambient TMUX_TMPDIR/$TMUX that was ignored",
    },
    {
        "name": "sessions",
        "summary": "every session on the resolved socket, each with a 30-line sliver",
        "model_backed": False,
        "args": "[--snapshot-lines N] [--socket-dir DIR] [--socket-name NAME]",
        "returns": "a list of sessions (last_line, tri-state at_prompt, harness, idle, cwd) + counts + a _completeness block",
    },
    {
        "name": "attention",
        "summary": "deterministic triage ORDER: which sessions plausibly want a human",
        "model_backed": False,
        "args": "[--quiet-seconds N] [--socket-dir DIR] [--socket-name NAME]",
        "returns": "candidates ordered by a heuristic bucketing, with fleet-wide counts. A prior for where to look, not a verdict",
    },
    {
        "name": "read",
        "summary": "read one session's pane/scrollback, with an honest completeness bound",
        "model_backed": False,
        "args": "SESSION [--lines N] [--keep-ansi] [--socket-dir DIR] [--socket-name NAME]",
        "returns": "the captured pane text + a _completeness block that is complete=true only when the whole retained scrollback was reached",
    },
    {
        "name": "doctor",
        "summary": "preflight: tmux present, socket resolvable/writable, server reachable",
        "model_backed": False,
        "args": "[--socket-dir DIR] [--socket-name NAME]",
        "returns": "an ok boolean + per-check results with a remedy for each failure. Reporting a problem is its success (exit 0)",
    },
    {
        "name": "exit-code",
        "summary": "tmux-native exit status of a finished session's active pane",
        "model_backed": False,
        "args": "SESSION [--socket-dir DIR] [--socket-name NAME]",
        "returns": "status (running/finished) and exit_code (null unless the pane is dead and tmux retained its status)",
    },
    {
        "name": "send",
        "summary": "type into a session -- REFUSES without --confirmed",
        "model_backed": False,
        "args": "SESSION (--text TEXT | --key KEY) [--submit] --confirmed [--socket-dir DIR] [--socket-name NAME]",
        "returns": "outcome (submitted/armed/uncertain) backed by a pane readback; every attempt (refused or delivered) is audited",
    },
    {
        "name": "create",
        "summary": "create a NEW detached session -- REFUSES without --confirmed",
        "model_backed": False,
        "args": "NAME [--cwd DIR] [--command CMD] --confirmed [--socket-dir DIR] [--socket-name NAME]",
        "returns": "the created session verified by re-enumeration; a name collision refuses with an informative description",
    },
    {
        "name": "triage",
        "summary": "fleet-wide: what needs attention and why, structured",
        "model_backed": True,
        "args": "[--quiet-seconds N] [--socket-dir DIR] [--socket-name NAME] [--timeout-ms MS]",
        "returns": "a model's structured judgment (needs_attention/quiet/summary) over mechanically-collected slivers. Fails loudly if amplifier-agent is absent",
    },
    {
        "name": "interpret",
        "summary": "what this session's state/output means, structured",
        "model_backed": True,
        "args": "SESSION [--lines N] [--socket-dir DIR] [--socket-name NAME] [--timeout-ms MS]",
        "returns": "a model's structured interpretation of the mechanically-captured scrollback. Fails loudly if amplifier-agent is absent",
    },
    {
        "name": "manifest",
        "summary": "print this tool's SMART_TOOL.md manifest as JSON (from the library accessor)",
        "model_backed": False,
        "args": "",
        "returns": "the manifest frontmatter (smart_tool_format, name, version, description, use_cases, platforms, requires)",
    },
]

_MODEL_BACKED = sorted(v["name"] for v in VERBS if v["model_backed"])


def _terse_help() -> str:
    lines = [
        "tmux-fleet -- observe the tmux fleet on this machine and, only when",
        "explicitly confirmed, type into it or start a session.",
        "",
        "Verbs:",
    ]
    for v in VERBS:
        tag = "  [model-backed]" if v["model_backed"] else ""
        lines.append(f"  {v['name']:<11} {v['summary']}{tag}")
    lines += [
        "",
        f"Model-backed verbs (need amplifier-agent): {', '.join(_MODEL_BACKED)}",
        "stdout is always JSON. Failures are a JSON error envelope on stdout with",
        "a non-zero exit (2 = refused, 1 = read/agent failure).",
        "Run `tmux-fleet --help` for the complete, agent-facing listing.",
    ]
    return "\n".join(lines) + "\n"


def _full_help() -> str:
    lines = [
        "tmux-fleet -- a smart tool for tmux fleets (library-first; this CLI is a",
        "thin adapter). Observe every tmux session on the resolved socket and,",
        "only under an explicit per-invocation --confirmed, type into one or start",
        "one. There is deliberately no verb that kills or renames a session.",
        "",
        "OUTPUT CONTRACT",
        "  stdout is one JSON document on success, or a JSON error envelope",
        '  {"error": {"code","message","remedy"}} on failure. Nothing but JSON is',
        "  written to stdout. Exit: 0 success, 2 refused (deny-by-default write,",
        "  unknown session, bad argument), 1 read/agent failure.",
        "",
        "SOCKET RESOLUTION (every verb)",
        "  --socket-dir DIR   absolute TMUX_TMPDIR-style parent directory; the",
        "                     server socket is DIR/tmux-$UID/<name>. Highest",
        f"                     priority, then the config file, then "
        f"${socket_resolution.SOCKET_DIR_ENV_VAR}, then the system default",
        f"                     ({socket_resolution.SYSTEM_DEFAULT_SOCKET_DIR}).",
        "  --socket-name NAME advanced: the socket name within that directory",
        f"                     (default {socket_resolution.DEFAULT_SOCKET_NAME!r}); "
        "pins an exact server, e.g. an isolated test server.",
        "  The ambient TMUX_TMPDIR/$TMUX are deliberately NOT auto-detected; they",
        "  are reported as seen-and-ignored. Every tmux call names its socket (-S).",
        "",
        "VERBS",
    ]
    for v in VERBS:
        tag = "   [MODEL-BACKED]" if v["model_backed"] else ""
        lines.append(f"  {v['name']}{tag}")
        lines.append(f"      {v['summary']}")
        if v["args"]:
            lines.append(f"      args:    {v['args']}")
        lines.append(f"      returns: {v['returns']}")
        lines.append("")
    lines += [
        "MODEL-BACKED VERBS",
        f"  {', '.join(_MODEL_BACKED)} execute through the amplifier-agent engine",
        "  library, imported IN-PROCESS (no subprocess, no PATH-resolved binary).",
        "  The engine ships as a dependency; a provider SDK arrives via an install",
        "  extra (e.g. `tmux-fleet[anthropic]`) and provider credentials arrive",
        "  from your environment -- this tool stores none. Invoked without a usable",
        "  substrate they FAIL naming exactly which precondition is missing (engine",
        "  dependency, provider SDK extra, no provider configured, or no",
        "  credentials in the environment) and how to fix it -- never a silent",
        "  fallback to a deterministic approximation. Every other verb runs with no",
        "  AI substrate configured at all.",
    ]
    return "\n".join(lines) + "\n"


def _emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def _emit_error(code: str, message: str, remedy: str | None) -> None:
    _emit({"error": {"code": code, "message": message.rstrip(), "remedy": remedy}})


class _EnvelopeArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors speak the tool's own envelope.

    Default argparse ``error()`` prints usage to STDERR and exits 2, leaving
    nothing on STDOUT for an agent caller to parse. This overrides that one
    method so a bad/missing argument still exits non-zero, but prints the same
    ``{"error": {...}}`` shape every other refusal path prints. add_subparsers
    propagates this class to every subparser automatically.
    """

    def error(self, message: str) -> NoReturn:
        _emit_error(
            "usage",
            message,
            "Run `tmux-fleet --help` for the accepted subcommands and options.",
        )
        raise SystemExit(EXIT_REFUSED)


class _TerseHelpAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, **kwargs):
        super().__init__(option_strings, dest, nargs=0, help="terse human summary", **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001
        sys.stdout.write(_terse_help())
        parser.exit(EXIT_OK)


class _FullHelpAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, **kwargs):
        super().__init__(
            option_strings, dest, nargs=0, help="complete agent-facing listing", **kwargs
        )

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001
        sys.stdout.write(_full_help())
        parser.exit(EXIT_OK)


def _socket_dir_parent() -> _EnvelopeArgumentParser:
    """The shared ``--socket-dir`` / ``--socket-name`` flags, added to every verb.

    Per-verb rather than global-before-the-verb so the natural
    ``tmux-fleet sessions --socket-dir X`` works.
    """
    parent = _EnvelopeArgumentParser(add_help=False)
    parent.add_argument(
        "--socket-dir",
        default=None,
        metavar="DIR",
        help=(
            "ADVANCED. Absolute TMUX_TMPDIR-style directory holding the tmux "
            f"server socket (the socket itself is DIR/tmux-$UID/NAME). "
            "Highest-priority source; otherwise the config file, then "
            f"${socket_resolution.SOCKET_DIR_ENV_VAR}, then the system default "
            f"({socket_resolution.SYSTEM_DEFAULT_SOCKET_DIR}). The ambient "
            "TMUX_TMPDIR is deliberately NOT auto-detected."
        ),
    )
    parent.add_argument(
        "--socket-name",
        default=None,
        metavar="NAME",
        help=(
            "ADVANCED. The tmux socket NAME within the directory (default "
            f"{socket_resolution.DEFAULT_SOCKET_NAME!r}). Pins an exact server, "
            "e.g. one created by tmux_kit.isolated_tmux_server()."
        ),
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = _EnvelopeArgumentParser(
        prog="tmux-fleet",
        add_help=False,
        description="Observe the tmux fleet and, only when explicitly confirmed, type into it.",
    )
    parser.add_argument("-h", action=_TerseHelpAction)
    parser.add_argument("--help", action=_FullHelpAction)

    sub = parser.add_subparsers(dest="command", required=True)
    socket_opt = _socket_dir_parent()

    sub.add_parser("socket", parents=[socket_opt], add_help=False)

    p_sessions = sub.add_parser("sessions", parents=[socket_opt], add_help=False)
    p_sessions.add_argument(
        "--snapshot-lines", type=int, default=fleet.LIST_SNAPSHOT_LINES
    )

    p_attention = sub.add_parser("attention", parents=[socket_opt], add_help=False)
    p_attention.add_argument(
        "--quiet-seconds", type=int, default=fleet.DEFAULT_QUIET_SECONDS
    )

    p_read = sub.add_parser("read", parents=[socket_opt], add_help=False)
    p_read.add_argument("session")
    p_read.add_argument("--lines", type=int, default=fleet.DEFAULT_READ_LINES)
    p_read.add_argument("--keep-ansi", action="store_true")

    p_doctor = sub.add_parser("doctor", parents=[socket_opt], add_help=False)  # noqa: F841

    p_exit = sub.add_parser("exit-code", parents=[socket_opt], add_help=False)
    p_exit.add_argument("session")

    p_send = sub.add_parser("send", parents=[socket_opt], add_help=False)
    p_send.add_argument("session")
    group = p_send.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--key")
    p_send.add_argument("--submit", action="store_true")
    p_send.add_argument("--confirmed", action="store_true")

    p_create = sub.add_parser("create", parents=[socket_opt], add_help=False)
    p_create.add_argument("name")
    p_create.add_argument("--cwd", default=None, metavar="DIR")
    p_create.add_argument("--command", dest="command_", default=None, metavar="CMD")
    p_create.add_argument("--confirmed", action="store_true")

    p_triage = sub.add_parser("triage", parents=[socket_opt], add_help=False)
    p_triage.add_argument(
        "--quiet-seconds", type=int, default=fleet.DEFAULT_QUIET_SECONDS
    )
    p_triage.add_argument(
        "--timeout-ms", type=int, default=agent.DEFAULT_TIMEOUT_MS
    )

    p_interpret = sub.add_parser("interpret", parents=[socket_opt], add_help=False)
    p_interpret.add_argument("session")
    p_interpret.add_argument("--lines", type=int, default=fleet.DEFAULT_READ_LINES)
    p_interpret.add_argument(
        "--timeout-ms", type=int, default=agent.DEFAULT_TIMEOUT_MS
    )

    sub.add_parser("manifest", add_help=False)

    return parser


async def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.command
    if cmd == "socket":
        return await fleet.socket_status(
            socket_dir=args.socket_dir, socket_name=args.socket_name
        )
    if cmd == "sessions":
        return await fleet.list_sessions(
            snapshot_lines=args.snapshot_lines,
            socket_dir=args.socket_dir,
            socket_name=args.socket_name,
        )
    if cmd == "attention":
        return await fleet.attention(
            quiet_seconds=args.quiet_seconds,
            socket_dir=args.socket_dir,
            socket_name=args.socket_name,
        )
    if cmd == "read":
        return await fleet.read_session(
            args.session,
            lines=args.lines,
            keep_ansi=args.keep_ansi,
            socket_dir=args.socket_dir,
            socket_name=args.socket_name,
        )
    if cmd == "doctor":
        return await diagnostics.doctor(
            socket_dir=args.socket_dir, socket_name=args.socket_name
        )
    if cmd == "exit-code":
        return await diagnostics.exit_code(
            args.session, socket_dir=args.socket_dir, socket_name=args.socket_name
        )
    if cmd == "send":
        return await fleet.send_input(
            args.session,
            text=args.text,
            key=args.key,
            submit=args.submit,
            confirmed=args.confirmed,
            socket_dir=args.socket_dir,
            socket_name=args.socket_name,
        )
    if cmd == "create":
        return await creation.create_session(
            args.name,
            cwd=args.cwd,
            command=args.command_,
            confirmed=args.confirmed,
            socket_dir=args.socket_dir,
            socket_name=args.socket_name,
        )
    if cmd == "triage":
        return await smart.triage(
            quiet_seconds=args.quiet_seconds,
            socket_dir=args.socket_dir,
            socket_name=args.socket_name,
            timeout_ms=args.timeout_ms,
        )
    if cmd == "interpret":
        return await smart.interpret(
            args.session,
            lines=args.lines,
            socket_dir=args.socket_dir,
            socket_name=args.socket_name,
            timeout_ms=args.timeout_ms,
        )
    if cmd == "manifest":
        return _manifest_dict()
    raise fleet.FleetError(f"unknown command {cmd!r}")


def _classify(exc: Exception) -> tuple[str, int, str | None]:
    """Map an exception to ``(code, exit_code, remedy)`` for the error envelope."""
    message = str(exc)
    refused = message.startswith("REFUSED")
    if isinstance(exc, socket_resolution.SocketConfigError):
        return "socket_config", EXIT_REFUSED, "fix the socket configuration named above"
    if isinstance(exc, creation.CreateRefused):
        return "refused", EXIT_REFUSED, "re-run with --confirmed if you genuinely intend it"
    if isinstance(exc, creation.CreateFailed):
        return "create_failed", EXIT_ERROR, "inspect the fleet with `tmux-fleet sessions`"
    if isinstance(exc, agent.AgentUnavailable):
        return "agent_unavailable", EXIT_ERROR, "install and configure amplifier-agent (see message)"
    if isinstance(exc, agent.AgentError):
        return "agent_error", EXIT_ERROR, "inspect amplifier-agent (try `amplifier-agent doctor`)"
    if isinstance(exc, _ManifestError):
        return "manifest", EXIT_ERROR, "the tool's own SMART_TOOL.md is malformed or missing"
    if isinstance(exc, audit.AuditError):
        return "audit", EXIT_ERROR, "make the audit log path writable (see message)"
    if isinstance(exc, fleet.FleetError):
        if refused:
            return "refused", EXIT_REFUSED, None
        return "fleet", EXIT_ERROR, "run `tmux-fleet socket` to check which socket is being read"
    if isinstance(exc, socket_resolution.SocketNotInstalledError):  # pragma: no cover
        return "internal", EXIT_ERROR, None
    return "error", EXIT_ERROR, None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = asyncio.run(_dispatch(args))
    except (
        fleet.FleetError,
        socket_resolution.SocketConfigError,
        socket_resolution.SocketNotInstalledError,
        creation.CreateRefused,
        creation.CreateFailed,
        audit.AuditError,
        agent.AgentUnavailable,
        agent.AgentError,
        _ManifestError,
    ) as exc:
        code, exit_code, remedy = _classify(exc)
        _emit_error(code, str(exc), remedy)
        return exit_code
    _emit(payload)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
