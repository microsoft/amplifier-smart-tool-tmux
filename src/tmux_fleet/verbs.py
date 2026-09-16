"""The verb registry: what this tool can do, said once.

Every rendering of the surface comes from here -- the skill an agent gets from
``tmux-fleet --help``, the terse summary a person gets from ``-h``, and the two
levels of per-verb help -- so no two of them can disagree.

A verb carries: its name, a one-line summary, whether it is model-backed, what
it returns, whether it accepts the shared socket options, and its own
parameters. A parameter carries:
  label   the argument as written, e.g. "--lines N"
  usage   its contribution to the usage line, or None when another parameter
          already spells it (a mutually exclusive pair contributes one token)
  type    the value type an agent must supply
  default what happens when it is omitted
  short   a few words, for the terse rendering
  detail  the full sentence, for the complete rendering
"""

from __future__ import annotations

from typing import Any

from tmux_fleet import agent, fleet, socket_resolution

SOCKET_PARAMS: list[dict[str, Any]] = [
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
                "detail": "Literal text typed into the pane. Exactly one of --text or --key is required. CR/LF text refuses unless --paste explicitly selects native buffered paste. Nothing is submitted unless --submit is also given.",
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
                "label": "--paste",
                "usage": "[--paste]",
                "type": "flag",
                "default": "false (CR/LF text refuses)",
                "short": "use native buffered paste for --text",
                "detail": "Deliver the complete unchanged --text through tmux-kit's native buffered paste primitive. Choose it only for a bracketed-paste-supporting target: tmux wraps only when that application enabled bracketed paste, so this is not universal transaction safety. Cannot be combined with --key.",
            },
            {
                "label": "--submit",
                "usage": "[--submit]",
                "type": "flag",
                "default": "false (the text is left armed at the prompt)",
                "short": "press Enter after the text",
                "detail": "Submit the typed text with exactly one additional Enter key event. Without it no Enter is generated and the outcome reports 'armed'.",
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

MODEL_BACKED = sorted(v["name"] for v in VERBS if v["model_backed"])
VERB_BY_NAME: dict[str, dict[str, Any]] = {v["name"]: v for v in VERBS}

#: What the model-backed verbs need, and how they fail without it. Rendered
#: into each model-backed verb's own --help.
MODEL_BACKED_NOTE = [
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


def params(verb: dict[str, Any]) -> list[dict[str, Any]]:
    """Every parameter a verb accepts, its own then the shared socket options."""
    return list(verb["params"]) + (SOCKET_PARAMS if verb["socket_opts"] else [])


def usage(verb: dict[str, Any]) -> str:
    """The usage line's argument portion, derived from the parameters."""
    return " ".join(p["usage"] for p in params(verb) if p["usage"])
