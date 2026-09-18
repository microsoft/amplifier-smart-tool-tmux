"""Collaborative actions use only tmux-kit's explicitly isolated fixture socket."""

import asyncio
import base64
import uuid
from pathlib import Path

import pytest
from _helpers import fleet, run

from tmux_fleet.terminal import TerminalError, TerminalFleet
from tmux_fleet.terminal_stream import decode_output


def rid():
    return uuid.uuid4().hex


def test_exact_pane_input_receipt_and_no_replay(tmp_path):
    async def check():
        async with fleet("one") as (srv, kw):
            library = TerminalFleet(
                tmp_path, **kw, allow_input=True, allow_management=True
            )
            original = (await library.fleet())["panes"][0]
            await srv.run("split-window", "-d", "-t", original["pane_id"], "cat")
            await srv.run("send-keys", "-t", original["pane_id"], "C-c")
            await asyncio.sleep(0.1)
            key = rid()
            args = {
                "target_id": original["id"],
                "request_id": key,
                "kind": "text",
                "value": 'printf "EXACT_INPUT_MARKER\\n"',
                "submit": True,
                "confirmed": True,
            }
            receipt = await library.input(**args)
            assert receipt["status"] == "succeeded", receipt
            assert await library.input(**args) == receipt
            with pytest.raises(TerminalError, match="different action"):
                await library.input(**{**args, "value": "different"})
            await asyncio.sleep(0.2)
            capture = await library.capture(original["id"])
            assert "EXACT_INPUT_MARKER" in capture["text"]
            second = (await library.fleet())["panes"][1]
            assert (
                "EXACT_INPUT_MARKER"
                not in (await library.capture(second["id"]))["text"]
            )
            assert "EXACT_INPUT_MARKER" not in (
                tmp_path / "terminals.sqlite3"
            ).read_bytes().decode(errors="ignore")
            await library.close()

    run(check())


def test_grants_are_target_expiry_byte_scoped_and_host_permission_cannot_be_changed(
    tmp_path,
):
    async def check():
        async with fleet("one", "two") as (_, kw):
            library = TerminalFleet(tmp_path, **kw, allow_input=True)
            first, second = (await library.fleet())["panes"]
            grant = (
                await library.authorize_input(
                    first["id"], rid(), confirmed=True, max_bytes=3
                )
            )["result"]
            assert (
                await library.input(
                    second["id"], rid(), "text", "x", grant_id=grant["id"]
                )
            )["status"] == "refused"
            assert (
                await library.input(
                    first["id"], rid(), "text", "four", grant_id=grant["id"]
                )
            )["status"] == "refused"
            assert (
                await library.input(
                    first["id"], rid(), "text", "abc", grant_id=grant["id"]
                )
            )["status"] == "succeeded"
            assert (
                await library.input(
                    first["id"], rid(), "text", "x", grant_id=grant["id"]
                )
            )["status"] == "refused"
            revoked = (
                await library.authorize_input(first["id"], rid(), confirmed=True)
            )["result"]
            await library.revoke_input(revoked["id"])
            assert (
                await library.input(
                    first["id"], rid(), "text", "x", grant_id=revoked["id"]
                )
            )["status"] == "refused"
            library.allow_input = False
            assert (await library.authorize_input(first["id"], rid(), confirmed=True))[
                "status"
            ] == "refused"
            await library.close()

    run(check())


def test_unknown_dispatch_is_durable_and_blocks_new_input_until_reviewed(tmp_path):
    async def check():
        async with fleet("one") as (_, kw):
            library = TerminalFleet(tmp_path, **kw, allow_input=True)
            pane = (await library.fleet())["panes"][0]
            calls = []

            async def lost(row, args):
                calls.append(args)
                raise RuntimeError("lost response")

            library._guarded = lost
            args = {
                "target_id": pane["id"],
                "request_id": rid(),
                "kind": "text",
                "value": "uncertain",
                "confirmed": True,
            }
            first = await library.input(**args)
            assert first["status"] == "unknown"
            assert await library.input(**args) == first and len(calls) == 1
            assert (
                await library.input(pane["id"], rid(), "text", "new", confirmed=True)
            )["status"] == "refused"
            await library.close()
            reopened = TerminalFleet(tmp_path, **kw, allow_input=True)
            assert (await reopened.operation(args["request_id"]))["status"] == "unknown"
            await reopened.acknowledge_operation(args["request_id"], confirmed=True)
            assert (
                await reopened.input(pane["id"], rid(), "text", "new", confirmed=True)
            )["status"] == "succeeded"
            await reopened.close()

    run(check())


