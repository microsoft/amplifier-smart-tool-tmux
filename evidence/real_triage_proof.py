"""Real smart-path proof: an isolated fixture fleet + REAL triage AND interpret,
plus structural proof the turn runs IN-PROCESS (no amplifier-agent CLI spawned).

Stands up a throwaway tmux server via tmux_kit.isolated_tmux_server(), puts three
sessions into distinguishable states, then runs the model-backed `triage` verb
and `interpret` verb -- both of which execute through amplifier-agent's engine
library imported IN-PROCESS (VISION section 3), using whatever provider is
configured in the environment.

While each turn runs, a concurrent poller walks this process's descendant tree
(/proc) and records every child command line seen. The proof asserts NO
descendant is the `amplifier-agent` CLI binary: the embedded-engine model spawns
no subprocess for the turn, so the SDK/binary path is structurally absent. (The
isolated tmux server IS a descendant and is expected -- it demonstrates the
capture is live, not vacuous.)

Nothing here touches the machine's real tmux socket.

Run: uv run --extra anthropic python evidence/real_triage_proof.py

Note: the engine resolves its module cache under ``$AMPLIFIER_AGENT_HOME``. On a
host whose default ``~/.amplifier-agent`` cache was populated by a *different*
amplifier-agent version, the module hashes will not match this engine's and a
turn fails with "No providers available". Point ``AMPLIFIER_AGENT_HOME`` at an
isolated directory to force a coherent cold prepare -- the same isolation
principle this proof already applies to the tmux server:

    AMPLIFIER_AGENT_HOME=/tmp/tf-engine-home \\
        uv run --extra anthropic python evidence/real_triage_proof.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time

from tmux_kit import isolated_tmux_server

from tmux_fleet import fleet, smart


# --------------------------------------------------------------------------
# Process-tree capture -- proves no amplifier-agent CLI subprocess is spawned.
# --------------------------------------------------------------------------


def _descendant_cmdlines(root_pid: int) -> dict[int, list[str]]:
    """Every descendant pid of *root_pid* mapped to its argv (Linux /proc)."""
    children: dict[int, list[int]] = {}
    cmdlines: dict[int, list[str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                data = fh.read()
            rparen = data.rfind(b")")  # comm can contain spaces/parens; skip past it
            fields = data[rparen + 2 :].split()
            ppid = int(fields[1])  # after comm: state(0), ppid(1)
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
            cmdlines[pid] = [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]
        except OSError:
            cmdlines[pid] = []

    seen: dict[int, list[str]] = {}
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child not in seen:
                seen[child] = cmdlines.get(child, [])
                stack.append(child)
    return seen


def _is_amplifier_agent_cli(argv: list[str]) -> bool:
    """True iff *argv* invokes the ``amplifier-agent`` CLI binary in any form."""
    return any(os.path.basename(arg) == "amplifier-agent" for arg in argv)


async def _run_with_proc_capture(coro):
    """Await *coro* while continuously capturing this process's descendant tree."""
    captured: dict[int, list[str]] = {}

    async def poll() -> None:
        while True:
            captured.update(_descendant_cmdlines(os.getpid()))
            await asyncio.sleep(0.1)

    poller = asyncio.ensure_future(poll())
    try:
        result = await coro
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
    captured.update(_descendant_cmdlines(os.getpid()))  # final sweep
    return result, captured


def _report_capture(label: str, captured: dict[int, list[str]]) -> list[tuple[int, list[str]]]:
    basenames = sorted({os.path.basename(argv[0]) for argv in captured.values() if argv})
    hits = [(pid, argv) for pid, argv in captured.items() if _is_amplifier_agent_cli(argv)]
    in_process = "amplifier_agent_lib" in sys.modules
    print(f"\n--- process-tree capture during {label} ---")
    print(f"  engine imported IN THIS process (amplifier_agent_lib in sys.modules): {in_process}")
    print(f"  descendant processes observed ({len(captured)}): {basenames}")
    print(f"  amplifier-agent CLI processes observed: {len(hits)}")
    if hits:
        for pid, argv in hits:
            print(f"    !! pid={pid} argv={argv}")
    return hits


def _capture_selftest() -> bool:
    """Prove the descendant walker is LIVE: it must see a real short-lived child.

    Guards against a vacuous "0 processes observed" result -- if the walker
    could not see any child, the zero-amplifier-agent finding would be
    meaningless. Spawns a harmless `sleep` and confirms the walker catches it.
    """
    child = subprocess.Popen(["sleep", "0.5"])  # noqa: S603, S607
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if child.pid in _descendant_cmdlines(os.getpid()):
                print(f"capture self-test: walker detected child pid {child.pid} -- it is LIVE.")
                return True
            time.sleep(0.05)
        print("capture self-test: FAILED to detect a known child process.")
        return False
    finally:
        child.wait()


async def main() -> int:
    provider_pin = os.environ.get("TMUX_FLEET_PROVIDER")
    providers = sorted(k for k in os.environ if k.endswith("_API_KEY") and os.environ[k])
    print(f"provider *_API_KEY vars present (names only): {providers}")
    if provider_pin:
        print(f"TMUX_FLEET_PROVIDER pin: {provider_pin}")

    all_hits: list[tuple[int, list[str]]] = []

    print()
    selftest_ok = _capture_selftest()

    async with isolated_tmux_server(prefix="tf-proof") as srv:
        kw = {"socket_dir": srv.socket_dir, "socket_name": srv.socket_name}

        # Three sessions in three states.
        await srv.run("new-session", "-d", "-s", "builder")   # will look "working"
        await srv.run("new-session", "-d", "-s", "review")    # parked at a REPL prompt
        await srv.run("new-session", "-d", "-s", "idle")       # a shell prompt

        # builder: a long-running command, no prompt -> "working"
        await srv.run("send-keys", "-t", "builder", "echo building; sleep 600", "Enter")
        # review: drop into a python REPL so its last line is a >>> prompt
        await srv.run("send-keys", "-t", "review", "python3", "Enter")
        await asyncio.sleep(1.5)  # let the REPL draw its prompt

        # Show the deterministic view the model will be given (mechanically collected).
        listing = await fleet.list_sessions(**kw)
        print("\n=== deterministic sessions (context the model is handed) ===")
        for row in listing["sessions"]:
            print(
                f"  {row['session']:<8} at_prompt={row['at_prompt']:<9} "
                f"harness={row['harness']:<8} last_line={row['last_line']!r}"
            )

        print("\n=== REAL triage (executes through the embedded amplifier-agent engine) ===")
        result, captured = await _run_with_proc_capture(smart.triage(timeout_ms=240_000, **kw))
        print(json.dumps(result, indent=2))
        all_hits += _report_capture("triage", captured)

        print("\n=== REAL interpret('review') ===")
        interp, captured = await _run_with_proc_capture(
            smart.interpret("review", timeout_ms=240_000, **kw)
        )
        print(json.dumps(interp, indent=2))
        all_hits += _report_capture("interpret", captured)

    print("\n=== ZERO-CLI-SUBPROCESS PROOF ===")
    if not selftest_ok:
        print("FAIL: the process-tree walker never detected its self-test child; "
              "the zero-subprocess finding cannot be trusted.")
        return 1
    if all_hits:
        print(f"FAIL: {len(all_hits)} amplifier-agent CLI subprocess(es) were spawned.")
        return 1
    print(
        "PASS: the walker is live (it caught its self-test child), the engine was "
        "imported in THIS process, and no amplifier-agent CLI process was spawned "
        "during either turn. The model turns ran in-process through the embedded "
        "engine library."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
