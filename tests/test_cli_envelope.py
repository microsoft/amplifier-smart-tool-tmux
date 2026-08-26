"""CLI contract: JSON-only stdout, the error envelope on failure, and the exit
codes (0 success · 2 refused · 1 read/agent failure)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tmux_fleet import cli
from _helpers import fleet as make_fleet
from _helpers import run

_NO_SOCKET = "/tmp/tmux-fleet-tests-no-such-socket-dir"


def _stdout_json(capsys):
    out = capsys.readouterr()
    payload = json.loads(out.out)  # raises if stdout is not pure JSON
    return payload, out


def test_unknown_verb_emits_error_envelope_exit_2(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["definitely-not-a-verb"])
    assert ei.value.code == 2
    payload, _ = _stdout_json(capsys)
    assert "error" in payload and payload["error"]["code"] == "usage"


def test_missing_required_argument_is_usage_envelope(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["read"])  # missing SESSION
    assert ei.value.code == 2
    payload, _ = _stdout_json(capsys)
    assert payload["error"]["code"] == "usage"


def test_send_fence_is_refused_exit_2_json_only(capsys):
    # The fence fires before any tmux contact, so no server is needed.
    rc = cli.main(["send", "alpha", "--text", "echo x", "--socket-dir", _NO_SOCKET])
    assert rc == 2
    payload, out = _stdout_json(capsys)
    assert payload["error"]["code"] == "refused"
    assert "--confirmed" in payload["error"]["message"]


def test_read_no_server_is_error_exit_1(capsys):
    rc = cli.main(["read", "x", "--socket-dir", _NO_SOCKET])
    assert rc == 1
    payload, _ = _stdout_json(capsys)
    assert payload["error"]["code"] == "fleet"
    assert payload["error"]["remedy"]  # a remedy is named


def test_socket_success_is_json_only_and_clean_stderr(capsys):
    rc = cli.main(["socket", "--socket-dir", _NO_SOCKET])
    assert rc == 0
    payload, out = _stdout_json(capsys)
    assert "socket" in payload
    assert out.err == ""  # nothing but JSON on stdout; stderr silent on success


def test_cli_success_against_populated_server_via_subprocess():
    """The real console entry point, run as a subprocess while an isolated
    server is alive: exit 0, JSON on stdout, clean stderr."""

    async def scenario():
        async with make_fleet("alpha", "beta") as (_srv, kw):
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tmux_fleet.cli",
                    "sessions",
                    "--socket-dir",
                    kw["socket_dir"],
                    "--socket-name",
                    kw["socket_name"],
                ],
                capture_output=True,
                text=True,
            )
            return proc

    proc = run(scenario())
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)  # JSON-only stdout
    names = {r["session"] for r in payload["sessions"]}
    assert {"alpha", "beta"} <= names
