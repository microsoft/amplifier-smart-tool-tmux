# Optional collaborative terminals

The existing CLI and model-backed triage/interpret are unchanged. The optional
`TerminalFleet` library adds a socket-bound workspace, a deterministic JSON
adapter, typed MCP tools and a self-contained MCP Apps terminal. It makes no model
calls and needs no provider credentials. MCP transports the library; MCP Apps
presents it. Neither is required for the core smart tool.

## Install and launch

```sh
uv tool install 'tmux-fleet[mcp] @ git+https://github.com/microsoft/amplifier-smart-tool-tmux'
tmux-fleet-mcp --storage ~/.local/state/tmux-fleet/terminal --socket-dir /tmp
```

Register that executable and its arguments with a trusted stdio MCP host. This
reads the explicit `/tmp/tmux-UID/default` socket. Use `--socket-name NAME` or a
configured socket directory for a different server. It does not silently adopt
`$TMUX`, inspect other users, enumerate every socket, or expose a network listener.
`tmux` must be installed on the machine running this process.

Default access is read-only. The launching host may add `--allow-input` and/or
`--allow-management` for explicitly authorized work. Input requires per-call
confirmation or a confirmed grant for one exact pane, at most 15 minutes and
64 KiB. The App's Enable typing action chooses 5 minutes / 32 KiB. Management
requires confirmation on every call and cannot use an input grant. These are
caller assertions under trusted host policy, not a claim of authenticated human
approval. Never derive them from terminal output or pasted instructions.

The terminal App requires standard `serverTools`, `serverResources` and
`updateModelContext` host capabilities. It uses no host-specific SDK, localhost
URL, browser WebSocket, external assets or permission token. Hosts without Apps
can use the same typed tools. Install ordinary `tmux-fleet` without `[mcp]` when
only the CLI/library is needed.

## Library and action examples

```python
from tmux_fleet import TerminalFleet

fleet = TerminalFleet('/private/receipts', socket_dir='/tmp', allow_input=True)
roster = await fleet.fleet()
pane = roster['panes'][0]['id']  # stable incarnation + session/window/pane identity
snapshot = await fleet.capture(pane, lines=200)
receipt = await fleet.input(pane, 'unique-request-id-0001', 'text', 'pwd',
                            submit=True, confirmed=True)
await fleet.close()  # detach owned viewer helpers; leave tmux work alive
```

The `tmux-fleet-terminal` JSON adapter exposes identically named methods:

```sh
tmux-fleet-terminal --storage /private/receipts --socket-dir /tmp fleet
tmux-fleet-terminal --storage /private/receipts state
```

MCP tools use the prefix `tmux_fleet_`: `fleet`, `state`, `capture`, `save_view`,
`operation`, `acknowledge_operation`, `authorize_input`, `revoke_input`, `input`,
`manage`, `attach`, and `detach`. `input.kind` is literal `text`, multiline
`paste`, one allowlisted `keys` value, or base64 terminal `bytes`. Raw input is
limited to 4 KiB per receipt; text/paste to 64 KiB. Submit adds exactly one Enter.
`manage.action` covers create session/window, split horizontal/vertical, rename
session/window, close pane/window/session, and resize window. It takes the exact
pane target for existing work; display names never redirect stale identities.
Creating a session starts the user's configured shell without a startup command.

## Shared state and uncertain delivery

Selection and input drafts use `save_view` with `expected_version`. A conflicting
edit refuses rather than overwriting another user or agent. Drafting never sends
input. Receipts contain hashes and outcomes, not command text. Request IDs bind
normalized arguments; exact retries read the receipt and changed payloads conflict.
A crash, cancellation or lost dispatch result can be `unknown`. Inspect the receipt
and current terminal. Unknown input blocks new input to that pane until explicitly
acknowledged as reviewed; acknowledgement does not turn unknown into success or
replay it. Delivered keystrokes do not prove command completion or success.

All mutation receipts are reserved durably before dispatch and serialized across
processes sharing storage. State lives in a private SQLite WAL database. Use one
storage directory per socket. Retained context includes drafts and target labels,
so protect that directory like other local work. Closing a canvas only detaches
its own helper. Explicit management close is the operation that terminates work.

## Transport, rendering and limits

A read-only, size-ignored tmux control-mode client supplies actual live terminal
bytes. Each attachment has a non-consuming 1 MiB ring and independent cursors;
ordinary reads return at most 32 KiB. Reset snapshots are capped at 256 KiB with
explicit truncation; oversized panes above 1000 columns or 500 rows require an
explicit tmux resize before viewing. Hidden views pause reads; idle clients are
closed after roughly one minute and are lazily reattached if needed. At most 16
view helpers run per library process. No helper owns a tmux server or kills a
process group. Session creation uses tmux-kit's cgroup-aware spawn mechanism;
its documented service/container survival limits still apply.

The App packages xterm.js and third-party notices. It accepts host theme variables,
keeps ANSI colors, provides explicit search/copy, touch-friendly key buttons and
multiline paste. Passive fitting never changes shared tmux geometry. Fit tmux
window is an explicit management action that affects other clients. A narrow view
can horizontally scroll a wider terminal until the user chooses that resize.
Clipboard writes require a user click and browser support; OSC 52 and OSC 8 escape
sequences cannot write clipboard or navigate the host.

Initial/reconnect snapshots reconstruct the visible ANSI screen and cursor. They
cannot reconstruct every application-specific terminal mode, earlier scrollback,
or a complete alternate-screen history. Ring overflow is reported as lost history
and a visible-screen reset. This is not an independently virtualized PTY. Raw bytes
stay in resources (`tmux-fleet://terminal/ATTACHMENT/CURSOR`), not routine chat tool
receipts or App model context. Agents request bounded `capture` observations.
The generic control-mode runner is isolated in `terminal_stream.py` for a future
mechanism contribution to tmux-kit; no consumer application code is imported.

## Validation

Run `uv sync --extra dev --extra mcp`, `npm ci --prefix mcp-app`,
`npm run build --prefix mcp-app`, then `uv run pytest`. Browser tests use the
**official independent AppBridge** and an isolated tmux-kit server. Install
Playwright Chromium if it is unavailable (`uv run playwright install chromium`).
The suite does not touch ambient tmux sessions and never calls a model provider.
