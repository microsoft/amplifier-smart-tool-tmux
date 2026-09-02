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
* **-h is a terse human summary; --help is the complete agent-facing listing**,
  including WHICH verbs are model-backed. They are not aliases. Both levels
  exist at the top level and on every verb, and all four renderings come from
  the same library-level verb registry.

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

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


# --------------------------------------------------------------------------
# The verb registry -- the single source of truth every help rendering comes
# from (top-level -h and --help, per-verb -h and --help), so a terse summary
# and a complete listing can never disagree.
#
# A verb carries: its name, a one-line summary, whether it is model-backed,
# what it returns, whether it accepts the shared socket options, and its own
# parameters. A parameter carries:
#   label   the argument as written, e.g. "--lines N"
#   usage   its contribution to the usage line, or None when another parameter
#           already spells it (a mutually exclusive pair contributes one token)
#   type    the value type an agent must supply
#   default what happens when it is omitted
#   short   a few words, for the terse rendering
#   detail  the full sentence, for the complete rendering
# --------------------------------------------------------------------------

_SOCKET_PARAMS: list[dict[str, Any]] = [
    {
        "label": "--socket-dir DIR",
        "usage": "[--socket-dir DIR]",
        "type": "path, absolute",
        "default": f"the config file, then ${socket_resolution.SOCKET_DIR_ENV_VAR}, then {socket_resolution.SYSTEM_DEFAULT_SOCKET_DIR}",
        "short": "ADVANCED: the socket directory to read",
        "detail": (
            "TMUX_TMPDIR-style parent directory holding the tmux server socket "
            "(the socket itself is DIR/tmux-$UID/NAME). The highest-priority "
            "source. The ambient TMUX_TMPDIR/$TMUX are deliberately NOT "
            "auto-detected; they are reported as seen-and-ignored."
        ),
    },
    {
        "label": "--socket-name NAME",
        "usage": "[--socket-name NAME]",
        "type": "str",
        "default": repr(socket_resolution.DEFAULT_SOCKET_NAME),
        "short": "ADVANCED: the socket name in that directory",
        "detail": (
            "The tmux socket NAME within the directory. Pins an exact server, "
            "e.g. one created by tmux_kit.isolated_tmux_server()."
        ),
    },
]

_SESSION_PARAM: dict[str, Any] = {
    "label": "SESSION",
    "usage": "SESSION",
    "type": "str, positional, required",
    "default": None,
    "short": "the session to act on",
    "detail": "The tmux session name, as `tmux-fleet sessions` reports it. An unknown name refuses with exit 2.",
}

_QUIET_SECONDS_PARAM: dict[str, Any] = {
    "label": "--quiet-seconds N",
    "usage": "[--quiet-seconds N]",
    "type": "int, seconds",
    "default": str(fleet.DEFAULT_QUIET_SECONDS),
    "short": "how long counts as quiet",
    "detail": "How long a session must have been idle before it counts as quiet rather than a candidate for attention.",
}

_TIMEOUT_MS_PARAM: dict[str, Any] = {
    "label": "--timeout-ms MS",
    "usage": "[--timeout-ms MS]",
    "type": "int, milliseconds",
    "default": str(agent.DEFAULT_TIMEOUT_MS),
    "short": "budget for the model turn",
    "detail": "Wall-clock budget for the single model turn. Exceeding it fails loudly rather than returning a partial judgment.",
}

_READ_LINES_PARAM: dict[str, Any] = {
    "label": "--lines N",
    "usage": "[--lines N]",
    "type": "int",
    "default": str(fleet.DEFAULT_READ_LINES),
    "short": "how many lines of scrollback",
    "detail": "How many lines back to capture. The _completeness block reports whether the whole retained scrollback was reached.",
}

