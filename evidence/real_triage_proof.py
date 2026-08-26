"""Real smart-path proof: an isolated fixture fleet + a REAL `triage` run.

Stands up a throwaway tmux server via tmux_kit.isolated_tmux_server(), puts three
sessions into distinguishable states, then runs the model-backed `triage` verb
(which executes through amplifier-agent, using whatever provider is configured in
the environment) and prints the structured result. Also runs `interpret` on one
session. Nothing here touches the machine's real tmux socket.

Run: uv run python evidence/real_triage_proof.py
"""

from __future__ import annotations

import asyncio
import json
import os

from tmux_kit import isolated_tmux_server

from tmux_fleet import agent, fleet, smart


async def main() -> int:
    if not agent.agent_available():
        print("amplifier-agent is not available; cannot run the real proof.")
        return 2
    print(f"amplifier-agent binary: {agent.resolve_agent_command()}")
    providers = sorted(
        k for k in os.environ if k.endswith("_API_KEY") and os.environ[k]
    )
    print(f"provider *_API_KEY vars present (names only): {providers}")

    async with isolated_tmux_server(prefix="tf-proof") as srv:
        kw = {"socket_dir": srv.socket_dir, "socket_name": srv.socket_name}

        # Three sessions in three states.
        await srv.run("new-session", "-d", "-s", "builder")   # will look "working"
        await srv.run("new-session", "-d", "-s", "review")    # will be parked at a REPL prompt
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

        print("\n=== REAL triage (executes through amplifier-agent) ===")
        result = await smart.triage(timeout_ms=240_000, **kw)
        print(json.dumps(result, indent=2))

        print("\n=== REAL interpret('review') ===")
        interp = await smart.interpret("review", timeout_ms=240_000, **kw)
        print(json.dumps(interp, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
