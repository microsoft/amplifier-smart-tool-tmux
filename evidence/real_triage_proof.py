"""Live smart-path evidence: an isolated fleet + REAL triage AND interpret,
with public-binding import and sampled CLI-subprocess checks.

Stands up a throwaway tmux server via tmux_kit.isolated_tmux_server(), puts three
sessions into distinguishable states, then runs the model-backed `triage` verb
and `interpret` verb through amplifier-agent v0.22.0's supported public Python
binding, lazily imported IN-PROCESS (VISION section 3). Provider credentials come
only from the caller environment. Conflicting provider pins refuse; non-Anthropic
providers require AMPLIFIER_AGENT_MODEL. No provider SDK extra is mandatory for
Anthropic/OpenAI; those tmux-fleet extra names are compatibility no-ops.

While each turn runs, a concurrent poller walks this process's descendant tree
(/proc) and records observed child command lines. It checks that public
``amplifier_agent`` is in sys.modules and no observed descendant invokes the
``amplifier-agent`` CLI. A self-test child makes the walker check non-vacuous.
Sampling cannot rule out a process shorter than the polling interval.

Run ONLY inside an isolated Linux container with deliberately supplied credentials
and a matching model, after scripted installed-SDK and fresh-wheel checks:

    uv run --locked python evidence/real_triage_proof.py

This costs real inference. It uses only an owned tmux socket. Each turn has a
180-second budget plus the adapter's separate fixed 10-second TOTAL cleanup tail.
Ephemeral SDK sessions still write captures and honor deliberate configured
destinations; local temporary cleanup is NOT a no-forwarding promise.
See docs/installing-amplifier-agent.md for setup and the required verification
gates. This script is supplementary evidence, not a lifecycle/package/privacy gate.
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
# Process-tree capture -- checks for observed amplifier-agent CLI subprocesses.
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
    in_process = "amplifier_agent" in sys.modules
    print(f"\n--- process-tree capture during {label} ---")
    print(f"  public binding imported IN THIS process (amplifier_agent in sys.modules): {in_process}")
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

        print("\n=== REAL triage (embedded amplifier-agent public Python binding) ===")
        result, captured = await _run_with_proc_capture(smart.triage(timeout_ms=180_000, **kw))
        print(json.dumps(result, indent=2))
        all_hits += _report_capture("triage", captured)

        print("\n=== REAL interpret('review') ===")
        interp, captured = await _run_with_proc_capture(
            smart.interpret("review", timeout_ms=180_000, **kw)
        )
        print(json.dumps(interp, indent=2))
        all_hits += _report_capture("interpret", captured)

    print("\n=== PUBLIC-BINDING / SAMPLED CLI-SUBPROCESS CHECK ===")
    if not selftest_ok:
        print("FAIL: the process-tree walker never detected its self-test child; "
              "the zero-subprocess finding cannot be trusted.")
        return 1
    if "amplifier_agent" not in sys.modules:
        print("FAIL: the public amplifier_agent binding was not imported in this process.")
        return 1
    if all_hits:
        print(f"FAIL: {len(all_hits)} amplifier-agent CLI subprocess(es) were spawned.")
        return 1
    print(
        "PASS: the walker is live (it caught its self-test child), the public binding was "
        "imported in THIS process, and the poller observed no amplifier-agent CLI "
        "process during either turn. This is sampled evidence "
        "for in-process public-binding execution, not exhaustive subprocess proof."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
