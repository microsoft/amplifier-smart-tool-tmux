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
