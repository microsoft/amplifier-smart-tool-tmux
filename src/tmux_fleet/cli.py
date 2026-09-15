"""`tmux-fleet` -- the thin CLI adapter over the library.

Contract (cli.v1):

* **One binary, non-interactive.** A run with stdin closed never hangs.
* **Every result on stdout is JSON.** Success emits one JSON document; failure
  emits a JSON error envelope ``{"error": {"code", "message", "remedy"}}``.
  Both go to stdout. Progress/diagnostics (there are none in normal operation)
  would go to stderr, never stdout. Self-description is the one thing on stdout
  that is not JSON: ``-h``/``--help`` write plain text and exit 0, because help
  is addressed to whoever is deciding how to call the tool, not a result.
* **Exit codes:** ``0`` success · ``2`` refusal (deny-by-default write, unknown
  session, bad argument, usage) · ``1`` read/agent failure.
* **-h is a terse human summary; --help is the tool's skill**, rendered by the
  library (``tmux_fleet.skill``) and printed here unchanged. Every verb has
  both levels too: ``VERB -h`` is terse, ``VERB --help`` is the complete
  listing for that verb, including whether it is model-backed. All of them
  come from the same library-level verb registry (``tmux_fleet.verbs``).

Every capability lives in the library; this module only parses arguments, calls
the library, and formats the result. No domain logic lives here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import textwrap
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
from tmux_fleet.skill import skill
from tmux_fleet.verbs import MODEL_BACKED_NOTE, VERB_BY_NAME, VERBS, params as _params, usage as _usage

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


def _wrap(text: str, indent: str) -> list[str]:
    """One prose sentence as indented lines that fit a narrow terminal."""
    return textwrap.wrap(
        text, width=78, initial_indent=indent, subsequent_indent=indent
    )


def _verb_terse_help(verb: dict[str, Any]) -> str:
    """``tmux-fleet VERB -h`` -- what someone types to remember a flag name."""
    tag = "  [model-backed]" if verb["model_backed"] else ""
    usage = _usage(verb)
    lines = [
        f"tmux-fleet {verb['name']} -- {verb['summary']}{tag}",
        "",
        f"Usage: tmux-fleet {verb['name']} {usage}".rstrip(),
    ]
    params = _params(verb)
    if params:
        lines.append("")
        width = max(len(p["label"]) for p in params)
        for p in params:
            lines.append(f"  {p['label']:<{width}}  {p['short']}")
    lines += [
        "",
        f"Run `tmux-fleet {verb['name']} --help` for types, defaults, and what it returns.",
    ]
    return "\n".join(lines) + "\n"


def _verb_full_help(verb: dict[str, Any]) -> str:
    """``tmux-fleet VERB --help`` -- the complete listing for an agent caller."""
    lines = [
        f"tmux-fleet {verb['name']} -- {verb['summary']}",
        "",
    ]
    if verb["model_backed"]:
        lines.append("MODEL-BACKED. This verb calls a model; it does not run on")
        lines.append("mechanically-collected data alone.")
    else:
        lines.append("DETERMINISTIC. Runs correctly with no AI substrate configured at all.")
    lines += [
        "",
        "USAGE",
        f"  tmux-fleet {verb['name']} {_usage(verb)}".rstrip(),
        "",
    ]
    params = _params(verb)
    if params:
        lines.append("ARGUMENTS")
        for p in params:
            lines.append(f"  {p['label']}  ({p['type']})")
            lines += _wrap(p["detail"], "      ")
            if p["default"] is not None:
                lines += _wrap(f"default: {p['default']}", "      ")
        lines.append("")
    else:
        lines += ["ARGUMENTS", "  none", ""]
    lines += [
        "RETURNS",
        *_wrap(verb["returns"], "  "),
        "",
        "OUTPUT",
        "  One JSON document on stdout on success, or a JSON error envelope",
        '  {"error": {"code","message","remedy"}} on failure. Exit: 0 success,',
        "  2 refused (deny-by-default write, unknown session, bad argument),",
        "  1 read/agent failure. This help text is the one non-JSON thing on",
        "  stdout, and it exits 0.",
    ]
    if verb["model_backed"]:
        lines += ["", "MODEL-BACKED SUBSTRATE"] + MODEL_BACKED_NOTE
    return "\n".join(lines) + "\n"


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
        "Every result on stdout is JSON. Failures are a JSON error envelope on",
        "stdout with a non-zero exit (2 = refused, 1 = read/agent failure).",
        "Run `tmux-fleet --help` for this tool's skill, or",
        "`tmux-fleet VERB -h` / `tmux-fleet VERB --help` for one verb.",
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
        # ``prog`` is "tmux-fleet" on the top-level parser and "tmux-fleet read"
        # on a subparser, so the remedy names the help that answers this error.
        if self.prog == "tmux-fleet":
            remedy = "Run `tmux-fleet -h` for the verbs, and `tmux-fleet VERB --help` for one verb's arguments."
        else:
            remedy = f"Run `{self.prog} --help` for the accepted arguments."
        _emit_error("usage", message, remedy)
        raise SystemExit(EXIT_REFUSED)


class _TerseHelpAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, **kwargs):
        super().__init__(option_strings, dest, nargs=0, help="terse human summary", **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001
        sys.stdout.write(_terse_help())
        parser.exit(EXIT_OK)


class _SkillAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, **kwargs):
        super().__init__(option_strings, dest, nargs=0, help="this tool's skill", **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001
        sys.stdout.write(skill())
        parser.exit(EXIT_OK)


class _VerbHelpAction(argparse.Action):
    """Renders one verb's help and exits 0, mid-parse.

    Firing during parsing is the point: it returns before argparse reaches the
    required-argument check, so ``tmux-fleet read --help`` answers instead of
    demanding the SESSION you are asking how to supply. Genuine usage errors
    still reach ``_EnvelopeArgumentParser.error`` and exit 2.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS, *, verb, render, **kwargs):  # noqa: ANN001
        super().__init__(option_strings, dest, nargs=0, help=argparse.SUPPRESS, **kwargs)
        self._verb = verb
        self._render = render

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001
        sys.stdout.write(self._render(self._verb))
        parser.exit(EXIT_OK)


