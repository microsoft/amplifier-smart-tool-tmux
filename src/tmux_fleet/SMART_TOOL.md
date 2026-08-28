---
smart_tool_format: 1
name: tmux-fleet
version: 0.2.0
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

# tmux-fleet

A smart tool for tmux fleets. One library, one thin `tmux-fleet` CLI.

## When to reach for it

- A machine is running many tmux sessions and you need to know, quickly, which
  ones are parked at a prompt, which finished, and which need a human.
- You want a model-backed triage over the whole fleet (`triage`) or an
  interpretation of one session's output (`interpret`) — without attaching.
- You need to deliver one keystroke or one command into a session, or start a
  new detached session, *safely* — every write refuses without `--confirmed`
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
tmux-fleet create scratch --confirmed --command 'htop'         # fenced create
```

Every response is a single JSON document on stdout. Failures are a JSON error
envelope (`{"error": {"code", "message", "remedy"}}`) with a non-zero exit;
diagnostics go to stderr. `--help` lists every verb and names the model-backed
ones. There is deliberately no verb that kills or renames a session.
