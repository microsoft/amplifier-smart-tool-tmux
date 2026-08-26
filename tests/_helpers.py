"""Shared test helpers.

Fixture fleets are created EXCLUSIVELY through tmux_kit.isolated_tmux_server():
a throwaway, uniquely-socketed tmux server in its own scrubbed environment.
Nothing here ever touches the machine's real tmux socket, runs a bare `tmux`,
or calls kill-server.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable

from tmux_kit import isolated_tmux_server


def run(coro):
    """Drive an async coroutine from a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


@contextlib.asynccontextmanager
async def fleet(*names: str):
    """An isolated tmux server with *names* created, plus the kwargs a verb needs
    to be pinned at it (``socket_dir`` + ``socket_name``)."""
    async with isolated_tmux_server(prefix="tf-test") as srv:
        for name in names:
            await srv.run("new-session", "-d", "-s", name)
        kw = {"socket_dir": srv.socket_dir, "socket_name": srv.socket_name}
        yield srv, kw


def session_names(listing: dict) -> set[str]:
    return {row["session"] for row in listing.get("sessions", [])}


def scrub_providers(monkeypatch) -> Iterable[str]:
    """Remove every provider credential/config env var from the process.

    Proves the deterministic verbs need no AI provider configured at all.
    Returns the names that were removed (for assertion/debug).
    """
    import os
    import re

    pattern = re.compile(
        r"(ANTHROPIC|OPENAI|AZURE|GOOGLE|GEMINI|QWEN|OSS_|CLAUDE|COHERE|MISTRAL)",
        re.IGNORECASE,
    )
    removed = []
    for key in list(os.environ):
        if pattern.search(key) or key == "MODEL":
            monkeypatch.delenv(key, raising=False)
            removed.append(key)
    return removed
