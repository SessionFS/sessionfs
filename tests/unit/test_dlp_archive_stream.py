"""Streamed DLP scan of pushed archives (tk_3f6ffd2389724bbc).

A large push used to be scanned as one joined text (every member raw and
decoded, markers masked), which took about ten copies of the decompressed
archive and ran the API out of memory. These tests pin that the streamed
scan finds exactly what the whole-text scan found (same findings, order,
line numbers, uninspectable members), and that a large push stays within a
memory bound the old code exceeded many times over.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import tarfile
import tracemalloc

import pytest
from fastapi import HTTPException

from sessionfs.server import dlp
from sessionfs.security.secrets import scan_dlp
from sessionfs.utils.title_utils import extract_smart_title, message_ends_title_search
from tests.utils.large_push import PEM, build_large_archive, build_large_messages

# sha256 of the 403 findings list for build_large_archive(), computed with
# the whole-text scan this replaced (and reproduced by the streamed scan).
LARGE_PUSH_FINDINGS_SHA256 = "8fb0b29afae37a84e1245cb13dc9cbb94ef00d245633f4d3aed0c617180d9c93"

# ---------------------------------------------------------------------------
# Reference: the whole-text scan the push route used before streaming
# ---------------------------------------------------------------------------


def _old_strip_markers(text, custom_patterns, trusted_labels=()):
    """The previous implementation: one offset per kept character."""
    if "[REDACTED:" not in text:
        return text, None
    pieces, offsets, pos = [], [], 0
    for m in dlp._marker_re(custom_patterns, trusted_labels).finditer(text):
        pieces.append(text[pos:m.start()])
        offsets.extend(range(pos, m.start()))
        pos = m.end()
    if pos == 0:
        return text, None
    pieces.append(text[pos:])
    offsets.extend(range(pos, len(text)))
    return "".join(pieces), offsets


def _reference_scan(data: bytes, policy: dict) -> tuple[list, list[str]]:
    parts: list[str] = []
    uninspectable: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.name.endswith((".json", ".jsonl")):
                    continue
                f = tar.extractfile(member)
                if not f:
                    continue
                raw = f.read().decode("utf-8", errors="replace")
                parts.append(raw)
                try:
                    parts.append(dlp.decoded_json_text(raw))
                except dlp.DlpUninspectableError:
                    uninspectable.append(member.name)
    except Exception:
        uninspectable.append("archive")
    if uninspectable:
        return [], uninspectable
    text = _old_strip_markers("\n".join(parts), policy.get("custom_patterns"))[0]
    findings = scan_dlp(
        text,
        categories=policy.get("categories", ["secrets"]),
        custom_patterns=policy.get("custom_patterns"),
        allowlist=policy.get("allowlist"),
    )
    return dlp.dedupe_findings(findings), []


def _streamed(data: bytes, policy: dict, batch_chars: int = dlp._GATE_BATCH_CHARS):
    return dlp.scan_archive_for_dlp(
        data,
        categories=policy.get("categories", ["secrets"]),
        custom_patterns=policy.get("custom_patterns"),
        allowlist=policy.get("allowlist"),
        batch_chars=batch_chars,
    )


def _key(findings) -> list[tuple]:
    return [
        (f.pattern_name, f.category, f.severity, f.line_number, f.match_text, f.context)
        for f in findings
    ]


def _archive(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


CUSTOM = [
    {"name": "mycustom", "regex": r"abc", "category": "secret", "severity": "high"},
    # Matches only an empty line: catches a line added or lost at a seam.
    {"name": "empty_line", "regex": r"^$", "category": "secret", "severity": "low"},
    {"name": "broken", "regex": "(", "category": "secret", "severity": "low"},
]
POLICIES = [
    {"categories": ["secrets"]},
    {"categories": ["secrets", "phi"]},
    {"categories": None},
    {"categories": ["secrets"], "custom_patterns": CUSTOM, "allowlist": ["AKIAALLOWED"]},
]


def _assert_same(data: bytes, policy: dict, batch_chars: int) -> None:
    expected, expected_refused = _reference_scan(data, policy)
    got = _streamed(data, policy, batch_chars)
    assert got.uninspectable == expected_refused
    assert _key(got.findings) == _key(expected)


# ---------------------------------------------------------------------------
# Random archives
# ---------------------------------------------------------------------------

_SECRETS = [
    "AKIAABCDEFGHIJKLMNOP",
    "AKIAALLOWED123456789",
    "sk-ant-" + "a" * 30,
    "ghp_" + "b" * 40,
    PEM,
    'password = "hunter2hunter2"',
    "postgres://u:p@host/db",
    "123-45-6789",
    "patient name John Smith",
    "[REDACTED:aws_access_key_id]",
    "[REDACTED:mycustom]",
    "[REDACTED:bogus]",
    'token: "[REDACTED:github_token]abcdefgh1"',
]
_ODD = ["\r", "\r\n", "\x0b", " ", "\x85", "é", "日本", "\U0001f600", "\\", '"', "\t", " ", "﻿"]


def _rand_str(r: random.Random) -> str:
    out = []
    for _ in range(r.randint(0, 6)):
        k = r.random()
        if k < 0.3:
            out.append(r.choice(_SECRETS))
        elif k < 0.6:
            out.append(r.choice(_ODD))
        else:
            out.append("".join(r.choice("abc xyz:=\n") for _ in range(r.randint(0, 8))))
    return "".join(out)


def _rand_value(r: random.Random, depth: int = 0) -> object:
    k = r.random()
    if depth > 2 or k < 0.4:
        return _rand_str(r)
    if k < 0.5:
        return r.randint(-5, 10**6)
    if k < 0.75:
        return [_rand_value(r, depth + 1) for _ in range(r.randint(0, 3))]
    return {_rand_str(r)[:10] + str(i): _rand_value(r, depth + 1) for i in range(r.randint(0, 3))}


def _rand_member(r: random.Random) -> str:
    kind = r.random()
    if kind < 0.05:
        return ""
    if kind < 0.1:
        return r.choice([" \n\n ", "\n", "\r\n", "  \t"])
    if kind < 0.2:
        # A document over several lines (indented .json) or on one line.
        return json.dumps(_rand_value(r), indent=r.choice([None, 2]), ensure_ascii=r.random() < 0.5)
    if kind < 0.25:
        return "{\n" + _rand_str(r) + "\n}"
    if kind < 0.3:
        # Repeated keys (on one line or across lines) and too-deep nesting.
        return r.choice(['{"a": 1, "a": 2}\n{"b": 1}', "[" * 250 + "]" * 250, '{"a": 1,\n"a": 2}'])
    lines = []
    for _ in range(r.randint(0, 12)):
        k = r.random()
        if k < 0.6:
            lines.append(json.dumps(_rand_value(r), ensure_ascii=r.random() < 0.5))
        elif k < 0.75:
            lines.append(_rand_str(r))
        elif k < 0.85:
            lines.append("")
        else:
            lines.append(
                r.choice(["  ", "\r", '{"a": "b"} {"c": 1}', '{"x": 1', "{", "[1,", '"abc', "tru", "﻿{}"])
            )
    text = r.choice(["\n", "\n", "\r\n"]).join(lines)
    if r.random() < 0.5:
        text += "\n"
    if r.random() < 0.2:
        text = "\n  \n" + text
    return text


def _rand_archive(r: random.Random) -> bytes:
    members = []
    for i in range(r.randint(0, 4)):
        name = f"{i}_" + r.choice(["messages.jsonl", "manifest.json", "notes.txt", "tools.json", "a/b.jsonl"])
        raw = _rand_member(r).encode()
        if r.random() < 0.1:
            # A byte that isn't valid UTF-8 (decoded with replacement).
            pos = r.randint(0, len(raw))
            raw = raw[:pos] + bytes([r.choice([0xE2, 0x80, 0xFF, 0xC3])]) + raw[pos:]
        members.append((name, raw))
    return _archive(members)


@pytest.mark.parametrize("seed_block", range(4))
def test_streamed_scan_matches_whole_text_scan_on_random_archives(seed_block, monkeypatch):
    for seed in range(seed_block * 150, seed_block * 150 + 150):
        r = random.Random(seed)
        data = _rand_archive(r)
        policy = r.choice(POLICIES)
        for batch_chars in (1, 7, 64, dlp._GATE_BATCH_CHARS):
            monkeypatch.setattr(dlp, "_READ_CHUNK_BYTES", r.choice([1, 3, 16, 1 << 20]))
            _assert_same(data, policy, batch_chars)


def test_no_line_added_after_the_last_part():
    # The last decoded part ends with a newline; the joined text has no
    # separator after it, so there is no empty last line to match.
    data = _archive([("a.json", json.dumps({"k": "v\n"}).encode())])
    policy = {"categories": ["secrets"], "custom_patterns": CUSTOM}
    got = _streamed(data, policy)
    assert [f for f in got.findings if f.pattern_name == "empty_line"] == []
    _assert_same(data, policy, 7)


def test_document_over_several_lines_is_decoded_whole():
    # A repeated key only the whole-document parse sees: still refused.
    data = _archive([("x.json", b'{"a": 1,\n"a": 2}\n'), ("m.jsonl", b'{"b": "c"}\n')])
    got = _streamed(data, {"categories": ["secrets"]})
    assert got.uninspectable == ["x.json"]
    # A valid indented document: the secret behind escaped quotes is found.
    doc = json.dumps({"cfg": {"note": 'password = "hunter2hunter2"'}}, indent=2).encode()
    data = _archive([("y.json", doc)])
    _assert_same(data, {"categories": ["secrets"]}, 5)
    assert any(f.match_text == 'password = "hunter2hunter2"' for f in _streamed(data, {}).findings)


def test_uninspectable_members_and_read_errors_fail_closed_in_order():
    deep = b"[" * 250 + b"]" * 250
    good = b'{"a": "AKIAABCDEFGHIJKLMNOP"}\n'
    data = _archive([("1.jsonl", deep), ("2.jsonl", good), ("3.json", b'{"a":1,"a":2}')])
    got = _streamed(data, {"categories": ["secrets"]})
    assert got.uninspectable == ["1.jsonl", "3.json"]
    assert got.findings == []
    # A truncated archive: the members read so far, then "archive".
    truncated = _archive([("1.jsonl", deep), ("2.jsonl", good * 20000)])[:-4000]
    expected = _reference_scan(truncated, {})[1]
    got = _streamed(truncated, {})
    assert got.uninspectable == expected
    assert got.uninspectable[-1] == "archive"
    assert got.read_error is not None


def test_scanner_errors_are_not_reported_as_unreadable_archives(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("scanner broke")

    monkeypatch.setattr(dlp, "scan_dlp_compiled", boom)
    data = _archive([("m.jsonl", b'{"a": "b"}\n')])
    with pytest.raises(RuntimeError, match="scanner broke"):
        _streamed(data, {"categories": ["secrets"]})


def test_line_mode_decision():
    assert dlp._line_mode_is_exact('{"a": 1}')
    assert dlp._line_mode_is_exact('  {"a": 1} {"b": 2}')
    assert dlp._line_mode_is_exact('{"a": x}')  # fails mid-line: whole parse fails too
    assert dlp._line_mode_is_exact('{"a": "open')  # a string can't cross a newline
    assert dlp._line_mode_is_exact('{"a": 1, "a": 2}')  # refused either way
    assert not dlp._line_mode_is_exact("{")
    assert not dlp._line_mode_is_exact('{"a": 1,')
    assert not dlp._line_mode_is_exact('[1, 2 ')


def test_member_text_blocks_decode_like_a_whole_read():
    r = random.Random(7)
    alphabet = [b"a", b"\n", b"\r", b"\xc3\xa9", b"\xe6\x97\xa5", b"\xf0\x9f\x98\x80", b"\xe2", b"\x80", b"\xff", b"\xc3"]
    for _ in range(300):
        raw = b"".join(r.choice(alphabet) for _ in range(r.randint(0, 80)))
        for chunk in (1, 2, 3, 5, 64):
            dlp._READ_CHUNK_BYTES, saved = chunk, dlp._READ_CHUNK_BYTES
            try:
                blocks = list(dlp.iter_text_blocks(io.BytesIO(raw)))
            finally:
                dlp._READ_CHUNK_BYTES = saved
            assert "".join(blocks) == raw.decode("utf-8", errors="replace")
            assert all(b.endswith("\n") for b in blocks[:-1])


# ---------------------------------------------------------------------------
# Marker stripping
# ---------------------------------------------------------------------------


def test_strip_markers_map_matches_per_character_offsets():
    r = random.Random(11)
    pieces = ["[REDACTED:aws_access_key_id]", "[REDACTED:mycustom]", "[REDACTED:bogus]", "x", "ab", "é", "\n", "[RED"]
    custom = [{"name": "mycustom", "regex": "abc"}]
    for _ in range(2000):
        text = "".join(r.choice(pieces) for _ in range(r.randint(0, 12)))
        old_text, old_offsets = _old_strip_markers(text, custom)
        new_text, new_map = dlp._strip_markers(text, custom)
        assert new_text == old_text
        assert (new_map is None) == (old_offsets is None)
        if old_offsets is None:
            continue
        for start in range(len(new_text)):
            for end in range(start + 1, len(new_text) + 1):
                assert dlp._to_original(new_map, start, end) == (
                    old_offsets[start],
                    old_offsets[end - 1] + 1,
                )


# ---------------------------------------------------------------------------
# Metadata and knowledge-extraction readers
# ---------------------------------------------------------------------------


def _rand_message(r: random.Random) -> object:
    k = r.random()
    if k < 0.05:
        return r.choice([1, "text", None, ["x"]])  # not an object
    role = r.choice(["user", "user", "assistant", "system", "developer", "tool"])
    content = r.choice(
        [
            "",
            "# Agent: persona",
            "Fix the flaky retry test. Then ship it.",
            "---\ntitle: x\n---\nRefactor the cache layer please",
            "```\ncode\n```\n",
            [{"type": "text", "text": "Add a password = 'hunter2hunter2' check"}],
            [{"type": "tool_use", "name": "Bash"}],
            [{"type": "text", "text": 5}],  # raises when read
            7,  # raises when read
        ]
    )
    msg = {"role": role, "content": content}
    if r.random() < 0.1:
        msg["is_sidechain"] = True
    return msg


def test_title_from_first_stopping_message_matches_all_messages():
    r = random.Random(3)
    for _ in range(3000):
        messages = [_rand_message(r) for _ in range(r.randint(0, 6))]
        raw_title = r.choice([None, "", "# heading", "Real title here"])
        kept = [m for m in messages if message_ends_title_search(m)][:1]

        def title(msgs):
            try:
                return extract_smart_title(messages=msgs or None, raw_title=raw_title, message_count=3)
            except Exception as exc:
                return type(exc)

        assert title(kept) == title(messages)


def _old_extract_messages_list(text: str):
    try:
        return [json.loads(line) for line in text.strip().split("\n") if line.strip()]
    except Exception:
        return []


def test_message_list_reader_matches_whole_text_reader():
    from sessionfs.server.routes.sessions import _extract_messages_list

    r = random.Random(5)
    fragments = ['{"a": 1}', '{"b": "x"}', "", "  ", "\x0b", " ", "\r", "\t", "not json", "[1]"]
    for _ in range(1500):
        text = "".join(
            r.choice(fragments) + r.choice(["\n", "\n", "\r\n", " "]) for _ in range(r.randint(0, 6))
        )
        data = _archive([("messages.jsonl", text.encode())])
        assert _extract_messages_list(data) == _old_extract_messages_list(text), repr(text)


def test_manifest_metadata_title_and_fallbacks():
    from sessionfs.server.routes.sessions import _extract_manifest_metadata

    manifest = json.dumps({"title": "", "stats": {"message_count": 3}}).encode()
    lines = [
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "# Agent: persona"},
        {"role": "user", "content": "Fix the flaky retry test"},
        {"role": "user", "content": "Something else entirely"},
    ]
    body = "\n".join(json.dumps(m) for m in lines).encode() + b"\n"
    # The manifest may come after the messages.
    meta = _extract_manifest_metadata(_archive([("messages.jsonl", body), ("manifest.json", manifest)]))
    assert meta["title"] == "Fix the flaky retry test"
    # A line after the title that isn't JSON still voids the whole read.
    meta = _extract_manifest_metadata(
        _archive([("messages.jsonl", body + b"{broken\n"), ("manifest.json", manifest)])
    )
    assert meta["title"] is None and meta["message_count"] == 0
    # A message that can't be read before any title also does.
    bad = json.dumps({"role": "user", "content": 7}).encode() + b"\n"
    meta = _extract_manifest_metadata(_archive([("manifest.json", manifest), ("messages.jsonl", bad + body)]))
    assert meta["title"] is None


# ---------------------------------------------------------------------------
# A large push through the real preparation step
# ---------------------------------------------------------------------------


def test_medium_session_matches_whole_text_scan_across_many_batches():
    messages = build_large_messages(target_bytes=3 * 1024 * 1024)
    data = _archive([("manifest.json", b'{\n  "title": ""\n}'), ("messages.jsonl", messages)])
    policy = {"categories": ["secrets", "phi"], "custom_patterns": CUSTOM}
    for batch_chars in (4093, 100_003):
        _assert_same(data, policy, batch_chars)


def test_large_push_stays_within_memory_bound():
    """A ~45 MB session (non-ASCII text, markers from an earlier redact,
    private keys and secrets around every batch boundary) through
    _prepare_sync_archive in block mode.

    Measured with tracemalloc: the whole-text scan peaked at about 7.5 GB
    on this session (one offset per character while masking markers, plus
    ~10 copies of the joined text); the streamed scan at about 136 MiB.
    """
    from sessionfs.server.routes.sessions import _prepare_sync_archive
    from sessionfs.server.tiers import Tier
    from sessionfs.server.timing import PhaseTimer

    data, messages_size = build_large_archive()
    assert messages_size > 40 * 1024 * 1024
    tracemalloc.start()
    try:
        with pytest.raises(HTTPException) as caught:
            _prepare_sync_archive(
                data,
                session_id="ses_largepush0001",
                effective_tier=Tier.PRO,
                dlp_policy={"mode": "block", "categories": ["secrets", "phi"]},
                timer=PhaseTimer(),
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * len(data) + 128 * 1024 * 1024, f"peak {peak / 2**20:.0f} MiB"

    detail = caught.value.detail
    assert caught.value.status_code == 403 and detail["error"] == "dlp_blocked"
    patterns = {f["pattern"] for f in detail["findings"]}
    assert {"private_key_pem", "aws_access_key_id", "anthropic_api_key", "github_token"} <= patterns
    assert "bearer_token" in patterns  # from the last member
    # The whole-text scan reported exactly this list (pattern, category,
    # severity and line number of all 162 findings) on this fixture; the
    # fixture is seeded, so any change in what the scan finds shows here.
    digest = hashlib.sha256(json.dumps(detail["findings"], sort_keys=True).encode()).hexdigest()
    assert len(detail["findings"]) == 162
    assert digest == LARGE_PUSH_FINDINGS_SHA256
