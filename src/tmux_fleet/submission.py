"""Making submission REAL: newlines become Enter KEY EVENTS, never bytes.

WHY THIS MODULE EXISTS
----------------------
A relay was armed, confirmed, and reported "Sent ... followed by Enter". The
command was never submitted. The same verb DID execute an echo in a bare shell,
so the verb looked fine and the bug looked imaginary.

It is not imaginary, and it is not a race. It is a byte:

    `send-keys -l -- "cmd\\n"`   delivers LF   (0x0A)
    `send-keys -t sess Enter`    delivers CR   (0x0D)

A shell submits on either, because the kernel line discipline is on and
ICRNL/line-buffering make LF the line terminator. A raw-mode TUI (anything on
Ratatui/crossterm) submits on CR ONLY. crossterm says so in its own parser:

    b'\\r' => KeyCode::Enter
    // \\n = 0xA, which is also the keycode for Ctrl+J. In raw mode the
    // terminal no longer converts \\r into \\n for us, so \\n has no meaning.

In raw mode LF is Ctrl+J and is swallowed. No error, no diff. The text just
sits in the input box looking sent -- and does not politely vanish: it PREFIXES
the next thing typed, fusing two commands into one nonsense command.

THE FIX
-------
Every newline in the text becomes a distinct ``send-keys -t <sess> Enter`` -- a
real key event, the same one a human keypress produces -- instead of a literal
byte inside the paste payload. CR is strictly more faithful than LF for BOTH
target classes: it is what pressing the key actually emits, and a shell's line
discipline converts it to LF on the way in anyway.

This module is pure argv construction: no I/O, so the property that matters is
auditable at a glance and testable without tmux.
"""

from __future__ import annotations

import re

from tmux_kit import keys as tk_keys

#: CRLF, bare CR and bare LF are all ONE submission each. A CRLF that became two
#: Enters would submit an extra empty line into the target, so the two-character
#: sequence is matched before either single character.
_NEWLINE = re.compile(r"\r\n|\r|\n")

#: Ceiling on Enter key events in a single send. Each named key is one tmux
#: subprocess, so an unbounded count is a fork amplifier -- the same number as
#: tmux-kit's own MAX_KEYS, taken from upstream so the two caps cannot drift.
MAX_SUBMIT_KEYS = tk_keys.MAX_KEYS


def split_for_submission(text: str) -> tuple[list[str], int]:
    """Split *text* into literal segments and count the Enters between them.

    Returns ``(segments, enter_count)`` where ``len(segments) == enter_count +
    1`` always. Text with no newline yields one segment and zero Enters -- which
    is a NON-submitting send, and the caller must say so out loud rather than
    implying the line went in.
    """
    segments = _NEWLINE.split(text)
    return segments, len(segments) - 1


def build_send_argvs(session: str, text: str) -> tuple[list[list[str]], int]:
    """Build the ordered tmux argvs that type *text* and really submit it.

    Literal runs go through ``send-keys -l -- <segment>`` (argv, never a shell
    string; ``--`` keeps a leading ``-`` as data). Each newline becomes its own
    ``send-keys -t <session> Enter``. An empty segment contributes no argv.
    """
    segments, enters = split_for_submission(text)
    argvs: list[list[str]] = []
    last = len(segments) - 1
    for i, segment in enumerate(segments):
        if segment:
            argvs.append(tk_keys.build_send_text_argv(session, segment))
        if i < last:
            argvs.append(tk_keys.build_send_key_argv(session, "Enter"))
    return argvs, enters


# --------------------------------------------------------------------------
# ARMED vs EXECUTED -- the readback
# --------------------------------------------------------------------------
#
# Making the argv honest (a newline becomes a real Enter key event, and
# `submitted` stopped being conflated with `delivered`) did not make the ANSWER
# honest, because a caller that never asks for an Enter still gets
# `delivered: true`. So every send now READS THE PANE BACK and reports which of
# three things happened, by name:
#
#   armed      -- decided from the ARGV (zero Enter key events). Deterministic,
#                 no pane parsing, and it is the state that bit the owner.
#   submitted  -- an Enter went out AND the readback shows the input line no
#                 longer holding the text.
#   uncertain  -- an Enter went out and the readback could NOT confirm the
#                 input line was consumed.
#
# The pane heuristic can only ever move a result between "confirmed" and "could
# not confirm". It can never manufacture a `submitted` out of an `armed`,
# because `armed` never consults it. Under-claiming is the safe direction.

#: How long to keep re-reading the pane waiting for the input line to clear.
SUBMIT_CONFIRM_TIMEOUT_S = 2.0

#: Gap between readback polls.
SUBMIT_CONFIRM_POLL_S = 0.05

#: How many trailing characters of the typed text to look for on the input line.
INPUT_LINE_PROBE_CHARS = 40