def test_state_drafts_versioned_and_same_named_replacement_rejected(tmp_path):
    async def check():
        async with fleet("one") as (srv, kw):
            library = TerminalFleet(tmp_path, **kw, allow_input=True)
            pane = (await library.fleet())["panes"][0]
            saved = await library.save_view(pane["id"], "a draft", expected_version=0)
            assert saved["version"] == 1
            with pytest.raises(TerminalError, match="shared view changed"):
                await library.save_view(pane["id"], "lost update", expected_version=0)
            assert (await library.state())["view"]["draft"] == "a draft"
            await srv.run("new-session", "-d", "-s", "keeper")
            await srv.run("kill-session", "-t", pane["session_id"])
            await srv.run("new-session", "-d", "-s", "one")
            receipt = await library.input(
                pane["id"],
                rid(),
                "text",
                "must never reach replacement",
                confirmed=True,
            )
            assert receipt["status"] == "refused"
            replacement = next(
                row
                for row in (await library.fleet())["panes"]
                if row["session"] == "one"
            )
            assert replacement["id"] != pane["id"]
            assert (
                "must never" not in (await library.capture(replacement["id"]))["text"]
            )
            await library.close()

    run(check())


def test_management_create_split_rename_resize_and_view_detach_preserve_work(tmp_path):
    async def check():
        async with fleet("one") as (_srv, kw):
            library = TerminalFleet(tmp_path, **kw, allow_management=True)
            assert (await library.manage(rid(), "create_session", name="new"))[
                "status"
            ] == "refused"
            created = await library.manage(
                rid(), "create_session", name="new", confirmed=True
            )
            assert created["status"] == "succeeded", created
            pane = next(
                p for p in created["result"]["fleet"]["panes"] if p["session"] == "new"
            )
            for action, extra in [
                ("rename_session", {"name": "renamed"}),
                ("create_window", {"name": "another"}),
                ("split_horizontal", {}),
                ("resize_window", {"cols": 100, "rows": 30}),
            ]:
                result = await library.manage(
                    rid(), action, target_id=pane["id"], confirmed=True, **extra
                )
                assert result["status"] == "succeeded", result
            viewer = await library.attach(pane["id"])
            await library.detach(viewer["attachment_id"])
            assert any(p["id"] == pane["id"] for p in (await library.fleet())["panes"])
            await library.close()

    run(check())


def test_stream_cursor_is_non_consuming_bounded_and_disconnect_never_kills_session(
    tmp_path,
):
    async def check():
        async with fleet("one") as (srv, kw):
            library = TerminalFleet(tmp_path, **kw)
            pane = (await library.fleet())["panes"][0]
            viewer = await library.attach(pane["id"])
            first = await library.output(viewer["attachment_id"])
            assert first["reset"]
            stream = library.streams[viewer["attachment_id"]]
            # A real tmux write on the fixture proves actual control-mode output.
            await srv.run("send-keys", "-t", pane["pane_id"], "-l", "UNICODE_λ_😀")
            await asyncio.sleep(0.2)
            a = await library.output(viewer["attachment_id"], first["next_cursor"])
            b = await library.output(viewer["attachment_id"], first["next_cursor"])
            assert a["data"] == b["data"] and base64.b64decode(a["data"])
            assert len(base64.b64decode(a["data"])) <= 32768
            await stream.close()
            reset = await library.output(viewer["attachment_id"], a["next_cursor"])
            assert reset["reset"] and reset["lost_history"]
            await library.close()
            assert "one" in await srv.run("list-sessions", "-F", "#{session_name}")

    run(check())


def test_control_octal_decoding_retains_split_utf8_bytes():
    assert decode_output(rb"a\015\012\134b") == b"a\r\n\\b"
    parts = [decode_output(rb"\360\237"), decode_output(rb"\230\200")]
    assert b"".join(parts).decode() == "😀"


def test_socket_confirmation_accepts_a_canonical_directory_alias(tmp_path):
    from tmux_fleet.socket_resolution import describe, resolve

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    scope = resolve(str(alias), socket_name="fixture")
    result = describe(
        scope, tmux_reported_socket_path=str(Path(scope.server_socket_path).resolve())
    )
    assert result["socket_path_confirmed_by_tmux"] is True


