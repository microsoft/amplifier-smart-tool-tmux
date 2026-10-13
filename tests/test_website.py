"""Static website regressions; run this file directly for required browser proof.

pytest needs only pytest/PyYAML. Browser proof additionally needs Playwright and
its Chromium installation: python tests/test_website.py --evidence-dir PATH.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Thread
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/microsoft/amplifier-smart-tool-tmux"


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def build(output):
    subprocess.run(
        [sys.executable, str(ROOT / "site/theme/build.py"), "--output", str(output)],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return (output / "index.html").read_text()


@pytest.fixture
def built(tmp_path):
    text = build(tmp_path / "site")
    return tmp_path / "site", text, Page(text)


def test_builder_repeatable_and_current_setup(built, tmp_path):
    output, text, _ = built
    other = tmp_path / "second"
    assert text == build(other)
    assert {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()} == {
        p.relative_to(other): p.read_bytes() for p in other.rglob("*") if p.is_file()
    }
    config = json.loads((ROOT / "site/site.json").read_text())
    assert "provider extras" not in text
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    binding = next(dep for dep in dependencies if dep.startswith("amplifier-agent @"))
    tag = re.search(r"amplifier-agent@(v[^#]+)#subdirectory=packages/python$", binding).group(1)
    assert tag in text
    assert "caller" in config["model_note"] and "environment" in config["model_note"]
    assert "AMPLIFIER_AGENT_MODEL" in text and "non-Anthropic" in text
    assert "without" in config["model_note"]
    assert "capture" in text and "forwarding" in text and "sensitive" in text
    for path in ("docs/installing-amplifier-agent.md", "src/tmux_fleet/SMART_TOOL.md"):
        assert (ROOT / path).is_file()
        assert f'{REPO}/blob/main/{path}' in text
    assert "live dashboard" not in text.lower()


def test_complete_theme_notice_and_relative_assets(built):
    output, _, page = built
    assert (output / "LICENSE").read_bytes() == (ROOT / "site/theme/LICENSE").read_bytes()
    assert any(tag == "a" and attrs.get("href") == "./LICENSE" for tag, attrs in page.elements)
    ids = {attrs["id"] for _, attrs in page.elements if "id" in attrs}
    for tag, attrs in page.elements:
        for key in ("href", "src", "data-motion-src", "data-still-src"):
            value = attrs.get(key)
            if not value:
                continue
            url = urlsplit(value)
            if url.scheme or url.netloc:
                assert url.scheme == "https"
            elif url.path:
                assert not url.path.startswith("/")
                assert (output / url.path).is_file(), (tag, value)
            elif url.fragment:
                assert url.fragment in ids
    for path in ("style.css", "site.js", "mark-loop.png", "mark-loop.gif", "tmux.svg"):
        assert (output / "assets" / path).stat().st_size > 0
    assert (output / ".nojekyll").exists()


def test_motion_control_has_native_keyboard_semantics(built):
    _, _, page = built
    buttons = [attrs for tag, attrs in page.elements if tag == "button" and "data-motion-toggle" in attrs]
    assert buttons
    assert all(attrs.get("type") == "button" and attrs.get("aria-pressed") in ("true", "false") for attrs in buttons)
    images = [attrs for tag, attrs in page.elements if tag == "img" and "data-motion-src" in attrs]
    assert images and all(attrs["src"] == attrs["data-still-src"] for attrs in images)


@pytest.fixture
def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/website.yml").read_text())


def step(workflow, identity):
    return next(s for s in workflow["jobs"]["build"]["steps"] if s.get("id") == identity)


def condition(expression, event, ref, publish, repository, configured):
    """Evaluate the actual workflow's bounded guard syntax, not a copied policy."""
    values = {
        "github.event_name": event, "github.ref": ref, "inputs.publish": publish,
        "github.repository": repository, "steps.pages.outputs.configured": str(configured).lower(),
        "needs.build.outputs.configured": str(configured).lower(),
    }
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    for key, value in values.items():
        expression = expression.replace(key, repr(value))
    return eval(expression.replace("&&", " and ").replace("||", " or "), {"__builtins__": {}}, {})


