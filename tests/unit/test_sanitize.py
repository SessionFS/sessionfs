"""Tests for server/sanitize.py — NUL-byte stripping for archive-derived values.

PostgreSQL ``text`` rejects the NUL byte (``\\x00``) and asyncpg raises
``CharacterNotInRepertoireError``. These helpers strip NULs from values
*derived* from a pushed archive so the push is accepted while the derived DB
columns stay NUL-free. The archive blob itself is never modified.
"""

from __future__ import annotations

from sessionfs.server.sanitize import sanitize_meta, strip_nul


def test_strip_nul_removes_single_nul():
    assert strip_nul("abc\x00def") == ("abcdef", 1)


def test_strip_nul_removes_multiple_nuls():
    assert strip_nul("\x00a\x00\x00b\x00") == ("ab", 4)


def test_strip_nul_returns_unchanged_when_no_nul():
    value, count = strip_nul("hello")
    assert value == "hello"
    assert count == 0


def test_strip_nul_returns_non_string_unchanged():
    assert strip_nul(None) == (None, 0)
    assert strip_nul(42) == (42, 0)
    assert strip_nul(["a\x00b"]) == (["a\x00b"], 0)


def test_sanitize_meta_strips_string_bound_keys():
    meta = {
        "title": "Session\x00Title",
        "source_tool": "claude-code",
        "model_id": "claude-opus\x004-6",
        "model_provider": "anthropic\x00",
        "message_count": 5,  # integer counter — not a string key, untouched
        "tags": "[]",  # JSON-encoded — not in _META_STRING_KEYS
    }
    out, count = sanitize_meta(meta)
    assert out["title"] == "SessionTitle"
    assert out["source_tool"] == "claude-code"
    assert out["model_id"] == "claude-opus4-6"
    assert out["model_provider"] == "anthropic"
    assert out["message_count"] == 5
    assert out["tags"] == "[]"
    assert count == 3


def test_sanitize_meta_missing_keys_is_noop():
    meta = {"message_count": 5}
    out, count = sanitize_meta(meta)
    assert out == {"message_count": 5}
    assert count == 0


def test_sanitize_meta_none_values_left_untouched():
    meta = {"title": None, "source_tool": "claude-code"}
    out, count = sanitize_meta(meta)
    assert out["title"] is None
    assert count == 0
