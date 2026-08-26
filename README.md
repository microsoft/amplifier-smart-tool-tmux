# tmux-fleet

A **smart tool** for tmux fleets: one library, one thin `tmux-fleet` CLI that
tells you what is happening across every tmux session on a machine — which are
parked at a prompt, which finished and how, which need a human — and, only
under explicit per-invocation confirmation, types into one or starts one.

Deterministic paths (`socket`, `sessions`, `attention`, `read`, `send`,
`create`, `doctor`, `exit-code`) run with nothing configured. Smart paths
(`triage`, `interpret`) bring model-backed judgment, executed through
[amplifier-agent](https://github.com/microsoft/amplifier-agent).

This repo is the first reference implementation of the
[amplifier smart-tools spec](https://github.com/DavidKoleczek/amplifier-smart-tools-spec).
See `docs/VISION.md` and `contracts/cli.v1.md` for the governing design.

## Install

```
uv tool install git+<this repo>
```

## Use

```
tmux-fleet --help          # full, agent-facing listing (names the model-backed verbs)
tmux-fleet socket          # which socket am I reading, and on whose authority
tmux-fleet sessions        # every session on the resolved socket, with slivers
tmux-fleet attention       # triage ORDER: which sessions plausibly want a human
tmux-fleet read <name>     # one session's scrollback, with an honest completeness bound
tmux-fleet doctor          # preflight: tmux present, socket resolvable/writable
tmux-fleet exit-code <name># tmux-native exit status of a finished session
tmux-fleet triage          # [model-backed] fleet-wide: what needs attention and why
tmux-fleet interpret <name># [model-backed] what this session's state/output means
tmux-fleet send <name> --text 'echo hi' --submit --confirmed   # fenced write
tmux-fleet create <name> --confirmed                            # fenced create
```

Every response is one JSON document on stdout. Failures are a JSON error
envelope (`{"error": {"code", "message", "remedy"}}`) on stdout with a non-zero
exit; diagnostics go to stderr. The write verbs refuse without `--confirmed`.
There is deliberately no verb that kills or renames a session.

## Library

Every capability is in the library; the CLI is a thin adapter.

```python
import asyncio
from tmux_fleet import list_sessions, triage, manifest

asyncio.run(list_sessions())
manifest()  # structured manifest, read from the copy built into the tool
```
