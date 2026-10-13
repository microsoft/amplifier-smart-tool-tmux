"""Self-description: two levels of detail for two readers. At the top level `-h`
is the terse summary and `--help` is the tool's skill; on every verb `-h` is
terse and `--help` is that verb's complete listing. None of them are aliases."""

from __future__ import annotations

import json
from dataclasses import replace
from email.message import Message
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import subprocess
import sys
import textwrap
import tomllib

import pytest

from tmux_fleet import cli
from tmux_fleet.manifest import ManifestError, manifest
from tmux_fleet.skill import skill, skill_directory, skill_resources

_VERB_NAMES = [v["name"] for v in cli.VERBS]


def _help_output(capsys, argv: list[str]) -> str:
    with pytest.raises(SystemExit) as ei:
        cli.main(argv)
    assert ei.value.code == 0, argv
    out = capsys.readouterr()
    assert out.err == "", f"{argv} wrote to stderr"
    return out.out


def test_top_level_terse_help_exits_zero_on_stdout(capsys):
    assert _help_output(capsys, ["-h"]).startswith("tmux-fleet")


def test_top_level_help_prints_the_skill_the_library_renders(capsys):
    """The CLI adds nothing: `--help` is the library's skill, byte for byte."""
    assert _help_output(capsys, ["--help"]) == skill()


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_is_installed_tool_package_not_sdk(capsys, flag):
    output = _help_output(capsys, [flag])
    package_version = version("tmux-fleet")
    assert output == package_version + "\n"
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert package_version == project["project"]["version"] == manifest().version


def test_help_names_version_flags(capsys):
    for flag in ("-h", "--help"):
        output = _help_output(capsys, [flag])
        assert "-V" in output and "--version" in output


def test_the_skill_has_the_shape_of_an_agent_skill():
    rendered = skill()
    assert rendered.startswith('<skill_content name="tmux-fleet">\n')
    assert rendered.rstrip("\n").endswith("</skill_content>")
    assert f"Skill directory: {skill_directory()}\n" in rendered
    assert "Repository: https://github.com/microsoft/amplifier-smart-tool-tmux\n" in rendered
    assert "\n# tmux-fleet\n" in rendered
    assert manifest().body in rendered
    assert "\n## Capabilities\n" in rendered
    for verb in cli.VERBS:
        kind = "model-backed" if verb["model_backed"] else "deterministic"
        assert f"- `{verb['name']}` [{kind}] -- " in rendered
        assert f"`tmux-fleet {verb['name']} --help`" in rendered
    assert "<skill_resources>\n" in rendered
    for path in skill_resources():
        assert f"  <file>{path}</file>\n" in rendered


def test_the_skill_header_carries_the_manifest_version():
    """David's PR18 header sits immediately after Repository, not in the body."""
    lines = skill().splitlines()
    repo = next(i for i, line in enumerate(lines) if line.startswith("Repository: "))
    assert lines[repo + 1] == f"Version: {manifest().version}"
    assert lines[repo + 2].startswith("Relative paths")
    assert lines.count(f"Version: {manifest().version}") == 1
    assert manifest().version == version("tmux-fleet")


def test_the_skill_version_comes_from_the_manifest_not_installed_metadata(monkeypatch):
    skill_module = import_module("tmux_fleet.skill")
    synthetic = replace(manifest(), version="9.8.7")
    monkeypatch.setattr(skill_module, "manifest", lambda: synthetic)
    assert skill_module.version() == "9.8.7"
    assert "\nVersion: 9.8.7\n" in skill()
    assert version("tmux-fleet") != synthetic.version


@pytest.mark.parametrize("metadata_missing", [False, True])
def test_the_skill_keeps_version_without_repository_metadata(monkeypatch, metadata_missing):
    skill_module = import_module("tmux_fleet.skill")

    def no_repository(name):
        if metadata_missing:
            raise PackageNotFoundError(name)
        return Message()

    monkeypatch.setattr(skill_module, "metadata", no_repository)
    lines = skill().splitlines()
    assert not any(line.startswith("Repository: ") for line in lines)
    assert lines[2] == f"Version: {manifest().version}"
    assert lines[3].startswith("Relative paths")


