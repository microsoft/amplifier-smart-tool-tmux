"""Owned read-only control-mode client and bounded, non-consuming output ring.

This narrow mechanism is isolated for later extraction into tmux-kit. No network
listener, shell command, terminal input path or consumer-specific protocol exists.
"""

from __future__ import annotations

import asyncio
import base64
import re
import shlex
import time
import uuid
from collections import deque

from .terminal import TerminalError

OCTAL = re.compile(rb"\\([0-7]{3})")


def decode_output(value):
    """tmux control-mode octal encoding, preserving bytes across UTF-8 boundaries."""
    return OCTAL.sub(lambda match: bytes([int(match[1], 8)]), value)


class ControlStream:
    def __init__(self, library, target):
        self.library, self.target = library, target
        self.identity = uuid.uuid4().hex
        self.chunks = deque()
        self.offset = 0
        self.base = 0
        self.closed = False
        self.process = None
        self.reader = None
        self.error_reader = None
        self.ready = asyncio.Event()
        self.pending = None
        self.command_lock = asyncio.Lock()
        self.last_read = time.monotonic()

    async def start(self):
        # -r grants this helper no interactive authority; ignore-size means a
        # viewer does not silently resize or focus another person's terminal.
        self.process = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            self.library.socket,
            "-C",
            "attach-session",
            "-r",
            "-f",
            "ignore-size",
            "-t",
            self.target["session_id"],
            env=self.library.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=2 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._receive())
        self.error_reader = asyncio.create_task(self._discard_errors())
        try:
            await asyncio.wait_for(self.ready.wait(), 5)
        except BaseException:
            await self.close()
            raise
        if self.closed:
            raise TerminalError(
                "VIEW_UNAVAILABLE", "The read-only tmux viewer could not attach."
            )

    async def _discard_errors(self):
        while await self.process.stderr.read(4096):
            pass

    async def _receive(self):
        block = None
        content = []
        try:
            while line := await self.process.stdout.readline():
                line = line.rstrip(b"\n")
                if line.startswith(b"%begin "):
                    block = line[7:]
                    content = []
                elif block is not None and line in (
                    b"%end " + block,
                    b"%error " + block,
                ):
                    failed = line.startswith(b"%error")
                    if not self.ready.is_set():
                        self.ready.set()
                    elif self.pending and not self.pending.done():
                        if failed:
                            self.pending.set_exception(
                                TerminalError(
                                    "VIEW_UNAVAILABLE",
                                    "tmux refused the read-only screen capture.",
                                )
                            )
                        else:
                            self.pending.set_result((b"\n".join(content), self.offset))
                    block = None
                elif block is not None:
                    content.append(line)
                elif line.startswith(
                    b"%output " + self.target["pane_id"].encode() + b" "
                ):
                    payload = decode_output(line.split(b" ", 2)[2])
                    self.chunks.append((self.offset, payload))
                    self.offset += len(payload)
                    while self.chunks and self.offset - self.chunks[0][0] > 1048576:
                        start, old = self.chunks.popleft()
                        self.base = start + len(old)
                elif line.startswith(b"%exit"):
                    break
        finally:
            self.closed = True
            self.ready.set()
            if self.pending and not self.pending.done():
                self.pending.set_exception(
                    TerminalError(
                        "VIEW_DISCONNECTED",
                        "The terminal viewer disconnected. Reopen to resync; input is never replayed.",
                    )
                )

    async def _command(self, args):
        async with self.command_lock:
            if self.closed:
                raise TerminalError(
                    "VIEW_DISCONNECTED", "The terminal viewer is disconnected."
                )
            self.pending = asyncio.get_running_loop().create_future()
            self.process.stdin.write((shlex.join(args) + "\n").encode())
            await self.process.stdin.drain()
            try:
                return await asyncio.wait_for(self.pending, 5)
            finally:
                self.pending = None

    async def read(self, cursor):
        self.last_read = time.monotonic()
        start = None
        if cursor != "start":
            match = re.fullmatch(r"([a-f0-9]{32})-(\d{1,20})", cursor)
            if not match:
                raise TerminalError(
                    "INVALID_CURSOR",
                    "Use the next_cursor returned by this terminal resource.",
                )
            if match[1] == self.identity:
                start = int(match[2])
        reset = start is None or start < self.base or start > self.offset
        geometry = (
            (
                await self.library._guarded(
                    self.target,
                    [
                        "display-message",
                        "-p",
                        "-t",
                        self.target["pane_id"],
                        "#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}|#{cursor_flag}|#{alternate_on}",
                    ],
                )
            )
            .strip()
            .split("|")
        )
        if len(geometry) != 6 or not all(v.isdecimal() for v in geometry):
            raise TerminalError("INVALID_SNAPSHOT", "Terminal geometry is unavailable.")
        cols, rows, x, y, cursor_visible, alternate = map(int, geometry)
        if not (1 <= cols <= 1000 and 1 <= rows <= 500):
            raise TerminalError(
                "VIEW_GEOMETRY_LIMIT",
                "This pane exceeds the viewer limit of 1000 columns by 500 rows. Explicitly resize its tmux window before opening the view.",
            )
        truncated = False
        if reset:
            screen, start = await self._command(
                ["capture-pane", "-p", "-e", "-N", "-t", self.target["pane_id"]]
            )
            # A reset is visible-screen reconstruction, not a fabricated replay
            # of historical bytes or a claim to restore every emulator mode.
            truncated = len(screen) > 262144
            screen = screen[:262144].replace(b"\n", b"\r\n")
            data = (
                b"\x1bc"
                + screen
                + f"\x1b[{y + 1};{x + 1}H\x1b[?25{'h' if cursor_visible else 'l'}".encode()
            )
        else:
            data = b"".join(
                payload[max(0, start - position) :]
                for position, payload in self.chunks
                if position + len(payload) > start
            )[:32768]
            start += len(data)
        return {
            "stream_id": self.identity,
            "target_id": self.target["id"],
            "reset": reset,
            "lost_history": cursor != "start" and reset,
            "data": base64.b64encode(data).decode(),
            "next_cursor": f"{self.identity}-{start}",
            "cols": cols,
            "rows": rows,
            "alternate": bool(alternate),
            "truncated": truncated,
            "poll_after_ms": 150 if data else 400,
            "reconstruction": "visible ANSI screen and cursor; private emulator modes and prior scrollback are not reconstructed"
            if reset
            else None,
        }

    async def close(self):
        self.closed = True
        if self.process and self.process.returncode is None:
            # Only this owned attach client; never a process group or tmux server.
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.error_reader):
            if task and task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.reader, self.error_reader) if task),
            return_exceptions=True,
        )