def test_scope_does_not_follow_process_global_tmux_configuration(tmp_path):
    async def check():
        async with fleet("first") as (_, first_kw), fleet("second") as (_, second_kw):
            first = TerminalFleet(tmp_path / "first", **first_kw)
            second = TerminalFleet(tmp_path / "second", **second_kw)
            for _ in range(2):
                a, b = await asyncio.gather(first.fleet(), second.fleet())
                assert {row["session"] for row in a["panes"]} == {"first"}
                assert {row["session"] for row in b["panes"]} == {"second"}
            await first.close()
            await second.close()

    run(check())


def test_management_close_and_expired_grant_never_retarget(tmp_path):
    async def check():
        async with fleet("original", "keeper") as (srv, kw):
            library = TerminalFleet(
                tmp_path, **kw, allow_input=True, allow_management=True
            )
            original = next(
                p
                for p in (await library.fleet())["panes"]
                if p["session"] == "original"
            )
            grant = (
                await library.authorize_input(
                    original["id"], rid(), confirmed=True, seconds=1
                )
            )["result"]
            await asyncio.sleep(1.05)
            assert (
                await library.input(
                    original["id"], rid(), "text", "expired", grant_id=grant["id"]
                )
            )["status"] == "refused"
            closed = await library.manage(
                rid(), "close_session", target_id=original["id"], confirmed=True
            )
            assert closed["status"] == "succeeded"
            assert {p["session"] for p in closed["result"]["fleet"]["panes"]} == {
                "keeper"
            }
            await srv.run("new-session", "-d", "-s", "original")
            assert (
                await library.input(
                    original["id"], rid(), "text", "do not redirect", confirmed=True
                )
            )["status"] == "refused"
            await library.close()

    run(check())


def test_stream_filters_other_panes_and_rejects_foreign_resource_handles(tmp_path):
    async def check():
        async with fleet("one") as (srv, kw):
            library = TerminalFleet(tmp_path, **kw)
            pane = (await library.fleet())["panes"][0]
            await srv.run("split-window", "-d", "-t", pane["pane_id"], "cat")
            second = next(
                p for p in (await library.fleet())["panes"] if p["id"] != pane["id"]
            )
            viewer = await library.attach(pane["id"])
            await asyncio.sleep(0.3)
            screen = await library.output(viewer["attachment_id"])
            await srv.run(
                "send-keys", "-t", second["pane_id"], "-l", "OTHER_PANE_PRIVATE"
            )
            await asyncio.sleep(0.1)
            frame = await library.output(viewer["attachment_id"], screen["next_cursor"])
            assert b"OTHER_PANE_PRIVATE" not in base64.b64decode(frame["data"])
            with pytest.raises(TerminalError, match="detached"):
                await library.output("f" * 32)
            await library.close()

    run(check())


def test_type_and_enter_are_not_replayed_after_a_lost_reply(tmp_path):
    async def check():
        async with fleet("one") as (srv, kw):
            library = TerminalFleet(tmp_path, **kw, allow_input=True)
            pane = (await library.fleet())["panes"][0]
            await srv.run("respawn-pane", "-k", "-t", pane["pane_id"], "/bin/sh")
            await asyncio.sleep(0.1)
            sink = tmp_path / "effects"
            import shlex

            args = {
                "target_id": pane["id"],
                "request_id": rid(),
                "kind": "text",
                "value": "printf x >> " + shlex.quote(str(sink)),
                "submit": True,
                "confirmed": True,
            }
            original = library._guarded

            async def lost_after_enter(row, argv):
                result = await original(row, argv)
                if argv[-1] == "Enter":
                    raise RuntimeError("reply lost after Enter reached tmux")
                return result

            library._guarded = lost_after_enter
            receipt = await library.input(**args)
            assert receipt["status"] == "unknown"
            for _ in range(20):
                if sink.exists():
                    break
                await asyncio.sleep(0.05)
            assert sink.read_text() == "x"
            assert await library.input(**args) == receipt
            await library.close()
            reopened = TerminalFleet(tmp_path, **kw, allow_input=True)
            assert await reopened.input(**args) == receipt
            await asyncio.sleep(0.1)
            assert sink.read_text() == "x"
            await reopened.close()

    run(check())


