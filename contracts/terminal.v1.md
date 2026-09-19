# Collaborative terminals — additive v1 proposal

This optional public library, deterministic command adapter, MCP server and MCP
App complement the existing CLI contract. MCP is transport; the product remains
the library. This is a proposed capability contract, not an ecosystem standard.

1. One immutable configured socket per library/server. Every tmux call carries
   explicit `-S` and a scrubbed environment. Discovery states its socket scope;
   it never implies that one socket enumerates every socket on the host.
2. Targets identify server incarnation, session, window and pane. Display names
   are labels. A stale target refuses, never redirects by a reused name. Viewing
   a pane does not select it for other tmux clients.
3. Read-only is the default. Host configuration separately permits input and
   management. Every management call requires its own explicit confirmation.
   A terminal input grant may cover one exact pane, expiry and byte allowance;
   it never covers management, another pane, or a replacement server. Confirmation
   and actor labels are caller assertions, not authenticated human identity.
4. New sessions/windows/splits, renames, explicit close and explicit resize are
   supported management effects. Closing a viewer only detaches its owned helper.
   Closing tmux work is a separate confirmed operation naming the affected scope.
   Resizing is shared terminal geometry; passive browser fitting cannot resize tmux.
5. Input, management and grant authorization reserve durable request receipts
   before dispatch. Same ID and same
   payload returns the receipt; a changed payload conflicts. An interrupted or
   uncertain dispatch is `unknown`, never automatically replayed. Delivery does
   not prove that a command completed or succeeded. Input contents are not logged.
6. Public versioned view and draft operations are shared by users and agents.
   Drafts, terminal output and App model context are observations, never authority.
   Raw terminal output stays in bounded resources, outside routine tool results.
7. An owned read-only control-mode client supplies bounded terminal output.
   Cursor reads are non-consuming and scoped to one attachment; reconnect/resync
   never replays input. Lost history is disclosed. A reset reconstructs the visible
   ANSI screen/cursor; it cannot reconstruct every private emulator mode or prior
   scrollback. Do not claim an independently virtualized PTY or perfect restoration.
8. The App packages its renderer and licenses, accepts host theme context and
   works at narrow widths. Terminal escape sequences cannot write clipboard or
   open links without user action. Missing resource capability produces a readable
   diagnostic rather than a blank terminal or an undocumented network fallback.
9. Deterministic operations require no model credentials or model calls. Existing
   smart triage/interpret remain available through their current library/CLI.

## Acceptance work items

- Exact targets, durable effects and input grants; stale identity and lost-receipt tests.
- Shared fleet/view/draft API and thin deterministic/MCP adapters.
- Bounded terminal resource and owned-client teardown, reset and burst tests.
- Self-contained xterm App; independent official AppBridge, theme and mobile tests.
- Preserve original CLI/help/envelope conformance; package/install validation.

All tmux tests use `tmux_kit.isolated_tmux_server`; none inspect or touch ambient
sessions. The generic control-mode mechanism is a candidate for tmux-kit extraction;
this proposal must not import any consumer's application or authentication policy.
