"""Self-description: two levels of detail for two readers. At the top level `-h`
is the terse summary and `--help` is the tool's skill; on every verb `-h` is
terse and `--help` is that verb's complete listing. None of them are aliases."""

from __future__ import annotations

import json

import pytest

from tmux_fleet import cli
from tmux_fleet.manifest import manifest
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
