---
smart_tool_format: 1
name: tmux-fleet
version: 0.3.0
description: >
  Tells you what is happening across every tmux session on a machine — which are
  parked at a prompt, which finished and how, which need a human — and, only
  under explicit per-invocation confirmation, types into one or starts one.
  Reach for it when a box is running many tmux sessions (agents, builds, REPLs,
  long jobs) and "what needs me right now?" is the question, or when something
  needs one keystroke or one new session delivered safely. The model-backed
  verbs run through amplifier-agent's engine embedded in-process.
use_cases:
  - Survey every tmux session on a machine and see which are parked at a prompt
  - Triage a busy fleet to find which sessions plausibly need a human, and why
  - Read one session's scrollback with an honest bound on what was captured
  - Interpret what a session's recent output means without attaching to it
  - Type a command into a session, or start a new one, under explicit confirmation
platforms:
  - linux
  - macos
requires:
  - name: tmux
    purpose: >
      The terminal multiplexer this tool observes and drives. Every verb is a
      tmux client; nothing works without it.
    install: docs/installing-tmux.md
  - name: ai-provider
    purpose: >
      The model-backed verbs (triage, interpret) run through the amplifier-agent
      engine, which is embedded in-process and ships as a regular dependency of
      this tool — nothing to install separately. What they DO need is a provider:
      a provider SDK, installed via an extra (e.g. `tmux-fleet[anthropic]`), plus
      that provider's credentials in the environment. Optional: every
      deterministic verb (socket, sessions, attention, read, send, create,
      doctor, exit-code) works with no provider at all; only triage and interpret
      need one, and they fail loudly naming the missing precondition rather than
      degrading to a guess. This tool never stores credentials of its own.
    optional: true
    install: docs/installing-amplifier-agent.md
---

A smart tool for tmux fleets. One library, one thin `tmux-fleet` CLI. Every verb is
reachable from `tmux_fleet` as a Python function; the CLI parses arguments, calls the
library, and prints the result. To chain verbs, or combine them with other smart tools,
write a script against the libraries and pass return values between calls rather than
piping CLI output.

## When to reach for it

- A machine is running many tmux sessions and you need to know, quickly, which
  ones are parked at a prompt, which finished, and which need a human.
- You want a model-backed triage over the whole fleet (`triage`) or an
  interpretation of one session's output (`interpret`) without attaching.
- You need to deliver one keystroke or one command into a session, or start a
  new detached session, safely: every write refuses without `--confirmed`
  and is recorded in an append-only audit log.

## What it is deliberately bad at

- **Full-screen TUIs.** Prompt classification is a heuristic over the pane's
  last line. A vim/htop/pager blocked on a keypress classifies as
  `at_prompt: no` and will not surface as needing attention. `read`/`interpret`
  it directly.
- **"Did the command succeed?"** `send`/`create` confirm the *keystrokes* were
  delivered (armed / submitted / uncertain), never that the command finished or
  succeeded. Use `exit-code` for a finished session, or `read`/`interpret`.
- **Finding sessions on another socket.** Every verb reads exactly ONE resolved
  socket and says which. It does not auto-detect the ambient `TMUX_TMPDIR`. If a
  fleet looks empty, run `socket` first.

## Install

```
uv tool install git+https://github.com/microsoft/amplifier-smart-tool-tmux
```

`tmux` must be on PATH; every verb is a tmux client. The model-backed verbs also need
a provider SDK, installed as an extra (`tmux-fleet[anthropic]` or `tmux-fleet[openai]`),
and that provider's credentials in the environment. `doctor` reports what this host
has and what is missing.

## Model-backed verbs

`triage` and `interpret` run a single model turn through the amplifier-agent engine,
embedded in-process. They consume tokens and may answer differently on a second run.
Invoked without a usable substrate (provider SDK extra, provider configuration, or
credentials) they fail naming exactly which precondition is missing, never falling
back to a deterministic guess. Every other verb runs with nothing configured.

## Worked invocations

```
tmux-fleet socket                 # which socket am I reading, and on whose authority
tmux-fleet sessions               # every session, each with a 30-line sliver + tri-state at_prompt
tmux-fleet attention              # heuristic triage ORDER (deterministic, no model)
tmux-fleet triage                 # [model-backed] what needs attention and why, structured
tmux-fleet read work --lines 400  # one session's scrollback, with a completeness bound
tmux-fleet interpret work         # [model-backed] what this session's output means
tmux-fleet doctor                 # preflight: tmux present, socket resolvable/writable
tmux-fleet exit-code build        # tmux-native exit status of a finished session
tmux-fleet send work --text 'make test' --submit --confirmed   # fenced write
tmux-fleet send work --text $'line one\nline two\n' --paste --confirmed # buffered paste
tmux-fleet create scratch --confirmed --command 'htop'         # fenced create
```

## Output and failure contract

Every response is a single JSON document on stdout. Failures are a JSON error
envelope (`{"error": {"code", "message", "remedy"}}`) with a non-zero exit: `2`
refused (deny-by-default write, unknown session, bad argument), `1` read or agent
failure. Diagnostics go to stderr. There is deliberately no verb that kills or
renames a session.

`send --text` refuses CR/LF data by default so it cannot synthesize interior
Enter events. For a target that supports bracketed paste, add `--paste` to
deliver the exact unchanged text through tmux-kit's native buffered paste; it
generates no Enter. `--submit` adds exactly one Enter event. tmux only wraps
the paste when the target enabled bracketed-paste mode, so `--paste` is the
caller's explicit target selection, not universal transaction safety.

## Reading more

The repository's `docs/installing-tmux.md` and `docs/installing-amplifier-agent.md`
cover the prerequisites; `contracts/cli.v1.md` is the frozen invocation surface.

## Optional collaborative terminals

`TerminalFleet` is a socket-bound public library for exact pane discovery, bounded
observations, retained views/drafts and durable input/management receipts. It makes
no model calls. `tmux-fleet-terminal --help` exposes its deterministic JSON adapter.
Install `[mcp]` and run `tmux-fleet-mcp --storage /private/path --socket-dir /tmp`
for typed MCP tools and an optional self-contained terminal App. Input and management
are disabled unless the launching host supplies `--allow-input` / `--allow-management`.
Management still needs per-action confirmation; interactive input needs a confirmed
exact-pane grant with expiry/byte limits. Every input has a stable request ID: inspect
its receipt on timeout, never retype blindly. Closing the App leaves tmux work running.
See `contracts/terminal.v1.md` and `docs/collaborative-terminals.md` for scope and limits.