def test_display_names_are_untrusted_labels_not_command_targets(tmp_path):
    async def check():
        async with fleet("one") as (srv, kw):
            library = TerminalFleet(tmp_path, **kw, allow_input=True)
            pane = (await library.fleet())["panes"][0]
            label = "odd 'double\";$(printf BAD) # {name}"
            await srv.run("rename-session", "-t", pane["session_id"], label)
            await srv.run("rename-window", "-t", pane["window_id"], label)
            fresh = (await library.fleet())["panes"][0]
            assert fresh["id"] == pane["id"]
            assert fresh["session"] == fresh["window"] == label
            await srv.run("respawn-pane", "-k", "-t", pane["pane_id"], "cat")
            value = "literal 'double\";$(printf NOT_EXECUTED) # {name}"
            receipt = await library.input(
                pane["id"], rid(), "text", value, confirmed=True
            )
            assert receipt["status"] == "succeeded"
            await asyncio.sleep(0.1)
            assert value in (await library.capture(pane["id"]))["text"]
            await library.close()

    run(check())


def test_restarted_server_does_not_reuse_a_saved_pane_or_attachment(tmp_path):
    async def check():
        async with fleet("one") as (srv, kw):
            library = TerminalFleet(tmp_path, **kw, allow_input=True)
            old = (await library.fleet())["panes"][0]
            viewer = await library.attach(old["id"])
            await srv.run("kill-session", "-t", old["session_id"])
            for _ in range(30):
                if not Path(library.socket).exists():
                    break
                await asyncio.sleep(0.05)
            await srv.run("new-session", "-d", "-s", "one")
            fresh = (await library.fleet())["panes"][0]
            assert fresh["pane_id"] == old["pane_id"]
            assert fresh["id"] != old["id"]
            with pytest.raises(TerminalError, match="earlier tmux server"):
                await library.output(viewer["attachment_id"])
            with pytest.raises(TerminalError, match="earlier tmux server"):
                await library.attach(old["id"])
            receipt = await library.input(
                old["id"], rid(), "text", "DO_NOT_RETARGET", confirmed=True
            )
            assert receipt["status"] == "refused"
            assert "DO_NOT_RETARGET" not in (await library.capture(fresh["id"]))["text"]
            await library.close()

    run(check())


def test_reopened_resources_share_the_viewer_limit_and_idle_cleanup(
    tmp_path, monkeypatch
):
    import time

    from tmux_fleet import terminal_stream

    class Viewer:
        def __init__(self, library, target):
            self.closed = False
            self.last_read = time.monotonic()

        async def start(self):
            await asyncio.sleep(0)

        async def read(self, cursor):
            return {"cursor": cursor}

        async def close(self):
            self.closed = True

    monkeypatch.setattr(terminal_stream, "ControlStream", Viewer)

    async def check():
        async with fleet("one") as (_, kw):
            library = TerminalFleet(tmp_path, **kw)
            pane = (await library.fleet())["panes"][0]
            retained = [rid() for _ in range(17)]
            for identity in retained:
                library._put(
                    "attachment",
                    identity,
                    {"id": identity, "target_id": pane["id"], "closed": False},
                )
            results = await asyncio.gather(
                *(library.output(identity) for identity in retained),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, Exception)]
            assert len(failures) == 1 and failures[0].code == "VIEW_LIMIT"
            assert len(library.streams) == 16 and library.sweeper is not None
            idle = next(iter(library.streams.values()))
            idle.last_read -= 61
            missing = next(
                identity for identity in retained if identity not in library.streams
            )
            await library.output(missing)
            assert idle.closed and len(library.streams) == 16
            await library.detach(missing)
            with pytest.raises(TerminalError, match="detached"):
                await library.output(missing)
            await library.close()

    run(check())


def test_terminal_cli_refusal_is_machine_visible_and_never_sends(tmp_path):
    import json
    import sys

    async def check():
        async with fleet("one") as (_, kw):
            library = TerminalFleet(tmp_path, **kw)
            pane = (await library.fleet())["panes"][0]
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tmux_fleet.terminal_cli",
                "--storage",
                str(tmp_path),
                "--socket-dir",
                kw["socket_dir"],
                "--socket-name",
                kw["socket_name"],
                "input",
                "--arguments",
                json.dumps(
                    {
                        "target_id": pane["id"],
                        "request_id": rid(),
                        "kind": "text",
                        "value": "NOT_ALLOWED",
                        "confirmed": True,
                    }
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output, error = await process.communicate()
            assert process.returncode == 2 and not error
            receipt = json.loads(output)
            assert receipt["status"] == "refused"
            assert receipt["error"]["code"] == "INPUT_AUTHORITY_REQUIRED"
            assert "NOT_ALLOWED" not in (await library.capture(pane["id"]))["text"]
            await library.close()

    run(check())