VERBS: list[dict[str, Any]] = [
    {
        "name": "socket",
        "summary": "which tmux socket this tool reads, and on whose authority",
        "model_backed": False,
        "socket_opts": True,
        "params": [],
        "returns": "the resolved socket, its source, whether a server answers, and any ambient TMUX_TMPDIR/$TMUX that was ignored",
    },
    {
        "name": "sessions",
        "summary": "every session on the resolved socket, each with a 30-line sliver",
        "model_backed": False,
        "socket_opts": True,
        "params": [
            {
                "label": "--snapshot-lines N",
                "usage": "[--snapshot-lines N]",
                "type": "int",
                "default": str(fleet.LIST_SNAPSHOT_LINES),
                "short": "sliver depth per session",
                "detail": "How many trailing lines of each session's pane to include as its sliver. Larger values cost one capture per session.",
            }
        ],
        "returns": "a list of sessions (last_line, tri-state at_prompt, harness, idle, recency, cwd) + counts + a _completeness block",
    },
    {
        "name": "attention",
        "summary": "deterministic triage ORDER: which sessions plausibly want a human",
        "model_backed": False,
        "socket_opts": True,
        "params": [_QUIET_SECONDS_PARAM],
        "returns": "candidates ordered by a heuristic bucketing, with fleet-wide counts. A prior for where to look, not a verdict",
    },
    {
        "name": "read",
        "summary": "read one session's pane/scrollback, with an honest completeness bound",
        "model_backed": False,
        "socket_opts": True,
        "params": [
            _SESSION_PARAM,
            _READ_LINES_PARAM,
            {
                "label": "--keep-ansi",
                "usage": "[--keep-ansi]",
                "type": "flag",
                "default": "false (escape sequences are stripped)",
                "short": "keep ANSI escape sequences",
                "detail": "Return the pane text with its ANSI escape sequences intact, for a caller that renders colour rather than reads text.",
            },
        ],
        "returns": "the captured pane text + a _completeness block that is complete=true only when the whole retained scrollback was reached",
    },
    {
        "name": "doctor",
        "summary": "preflight: tmux present, socket resolvable/writable, server reachable",
        "model_backed": False,
        "socket_opts": True,
        "params": [],
        "returns": "an ok boolean + per-check results with a remedy for each failure. Reporting a problem is its success (exit 0)",
    },
    {
        "name": "exit-code",
        "summary": "tmux-native exit status of a finished session's active pane",
        "model_backed": False,
        "socket_opts": True,
        "params": [_SESSION_PARAM],
        "returns": "status (running/finished) and exit_code (null unless the pane is dead and tmux retained its status)",
    },
    {
        "name": "send",
        "summary": "type into a session -- REFUSES without --confirmed",
        "model_backed": False,
        "socket_opts": True,
        "params": [
            _SESSION_PARAM,
            {
                "label": "--text TEXT",
                "usage": "(--text TEXT | --key KEY)",
                "type": "str",
                "default": None,
                "short": "literal text to type (one of --text/--key)",
                "detail": "Literal text typed into the pane. Exactly one of --text or --key is required. Nothing is submitted unless --submit is also given.",
            },
            {
                "label": "--key KEY",
                "usage": None,
                "type": "str, a tmux key name",
                "default": None,
                "short": "a tmux key name (one of --text/--key)",
                "detail": "A tmux key name (e.g. C-c, Enter, Escape) sent as a keystroke rather than as text. Exactly one of --text or --key is required.",
            },
            {
                "label": "--submit",
                "usage": "[--submit]",
                "type": "flag",
                "default": "false (the text is left armed at the prompt)",
                "short": "press Enter after the text",
                "detail": "Submit the typed text. Without it the text is left armed at the prompt and the outcome reports 'armed'.",
            },
            {
                "label": "--confirmed",
                "usage": "--confirmed",
                "type": "flag, required",
                "default": "absent, which REFUSES the write with exit 2",
                "short": "REQUIRED: acknowledge the write",
                "detail": "The per-invocation fence. Without it the write is refused loudly before any tmux contact. There is no session-wide or environment unlock.",
            },
        ],
        "returns": "outcome (submitted/armed/uncertain) backed by a pane readback; every attempt (refused or delivered) is audited",
    },
    {
        "name": "create",
        "summary": "create a NEW detached session -- REFUSES without --confirmed",
        "model_backed": False,
        "socket_opts": True,
        "params": [
            {
                "label": "NAME",
                "usage": "NAME",
                "type": "str, positional, required",
                "default": None,
                "short": "name for the new session",
                "detail": "The name for the new detached session. A collision with an existing session refuses rather than attaching or renaming.",
            },
            {
                "label": "--cwd DIR",
                "usage": "[--cwd DIR]",
                "type": "path",
                "default": "the tmux server's own working directory",
                "short": "working directory for the session",
                "detail": "Working directory the new session starts in.",
            },
            {
                "label": "--command CMD",
                "usage": "[--command CMD]",
                "type": "str",
                "default": "the login shell",
                "short": "command to run instead of a shell",
                "detail": "Command the new session runs instead of a login shell. The session ends when the command does.",
            },
            {
                "label": "--confirmed",
                "usage": "--confirmed",
                "type": "flag, required",
                "default": "absent, which REFUSES the create with exit 2",
                "short": "REQUIRED: acknowledge the create",
                "detail": "The per-invocation fence. Without it the create is refused loudly before any tmux contact. There is no session-wide or environment unlock.",
            },
        ],
        "returns": "the created session verified by re-enumeration; a name collision refuses with an informative description",
    },
    {
        "name": "triage",
        "summary": "fleet-wide: what needs attention and why, structured",
        "model_backed": True,
        "socket_opts": True,
        "params": [_QUIET_SECONDS_PARAM, _TIMEOUT_MS_PARAM],
        "returns": "a model's structured judgment (needs_attention/quiet/summary) over mechanically-collected slivers. Fails loudly if amplifier-agent is absent",
    },
    {
        "name": "interpret",
        "summary": "what this session's state/output means, structured",
        "model_backed": True,
        "socket_opts": True,
        "params": [_SESSION_PARAM, _READ_LINES_PARAM, _TIMEOUT_MS_PARAM],
        "returns": "a model's structured interpretation of the mechanically-captured scrollback. Fails loudly if amplifier-agent is absent",
    },
    {
        "name": "manifest",
        "summary": "print this tool's SMART_TOOL.md manifest as JSON (from the library accessor)",
        "model_backed": False,
        "socket_opts": False,
        "params": [],
        "returns": "the manifest frontmatter (smart_tool_format, name, version, description, use_cases, platforms, requires)",
    },
]

