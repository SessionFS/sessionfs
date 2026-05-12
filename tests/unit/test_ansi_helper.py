"""Unit tests for tests/utils/ansi.py.

Lock in the contract so refactors / future tests rely on a known
helper shape. The helper is the antidote to the v0.9.9.9 CI-color
flake class.
"""

from __future__ import annotations

import pytest

from tests.utils.ansi import assert_in_ansi, assert_not_in_ansi, strip_ansi


def test_strip_ansi_removes_color_codes():
    raw = "\x1b[33musage:\x1b[0m sfs handoff"
    assert strip_ansi(raw) == "usage: sfs handoff"


def test_strip_ansi_handles_split_dashes():
    """The exact CI-flake shape: --to split across two escape codes."""
    raw = "missing option '\x1b[1;36m-\x1b[0m\x1b[1;36m-to\x1b[0m'"
    assert strip_ansi(raw) == "missing option '--to'"


def test_strip_ansi_idempotent_on_clean_text():
    assert strip_ansi("plain text") == "plain text"


def test_strip_ansi_handles_empty_and_none_like():
    assert strip_ansi("") == ""


def test_assert_in_ansi_passes_after_strip():
    raw = "missing option '\x1b[1;36m-\x1b[0m\x1b[1;36m-to\x1b[0m'"
    assert_in_ansi("--to", raw)  # would fail without strip


def test_assert_in_ansi_default_case_insensitive():
    raw = "\x1b[33mMISSING OPTION\x1b[0m"
    assert_in_ansi("missing option", raw)  # case folded


def test_assert_in_ansi_case_sensitive_when_asked():
    raw = "\x1b[33mMissing Option\x1b[0m"
    with pytest.raises(AssertionError):
        assert_in_ansi("missing option", raw, case_insensitive=False)


def test_assert_in_ansi_raises_on_miss():
    with pytest.raises(AssertionError) as exc_info:
        assert_in_ansi("zzz", "abc")
    assert "expected 'zzz'" in str(exc_info.value)
    # Cleaned output appears in the message so failures are debuggable
    assert "abc" in str(exc_info.value)


def test_assert_not_in_ansi_passes_when_missing():
    assert_not_in_ansi("unexpected error", "\x1b[31mclean output\x1b[0m")


def test_assert_not_in_ansi_raises_when_present():
    with pytest.raises(AssertionError):
        assert_not_in_ansi("--to", "missing option '\x1b[1;36m--to\x1b[0m'")
