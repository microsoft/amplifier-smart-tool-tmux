"""The write-attempt audit log: one JSONL line per attempt, refused or not.

TWO verbs change the world (``send`` and ``create``) and they must write to the
SAME log. A second write surface with its own private log would make the fence
unauditable in exactly the way one log exists to prevent.

Two properties, both load-bearing:

1. **Refusals are recorded, not just actions.** A deny-by-default fence that
   leaves no trace when it fires cannot be audited, and "the agent never tried"
   is indistinguishable from "the agent tried and was stopped". Both are
   written.
2. **An unwritable log fails the action.** If the record cannot be written, the
   action is refused rather than performed unrecorded -- the alternative is a
   world-changing act with no trace, which is the worst of both outcomes.

The log is also READ, by ``creation``'s rate guard: it is the only durable
record of what this tool has created, so it is what makes "have I been creating
sessions in a loop?" an answerable question.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from pathlib import Path
from typing import Any

#: Timestamp format written on every record. Parsed back by the rate guard, so
#: the two must stay in step -- hence one constant, not two format strings.
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

AUDIT_LOG_ENV_VAR = "TMUX_FLEET_AUDIT_LOG"
DEFAULT_AUDIT_LOG = "~/.local/state/tmux-fleet/input-audit.jsonl"


class AuditError(RuntimeError):
    """The audit record could not be written or read.

    Always fatal to the action it accompanies. Never downgraded to a warning:
    an unrecorded write is the thing this module exists to make impossible.
    """


def audit_log_path() -> Path:
    return Path(os.environ.get(AUDIT_LOG_ENV_VAR, DEFAULT_AUDIT_LOG)).expanduser()


def iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return time.strftime(TIME_FORMAT, time.gmtime(epoch))


def parse_iso(text: str) -> float:
    """Parse a timestamp this module wrote. Raises ``ValueError`` if not.

    ``calendar.timegm`` rather than ``time.mktime``: the records are UTC ("Z"),
    and ``mktime`` would interpret them as local time, silently shifting every
    record by the machine's UTC offset.
    """
    return calendar.timegm(time.strptime(text, TIME_FORMAT))


def append(record: dict[str, Any]) -> None:
    """Append one line per write ATTEMPT -- refused or delivered, both."""
    path = audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": iso(time.time()), **record}) + "\n")
    except OSError as exc:  # pragma: no cover - audit must never mask the action
        raise AuditError(
            f"could not write the input audit log at {path}: {exc}. Refusing to "
            "act on a fleet this tool does not own without recording it."
        ) from exc


def read_records() -> tuple[list[dict[str, Any]], int]:
    """Every parseable record, plus a count of lines that were not.

    Returns ``(records, unparsed_line_count)``. A MISSING log is an empty
    history -- legitimately "nothing has happened yet", not a failure. An
    UNREADABLE log is fatal, because a guard that cannot read its own history
    would otherwise silently become no guard at all.

    Individual malformed lines are skipped rather than fatal (a truncated final
    line from a killed process must not brick the tool), but they are COUNTED
    and the count is reported, so degraded history is visible.
    """
    path = audit_log_path()
    if not path.exists():
        return [], 0

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditError(
            f"REFUSED: could not read the audit log at {path}: {exc}. This log "
            "is what bounds how many sessions this tool may create; refusing to "
            "create one while that bound is unknowable."
        ) from exc

    records: list[dict[str, Any]] = []
    unparsed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            unparsed += 1
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            unparsed += 1
    return records, unparsed
