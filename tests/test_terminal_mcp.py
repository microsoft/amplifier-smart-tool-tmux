import asyncio
import base64
import json
import subprocess
import uuid
from pathlib import Path

import pytest
from _helpers import fleet, run

from tmux_fleet.terminal import TerminalFleet

pytest.importorskip("mcp")
from mcp import Client

from tmux_fleet.mcp import UI_URI, create_server

ROOT = Path(__file__).parents[1]


def test_live_input_queue():
    subprocess.run(
        ["node", "--test", str(ROOT / "mcp-app/live-input.test.mjs")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_mcp_tools_app_resource_and_strict_authority(tmp_path):
    async def check():
        async with fleet("fixture") as (_, kw):
            library = TerminalFleet(tmp_path, **kw)
            async with Client(create_server(library)) as client:
                listed = await client.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                assert (
                    tools["tmux_fleet_input"].input_schema["properties"]["confirmed"][
                        "type"
                    ]
                    == "boolean"
                )
                assert tools["tmux_fleet_fleet"].annotations.read_only_hint
                result = await client.call_tool("tmux_fleet_fleet", {})
                pane = result.structured_content["result"]["panes"][0]
                denied = await client.call_tool(
                    "tmux_fleet_input",
                    {
                        "target_id": pane["id"],
                        "request_id": uuid.uuid4().hex,
                        "kind": "text",
                        "value": "never delivered",
                        "confirmed": True,
                    },
                )
                assert (
                    denied.is_error
                    and denied.structured_content["result"]["status"] == "refused"
                )
                html = await client.read_resource(UI_URI)
                assert "xterm" in html.contents[0].text
                attachment = (
                    await client.call_tool(
                        "tmux_fleet_attach", {"target_id": pane["id"]}
                    )
                ).structured_content["result"]
                frame = json.loads(
                    (await client.read_resource(attachment["resource"]))
                    .contents[0]
                    .text
                )
                assert frame["reset"] and frame["data"]
                assert "data" not in json.dumps(
                    (await client.call_tool("tmux_fleet_state", {})).structured_content[
                        "result"
                    ]["view"]
                )

    run(check())


def test_browser_keyboard_queue_clipboard_and_unknown_delivery(tmp_path):
    """Exercise the packaged App through the official bridge with slow replies."""
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright, expect

    script = subprocess.check_output(
        [
            str(ROOT / "mcp-app/node_modules/.bin/esbuild"),
            str(ROOT / "mcp-app/test-host.js"),
            "--bundle",
            "--format=iife",
            "--log-level=error",
        ],
        text=True,
    )

    async def check():
        state = {
            "allow_input": True,
            "allow_management": True,
            "view": {"target_id": None, "draft": "", "follow": True, "version": 0},
            "grants": [],
            "operations": [],
        }
        roster = {
            "socket": "isolated-fixture",
            "available": True,
            "panes": [
                {
                    "id": "pane-one",
                    "session": "test",
                    "window": "shell",
                    "session_id": "$1",
                    "window_id": "@1",
                    "pane_id": "%1",
                    "cols": 80,
                    "rows": 24,
                },
                {
                    "id": "pane-two",
                    "session": "test",
                    "window": "other",
                    "session_id": "$1",
                    "window_id": "@2",
                    "pane_id": "%2",
                    "cols": 80,
                    "rows": 24,
                },
            ],
        }
        inputs, authorizations = [], []
        fail_next = False
        current_attachment = None
        state_reads = 0

        def result(value):
            return {"content": [], "structuredContent": {"result": value}}

        async def call(params):
            nonlocal fail_next, current_attachment
            name, args = (
                params["name"].removeprefix("tmux_fleet_"),
                params.get("arguments", {}),
            )
            if name == "fleet":
                return result(roster)
            if name == "save_view":
                state["view"] = {
                    **state["view"],
                    **args,
                    "version": state["view"]["version"] + 1,
                }
                return result(state["view"])
            if name == "attach":
                current_attachment = uuid.uuid4().hex
                return result({"attachment_id": current_attachment})
            if name == "authorize_input":
                authorizations.append(args)
                grant = {
                    "id": uuid.uuid4().hex,
                    "target_id": args["target_id"],
                    "active": True,
                    "scope": "attachment",
                    "attachment_id": args["attachment_id"],
                    "expires_at": None,
                    "remaining_bytes": None,
                }
                state["grants"].append(grant)
                return result(grant)
            if name == "revoke_input":
                for grant in state["grants"]:
                    if grant["id"] == args["grant_id"]:
                        grant["active"] = False
                return result({})
            if name == "detach":
                for grant in state["grants"]:
                    if grant["attachment_id"] == args["attachment_id"]:
                        grant["active"] = False
                return result({})
            if name == "input":
                raw = base64.b64decode(args["value"], validate=True)
                assert len(raw) <= 4096
                raw.decode("utf-8", errors="strict")
                inputs.append((args, raw))
                await asyncio.sleep(0.12)
                if fail_next:
                    fail_next = False
                    receipt = {
                        "request_id": args["request_id"],
                        "action": "input",
                        "status": "unknown",
                        "target_id": args["target_id"],
                        "error": {"message": "Response lost"},
                    }
                    state["operations"].append(receipt)
                    return {**result(receipt), "isError": True}
                return result({"delivery": "delivered"})
            if name == "operation":
                return result(state["operations"][-1])
            if name == "acknowledge_operation":
                state["operations"][-1]["reviewed_at"] = 1
                return result({})
            raise AssertionError(name)

        async def read(params):
            nonlocal state_reads
            uri = params["uri"]
            if uri == "tmux-fleet://state":
                state_reads += 1
            value = (
                state
                if uri == "tmux-fleet://state"
                else {
                    "reset": uri.endswith("/start"),
                    "cols": 80,
                    "rows": 24,
                    "data": base64.b64encode(
                        b"\x1b[2J\x1b[HCLIPBOARD_SELECTION\r\n\x1b[?2004h"
                    ).decode()
                    if uri.endswith("/start")
                    else "",
                    "next_cursor": "next",
                    "poll_after_ms": 50,
                }
            )
            return {"contents": [{"uri": uri, "text": json.dumps(value)}]}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1100, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.expose_function("hostCall", call)
            await page.expose_function("hostRead", read)
            await page.goto("about:blank")
            await page.add_script_tag(content=script)
            await page.evaluate(
                "([html,result])=>mountTerminal(html,result)",
                [
                    (ROOT / "src/tmux_fleet/resources/mcp_app.html").read_text(),
                    result(roster),
                ],
            )
            frame = page.frame_locator("#app")
            await expect(frame.locator("#notice")).to_have_text(
                "Choose a pane to open its read-only terminal."
            )
            await frame.locator("#pane").select_option("pane-one")
            await expect(frame.locator("#transport")).to_contain_text(
                "Live read-only stream"
            )
            textarea = frame.locator(".xterm-helper-textarea")
            await textarea.focus()
            await page.keyboard.type("READ_ONLY")
            await page.wait_for_timeout(80)
            assert inputs == []
            await frame.locator("#enable").click()
            await expect(frame.locator("#writable")).to_have_text(
                "Typing enabled · until this view closes"
            )
            assert authorizations[-1]["attachment_id"] == current_attachment
            assert (
                "seconds" not in authorizations[-1]
                and "max_bytes" not in authorizations[-1]
            )

            text = "continuous_typing_is_not_blocked_by_slow_tool_replies"
            reads_before_typing = state_reads
            typing = asyncio.create_task(page.keyboard.type(text, delay=5))
            await page.wait_for_timeout(60)
            assert inputs and not typing.done()
            await expect(frame.locator("#type")).to_be_enabled()
            await expect(frame.locator("#manage")).to_be_enabled()
            await expect(frame.locator("#inspect")).to_be_hidden()
            await typing
            for _ in range(50):
                if b"".join(raw for _, raw in inputs).decode() == text:
                    break
                await page.wait_for_timeout(30)
            assert b"".join(raw for _, raw in inputs).decode() == text
            await page.wait_for_timeout(150)
            assert state_reads == reads_before_typing

            # A real paste event enters xterm's bracketed-paste processing, then
            # becomes multiple valid UTF-8 tool messages even above the old 32KiB quota.
            large = "é🦊漢" * 6000
            start = len(inputs)
            await textarea.evaluate(
                """(el, text) => {
                const data = new DataTransfer(); data.setData('text/plain', text);
                el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
            }""",
                large,
            )
            expected = "\x1b[200~" + large + "\x1b[201~"
            for _ in range(150):
                if b"".join(raw for _, raw in inputs[start:]).decode() == expected:
                    break
                await page.wait_for_timeout(30)
            assert b"".join(raw for _, raw in inputs[start:]).decode() == expected
            await page.wait_for_timeout(150)

            # Browser copy events and the Copy button work without Async Clipboard.
            screen = await frame.locator(".xterm-screen").bounding_box()
            await page.mouse.dblclick(screen["x"] + 40, screen["y"] + 8)
            copied = await textarea.evaluate("""el => {
                const data = new DataTransfer();
                el.dispatchEvent(new ClipboardEvent('copy', {clipboardData: data, bubbles: true, cancelable: true}));
                document.execCommand = command => {
                    window.copied = document.activeElement.value; return command === 'copy';
                };
                return data.getData('text/plain');
            }""")
            assert copied == "CLIPBOARD_SELECTION"
            await frame.locator("#copy").click()
            await expect(frame.locator("#notice")).to_have_text("Selection copied.")
            assert await frame.locator("html").evaluate("() => window.copied") == copied
            await textarea.focus()
            for shortcut in ("Meta+c", "Control+Shift+C"):
                await frame.locator("html").evaluate("() => window.copied = null")
                await page.keyboard.press(shortcut)
                assert (
                    await frame.locator("html").evaluate("() => window.copied")
                    == copied
                )

            # An uncertain first message stops an already-buffered paste, and
            # neither continued typing nor receipt inspection retries it.
            fail_next = True
            start = len(inputs)
            await textarea.focus()
            await page.keyboard.type("q")
            await page.wait_for_timeout(30)
            await page.keyboard.type("must_be_discarded", delay=1)
            await expect(frame.locator("#inspect")).to_be_visible()
            await expect(frame.locator("#writable")).to_have_text("Typing paused")
            await page.wait_for_timeout(200)
            assert len(inputs) == start + 1
            await frame.locator("#inspect").click()
            await frame.locator("#reviewed").click()
            await page.wait_for_timeout(100)
            assert len(inputs) == start + 1

            # Switching panes while one message is in flight invalidates the
            # remainder of that pane's queue, and the new view starts read only.
            await textarea.focus()
            await page.keyboard.type("x")
            await page.wait_for_timeout(30)
            await page.keyboard.type("discard_on_switch", delay=1)
            await frame.locator("#pane").select_option("pane-two")
            await expect(frame.locator("#writable")).to_have_text("Read only")
            await page.wait_for_timeout(250)
            assert len(inputs) == start + 2
            assert inputs[-1][1] == b"x"
            await textarea.focus()
            await page.keyboard.type("still_read_only")
            await page.wait_for_timeout(100)
            assert len(inputs) == start + 2
            assert errors == []
            await browser.close()

    run(check())


def test_official_app_bridge_live_terminal_drafts_receipts_theme_and_mobile(tmp_path):
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright, expect

    script = subprocess.check_output(
        [
            str(ROOT / "mcp-app/node_modules/.bin/esbuild"),
            str(ROOT / "mcp-app/test-host.js"),
            "--bundle",
            "--format=iife",
            "--log-level=error",
        ],
        text=True,
    )

    async def check():
        async with fleet("browser-fixture") as (srv, kw):
            library = TerminalFleet(
                tmp_path, **kw, allow_input=True, allow_management=True
            )
            async with (
                Client(create_server(library)) as client,
                async_playwright() as pw,
            ):
                browser = await pw.chromium.launch()
                page = await browser.new_page(viewport={"width": 1100, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def call(params):
                    return (
                        await client.call_tool(
                            params["name"], params.get("arguments", {})
                        )
                    ).model_dump(by_alias=True, exclude_none=True)

                async def read(params):
                    return (await client.read_resource(params["uri"])).model_dump(
                        by_alias=True, exclude_none=True
                    )

                await page.expose_function("hostCall", call)
                await page.expose_function("hostRead", read)
                await page.goto("about:blank")
                await page.add_script_tag(content=script)
                initial = await client.call_tool("tmux_fleet_fleet", {})
                target = initial.structured_content["result"]["panes"][0]["id"]
                html = (ROOT / "src/tmux_fleet/resources/mcp_app.html").read_text()
                await page.evaluate(
                    "([html,result])=>mountTerminal(html,result)",
                    [html, initial.model_dump(by_alias=True, exclude_none=True)],
                )
                frame = page.frame_locator("#app")
                await expect(frame.locator("#notice")).to_have_text(
                    "Choose a pane to open its read-only terminal."
                )
                await frame.locator("#pane").select_option(target)
                await expect(frame.locator("#transport")).to_contain_text(
                    "Live read-only stream"
                )
                await expect(frame.locator("#writable")).to_have_text("Read only")
                await frame.locator("#draft").fill(
                    'printf "BROWSER_TERMINAL_MARKER\\n"'
                )
                await page.wait_for_timeout(900)
                assert (await library.state())["view"]["draft"].startswith("printf")
                await frame.locator("#submit").check()
                await frame.locator("#paste").click()
                await expect(frame.locator("#notice")).to_have_text(
                    "Paste delivered. Inspect the terminal for its outcome."
                )
                assert (await library.capture(target))["text"].count(
                    "BROWSER_TERMINAL_MARKER"
                ) >= 1
                await frame.locator("#enable").click()
                await expect(frame.locator("#writable")).to_contain_text(
                    "Typing enabled"
                )
                await frame.locator(".xterm-helper-textarea").focus()
                await page.keyboard.type("LIVE_TYPED_MARKER", delay=25)
                await page.wait_for_timeout(500)
                assert "LIVE_TYPED_MARKER" in (await library.capture(target))["text"]
                # Agent edits the same retained selection/draft seen by the App.
                view = (await library.state())["view"]
                await library.save_view(
                    target, "Agent-authored draft", expected_version=view["version"]
                )
                await expect(frame.locator("#draft")).to_have_value(
                    "Agent-authored draft", timeout=7000
                )
                assert (
                    await page.evaluate(
                        "window.savedContext.structuredContent.draft_is_authority"
                    )
                ) is False
                assert (
                    await frame.locator("html").evaluate(
                        "el=>getComputedStyle(el).colorScheme"
                    )
                    == "dark"
                )
                await page.screenshot(
                    path=str(tmp_path / "terminal-desktop.png"), full_page=True
                )
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await frame.locator("body").evaluate(
                    "el=>el.scrollWidth<=innerWidth"
                )
                await page.screenshot(
                    path=str(tmp_path / "terminal-mobile.png"), full_page=True
                )
                # An unknown dispatch is inspectable as a successful receipt
                # read, then explicitly reviewed. The App never resends it.
                original_guarded = library._guarded
                attempted_inputs = []

                async def lose_input(row, argv):
                    if argv[0] == "send-keys":
                        attempted_inputs.append(argv)
                        raise RuntimeError("lost terminal dispatch response")
                    return await original_guarded(row, argv)

                library._guarded = lose_input
                await frame.locator("#type").click()
                await expect(frame.locator("#inspect")).to_be_visible()
                await frame.locator("#inspect").click()
                await expect(frame.locator("#reviewed")).to_be_visible()
                await frame.locator("#reviewed").click()
                await expect(frame.locator("#notice")).to_have_text(
                    "Outcome marked reviewed; nothing was replayed."
                )
                assert len(attempted_inputs) == 1
                unknown = next(
                    row
                    for row in (await library.state())["operations"]
                    if row["status"] == "unknown"
                )
                assert unknown["reviewed_at"]
                inspected = await client.call_tool(
                    "tmux_fleet_operation", {"request_id": unknown["request_id"]}
                )
                assert not inspected.is_error
                library._guarded = original_guarded
                await frame.locator("#detach").click()
                await expect(frame.locator("#notice")).to_have_text(
                    "View detached. tmux work is still running."
                )
                assert "browser-fixture" in await srv.run(
                    "list-sessions", "-F", "#{session_name}"
                )
                assert errors == []
                await browser.close()

    run(check())