#: Shortest overlap that counts as a match. A pane row of one or two characters
#: is a suffix of almost any command by coincidence.
MIN_PROBE_CHARS = 6

OUTCOME_ARMED = "armed"
OUTCOME_SUBMITTED = "submitted"
OUTCOME_UNCERTAIN = "uncertain"


def input_line_probe(text: str) -> str:
    """The characters that would be left sitting on the target's input line.

    The final segment of *text* (everything after its last newline), trimmed to
    a bounded suffix. Empty when the text ends in a newline.
    """
    tail = _NEWLINE.split(text.rstrip("\r\n"))[-1].rstrip()
    return tail[-INPUT_LINE_PROBE_CHARS:]


def input_line_holds_text(last_line: str | None, text: str) -> bool | None:
    """Is the typed text still sitting, unsubmitted, on the input line?

    Matched in BOTH directions, because which string is longer depends on the
    target's width: the row is longer (prompt + command) -> the row ENDS WITH
    the probe; the command WRAPPED and the row holds only its tail -> the probe
    ENDS WITH the row. A one-directional check manufactures a false confirmation
    entirely by line wrapping.

    ``None`` means UNJUDGEABLE -- no pane came back, the text left no probe, or
    the overlap is too short to mean anything -- and is never silently folded
    into ``False``.
    """
    probe = input_line_probe(text)
    if not probe or last_line is None:
        return None
    row = last_line.rstrip()
    if not row:
        return None
    if row.endswith(probe):
        return True
    if probe.endswith(row):
        return True if len(row) >= MIN_PROBE_CHARS else None
    return False


def classify_submission(
    *, enter_count: int, appeared: bool | None, cleared: bool | None
) -> tuple[str, bool]:
    """Return ``(outcome, submission_confirmed)`` -- armed vs executed.

    * ``armed`` is decided by the ARGV alone (no Enter key event was sent).
    * ``submitted`` needs BOTH halves of a positive readback: the text was SEEN
      on the input line, and then seen GONE.
    * ``uncertain`` is everything else, including every case the pane could not
      answer.
    """
    if enter_count == 0:
        return OUTCOME_ARMED, False
    if appeared is True and cleared is True:
        return OUTCOME_SUBMITTED, True
    return OUTCOME_UNCERTAIN, False


def outcome_note(outcome: str, *, submit: bool) -> str:
    """The lead sentence of the response: executed, or armed, by name."""
    if outcome == OUTCOME_SUBMITTED:
        return (
            "OUTCOME: SUBMITTED. An Enter key event was delivered and the "
            "readback confirms the typed text is no longer sitting on the "
            "target's input line, so the target consumed it. This says the line "
            "was SUBMITTED -- not that whatever it started has finished or "
            "succeeded."
        )
    if outcome == OUTCOME_ARMED:
        return (
            "OUTCOME: ARMED, NOT SUBMITTED. Nothing was executed by this call. "
            "The text was typed and is sitting unsubmitted on the target's "
            "input line, where it will PREFIX whatever is typed next. Do NOT "
            "report this as sent. Re-send with --submit (one call: types the "
            "text and submits it), or send `--key Enter`."
        )
    return (
        "OUTCOME: UNCERTAIN. An Enter key event was delivered, but the readback "
        "could NOT confirm the target consumed it"
        + (
            f" -- the typed text still appears on the input line after "
            f"{SUBMIT_CONFIRM_TIMEOUT_S}s."
            if submit
            else "."
        )
        + " Read the session before reporting this as sent."
    )


def submission_note(*, enter_count: int, key: str | None = None) -> str:
    """The honest sentence about whether anything was SUBMITTED."""
    if enter_count > 0:
        plural = "" if enter_count == 1 else f" x{enter_count}"
        return (
            f" SUBMISSION ATTEMPTED, NOT CONFIRMED: Enter{plural} was delivered "
            "as a distinct key event (CR, 0x0D) -- what a raw-mode TUI accepts; "
            "a trailing '\\n' in the text is LF (0x0A), which such a target "
            "reads as Ctrl+J and silently ignores. Nothing here verifies the "
            "application acted on the Enter. Read the session back before "
            "reporting the command as run."
        )
    if key is not None:
        return (
            f" NO SUBMISSION was requested: {key!r} is not Enter. Whatever sits "
            "in the target's input buffer is still unsubmitted."
        )
    return (
        " NOTHING WAS SUBMITTED by this call: the text was typed but no Enter "
        "key event was sent, so it is sitting unsubmitted in the target's input "
        "buffer. Send `--key Enter` (or include a newline in --text, which is "
        "now delivered as a real Enter key) to submit it. Unsubmitted text does "
        "not vanish -- it PREFIXES whatever is typed next, fusing two commands "
        "into one."
    )