def test_unavailable_manifest_omits_version_and_keeps_loud_body(capsys, monkeypatch):
    manifest_module = import_module("tmux_fleet.manifest")

    def unavailable():
        raise ManifestError("synthetic missing manifest")

    monkeypatch.setattr(manifest_module, "_read_manifest_text", unavailable)
    rendered = _help_output(capsys, ["--help"])
    assert rendered == skill()
    assert not any(line.startswith("Version: ") for line in rendered.splitlines())
    assert "(manifest unavailable: synthetic missing manifest)" in rendered
    assert "\n## Capabilities\n" in rendered
    assert "prints the installed tool version." in rendered


def test_rendered_help_qualifies_uv_upgrade_and_explicit_ref_reinstall(capsys):
    """Pins shipped guidance, not an execution proof of uv's upgrade semantics."""
    rendered = _help_output(capsys, ["--help"])
    assert "For **uv-managed installs**" in rendered
    assert "uv tool upgrade tmux-fleet" in rendered
    assert "saved source and version constraints" in rendered
    assert "A pinned Git revision stays pinned; upgrade does not switch it to main/latest." in rendered
    assert "reinstall with an explicit Git URL/ref" in rendered
    assert "uv tool install git+https://github.com/microsoft/amplifier-smart-tool-tmux@main" in rendered
    assert "Replace `@main` with the desired branch, tag, or commit." in rendered
    assert "https://docs.astral.sh/uv/guides/tools/#upgrading-tools" in rendered
    assert "upgrade" not in _VERB_NAMES


def test_help_without_tmux_or_provider_never_imports_sdk_or_provider(tmp_path):
    """Fresh process: no PATH tools/config, and SDK/provider imports fail loudly."""
    code = textwrap.dedent("""
        import builtins, contextlib, io, os, sys
        blocked_prefixes = ("amplifier_agent", "amplifier_core", "anthropic", "openai", "google.genai")
        real_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name.startswith(blocked_prefixes):
                raise AssertionError(f"SDK/provider imported: {name}")
            return real_import(name, *args, **kwargs)
        builtins.__import__ = guarded_import
        before = dict(os.environ)
        from tmux_fleet import cli, skill
        from tmux_fleet.manifest import manifest
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                cli.main(["--help"])
            except SystemExit as exc:
                assert exc.code == 0
            else:
                raise AssertionError("help did not exit")
        assert output.getvalue() == skill()
        assert f"Version: {manifest().version}\\n" in output.getvalue()
        assert "uv tool upgrade tmux-fleet" in output.getvalue()
        assert not any(name.startswith(blocked_prefixes) for name in sys.modules)
        assert before == dict(os.environ)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env={"PATH": "", "HOME": str(tmp_path)}, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == proc.stderr == ""


def test_every_skill_resource_ships_inside_the_package():
    for path in skill_resources():
        assert (skill_directory() / path).is_file(), path
    assert "SMART_TOOL.md" in skill_resources()


def test_the_manifest_body_carries_no_heading_of_its_own():
    """The skill supplies the `# tmux-fleet` heading; one in the body would render twice."""
    assert not manifest().body.startswith("# ")


@pytest.mark.parametrize("verb", _VERB_NAMES)
@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_every_verb_accepts_both_flags(capsys, verb, flag):
    """Including verbs with required arguments: help must work before you know
    how to call the verb."""
    assert _help_output(capsys, [verb, flag]).startswith(f"tmux-fleet {verb} ")


@pytest.mark.parametrize("verb", _VERB_NAMES)
def test_terse_and_complete_are_not_aliases(capsys, verb):
    terse = _help_output(capsys, [verb, "-h"])
    complete = _help_output(capsys, [verb, "--help"])
    assert terse != complete
    assert len(complete.splitlines()) > len(terse.splitlines())


@pytest.mark.parametrize("verb", [v["name"] for v in cli.VERBS if v["model_backed"]])
def test_model_backed_verbs_disclose_it(capsys, verb):
    assert "MODEL-BACKED" in _help_output(capsys, [verb, "--help"])


@pytest.mark.parametrize("verb", _VERB_NAMES)
def test_complete_listing_names_every_argument_and_its_type(capsys, verb):
    complete = _help_output(capsys, [verb, "--help"])
    for param in cli._params(cli.VERB_BY_NAME[verb]):
        assert param["label"] in complete
        assert param["type"] in complete
    assert "RETURNS" in complete


def test_usage_errors_are_still_the_envelope_not_help(capsys):
    """Help is not an error: a genuine usage error still exits 2 with JSON."""
    with pytest.raises(SystemExit) as ei:
        cli.main(["read"])  # missing SESSION
    assert ei.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "usage"
    assert "tmux-fleet read --help" in payload["error"]["remedy"]