_MODEL_BACKED = sorted(v["name"] for v in VERBS if v["model_backed"])
VERB_BY_NAME: dict[str, dict[str, Any]] = {v["name"]: v for v in VERBS}

#: What the model-backed verbs need, and how they fail without it. Rendered
#: into the top-level --help and into each model-backed verb's own --help.
_MODEL_BACKED_NOTE = [
    "  These verbs execute through the amplifier-agent engine library, imported",
    "  IN-PROCESS (no subprocess, no PATH-resolved binary). The engine ships as a",
    "  dependency; a provider SDK arrives via an install extra (e.g.",
    "  `tmux-fleet[anthropic]`) and provider credentials arrive from your",
    "  environment -- this tool stores none. Invoked without a usable substrate",
    "  they FAIL naming exactly which precondition is missing (engine dependency,",
    "  provider SDK extra, no provider configured, or no credentials in the",
    "  environment) and how to fix it -- never a silent fallback to a",
    "  deterministic approximation.",
]


def _wrap(text: str, indent: str) -> list[str]:
    """One prose sentence as indented lines that fit a narrow terminal."""
    return textwrap.wrap(
        text, width=78, initial_indent=indent, subsequent_indent=indent
    )


def _params(verb: dict[str, Any]) -> list[dict[str, Any]]:
    """Every parameter a verb accepts, its own then the shared socket options."""
    return list(verb["params"]) + (_SOCKET_PARAMS if verb["socket_opts"] else [])


def _usage(verb: dict[str, Any]) -> str:
    """The usage line's argument portion, derived from the parameters."""
    return " ".join(p["usage"] for p in _params(verb) if p["usage"])


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
        lines += ["", "MODEL-BACKED SUBSTRATE"] + _MODEL_BACKED_NOTE
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
        f"Model-backed verbs (need amplifier-agent): {', '.join(_MODEL_BACKED)}",
        "Every result on stdout is JSON. Failures are a JSON error envelope on",
        "stdout with a non-zero exit (2 = refused, 1 = read/agent failure).",
        "Run `tmux-fleet --help` for the complete, agent-facing listing, or",
        "`tmux-fleet VERB -h` / `tmux-fleet VERB --help` for one verb.",
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
        "  Every RESULT on stdout is JSON: one document on success, or a JSON",
        '  error envelope {"error": {"code","message","remedy"}} on failure.',
        "  Self-description is the one thing on stdout that is not a result and",
        "  not JSON -- `-h` and `--help`, at the top level and on every verb,",
        "  write plain text and exit 0. Exit: 0 success, 2 refused",
        "  (deny-by-default write, unknown session, bad argument), 1 read/agent",
        "  failure.",
        "",
        "SELF-DESCRIPTION",
        "  Two levels, for two readers, at both levels of the command.",
        "  `-h`     the terse summary: what to type to remember a flag name.",
        "  `--help` the complete listing: every argument and its type, what the",
        "           verb returns, and which capabilities are model-backed.",
        "  `tmux-fleet VERB -h` and `tmux-fleet VERB --help` narrow both to one",
        "  verb, and neither requires that verb's arguments.",
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
        usage = _usage(v)
        if usage:
            lines.append(f"      args:    {usage}")
        # The verb's own parameters. The shared socket options appear in the
        # usage line above and are documented once under SOCKET RESOLUTION,
        # rather than eleven times here.
        for p in v["params"]:
            default = f", default {p['default']}" if p["default"] is not None else ""
            lines.append(f"        {p['label']}  ({p['type']}{default})")
        lines.append(f"      returns: {v['returns']}")
        lines.append("")
    lines += [
        f"MODEL-BACKED VERBS: {', '.join(_MODEL_BACKED)}",
        *_MODEL_BACKED_NOTE,
        "  Every other verb runs with no AI substrate configured at all.",
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
        _emit_error(
            "usage",
            message,
            f"Run `{self.prog} --help` for the accepted arguments.",
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
    parser.add_argument("--help", action=_FullHelpAction)

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