def _add_verb_help(parser: argparse.ArgumentParser, name: str) -> None:
    """Give one subcommand both levels of self-description."""
    verb = VERB_BY_NAME[name]
    parser.add_argument("-h", action=_VerbHelpAction, verb=verb, render=_verb_terse_help)
    parser.add_argument("--help", action=_VerbHelpAction, verb=verb, render=_verb_full_help)


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
    parser.add_argument("--help", action=_SkillAction)

    sub = parser.add_subparsers(dest="command", required=True)
    socket_opt = _socket_dir_parent()

    def verb(name: str, *, socket_opts: bool = True) -> argparse.ArgumentParser:
        """One subcommand, carrying both levels of its own self-description."""
        p = sub.add_parser(
            name, parents=[socket_opt] if socket_opts else [], add_help=False
        )
        _add_verb_help(p, name)
        return p

    verb("socket")

    p_sessions = verb("sessions")
    p_sessions.add_argument(
        "--snapshot-lines", type=int, default=fleet.LIST_SNAPSHOT_LINES
    )

    p_attention = verb("attention")
    p_attention.add_argument(
        "--quiet-seconds", type=int, default=fleet.DEFAULT_QUIET_SECONDS
    )

    p_read = verb("read")
    p_read.add_argument("session")
    p_read.add_argument("--lines", type=int, default=fleet.DEFAULT_READ_LINES)
    p_read.add_argument("--keep-ansi", action="store_true")

    verb("doctor")

    p_exit = verb("exit-code")
    p_exit.add_argument("session")

    p_send = verb("send")
    p_send.add_argument("session")
    group = p_send.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--key")
    p_send.add_argument("--submit", action="store_true")
    p_send.add_argument("--confirmed", action="store_true")

    p_create = verb("create")
    p_create.add_argument("name")
    p_create.add_argument("--cwd", default=None, metavar="DIR")
    p_create.add_argument("--command", dest="command_", default=None, metavar="CMD")
    p_create.add_argument("--confirmed", action="store_true")

    p_triage = verb("triage")
    p_triage.add_argument(
        "--quiet-seconds", type=int, default=fleet.DEFAULT_QUIET_SECONDS
    )
    p_triage.add_argument(
        "--timeout-ms", type=int, default=agent.DEFAULT_TIMEOUT_MS
    )

    p_interpret = verb("interpret")
    p_interpret.add_argument("session")
    p_interpret.add_argument("--lines", type=int, default=fleet.DEFAULT_READ_LINES)
    p_interpret.add_argument(
        "--timeout-ms", type=int, default=agent.DEFAULT_TIMEOUT_MS
    )

    verb("manifest", socket_opts=False)

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
