"""The model-backed verbs: `triage` (fleet-wide) and `interpret <session>`.

Both hold the four properties VISION section 4 demands of a smart path:

* **Never lies about itself.** With no working amplifier-agent they fail saying
  exactly that (``AgentUnavailable``, checked up front) -- never a silent
  fallback to a deterministic approximation.
* **Context is assembled mechanically by code.** The fleet state / scrollback a
  smart verb reasons over is collected by the deterministic verbs in this
  library (``attention``, ``read``), never by the agent's own judgment about
  what to look at.
* **Results are structured.** The agent is instructed to return strict JSON; the
  parsed object is returned. Unparseable output is a failure, not a shrug.
* **Partial is failure.** A turn that returns nothing usable raises rather than
  returning half an answer.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from tmux_fleet import agent, fleet

# --------------------------------------------------------------------------
# Parsing the model's answer back into structured data
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _extract_json(text: str) -> Any:
    """Parse the first JSON object/array out of the agent's final text.

    Tolerant of a ```json fence or leading/trailing prose, because a model can
    wrap structured output even when asked not to. Raises ``AgentError`` when no
    JSON can be recovered -- an unparseable smart result is a failure, never a
    prose answer smuggled through.
    """
    stripped = text.strip()

    # Whole-string fence, e.g. ```json\n{...}\n```
    fenced = _FENCE_RE.sub("", stripped).strip()
    for candidate in (stripped, fenced):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            pass

    # Fall back to the first balanced {...} or [...] span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = fenced.find(opener)
        end = fenced.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(fenced[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise agent.AgentError(
        "amplifier-agent did not return parseable JSON. A smart verb's result "
        "must be structured; refusing to pass prose off as a structured answer. "
        f"Raw model output (truncated): {text[:500]!r}"
    )


def _require_agent() -> None:
    """Fail loud, up front, if the substrate is absent -- before any tmux work.

    A smart verb that cannot reach amplifier-agent must say so and stop, not
    quietly return a deterministic approximation. Checking here (rather than
    only inside ``run_agent_turn``) means the refusal does not depend on there
    being a live tmux server to assemble context from first.
    """
    if not agent.agent_available():
        raise agent.AgentUnavailable(agent.INSTALL_HINT)


_ATTENTION_BUCKETS = [
    "parked_at_prompt_quiet",
    "unreadable_or_ambiguous",
    "parked_at_prompt_recent",
    "working_quiet",
    "working",
]


def _attention_order(rows: list[dict[str, Any]], quiet_seconds: int) -> list[str]:
    """Session names ordered by the same deterministic heuristic ``attention``
    uses, as a prior handed to the model (it may override it)."""

    def bucket(row: dict[str, Any]) -> str:
        idle = row.get("idle_seconds")
        quiet = idle is not None and idle >= quiet_seconds
        if row.get("at_prompt") == "yes":
            return "parked_at_prompt_quiet" if quiet else "parked_at_prompt_recent"
        if row.get("at_prompt") == "uncertain":
            return "unreadable_or_ambiguous"
        return "working_quiet" if quiet else "working"

    ranked = sorted(
        rows,
        key=lambda r: (_ATTENTION_BUCKETS.index(bucket(r)), -(r.get("idle_seconds") or 0)),
    )
    return [r["session"] for r in ranked]


# --------------------------------------------------------------------------
# triage
# --------------------------------------------------------------------------

_TRIAGE_INSTRUCTIONS = """\
You are a tmux fleet triage assistant. You are given a JSON snapshot of every \
tmux session on one machine, already collected mechanically (slivers of each \
pane's last line, a tri-state at_prompt classification, a harness label, idle \
time, and a heuristic candidate ordering). Decide which sessions plausibly need \
a human, and why.

Reason ONLY from the data provided. Do not invent sessions, output, or state \
that is not in the snapshot. A session at_prompt="uncertain" means its state \
could not be read -- treat that as a reason to look, not as idleness. A \
full-screen TUI blocked on a keypress can classify as at_prompt="no"; say so if \
a session's last_line hints at that.

Return ONLY a single JSON object, no prose, no markdown fence, matching exactly:
{
  "summary": "<one sentence: the state of the fleet>",
  "needs_attention": [
    {
      "session": "<name>",
      "why": "<why this session plausibly wants a human, citing its data>",
      "urgency": "high" | "medium" | "low",
      "suggested_action": "<what a human should do next, e.g. read it, answer a prompt>"
    }
  ],
  "quiet": ["<session names that look fine to leave alone>"],
  "notes": "<caveats, blind spots, or an empty string>"
}
If no session needs attention, return an empty "needs_attention" list.
"""

_INTERPRET_INSTRUCTIONS = """\
You are interpreting the state of ONE tmux session from a mechanically-collected \
capture of its pane scrollback. Decide what the session's current state and \
recent output MEAN.

Reason ONLY from the captured text provided. Do not assume output that is not \
present. If the capture is incomplete (the completeness block says so), factor \
that into your confidence rather than guessing beyond it.

Return ONLY a single JSON object, no prose, no markdown fence, matching exactly:
{
  "summary": "<one to three sentences: what is happening in this session>",
  "state": "working" | "waiting_for_input" | "finished_success" | "finished_error" | "error" | "idle" | "unknown",
  "evidence": "<the specific lines or signals that justify the state>",
  "confidence": "high" | "medium" | "low",
  "suggested_next_action": "<what a human or agent should do next>"
}
"""


async def triage(
    *,
    quiet_seconds: int = fleet.DEFAULT_QUIET_SECONDS,
    socket_dir: str | None = None,
    socket_name: str | None = None,
    timeout_ms: int = agent.DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """[model-backed] Fleet-wide: what needs attention and why, structured.

    Context (every session's sliver + the heuristic attention ordering) is
    assembled mechanically by ``attention``/``list_sessions``; the model is
    asked only to judge it. Executes through amplifier-agent; with no working
    agent this raises ``AgentUnavailable`` naming the remedy -- never a silent
    fallback.
    """
    _require_agent()

    # ONE consistent snapshot of the WHOLE fleet. The deterministic `attention`
    # rollup gives a heuristic prior, but its candidate list EXCLUDES working
    # sessions -- handing the model only candidates makes the session count
    # disagree with the session list, and the model (correctly) spends its
    # attention reconciling that data gap instead of triaging. So the model gets
    # every session, plus the heuristic ordering as a hint.
    listing = await fleet.list_sessions(socket_dir=socket_dir, socket_name=socket_name)
    rows = listing["sessions"]
    order = _attention_order(rows, quiet_seconds)

    context = {
        "server_running": listing["server_running"],
        "fleet_counts": listing["counts"],
        "quiet_threshold_seconds": quiet_seconds,
        "heuristic_attention_order": order,
        "sessions": [
            {
                "session": row["session"],
                "last_line": row.get("last_line"),
                "at_prompt": row.get("at_prompt"),
                "at_prompt_reason": row.get("at_prompt_reason"),
                "annotation": row.get("annotation"),
                "harness": row.get("harness"),
                "idle_seconds": row.get("idle_seconds"),
                "cwd": row.get("cwd"),
            }
            for row in rows
        ],
    }

    prompt = (
        _TRIAGE_INSTRUCTIONS
        + "\n\nFLEET SNAPSHOT (JSON):\n"
        + json.dumps(context, indent=2, sort_keys=False)
    )

    raw = await asyncio.to_thread(agent.run_agent_turn, prompt, timeout_ms=timeout_ms)
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        raise agent.AgentError(
            "triage expected a JSON object from amplifier-agent, got "
            f"{type(parsed).__name__}."
        )

    return {
        "verb": "triage",
        "model_backed": True,
        "executed_through": "amplifier-agent",
        "socket": listing["socket"],
        "server_running": listing["server_running"],
        "triage": parsed,
        "context_assembled_by": (
            "tmux-fleet library code (list_sessions slivers + the attention "
            "heuristic order) -- the agent judged this context but did not "
            "choose or collect it"
        ),
        "fleet_counts": listing["counts"],
        "note": (
            "the triage verdict is a model's judgment over mechanically-collected "
            "slivers, NOT a readback of any session. Use `read`/`interpret` on a "
            "flagged session before acting on it."
        ),
    }


async def interpret(
    session: str,
    *,
    lines: int = fleet.DEFAULT_READ_LINES,
    socket_dir: str | None = None,
    socket_name: str | None = None,
    timeout_ms: int = agent.DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """[model-backed] What this session's state/output means, structured.

    The scrollback is captured mechanically by ``read``; the model is asked only
    to interpret it. Executes through amplifier-agent; with no working agent this
    raises ``AgentUnavailable`` naming the remedy.
    """
    _require_agent()

    read = await fleet.read_session(
        session, lines=lines, socket_dir=socket_dir, socket_name=socket_name
    )

    context = {
        "session": session,
        "last_line": read.get("last_line"),
        "at_prompt": read.get("at_prompt"),
        "at_prompt_reason": read.get("at_prompt_reason"),
        "annotation": read.get("annotation"),
        "completeness": read.get("_completeness"),
        "pane": read.get("pane"),
    }

    prompt = (
        _INTERPRET_INSTRUCTIONS
        + "\n\nSESSION CAPTURE (JSON):\n"
        + json.dumps(context, indent=2, sort_keys=False)
    )

    raw = await asyncio.to_thread(agent.run_agent_turn, prompt, timeout_ms=timeout_ms)
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        raise agent.AgentError(
            "interpret expected a JSON object from amplifier-agent, got "
            f"{type(parsed).__name__}."
        )

    return {
        "verb": "interpret",
        "model_backed": True,
        "executed_through": "amplifier-agent",
        "socket": read["socket"],
        "session": session,
        "interpretation": parsed,
        "context_assembled_by": (
            "tmux-fleet library code (read scrollback) -- the agent interpreted "
            "this capture but did not choose or collect it"
        ),
        "read_completeness": read.get("_completeness"),
        "note": (
            "this is a model's interpretation of captured pane text, NOT a "
            "readback of the process's own state. Confidence is the model's; "
            "weigh it against the completeness block."
        ),
    }
