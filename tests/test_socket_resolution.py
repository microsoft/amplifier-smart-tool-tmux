"""Socket resolution: explicit-not-ambient, fail-loud on bad config, and the
socket-name threading that lets the tool be pinned at an exact server."""

from __future__ import annotations

import json

import pytest

from tmux_fleet import socket_resolution as SR


def test_explicit_dir_resolves_from_argument():
    res = SR.resolve("/tmp/somewhere")
    assert res.source == SR.SOURCE_ARGUMENT
    assert res.socket_dir == "/tmp/somewhere"
    assert res.socket_name == SR.DEFAULT_SOCKET_NAME
    assert res.server_socket_path.endswith(f"/{SR.DEFAULT_SOCKET_NAME}")


def test_socket_name_threads_into_server_path():
    res = SR.resolve("/tmp/x", socket_name="my-isolated-server")
    assert res.socket_name == "my-isolated-server"
    assert res.server_socket_path.endswith("/my-isolated-server")


def test_socket_name_from_env(monkeypatch):
    monkeypatch.setenv("TMUX_FLEET_SOCKET_NAME", "envname")
    res = SR.resolve("/tmp/x")
    assert res.socket_name == "envname"


def test_socket_name_with_slash_is_refused():
    with pytest.raises(SR.SocketConfigError):
        SR.resolve("/tmp/x", socket_name="a/b")


def test_blank_and_relative_dirs_are_refused():
    with pytest.raises(SR.SocketConfigError):
        SR.resolve("   ")
    with pytest.raises(SR.SocketConfigError):
        SR.resolve("relative/dir")


def test_config_wrong_type_is_fatal(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"socket_dir": 5}))
    monkeypatch.setenv("TMUX_FLEET_CONFIG", str(cfg))
    with pytest.raises(SR.SocketConfigError):
        SR.resolve()


def test_config_null_falls_through_to_default(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"socket_dir": None}))
    monkeypatch.setenv("TMUX_FLEET_CONFIG", str(cfg))
    monkeypatch.delenv("TMUX_KIT_SOCKET_DIR", raising=False)
    res = SR.resolve()
    assert res.source == SR.SOURCE_SYSTEM_DEFAULT
    assert res.socket_dir == SR.SYSTEM_DEFAULT_SOCKET_DIR


def test_ambient_tmux_tmpdir_is_reported_and_ignored(monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", "/some/ambient/dir")
    monkeypatch.delenv("TMUX", raising=False)
    res = SR.resolve("/tmp/chosen")
    assert res.ambient_tmux_tmpdir == "/some/ambient/dir"
    assert res.ambient_ignored is True
    block = SR.describe(res)
    assert block["ambient_ignored"] is True
    assert "ambient_ignored_note" in block
    # the tool named the socket it DID use, not the ambient one
    assert block["socket_dir"] == "/tmp/chosen"


def test_describe_confirmation_by_tmux():
    res = SR.resolve("/tmp/x", socket_name="s")
    # tmux reports the same path -> confirmed True
    block = SR.describe(res, tmux_reported_socket_path=res.server_socket_path)
    assert block["socket_path_confirmed_by_tmux"] is True
    # tmux reports a different path -> confirmed False
    block = SR.describe(res, tmux_reported_socket_path="/tmp/other/tmux-1000/s")
    assert block["socket_path_confirmed_by_tmux"] is False
    # no server -> unknown (None)
    block = SR.describe(res, tmux_reported_socket_path=None)
    assert block["socket_path_confirmed_by_tmux"] is None


def test_installed_socket_path_fails_loud_when_uninstalled(monkeypatch):
    monkeypatch.setattr(SR, "_INSTALLED", None)
    with pytest.raises(SR.SocketNotInstalledError):
        SR.installed_socket_path()
