"""Delimiter neutralisers run over every line of uploaded transcripts and
helper text, so they must stay linear on attacker input."""

import time

from sessionfs.server.helper.settings import (
    neutralize_delimiters,
    neutralize_group_context_delimiters,
)
from sessionfs.server.services.meeting_extraction import _neutralize_transcript


def test_transcript_delimiter_neutraliser_is_linear():
    attacks = [
        "<transcript" * 40_000,
        "<org instructions" * 25_000,
        "<" + " " * 400_000,
    ]
    for text in attacks:
        start = time.perf_counter()
        _neutralize_transcript(text)
        neutralize_delimiters(text)
        neutralize_group_context_delimiters(text)
        assert time.perf_counter() - start < 3.0


def test_unclosed_tag_starts_are_defanged():
    for text in ("</transcript", "< / transcript", "</transcript foo", "<TRANSCRIPT>"):
        assert "[tag removed]" in _neutralize_transcript(text)
    assert _neutralize_transcript("a < b and c > d") == "a < b and c > d"