@pytest.mark.parametrize("event,ref,publish,repository,eligible", [
    ("push", "refs/heads/main", False, "microsoft/amplifier-smart-tool-tmux", True),
    ("workflow_dispatch", "refs/heads/main", True, "microsoft/amplifier-smart-tool-tmux", True),
    ("workflow_dispatch", "refs/heads/main", False, "microsoft/amplifier-smart-tool-tmux", False),
    ("workflow_dispatch", "refs/heads/preview", True, "microsoft/amplifier-smart-tool-tmux", False),
    ("push", "refs/heads/preview", False, "microsoft/amplifier-smart-tool-tmux", False),
    ("pull_request", "refs/pull/15/merge", True, "microsoft/amplifier-smart-tool-tmux", False),
    ("push", "refs/heads/main", False, "example/amplifier-smart-tool-tmux", False),
    ("workflow_dispatch", "refs/heads/main", True, "example/amplifier-smart-tool-tmux", False),
])
@pytest.mark.parametrize("configured", [False, True])
def test_publish_guard_matrix(workflow, event, ref, publish, repository, eligible, configured):
    assert condition(step(workflow, "pages")["if"], event, ref, publish, repository, configured) == eligible
    for expression in (step(workflow, "artifact")["if"], workflow["jobs"]["deploy"]["if"]):
        assert condition(expression, event, ref, publish, repository, configured) == (eligible and configured)


