"""Test-wide isolation.

Every test gets a private audit log and config path (never ~/.local/state or
~/.config), and no ambient socket env leaks into resolution.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX_FLEET_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("TMUX_FLEET_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("TMUX_FLEET_SOCKET_NAME", raising=False)
    monkeypatch.delenv("TMUX_KIT_SOCKET_DIR", raising=False)
    yield
