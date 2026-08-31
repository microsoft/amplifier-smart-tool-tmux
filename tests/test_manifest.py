"""Manifest conformance: the closed field set, version agreement, and the
file<->accessor no-drift guarantee."""

from __future__ import annotations

import importlib
import tomllib
from importlib.metadata import version as pkg_version
from pathlib import Path

import pytest

import tmux_fleet

# Imported via importlib (not `from tmux_fleet import manifest`) because the
# package __init__ binds the ATTRIBUTE `tmux_fleet.manifest` to the accessor
# FUNCTION, which a plain import would return instead of the submodule.
manifest_mod = importlib.import_module("tmux_fleet.manifest")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALLOWED = {
    "smart_tool_format",
    "name",
    "version",
    "description",
    "use_cases",
    "platforms",
    "requires",
}


def test_exactly_one_manifest_ships_with_the_tool():
    """One manifest, one copy, beside the code that reads it."""
    found = [p for p in _REPO_ROOT.rglob("SMART_TOOL.md") if ".git" not in p.parts]
    assert found == [_REPO_ROOT / "src" / "tmux_fleet" / "SMART_TOOL.md"], found


def test_accessor_returns_structured_manifest():
    m = manifest_mod.manifest()
    assert m.smart_tool_format == 1
    assert m.name == "tmux-fleet"
    assert isinstance(m.use_cases, list) and all(isinstance(x, str) for x in m.use_cases)
    assert m.platforms == ["linux", "macos"]
    assert m.body  # the free-form guidance body exists


def test_manifest_dict_has_exactly_the_closed_field_set():
    d = manifest_mod.manifest_dict()
    assert set(d) == _ALLOWED


def test_version_matches_pyproject_and_package_metadata():
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    pyproject_version = pyproject["project"]["version"]
    m = manifest_mod.manifest()
    assert m.version == pyproject_version
    assert m.version == pkg_version("tmux-fleet")
    assert m.version == tmux_fleet.__version__


def test_requires_shape_and_installs_are_doc_references():
    m = manifest_mod.manifest()
    by_name = {r.name: r for r in m.requires}
    assert "tmux" in by_name and by_name["tmux"].optional is False
    # amplifier-agent is no longer an external prerequisite -- the engine ships
    # as a regular dependency now (VISION section 3 amended). The remaining
    # optional requirement is the AI provider (an SDK extra + credentials) the
    # model-backed verbs need.
    assert "amplifier-agent" not in by_name
    assert "ai-provider" in by_name and by_name["ai-provider"].optional is True
    for r in m.requires:
        # install is a doc reference, never a command
        assert not any(tok in r.install for tok in ("&&", "|", ";"))
        assert r.install.split()[0] not in {"pip", "uv", "brew", "apt", "npm"}
        assert r.install.endswith(".md") or r.install.startswith("http")


def test_unknown_top_level_field_is_refused(monkeypatch):
    bad = (
        "---\n"
        "smart_tool_format: 1\n"
        "name: tmux-fleet\n"
        "version: 0.1.0\n"
        "description: x\n"
        "use_cases: [a]\n"
        "platforms: [linux]\n"
        "requires: []\n"
        "surprise_field: nope\n"
        "---\n\nbody\n"
    )
    monkeypatch.setattr(manifest_mod, "_read_manifest_text", lambda: bad)
    with pytest.raises(manifest_mod.ManifestError):
        manifest_mod.manifest()


def test_install_that_looks_like_a_command_is_refused(monkeypatch):
    bad = (
        "---\n"
        "smart_tool_format: 1\n"
        "name: tmux-fleet\n"
        "version: 0.1.0\n"
        "description: x\n"
        "use_cases: [a]\n"
        "platforms: [linux]\n"
        "requires:\n"
        "  - name: tmux\n"
        "    purpose: p\n"
        "    install: brew install tmux\n"
        "---\n\nbody\n"
    )
    monkeypatch.setattr(manifest_mod, "_read_manifest_text", lambda: bad)
    with pytest.raises(manifest_mod.ManifestError):
        manifest_mod.manifest()
