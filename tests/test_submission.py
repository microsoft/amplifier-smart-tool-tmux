"""Pure argv-construction and readback-classification logic (no tmux)."""

from __future__ import annotations

from tmux_fleet import submission as S


def test_split_counts_enters():
    assert S.split_for_submission("echo x") == (["echo x"], 0)
    segs, n = S.split_for_submission("a\nb\nc")
    assert segs == ["a", "b", "c"] and n == 2
    # CRLF is one submission, not two
    _, n = S.split_for_submission("a\r\nb")
    assert n == 1


def test_build_send_argvs_no_newline_is_non_submitting():
    argvs, enters = S.build_send_argvs("sess", "echo hi")
    assert enters == 0
    assert len(argvs) == 1  # one literal send, no Enter


def test_build_send_argvs_newline_becomes_enter_key_event():
    argvs, enters = S.build_send_argvs("sess", "a\nb")
    assert enters == 1
    # text, Enter, text -> the middle argv is a real Enter key event
    joined = [" ".join(a) for a in argvs]
    assert any("Enter" in j for j in joined)


def test_classify_submission_is_asymmetric():
    # zero enters -> armed, and no pane reading can talk it out of that
    assert S.classify_submission(enter_count=0, appeared=True, cleared=True) == (
        S.OUTCOME_ARMED,
        False,
    )
    # both halves positive -> submitted
    assert S.classify_submission(enter_count=1, appeared=True, cleared=True) == (
        S.OUTCOME_SUBMITTED,
        True,
    )
    # enter went out but not confirmed -> uncertain
    assert S.classify_submission(enter_count=1, appeared=True, cleared=None) == (
        S.OUTCOME_UNCERTAIN,
        False,
    )


def test_input_line_holds_text_matches_both_directions():
    probe = S.input_line_probe("echo RELAY_SHAPE_MARKER")
    # row is longer (prompt + command): row endswith probe
    assert S.input_line_holds_text(f"$ {probe}", "echo RELAY_SHAPE_MARKER") is True
    # command wrapped, row holds only its tail: probe endswith row (>= MIN_PROBE_CHARS)
    tail = probe[-10:]
    assert S.input_line_holds_text(tail, "echo RELAY_SHAPE_MARKER") is True
    # a bare short prompt is the empty input line, not missing evidence
    assert S.input_line_holds_text("$", "echo RELAY_SHAPE_MARKER") is False
    # unjudgeable: nothing to look for, or no pane
    assert S.input_line_holds_text(None, "echo x") is None
    # a purely-newline text leaves nothing on the input line -> empty probe -> None
    assert S.input_line_probe("\n") == ""
    assert S.input_line_holds_text("$ ", "\n") is None


def test_max_submit_keys_is_tmux_kit_cap():
    from tmux_kit import keys as tk_keys

    assert S.MAX_SUBMIT_KEYS == tk_keys.MAX_KEYS
