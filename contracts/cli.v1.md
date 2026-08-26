# tmux-fleet CLI Contract — v1 (DRAFT — no implementation passes yet; this is the seam consumers build against)

## Who builds against this

- This repo's own thin CLI (`tmux-fleet`) — the only implementation.
- The drumbeat drumpack (`amplifier-drumpack-tmux`) — exposes this CLI to automations.
- Any agent invoking the tool from a shell, and any host wrapping it.

## Purpose

The stable invocation surface of the tmux-fleet smart tool. The library is the
product; this contract freezes only what a *caller of the binary* may rely on,
so wrappers (the drumpack, scripts, other hosts) survive library evolution.

## Core (the frozen part — small on purpose)

1. **One binary, `tmux-fleet`, invocable from a shell on PATH.** Non-interactive:
   a run with stdin closed never hangs. `-h` is a terse human summary; `--help`
   is the complete listing written for an agent — every verb, its arguments,
   what it returns, and **which verbs are model-backed**.

2. **Deterministic verbs** — run correctly with no AI substrate configured at
   all: `socket`, `sessions`, `attention`, `read`, `send`, `create`, `doctor`,
   `exit-code`.

3. **Model-backed verbs** — `triage` (fleet-wide: what needs attention and
   why, structured) and `interpret <session>` (what this session's state/output
   means, structured). These execute through amplifier-agent; invoked without a
   working amplifier-agent they fail saying exactly that and how to configure
   it — **never** a silent fallback to a deterministic approximation.

4. **Writes are fenced.** `send` and `create` refuse without an explicit
   per-invocation `--confirmed`. The refusal is loud and names the flag. There
   is no session-wide or environment unlock. No verb kills or renames a session.

5. **Structured output.** Success emits one JSON document on stdout. Failure
   emits a JSON error envelope `{"error": {"code", "message", "remedy"}}` on
   stdout and exits non-zero. Progress/diagnostics go to stderr, never stdout.

6. **Sockets are explicit.** Every underlying tmux invocation names its socket;
   ambient `$TMUX` / `TMUX_TMPDIR` are ignored, and `socket` reports what was
   resolved, from where, and what ambient state was ignored.

## Explicitly backlogged (not in v1)

Promotion trigger: a real consumer demonstrates the need; amend here first.

- MCP server surface.
- Watch/streaming/progress for long smart calls (tracks the upstream spec's
  open ROADMAP question — we expect to hit it first and feed evidence back).
- Scrollback `search`/`page` verbs (tmux-kit has them; no consumer asked yet).
- Machine-readable schema for each verb's success payload (field-level).

## Conformance

- Per-verb tests: deterministic verbs pass with amplifier-agent absent
  (env-scrubbed); smart verbs refuse with the named remedy when it is absent
  and succeed against a real agent when present.
- Fence tests: `send`/`create` without `--confirmed` refuse loudly; audit log
  records both refused and delivered attempts.
- Envelope tests: malformed invocations produce the error envelope, non-zero
  exit, nothing but JSON on stdout.

Freeze bar: this spec · the conformance surface green · the drumpack consuming
it in a real drumbeat workspace · a worked example. DRAFT until all four.

## Reserved / open questions (NOT frozen)

- Exact success-payload shapes per verb (frozen only as "one JSON document").
- Harness-classification exposure (inside `sessions`/`triage` output vs its own
  verb).
- Exit-code taxonomy beyond zero/non-zero.

## Changelog

- **2026-08-26** — v1 drafted from the negotiated design (deterministic verb set
  ported from the proven fleet pack + `doctor`/`exit-code` gap-fills; smart
  verbs `triage`/`interpret` on amplifier-agent). No implementation exists yet;
  the build lane implements against this and amends with evidence where reality
  pushes back.
