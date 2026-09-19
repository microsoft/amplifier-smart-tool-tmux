"""Socket-bound collaborative terminal library; no host, web, or model imports."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from tmux_kit import proc, spawn
from tmux_kit.keys import ALLOWED_KEYS
from tmux_kit.names import is_valid_session_name

from .socket_resolution import resolve


class TerminalError(ValueError):
    def __init__(
        self,
        code,
        message,
        remedy="Refresh the fleet and inspect the receipt before trying a new action.",
    ):
        super().__init__(message)
        self.code, self.message, self.remedy = code, message, remedy

    def as_dict(self):
        return {"code": self.code, "message": self.message, "remedy": self.remedy}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def bounded(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise TerminalError(
            "INVALID_INPUT", f"{name} must be an integer from {low} to {high}."
        )
    return value


class TerminalFleet:
    """One explicit socket, private retained state, and separately granted effects.

    ``allow_input`` and ``allow_management`` are trusted host configuration, never
    writable through a model/tool call. All identity and action checks live here.
    """

    def __init__(
        self,
        storage,
        *,
        socket_dir=None,
        socket_name=None,
        allow_input=False,
        allow_management=False,
    ):
        self.scope = resolve(socket_dir, socket_name=socket_name)
        self.socket = self.scope.server_socket_path
        self.env = proc.tmux_env(self.scope.socket_dir)
        self.allow_input, self.allow_management = (
            bool(allow_input),
            bool(allow_management),
        )
        self.storage = Path(storage).expanduser().resolve()
        self.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.storage, 0o700)
        self.db = sqlite3.connect(
            self.storage / "terminals.sqlite3", isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id));
          CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, digest TEXT NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS operations_status ON operations(json_extract(data, '$.status'));
        """)
        for path in self.storage.glob("terminals.sqlite3*"):
            os.chmod(path, 0o600)
        self.lock_path = self.storage / "operations.lock"
        self.backend_id = uuid.uuid4().hex
        self.streams = {}
        self.stream_lock = asyncio.Lock()
        self.sweeper = None
        self._recover()

    def _get(self, kind, identity):
        row = self.db.execute(
            "SELECT data FROM records WHERE kind=? AND id=?", (kind, identity)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, kind, identity, value):
        self.db.execute(
            "INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
            (kind, identity, canonical(value)),
        )

    def _all(self, kind):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT data FROM records WHERE kind=?", (kind,))
        ]

    def _recover(self):
        with self.lock_path.open("a") as lock:
            os.chmod(self.lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            for row in self.db.execute(
                "SELECT id,data FROM operations WHERE json_extract(data, '$.status')='running'"
            ).fetchall():
                receipt = json.loads(row["data"])
                if receipt["status"] == "running":
                    receipt.update(
                        status="unknown",
                        error={
                            "code": "INTERRUPTED",
                            "message": "The previous process stopped before recording an outcome. Input/work is never replayed.",
                        },
                    )
                    self.db.execute(
                        "UPDATE operations SET data=? WHERE id=?",
                        (canonical(receipt), row["id"]),
                    )

    @asynccontextmanager
    async def _lock(self):
        with self.lock_path.open("a") as lock:
            until = time.monotonic() + 15
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > until:
                        raise TerminalError(
                            "BUSY", "Another terminal action is still being recorded."
                        )
                    await asyncio.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def _run(self, *args, input_bytes=None):
        return await asyncio.wait_for(
            proc.run_tmux(
                "-S",
                self.socket,
                *map(str, args),
                env=self.env,
                input_bytes=input_bytes,
            ),
            15,
        )

    def _socket_info(self):
        info = Path(self.socket).stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise TerminalError(
                "SOCKET_SCOPE",
                "The configured tmux socket is not owned by the current account.",
            )
        return info

    def _server_identity(self, info, values):
        if len(values) != 2 or not all(value.isdecimal() for value in values):
            raise ValueError("Invalid server identity")
        stamp = f"{self.socket}:{info.st_dev}:{info.st_ino}:{values}"
        return {
            "id": hashlib.sha256(stamp.encode()).hexdigest(),
            "pid": values[0],
            "started": values[1],
            "socket_identity": [info.st_dev, info.st_ino],
        }

    async def _epoch(self):
        try:
            info = self._socket_info()
            values = (
                (await self._run("display-message", "-p", "#{pid}|#{start_time}"))
                .strip()
                .split("|")
            )
            return self._server_identity(info, values)
        except (OSError, RuntimeError) as error:
            raise TerminalError(
                "SERVER_UNAVAILABLE",
                "No accessible tmux server is running on the configured socket.",
                "Check the explicit socket settings, or create a session with management permission.",
            ) from error

    async def fleet(self):
        """Discover sessions/windows/panes on the configured socket, with exact targets."""
        try:
            epoch = await self._epoch()
        except TerminalError as error:
            if error.code != "SERVER_UNAVAILABLE":
                raise
            return {
                "socket": self.socket,
                "server": None,
                "panes": [],
                "available": False,
                "detail": error.message,
            }
        raw = await self._run(
            "list-panes",
            "-a",
            "-F",
            "#{session_id}|#{window_id}|#{pane_id}|#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}|#{alternate_on}|#{pane_dead}",
        )
        rows, names = [], {}
        for line in raw.splitlines():
            fields = line.split("|")
            if (
                len(fields) != 9
                or not re.fullmatch(r"\$\d+", fields[0])
                or not re.fullmatch(r"@\d+", fields[1])
                or not re.fullmatch(r"%\d+", fields[2])
                or not all(value.isdecimal() for value in fields[3:])
            ):
                raise TerminalError(
                    "INVALID_SNAPSHOT", "tmux returned an unrecognized pane identity."
                )
            session, window, pane = fields[:3]
            for identity, formatter in [
                (session, "session_name"),
                (window, "window_name"),
            ]:
                if identity not in names:
                    names[identity] = (
                        await self._run(
                            "display-message", "-p", "-t", pane, f"#{{{formatter}}}"
                        )
                    ).rstrip("\n")
            identity = hashlib.sha256(
                f"{epoch['id']}:{session}:{window}:{pane}".encode()
            ).hexdigest()[:32]
            row = dict(
                zip(
                    ("cols", "rows", "cursor_x", "cursor_y", "alternate", "dead"),
                    map(int, fields[3:]),
                )
            )
            row.update(
                id=identity,
                server=epoch,
                session_id=session,
                window_id=window,
                pane_id=pane,
                session=names[session],
                window=names[window],
                socket=self.socket,
            )
            self._put("target", identity, row)
            rows.append(row)
        if (await self._epoch()) != epoch:
            raise TerminalError(
                "STALE_TARGET", "The tmux server changed during discovery."
            )
        return {
            "socket": self.socket,
            "server": epoch,
            "panes": rows,
            "available": True,
            "scope": "Only this configured socket; ambient TMUX/TMUX_TMPDIR are ignored.",
        }

    def _saved_target(self, target_id):
        row = self._get("target", target_id)
        if not row or row["socket"] != self.socket:
            raise TerminalError(
                "UNKNOWN_TARGET", "Choose a pane from this socket’s fleet first."
            )
        return row

    async def _target(self, target_id):
        row = self._saved_target(target_id)
        try:
            info = self._socket_info()
            current = (
                await self._run(
                    "display-message",
                    "-p",
                    "-t",
                    row["pane_id"],
                    "#{pid}|#{start_time}|#{session_id}|#{window_id}|#{pane_id}",
                )
            ).strip().split("|")
        except (OSError, RuntimeError) as error:
            raise TerminalError(
                "STALE_TARGET", "The exact pane is no longer available."
            ) from error
        if self._server_identity(info, current[:2])["id"] != row["server"]["id"]:
            raise TerminalError(
                "STALE_TARGET",
                "This pane belongs to an earlier tmux server incarnation.",
            )
        if "|".join(current[2:]) != "|".join(
            row[key] for key in ("session_id", "window_id", "pane_id")
        ):
            raise TerminalError(
                "STALE_TARGET",
                "The pane no longer belongs to its recorded session/window.",
            )
        return row

    async def _guarded(self, row, args):
        # The condition and effect execute on the same server command queue.
        # No name re-resolution between type and Enter, or across server restart.
        epoch = row["server"]
        conditions = [
            f"#{{==:#{{pid}},{epoch['pid']}}}",
            f"#{{==:#{{start_time}},{epoch['started']}}}",
        ]
        for key in ("session_id", "window_id", "pane_id"):
            conditions.append(f"#{{==:#{{{key}}},{row[key]}}}")
        condition = conditions[0]
        for part in conditions[1:]:
            condition = f"#{{&&:{condition},{part}}}"
        value = await self._run(
            "if-shell",
            "-F",
            "-t",
            row["pane_id"],
            condition,
            shlex.join(list(map(str, args))),
            "display-message -p TMUX_FLEET_STALE_TARGET",
        )
        if value.strip() == "TMUX_FLEET_STALE_TARGET":
            raise TerminalError(
                "STALE_TARGET", "The exact pane changed before the operation could run."
            )
        return value

    def _attachment_available(self, attachment_id, target_id):
        attachment = self._get("attachment", attachment_id)
        return bool(
            attachment
            and not attachment.get("closed")
            and attachment["target_id"] == target_id
        )

    def _grant_available(self, grant, charge=1):
        if not grant or grant.get("revoked") or not self.allow_input:
            return False
        if grant.get("scope") == "attachment":
            return (
                grant.get("backend_id") == self.backend_id
                and self._attachment_available(
                    grant["attachment_id"], grant["target_id"]
                )
            )
        return grant["expires_at"] > time.time() and grant["remaining_bytes"] >= charge

    async def capture(self, target_id, lines=200):
        """Read bounded text for reasoning. Observations are never input authority."""
        bounded(lines, 0, 2000, "lines")
        row = await self._target(target_id)
        text = await self._guarded(
            row, ["capture-pane", "-p", "-t", row["pane_id"], "-S", str(-lines)]
        )
        encoded = text.encode()
        truncated = len(encoded) > 65536
        return {
            "target": row,
            "text": encoded[-65536:].decode(errors="replace"),
            "truncated": truncated,
            "history_lines_requested": lines,
            "complete_history": False,
        }

    def _unresolved_inputs(self, target_id=None):
        query = """SELECT data FROM operations
                   WHERE json_extract(data, '$.status')='unknown'
                   AND json_extract(data, '$.action')='input'
                   AND json_extract(data, '$.reviewed_at') IS NULL"""
        args = ()
        if target_id is not None:
            query += " AND json_extract(data, '$.target_id')=?"
            args = (target_id,)
        return [json.loads(row[0]) for row in self.db.execute(query, args)]

    async def state(self):
        """Read retained shared selection/drafts/grants and recent receipts, without output bytes."""
        self._recover()
        view = self._get("view", "shared") or {
            "version": 0,
            "target_id": None,
            "draft": "",
            "follow": True,
        }
        receipts = [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT data FROM operations ORDER BY rowid DESC LIMIT 30"
            )
        ]
        grants, live_targets = [], {}
        for grant in self._all("grant"):
            active = self._grant_available(grant)
            target_id = grant["target_id"]
            if active and target_id not in live_targets:
                try:
                    await self._target(target_id)
                    live_targets[target_id] = True
                except TerminalError:
                    live_targets[target_id] = False
            grants.append(
                {**grant, "active": active and live_targets.get(target_id, False)}
            )
        return {
            "socket": self.socket,
            "allow_input": self.allow_input,
            "allow_management": self.allow_management,
            "view": view,
            "grants": grants,
            "operations": receipts,
            "unresolved_inputs": self._unresolved_inputs(),
            "draft_is_authority": False,
            "model_usage": {"calls": 0, "cost": 0},
        }

    async def save_view(self, target_id, draft="", follow=True, expected_version=0):
        """Persist shared selection and draft with compare-and-swap. Never type it."""
        if target_id is not None:
            await self._target(target_id)
        if not isinstance(draft, str) or len(draft.encode()) > 65536:
            raise TerminalError("INVALID_INPUT", "Draft is limited to 64 KiB.")
        if type(follow) is not bool:
            raise TerminalError("INVALID_INPUT", "follow must be boolean.")
        async with self._lock():
            view = self._get("view", "shared") or {"version": 0}
            if view["version"] != expected_version:
                raise TerminalError(
                    "VIEW_CONFLICT",
                    "The shared view changed. Reload it before replacing another user’s or agent’s draft.",
                )
            view = {
                "version": expected_version + 1,
                "target_id": target_id,
                "draft": draft,
                "follow": follow,
            }
            self._put("view", "shared", view)
            return view

    async def operation(self, request_id):
        """Inspect a retained receipt; never retry input to discover its outcome."""
        self._recover()
        row = self.db.execute(
            "SELECT data FROM operations WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            raise TerminalError(
                "UNKNOWN_OPERATION", "No receipt exists for that request ID."
            )
        return json.loads(row[0])

    async def acknowledge_operation(self, request_id, confirmed=False):
        """Mark an unknown outcome reviewed; never change the outcome or replay it."""
        if confirmed is not True:
            raise TerminalError(
                "CONFIRMATION_REQUIRED",
                "Inspect the terminal and explicitly confirm that the uncertain outcome was reviewed.",
            )
        async with self._lock():
            row = self.db.execute(
                "SELECT data FROM operations WHERE id=?", (request_id,)
            ).fetchone()
            if not row:
                raise TerminalError("UNKNOWN_OPERATION", "No such request receipt.")
            receipt = json.loads(row[0])
            if receipt["status"] != "unknown":
                raise TerminalError(
                    "INVALID_INPUT", "Only unknown outcomes need this acknowledgement."
                )
            receipt["reviewed_at"] = time.time()
            self.db.execute(
                "UPDATE operations SET data=? WHERE id=?",
                (canonical(receipt), request_id),
            )
            return receipt

    async def _operate(self, action, request_id, payload, work):
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{16,100}", request_id
        ):
            raise TerminalError(
                "INVALID_INPUT",
                "Use a stable request ID of 16–100 letters, digits, underscores or hyphens.",
            )
        digest = hashlib.sha256(
            canonical(
                {"action": action, "socket": self.socket, "args": payload}
            ).encode()
        ).hexdigest()
        async with self._lock():
            existing = self.db.execute(
                "SELECT digest,data FROM operations WHERE id=?", (request_id,)
            ).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise TerminalError(
                        "REQUEST_CONFLICT",
                        "This request ID already names a different action or payload.",
                    )
                result = json.loads(existing["data"])
                if result["status"] == "running":
                    result.update(
                        status="unknown",
                        error={
                            "code": "INTERRUPTED",
                            "message": "Outcome was not recorded. Do not replay this input.",
                        },
                    )
                    self.db.execute(
                        "UPDATE operations SET data=? WHERE id=?",
                        (canonical(result), request_id),
                    )
                return result
            receipt = {
                "request_id": request_id,
                "action": action,
                "target_id": payload.get("target_id"),
                "status": "running",
                "created_at": time.time(),
                "actor_reported": payload.get("actor", "unspecified"),
                "input_logged": False,
            }
            self.db.execute(
                "INSERT INTO operations VALUES(?,?,?)",
                (request_id, digest, canonical(receipt)),
            )
            dispatched = False

            async def effect(row, args=None, spawn_operation=None, **kwargs):
                nonlocal dispatched
                dispatched = True
                if spawn_operation:
                    return await spawn_operation()
                return await (
                    self._guarded(row, args) if row else self._run(*args, **kwargs)
                )

            try:
                result = await work(effect)
                receipt.update(status="succeeded", result=result)
            except TerminalError as error:
                receipt.update(
                    status="unknown" if dispatched else "refused", error=error.as_dict()
                )
            except (Exception, asyncio.CancelledError) as error:
                receipt.update(
                    status="unknown" if dispatched else "refused",
                    error={
                        "code": "OUTCOME_UNKNOWN" if dispatched else "FAILED",
                        "message": "The terminal action did not produce a verified receipt. Inspect current state; it will not be replayed.",
                        "remedy": "Read this receipt and the exact target before choosing a new action.",
                    },
                )
                if isinstance(error, asyncio.CancelledError):
                    self.db.execute(
                        "UPDATE operations SET data=? WHERE id=?",
                        (canonical(receipt), request_id),
                    )
                    raise
            receipt["finished_at"] = time.time()
            self.db.execute(
                "UPDATE operations SET data=? WHERE id=?",
                (canonical(receipt), request_id),
            )
            return receipt

    async def authorize_input(
        self,
        target_id,
        request_id,
        confirmed=False,
        seconds=300,
        max_bytes=32768,
        actor="unspecified",
        attachment_id=None,
    ):
        """Confirm input authority for one exact pane.

        With attachment_id, authority lasts until detach, revoke or backend restart;
        expires_at and remaining_bytes are null. Otherwise seconds/max_bytes bound
        the grant. Confirmation is caller-reported under the host's input policy.
        """

        async def work(effect):
            if not self.allow_input or confirmed is not True:
                raise TerminalError(
                    "INPUT_AUTHORITY_REQUIRED",
                    "Host input permission and explicit confirmation are required.",
                )
            await self._target(target_id)
            bounded(seconds, 1, 900, "seconds")
            bounded(max_bytes, 1, 65536, "max_bytes")
            if attachment_id is not None:
                stream = self.streams.get(attachment_id)
                if (
                    not self._attachment_available(attachment_id, target_id)
                    or not stream
                    or stream.closed
                ):
                    raise TerminalError(
                        "ATTACHMENT_CLOSED",
                        "Open a live viewer for this exact pane before enabling attachment typing.",
                    )
            grant = {
                "id": uuid.uuid4().hex,
                "target_id": target_id,
                "scope": "attachment" if attachment_id is not None else "bounded",
                "expires_at": None
                if attachment_id is not None
                else time.time() + seconds,
                "remaining_bytes": None if attachment_id is not None else max_bytes,
                "actor_reported": actor,
            }
            if attachment_id is not None:
                grant.update(attachment_id=attachment_id, backend_id=self.backend_id)
            self._put("grant", grant["id"], grant)
            return grant

        payload = locals_without(locals(), "self", "work")
        if attachment_id is None:
            payload.pop("attachment_id")
        return await self._operate("authorize_input", request_id, payload, work)

    async def revoke_input(self, grant_id):
        """Revoke a retained input grant immediately; no terminal effect."""
        async with self._lock():
            grant = self._get("grant", grant_id)
            if not grant:
                raise TerminalError("UNKNOWN_GRANT", "No such input grant.")
            grant["revoked"] = True
            self._put("grant", grant_id, grant)
            return grant

    async def input(
        self,
        target_id,
        request_id,
        kind,
        value,
        submit=False,
        grant_id=None,
        confirmed=False,
        actor="unspecified",
    ):
        """Send literal text, multiline paste, a named key, or base64 terminal bytes.

        Each call requires an exact-target grant or explicit per-call confirmation.
        Delivery is not command completion. Unknown receipts must never be replayed.
        """

        async def work(effect):
            import base64

            if not self.allow_input:
                raise TerminalError(
                    "INPUT_AUTHORITY_REQUIRED",
                    "This host has not enabled terminal input.",
                )
            row = await self._target(target_id)
            previous = self._unresolved_inputs(target_id)
            if previous:
                raise TerminalError(
                    "UNCERTAIN_INPUT",
                    f"Review unknown receipt {previous[0]['request_id']} and inspect the terminal before new input.",
                )
            if not isinstance(value, str) or type(submit) is not bool:
                raise TerminalError(
                    "INVALID_INPUT",
                    "Input value must be a string and submit must be boolean.",
                )
            data = value.encode()
            if kind == "bytes":
                try:
                    data = base64.b64decode(value, validate=True)
                except ValueError as error:
                    raise TerminalError(
                        "INVALID_INPUT", "Terminal bytes must be valid base64."
                    ) from error
                if submit:
                    raise TerminalError(
                        "INVALID_INPUT",
                        "Raw bytes do not add Enter; include the intended bytes explicitly.",
                    )
            elif kind == "text":
                if "\n" in value or "\r" in value or "\0" in value:
                    raise TerminalError(
                        "INVALID_INPUT",
                        "Literal text cannot contain CR, LF or NUL. Use paste for multiline text.",
                    )
            elif kind == "keys":
                if value not in ALLOWED_KEYS or submit:
                    raise TerminalError(
                        "INVALID_INPUT",
                        "Choose an allowed key without an additional Enter.",
                    )
            elif kind != "paste":
                raise TerminalError(
                    "INVALID_INPUT", "kind must be text, paste, keys or bytes."
                )
            bounded(len(data), 1, 4096 if kind == "bytes" else 65536, "input bytes")
            charge = len(data) + int(submit)
            if grant_id:
                grant = self._get("grant", grant_id)
                if (
                    not self._grant_available(grant, charge)
                    or grant["target_id"] != target_id
                ):
                    raise TerminalError(
                        "GRANT_EXPIRED",
                        "The exact-pane input grant is unavailable, detached, expired or exhausted.",
                    )
                if grant["remaining_bytes"] is not None:
                    grant["remaining_bytes"] -= charge
                    self._put("grant", grant_id, grant)
            elif confirmed is not True:
                raise TerminalError(
                    "INPUT_AUTHORITY_REQUIRED",
                    "Confirm this input or obtain an exact-pane input grant.",
                )
            if kind == "paste":
                buffer = "tmux-fleet-" + uuid.uuid4().hex
                try:
                    await effect(
                        None, ["load-buffer", "-b", buffer, "-"], input_bytes=data
                    )
                    await effect(
                        row,
                        [
                            "paste-buffer",
                            "-d",
                            "-p",
                            "-b",
                            buffer,
                            "-t",
                            row["pane_id"],
                        ],
                    )
                finally:
                    try:
                        await self._run("delete-buffer", "-b", buffer)
                    except RuntimeError:
                        pass
            elif kind == "bytes":
                await effect(
                    row,
                    [
                        "send-keys",
                        "-t",
                        row["pane_id"],
                        "-H",
                        *[f"{value:02x}" for value in data],
                    ],
                )
            else:
                await effect(
                    row,
                    [
                        "send-keys",
                        "-t",
                        row["pane_id"],
                        *(["-l", "--", value] if kind == "text" else [value]),
                    ],
                )
            if submit:
                await effect(row, ["send-keys", "-t", row["pane_id"], "Enter"])
            return {
                "target_id": target_id,
                "bytes": charge,
                "delivery": "submitted"
                if submit or kind == "keys" and value == "Enter"
                else "delivered",
                "command_success": None,
            }

        return await self._operate(
            "input", request_id, locals_without(locals(), "self", "work"), work
        )

    async def manage(
        self,
        request_id,
        action,
        target_id=None,
        name=None,
        cwd=None,
        cols=80,
        rows=24,
        confirmed=False,
        actor="unspecified",
    ):
        """Explicit create/session/window/split/rename/close/resize management.

        Closing a view never invokes this. Resize changes geometry shared by other clients.
        """

        async def work(effect):
            if not self.allow_management or confirmed is not True:
                raise TerminalError(
                    "MANAGEMENT_AUTHORITY_REQUIRED",
                    "Host management permission and per-action confirmation are required.",
                )
            allowed = {
                "create_session",
                "create_window",
                "split_horizontal",
                "split_vertical",
                "rename_session",
                "rename_window",
                "close_pane",
                "close_window",
                "close_session",
                "resize_window",
            }
            if action not in allowed:
                raise TerminalError("INVALID_INPUT", "Unknown management action.")
            if action in {
                "create_session",
                "create_window",
                "rename_session",
                "rename_window",
            } and (not isinstance(name, str) or not is_valid_session_name(name)):
                raise TerminalError(
                    "INVALID_INPUT",
                    "Use a short name containing letters, numbers, underscores or hyphens.",
                )
            if cwd is not None and (
                not Path(cwd).is_absolute() or not Path(cwd).is_dir()
            ):
                raise TerminalError(
                    "INVALID_INPUT",
                    "Working directory must be an existing absolute directory.",
                )
            bounded(cols, 20, 300, "cols")
            bounded(rows, 5, 150, "rows")
            if action == "create_session":
                if target_id is not None:
                    raise TerminalError(
                        "INVALID_INPUT",
                        "New sessions do not take an existing pane target.",
                    )
                # tmux-kit owns cgroup-aware spawning. The generated template is
                # argv-quoted, never caller-supplied shell text, and carries -S.
                fleet = await self.fleet()
                if any(p["session"] == name for p in fleet["panes"]):
                    raise TerminalError(
                        "NAME_EXISTS", "A session already uses that name."
                    )
                recent = [
                    json.loads(r[0])
                    for r in self.db.execute("SELECT data FROM operations")
                ]
                if (
                    sum(
                        r["action"] == "manage"
                        and r.get("result", {}).get("action") == "create_session"
                        and r["created_at"] > time.time() - 600
                        for r in recent
                    )
                    >= 10
                ):
                    raise TerminalError(
                        "RATE_LIMIT",
                        "At most ten new sessions may be created in ten minutes.",
                    )
                args = [
                    "new-session",
                    "-d",
                    "-P",
                    "-F",
                    "#{session_id}|#{window_id}|#{pane_id}",
                    "-s",
                    name,
                    "-x",
                    str(cols),
                    "-y",
                    str(rows),
                    *(["-c", cwd] if cwd else []),
                ]
                if fleet["available"]:
                    epoch = fleet["server"]
                    condition = f"#{{&&:#{{==:#{{pid}},{epoch['pid']}}},#{{==:#{{start_time}},{epoch['started']}}}}}"
                    args = [
                        "if-shell",
                        "-F",
                        condition,
                        shlex.join(args),
                        "display-message -p STALE_SERVER",
                    ]
                # A private positive creation marker prevents tmux-kit's useful
                # existing-session fallback from being mistaken for our creation.
                with tempfile.TemporaryDirectory(
                    dir=self.storage, prefix="create-"
                ) as folder:
                    marker = Path(folder) / "created"
                    template = (
                        shlex.join(["tmux", "-S", self.socket, *args])
                        + " > "
                        + shlex.quote(str(marker))
                    )
                    ok, _error = await effect(
                        None,
                        spawn_operation=lambda: spawn.spawn_session(
                            name, template, env=self.env
                        ),
                    )
                    created = marker.read_text().strip() if marker.exists() else ""
                    if not ok or not re.fullmatch(r"\$\d+\|@\d+\|%\d+", created):
                        raise TerminalError(
                            "CREATE_UNCERTAIN",
                            "Session creation did not verify successfully; inspect the fleet before new work.",
                        )
            else:
                row = await self._target(target_id)
                commands = {
                    "create_window": [
                        "new-window",
                        "-d",
                        "-t",
                        row["session_id"],
                        "-n",
                        name,
                    ],
                    "split_horizontal": [
                        "split-window",
                        "-d",
                        "-h",
                        "-t",
                        row["pane_id"],
                    ],
                    "split_vertical": [
                        "split-window",
                        "-d",
                        "-v",
                        "-t",
                        row["pane_id"],
                    ],
                    "rename_session": ["rename-session", "-t", row["session_id"], name],
                    "rename_window": ["rename-window", "-t", row["window_id"], name],
                    "close_pane": ["kill-pane", "-t", row["pane_id"]],
                    "close_window": ["kill-window", "-t", row["window_id"]],
                    "close_session": ["kill-session", "-t", row["session_id"]],
                    "resize_window": [
                        "resize-window",
                        "-t",
                        row["window_id"],
                        "-x",
                        str(cols),
                        "-y",
                        str(rows),
                    ],
                }
                args = commands[action]
                if cwd and action in {
                    "create_window",
                    "split_horizontal",
                    "split_vertical",
                }:
                    args += ["-c", cwd]
                await effect(row, args)
            return {"action": action, "fleet": await self.fleet()}

        return await self._operate(
            "manage", request_id, locals_without(locals(), "self", "work"), work
        )

    async def _open_stream(self, identity, row, *, retained=False):
        """Serialize reconnect/open so retained resource handles share the limit."""
        async with self.stream_lock:
            if retained:
                attachment = self._get("attachment", identity)
                if not attachment or attachment.get("closed"):
                    raise TerminalError(
                        "ATTACHMENT_CLOSED", "This viewer was detached."
                    )
            stream = self.streams.get(identity)
            if stream and not stream.closed:
                return stream
            for key, old in list(self.streams.items()):
                if old.closed or time.monotonic() - old.last_read > 60:
                    await old.close()
                    self.streams.pop(key, None)
            if len(self.streams) >= 16:
                raise TerminalError(
                    "VIEW_LIMIT",
                    "Detach an existing view before opening more than sixteen terminal viewers.",
                )
            from .terminal_stream import ControlStream

            stream = ControlStream(self, row)
            await stream.start()
            self.streams[identity] = stream
            if self.sweeper is None:
                self.sweeper = asyncio.create_task(self._sweep())
            return stream

    async def attach(self, target_id):
        """Open an owned read-only viewer; never resize/focus/type into tmux."""
        row = await self._target(target_id)
        identity = uuid.uuid4().hex
        await self._open_stream(identity, row)
        self._put(
            "attachment",
            identity,
            {"id": identity, "target_id": target_id, "closed": False},
        )
        return {
            "attachment_id": identity,
            "target_id": target_id,
            "resource": "tmux-fleet://terminal/" + identity + "/start",
            "read_only": True,
        }

    async def output(self, attachment_id, cursor="start"):
        """Bounded, non-consuming terminal resource; no raw output in model context."""
        attachment = self._get("attachment", attachment_id)
        if not attachment or attachment.get("closed"):
            raise TerminalError(
                "ATTACHMENT_CLOSED",
                "This viewer was detached. Open a new viewer; no input is replayed.",
            )
        row = self._saved_target(attachment["target_id"])
        stream = self.streams.get(attachment_id)
        socket_identity = row["server"].get("socket_identity")
        if stream and not stream.closed and socket_identity is not None:
            try:
                info = self._socket_info()
                same_socket = socket_identity == [info.st_dev, info.st_ino]
            except OSError:
                same_socket = False
            if not same_socket:
                raise TerminalError(
                    "STALE_TARGET",
                    "This pane belongs to an earlier tmux server incarnation.",
                )
            # read() checks server/session/window/pane on the owned connection.
            # Do not launch a tmux subprocess on every resource poll.
        else:
            row = await self._target(attachment["target_id"])
        stream = await self._open_stream(attachment_id, row, retained=True)
        return await stream.read(cursor)

    async def detach(self, attachment_id):
        """Detach only this viewer/helper. Never close tmux work or replay input."""
        async with self._lock():
            attachment = self._get("attachment", attachment_id)
            if not attachment:
                raise TerminalError("UNKNOWN_ATTACHMENT", "No such terminal viewer.")
            attachment["closed"] = True
            self._put("attachment", attachment_id, attachment)
            async with self.stream_lock:
                stream = self.streams.pop(attachment_id, None)
                if stream:
                    await stream.close()
            return attachment

    async def _sweep(self):
        while True:
            await asyncio.sleep(15)
            async with self.stream_lock:
                for identity, stream in list(self.streams.items()):
                    if time.monotonic() - stream.last_read > 60:
                        await stream.close()
                        self.streams.pop(identity, None)

    async def close(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        for stream in self.streams.values():
            await stream.close()
        self.streams.clear()
        self.db.close()


def locals_without(values, *excluded):
    return {key: value for key, value in values.items() if key not in excluded}
