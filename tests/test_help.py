"""Self-description: two levels of detail for two readers, at the top level and
on every verb. `-h` is the terse summary, `--help` is the complete listing, and
they are not aliases."""

from __future__ import annotations

import json

import pytest

from tmux_fleet import cli

_VERB_NAMES = [v["name"] for v in cli.VERBS]


def _help_output(capsys, argv: list[str]) -> str:
    with pytest.raises(SystemExit) as ei:
        cli.main(argv)
    assert ei.value.code == 0, argv
    out = capsys.readouterr()
    assert out.err == "", f"{argv} wrote to stderr"
    return out.out


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_top_level_help_exits_zero_on_stdout(capsys, flag):
    assert _help_output(capsys, [flag]).startswith("tmux-fleet")


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
