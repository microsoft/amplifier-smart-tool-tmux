"""Container-only scripted HTTP, not an SDK/factory/engine/provider mock.

Based on the muxplex fixture pattern; metadata/count-token requests do not consume
completion ordinals. OS-allocated loopback sockets/tasks belong to each context.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


def require_container():
    assert Path("/.dockerenv").exists() or Path("/run/.containerenv").exists(), (
        "Real SDK integration must run in an isolated container; never silently skip."
    )


def frames(model, text, ordinal):
    yield {"type": "message_start", "message": {
        "id": f"msg_fixture_{ordinal}", "type": "message", "role": "assistant",
        "content": [], "model": model, "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 20, "output_tokens": 0},
    }}
    yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
    yield {"type": "content_block_stop", "index": 0}
    yield {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
           "usage": {"output_tokens": 10}}
    yield {"type": "message_stop"}


@dataclass
class ProviderFixture:
    replies: list[str] = field(default_factory=lambda: ['{"ok":true}'])
    requests: list[dict[str, Any]] = field(default_factory=list)
    metadata: int = 0
    counts: int = 0
    failure: int | None = None
    partial_eof: bool = False
    delay: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    connections: set = field(default_factory=set)
    tasks: set = field(default_factory=set)
    errors: list = field(default_factory=list)

    async def json_response(self, writer, value, status=200):
        encoded = json.dumps(value).encode()
        writer.write(
            f"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode() + encoded
        )
        await writer.drain()

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.connections.add(writer)
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            method, path, _ = header.splitlines()[0].split(" ")
            headers = {k.lower(): v.strip() for k, v in (
                line.split(":", 1) for line in header.splitlines()[1:] if ":" in line
            )}
            # Verify the exact caller-environment credential at the real wire.
            assert headers.get("x-api-key") == "fixture-api-key" or headers.get("authorization") == "Bearer fixture-api-key"
            if method == "GET" and path.startswith("/v1/models"):
                self.metadata += 1
                model = {"id": "claude-sonnet-5-5", "type": "model",
                         "display_name": "Fixture", "created_at": "2026-01-01T00:00:00Z"}
                value = {"data": [model], "has_more": False, "first_id": model["id"], "last_id": model["id"]}
                await self.json_response(writer, value if path.split("?")[0] == "/v1/models" else model)
                return
            body = json.loads(await reader.readexactly(int(headers["content-length"])))
            if path == "/v1/messages/count_tokens":
                self.counts += 1
                await self.json_response(writer, {"input_tokens": 20})
                return
            assert method == "POST" and path in {"/v1/messages", "/v1/chat/completions"}
            self.requests.append(body)
            self.started.set()
            ordinal = len(self.requests)
            text = self.replies[min(ordinal - 1, len(self.replies) - 1)]
            if self.failure:
                await self.json_response(writer, {
                    "error": {"type": "api_error", "message": "fixture-api-key fixture-response-secret synthetic failure"}
                }, self.failure)
                return
            if path == "/v1/chat/completions":
                assert body["stream"] is True
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
                chunks = [
                    {"id": f"chatcmpl-fixture-{ordinal}", "object": "chat.completion.chunk",
                     "created": 1, "model": body["model"],
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                  "finish_reason": None}]},
                    {"id": f"chatcmpl-fixture-{ordinal}", "object": "chat.completion.chunk",
                     "created": 1, "model": body["model"],
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}},
                ]
                for chunk in [*(json.dumps(c) for c in chunks), "[DONE]"]:
                    encoded = ("data: " + chunk + "\n\n").encode()
                    writer.write(f"{len(encoded):x}\r\n".encode() + encoded + b"\r\n")
                    await writer.drain()
                writer.write(b"0\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
            for frame in frames(body["model"], text, ordinal):
                encoded = ("event: " + frame["type"] + "\ndata: " + json.dumps(frame) + "\n\n").encode()
                writer.write(f"{len(encoded):x}\r\n".encode() + encoded + b"\r\n")
                await writer.drain()
                if frame["type"] == "content_block_delta":
                    if self.partial_eof:
                        return
                    if self.delay:
                        # Wait for cancellation to close the connection OR test
                        # release; do not leave an artificially parked server task.
                        eof = asyncio.create_task(reader.read())
                        release = asyncio.create_task(self.release.wait())
                        try:
                            await asyncio.wait({eof, release}, return_when=asyncio.FIRST_COMPLETED)
                        finally:
                            eof.cancel()
                            release.cancel()
                            await asyncio.gather(eof, release, return_exceptions=True)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            self.connections.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.tasks.discard(task)

    @contextlib.asynccontextmanager
    async def running(self):
        require_container()
        server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            self.release.set()
            server.close()
            await server.wait_closed()
            for writer in list(self.connections):
                writer.close()
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert not self.tasks and not self.connections
            assert not self.errors, repr(self.errors)