def test_workflow_pins_and_unconditional_validation(workflow):
    build_job = workflow["jobs"]["build"]
    assert "if" not in build_job
    assert build_job["permissions"] == {"contents": "read", "pages": "read"}
    assert build_job["outputs"]["configured"] == "${{ steps.pages.outputs.configured || 'false' }}"
    build_steps = build_job["steps"]
    for name in ("Build website", "Test website"):
        assert "if" not in next(s for s in build_steps if s.get("name") == name)
    assert "tests/test_website.py" in next(s for s in build_steps if s.get("name") == "Test website")["run"]
    refs = [s["uses"] for job in workflow["jobs"].values() for s in job["steps"] if "uses" in s]
    assert len(refs) == 4
    assert all(re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", ref) for ref in refs)
    assert step(workflow, "pages")["env"]["GH_TOKEN"] == "${{ github.token }}"


@pytest.mark.parametrize("status,payload,configured,message", [
    (200, {"build_type": "workflow"}, True, "configured=true"),
    (200, {"build_type": "legacy"}, False, "build_type"),
    (404, {}, False, "not configured"),
    (403, {}, False, "permission unknown"),
])
def test_pages_probe_known_branches(workflow, tmp_path, capsys, status, payload, configured, message):
    output = tmp_path / "outputs"
    calls = []

    def request(req, timeout):
        calls.append((req, timeout))
        if status != 200:
            raise HTTPError(req.full_url, status, "synthetic", {}, io.BytesIO(b""))
        response = io.BytesIO(json.dumps(payload).encode())
        response.status = status
        return response

    run_probe(workflow, output, request)
    assert output.read_text() == f"configured={str(configured).lower()}\n"
    assert message in capsys.readouterr().out
    req, timeout = calls[0]
    assert req.full_url == "https://api.github.com/repos/example/repo/pages"
    assert req.get_header("Authorization") == "Bearer synthetic-token"
    assert timeout == 10


def run_probe(workflow, output, request):
    script = step(workflow, "pages")["run"]
    python = script.split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_REPOSITORY": "example/repo", "GH_TOKEN": "synthetic-token"}), patch("urllib.request.urlopen", request):
        exec(compile(python, "website-pages-probe", "exec"), {})


@pytest.mark.parametrize("failure", ["timeout", "network", "server", "unauthorized", "bad-json", "unexpected-success"])
def test_pages_probe_errors_fail_closed(workflow, tmp_path, failure):
    output = tmp_path / "outputs"

    def request(req, timeout):
        if failure == "timeout":
            raise TimeoutError("synthetic timeout")
        if failure == "network":
            raise URLError("synthetic network failure")
        if failure in ("bad-json", "unexpected-success"):
            response = io.BytesIO(b"not json" if failure == "bad-json" else b'{"build_type":"workflow"}')
            response.status = 200 if failure == "bad-json" else 201
            return response
        raise HTTPError(req.full_url, 500 if failure == "server" else 401, "synthetic", {}, io.BytesIO(b""))

    with pytest.raises((SystemExit, ValueError)):
        run_probe(workflow, output, request)
    assert not output.exists() or "configured=true" not in output.read_text()


@contextmanager
def serve(directory):
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(directory)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def check_rendered_website(evidence_dir):
    """No skip path: desktop/mobile, keyboard, copy, reduced motion and no JS."""
    from playwright.sync_api import sync_playwright, expect

    evidence_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="website-") as temp:
        output = Path(temp) / "site"
        build(output)
        with serve(output) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                for label, width in (("desktop", 1440), ("mobile", 390)):
                    context = browser.new_context(viewport={"width": width, "height": 900}, permissions=["clipboard-read", "clipboard-write"])
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
                    response = page.goto(url, wait_until="networkidle")
                    assert response.status == 200
                    toggle = page.locator("[data-motion-toggle]")
                    expect(toggle).to_be_visible()
                    expect(toggle).to_have_text("Pause motion")
                    expect(toggle).to_have_attribute("aria-pressed", "false")
                    expect(page.locator(".brand-motion")).to_have_attribute("src", re.compile(r"mark-loop.gif$"))
                    # Reach the native control with Tab, not just scripted focus.
                    for _ in range(40):
                        page.keyboard.press("Tab")
                        if toggle.evaluate("(button) => button === document.activeElement"):
                            break
                    assert toggle.evaluate("(button) => button === document.activeElement")
                    page.keyboard.press("Enter")
                    expect(toggle).to_have_text("Play motion")
                    expect(toggle).to_have_attribute("aria-pressed", "true")
                    expect(page.locator(".brand-motion")).to_have_attribute("src", re.compile(r"mark-loop.png$"))
                    page.keyboard.press("Space")
                    expect(toggle).to_have_text("Pause motion")
                    expect(toggle).to_have_attribute("aria-pressed", "false")
                    for identity in ("install", "prompt"):
                        page.locator(f'[data-copy="{identity}"]').click()
                        expected = page.locator(f"#{identity}").text_content()
                        assert page.evaluate("navigator.clipboard.readText()") == expected
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
                    assert page.locator("img").evaluate_all("(images) => images.every(i => i.complete && i.naturalWidth > 0)")
                    assert not errors, errors
                    page.screenshot(path=str(evidence_dir / f"{label}.png"), full_page=True)
                    print(f"PASS rendered {label}: copy, Tab/Enter/Space motion, images, overflow, JS errors")
                    context.close()
                context = browser.new_context(reduced_motion="reduce", viewport={"width": 390, "height": 900})
                page = context.new_page()
                page.goto(url, wait_until="networkidle")
                toggle = page.locator("[data-motion-toggle]")
                expect(toggle).to_have_text("Play motion")
                expect(toggle).to_have_attribute("aria-pressed", "true")
                expect(page.locator(".brand-motion")).to_have_attribute("src", re.compile(r"mark-loop.png$"))
                toggle.press("Enter")
                expect(toggle).to_have_text("Pause motion")
                expect(page.locator(".brand-motion")).to_have_attribute("src", re.compile(r"mark-loop.gif$"))
                context.close()
                context = browser.new_context(java_script_enabled=False)
                page = context.new_page()
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#install")).to_contain_text("uv tool install")
                expect(page.locator('.site-footer a[href="./LICENSE"]')).to_be_visible()
                expect(page.locator(".brand-motion")).to_have_attribute("src", re.compile(r"mark-loop.png$"))
                assert page.locator("img").evaluate_all("(images) => images.every(i => i.complete && i.naturalWidth > 0)")
                context.close()
                print("PASS reduced-motion initial still and keyboard play; no-JS usable content/license/still")
            finally:
                browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    check_rendered_website(parser.parse_args().evidence_dir)