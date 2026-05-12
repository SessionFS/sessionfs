"""Regression coverage for `sfs project ask` keyword extraction.

The v0.9.9.10 trigram-index work added a 3-char floor to
`/entries?search=`. Before this fix the ask flow tokenized the
question and fired a search for every >1-char keyword, so a
2-char token like `db` or `ai` would hit the new 422 gate and
abort the whole command via typer.Exit(1) in `_api_request`.

These tests pin the keyword extractor so the ask flow stays
aligned with the server-side gate.
"""

from __future__ import annotations

from sessionfs.cli.cmd_project import (
    _ASK_MAX_KEYWORDS,
    _ASK_MIN_KEYWORD_LEN,
    _extract_search_keywords,
)


def test_extractor_drops_two_char_tokens():
    assert _extract_search_keywords("DB schema?") == ["schema"]
    assert _extract_search_keywords("AI safety review") == ["safety", "review"]
    assert _extract_search_keywords("UI ux refresh") == ["refresh"]


def test_extractor_drops_one_char_tokens():
    assert _extract_search_keywords("a b c migration") == ["migration"]


def test_extractor_strips_trailing_punctuation():
    assert _extract_search_keywords("Schema?") == ["schema"]
    # "is" is a stop word; "where", "schema", "exactly" survive, with
    # the trailing comma + exclamation stripped from the latter two.
    assert _extract_search_keywords("Where is schema, exactly!") == [
        "where",
        "schema",
        "exactly",
    ]


def test_extractor_drops_stop_words():
    # All stop words → empty list
    assert _extract_search_keywords("what is the about") == []


def test_extractor_caps_at_max_keywords():
    q = " ".join(f"word{i}" for i in range(20))
    out = _extract_search_keywords(q)
    assert len(out) == _ASK_MAX_KEYWORDS
    assert out == ["word0", "word1", "word2", "word3", "word4"]


def test_extractor_deduplicates_repeated_tokens():
    assert _extract_search_keywords("schema schema schema migration") == [
        "schema",
        "migration",
    ]


def test_extractor_handles_empty_and_punct_only():
    assert _extract_search_keywords("") == []
    assert _extract_search_keywords("???   !!!") == []


def test_min_keyword_len_matches_server_gate():
    # If this assertion fires, the server-side gate in
    # routes/knowledge.py was changed and this CLI path needs
    # to be re-aligned in the same release.
    assert _ASK_MIN_KEYWORD_LEN == 3
