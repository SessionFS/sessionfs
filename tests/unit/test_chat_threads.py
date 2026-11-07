"""Chat thread parsers + normaliser (GitHub #118, C1)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from sessionfs.server.services import chat_threads as ct
from sessionfs.server.services.chat_threads import (
    ChatMessage,
    normalise_messages,
    parse_chat_thread,
    parse_discord,
    parse_paste,
    parse_slack,
)


def _code(exc: pytest.ExceptionInfo) -> tuple[int, str]:
    return exc.value.status_code, exc.value.detail["error"]


# ── Slack ──

_SLACK = [
    {"type": "message", "user": "U1", "text": "We go with Dataproc", "ts": "1761746520.000100"},
    {"type": "message", "subtype": "channel_join", "user": "U3", "text": "joined", "ts": "1761746500.0"},
    {"type": "message", "user": "U2", "text": "ok <@U1>\nI'll own the ticket", "ts": "1761746580.0"},
    {"type": "message", "user": "U9", "user_profile": {"real_name": "Pat Guest"}, "text": "+1", "ts": "1761746590.0"},
    {"type": "message", "user": "U2", "text": "", "ts": "1761746595.0"},
]
_USERS = [
    {"id": "U1", "name": "tolu", "profile": {"display_name": "Tolu", "email": "tolu@acme.io"}},
    {"id": "U2", "name": "sam", "real_name": "Sam Ade", "profile": {"real_name": "Sam Ade"}},
    {"id": "U7", "name": "lurker", "profile": {"display_name": "Lurker", "email": "lurk@acme.io"}},
]


def test_slack_channel_array_with_users_map() -> None:
    msgs = parse_slack(json.dumps(_SLACK), json.dumps(_USERS), attestation=True)
    assert [m.author_raw for m in msgs] == ["Tolu", "Sam Ade", "Pat Guest"]
    assert msgs[0].email_raw == "tolu@acme.io"
    assert msgs[1].text == "ok @Tolu\nI'll own the ticket"
    assert msgs[0].posted_at == datetime.fromtimestamp(1761746520.0001, tz=timezone.utc)
    # The users.json-only lurker never appears.
    assert all("Lurker" not in m.author_raw for m in msgs)


def test_slack_without_users_shows_raw_id() -> None:
    msgs = parse_slack(json.dumps(_SLACK), attestation=True)
    assert msgs[0].author_raw == "U1"
    assert msgs[0].email_raw is None


def test_slack_thread_object_sorted_by_ts() -> None:
    body = {"ok": True, "messages": list(reversed(_SLACK)), "users": _USERS}
    msgs = parse_slack(json.dumps(body), attestation=True)
    assert [m.author_raw for m in msgs] == ["Tolu", "Sam Ade", "Pat Guest"]


@pytest.mark.parametrize(
    "body",
    [
        {"channel": {"id": "D123ABC", "is_im": True}, "messages": _SLACK},
        {"channel": {"id": "G1", "name": "mpdm-a--b-1", "is_mpim": True}, "messages": _SLACK},
        {"is_im": True, "messages": _SLACK},
        {"channel": "D0123ABCD", "messages": _SLACK},
        [{"type": "message", "user": "U1", "text": "hi", "ts": "1", "channel_type": "im"}],
        [{"type": "message", "user": "U1", "text": "hi", "ts": "1", "channel_type": "mpim"}],
    ],
)
def test_slack_dm_refused(body) -> None:
    with pytest.raises(HTTPException) as exc:
        parse_slack(json.dumps(body))
    assert _code(exc) == (422, "dm_not_supported")


@pytest.mark.parametrize("raw", ["not json", "{\"messages\": 3}", "42", "\"x\""])
def test_slack_malformed(raw: str) -> None:
    with pytest.raises(HTTPException) as exc:
        parse_slack(raw)
    assert _code(exc) == (422, "chat_unparseable")


def test_slack_raw_cap() -> None:
    big = json.dumps([{"text": "x" * 1024}] * 2100)
    with pytest.raises(HTTPException) as exc:
        parse_slack(big)
    assert _code(exc) == (413, "chat_too_large")


# ── Discord ──

_DISCORD = {
    "guild": {"id": "111", "name": "Acme"},
    "channel": {"id": "222", "type": "GuildTextChat", "name": "infra"},
    "messages": [
        {"id": "2", "type": "Default", "timestamp": "2026-10-29T14:05:00+00:00",
         "content": "second", "author": {"id": "a2", "name": "sam", "nickname": "Sam"}},
        {"id": "1", "type": "Default", "timestamp": "2026-10-29T14:02:00+00:00",
         "content": "first\nline", "author": {"id": "a1", "name": "tolu"}},
        {"id": "3", "type": "ChannelPinnedMessage", "timestamp": "2026-10-29T14:06:00+00:00",
         "content": "pinned", "author": {"id": "a1", "name": "tolu"}},
    ],
}


def test_discord_export() -> None:
    msgs = parse_discord(json.dumps(_DISCORD))
    assert [(m.author_raw, m.text) for m in msgs] == [("tolu", "first\nline"), ("Sam", "second")]


@pytest.mark.parametrize("ctype", ["DirectTextChat", "DirectGroupTextChat", 1, 3])
def test_discord_dm_refused(ctype) -> None:
    body = dict(_DISCORD, channel={"id": "9", "type": ctype})
    with pytest.raises(HTTPException) as exc:
        parse_discord(json.dumps(body))
    assert _code(exc) == (422, "dm_not_supported")


def test_discord_dm_guild_refused() -> None:
    body = dict(_DISCORD, guild={"id": "0", "name": "Direct Messages"})
    with pytest.raises(HTTPException) as exc:
        parse_discord(json.dumps(body))
    assert _code(exc) == (422, "dm_not_supported")


@pytest.mark.parametrize("raw", ["[]", "{\"messages\": {}}", "nope"])
def test_discord_malformed(raw: str) -> None:
    with pytest.raises(HTTPException) as exc:
        parse_discord(raw)
    assert _code(exc) == (422, "chat_unparseable")


# ── Paste ──


def test_paste_name_lines_with_continuation() -> None:
    msgs = parse_paste(
        "[2026-10-29 14:02] Tolu: We go with Dataproc\n"
        "and the LB cert moves too\n"
        "\n"
        "Sam Ade: I'll own it\n"
        "Tolu: thanks\n"
    )
    assert [m.author_raw for m in msgs] == ["Tolu", "Sam Ade", "Tolu"]
    assert msgs[0].text == "We go with Dataproc\nand the LB cert moves too"
    assert msgs[0].posted_at == datetime(2026, 10, 29, 14, 2, tzinfo=timezone.utc)
    assert msgs[1].posted_at is None


def test_paste_blocks_teams_style() -> None:
    msgs = parse_paste(
        "Tolu Ade 10:02 AM\nWe go with Dataproc\nsecond line\n\n"
        "Sam, 14:05\nI'll own it\n\n"
        "https://example.com/notes\n"
    )
    assert [m.author_raw for m in msgs] == ["Tolu Ade", "Sam"]
    assert msgs[0].text == "We go with Dataproc\nsecond line"
    # A lone non-header line continues the previous message.
    assert msgs[1].text.endswith("https://example.com/notes")


def test_paste_url_is_not_an_author() -> None:
    msgs = parse_paste("Tolu: see\nhttps://x.io/a: b\nSam: ok")
    assert [m.author_raw for m in msgs] == ["Tolu", "Sam"]


def test_paste_empty_refused() -> None:
    with pytest.raises(HTTPException) as exc:
        parse_paste("  \n\n ")
    assert _code(exc) == (422, "chat_unparseable")


# ── Normaliser ──


def test_normaliser_one_message_per_line() -> None:
    thread = normalise_messages([
        ChatMessage("Tolu", "We go with\n\nDataproc", datetime(2026, 10, 29, 14, 2, tzinfo=timezone.utc), "t@a.io"),
        ChatMessage("Sam: Ade", "ok", None),
        ChatMessage("Ghost", "   \n "),
        ChatMessage("Pat", "third"),
    ])
    lines = thread.content.split("\n")
    assert lines == [
        "[2026-10-29 14:02] Tolu: We go with ⏎ Dataproc",
        "Sam： Ade: ok",
        "Pat: third",
    ]
    assert thread.line_count == 3
    assert [(m.line_no, m.author_raw, m.email_raw) for m in thread.meta] == [
        (1, "Tolu", "t@a.io"), (2, "Sam： Ade", None), (3, "Pat", None),
    ]
    assert thread.first_posted_at == datetime(2026, 10, 29, 14, 2, tzinfo=timezone.utc)


def test_normaliser_message_cap(monkeypatch) -> None:
    monkeypatch.setattr(ct, "MAX_MESSAGES", 3)
    with pytest.raises(HTTPException) as exc:
        normalise_messages([ChatMessage("a", "x")] * 4)
    assert _code(exc) == (413, "chat_too_large")


def test_normaliser_byte_cap() -> None:
    with pytest.raises(HTTPException) as exc:
        normalise_messages([ChatMessage("a", "x" * 1000)] * 600)
    assert _code(exc) == (413, "chat_too_large")


def test_parse_chat_thread_users_only_for_slack() -> None:
    with pytest.raises(HTTPException) as exc:
        parse_chat_thread("paste", "a: b", users="[]")
    assert _code(exc) == (422, "users_not_supported")
    thread = parse_chat_thread("teams_paste", "Tolu: hi\nSam: ok")
    assert thread.content == "Tolu: hi\nSam: ok"


# ── Review fixes: DMs, attestation, emails, ordering, timestamps, ReDoS ──

_CHANNEL = {"id": "C0123", "name": "infra"}


@pytest.mark.parametrize(
    "channel",
    [
        {"id": "C9", "user": "U2"},
        {"id": "G9", "is_private": True},
        {"id": "G9", "is_group": True, "name": ""},
        {"id": "G9", "is_private": True, "is_group": True},
    ],
)
def test_slack_dm_shaped_channel_objects_refused(channel) -> None:
    with pytest.raises(HTTPException) as exc:
        parse_slack(json.dumps({"channel": channel, "messages": _SLACK}))
    assert _code(exc) == (422, "dm_not_supported")


def test_slack_named_private_channel_is_a_channel() -> None:
    body = {"channel": {"id": "G9", "name": "secret-infra", "is_private": True}, "messages": _SLACK}
    assert parse_slack(json.dumps(body))


@pytest.mark.parametrize(
    "body",
    [_SLACK, {"ok": True, "messages": _SLACK}, {"channel": "C0123", "messages": _SLACK}],
)
def test_slack_without_channel_object_needs_attestation(body) -> None:
    with pytest.raises(HTTPException) as exc:
        parse_slack(json.dumps(body))
    assert _code(exc) == (422, "attestation_required")
    with pytest.raises(HTTPException) as exc:
        parse_chat_thread("slack", json.dumps(body))
    assert _code(exc) == (422, "attestation_required")
    assert parse_chat_thread("slack", json.dumps(body), attestation=True).line_count == 3
    # A channel object needs no attestation.
    assert parse_slack(json.dumps({"channel": _CHANNEL, "messages": _SLACK}))


def test_slack_dm_message_refused_even_when_attested() -> None:
    body = [{"type": "message", "user": "U1", "text": "hi", "ts": "1", "channel_type": "im"}]
    with pytest.raises(HTTPException) as exc:
        parse_slack(json.dumps(body), attestation=True)
    assert _code(exc) == (422, "dm_not_supported")


def test_slack_email_keyed_by_user_id_and_dropped_on_name_collision() -> None:
    users = [
        {"id": "U1", "profile": {"display_name": "Sam", "email": "sam.a@acme.io"}},
        {"id": "U2", "profile": {"display_name": "Sam", "email": "sam.b@acme.io"}},
        {"id": "U3", "profile": {"display_name": "Tolu", "email": "tolu@acme.io"}},
    ]
    messages = [
        {"type": "message", "user": "U1", "text": "one", "ts": "10"},
        {"type": "message", "user": "U2", "text": "two", "ts": "20"},
        {"type": "message", "user": "U3", "text": "three", "ts": "30"},
    ]
    msgs = parse_slack(
        json.dumps({"channel": _CHANNEL, "messages": messages}), json.dumps(users)
    )
    # Two posters labelled "Sam": neither keeps an email (no first-wins).
    assert [(m.author_raw, m.email_raw) for m in msgs] == [
        ("Sam", None), ("Sam", None), ("Tolu", "tolu@acme.io"),
    ]
    # A namesake who never posted doesn't strip the poster's email.
    msgs = parse_slack(
        json.dumps({"channel": _CHANNEL, "messages": messages[:1] + messages[2:]}),
        json.dumps(users),
    )
    assert [(m.author_raw, m.email_raw) for m in msgs] == [
        ("Sam", "sam.a@acme.io"), ("Tolu", "tolu@acme.io"),
    ]


def test_slack_bad_ts_keeps_export_order() -> None:
    messages = [
        {"type": "message", "user": "U1", "text": "first", "ts": "10"},
        {"type": "message", "user": "U1", "text": "no time", "ts": "garbage"},
        {"type": "message", "user": "U1", "text": "third", "ts": "30"},
        {"type": "message", "user": "U1", "text": "second", "ts": "20"},
    ]
    msgs = parse_slack(json.dumps({"channel": _CHANNEL, "messages": messages}))
    # "no time" sorts with the message before it, not at the end.
    assert [m.text for m in msgs] == ["first", "no time", "second", "third"]
    assert msgs[1].posted_at is None


def test_discord_missing_time_keeps_export_order_and_odd_fractions_parse() -> None:
    body = dict(
        _DISCORD,
        messages=[
            {"type": "Default", "timestamp": "2026-10-29T14:02:00.1234567+00:00",
             "content": "a", "author": {"name": "tolu"}},
            {"type": "Default", "timestamp": "not a time", "content": "b", "author": {"name": "sam"}},
            {"type": "Default", "timestamp": "2026-10-29T14:03:00.5Z", "content": "c",
             "author": {"name": "sam"}},
            {"type": "Default", "timestamp": "2026-10-29T14:04:00Z", "content": "d",
             "author": {"name": "sam"}},
        ],
    )
    msgs = parse_discord(json.dumps(body))
    assert [m.text for m in msgs] == ["a", "b", "c", "d"]
    assert msgs[0].posted_at == datetime(2026, 10, 29, 14, 2, 0, 123456, tzinfo=timezone.utc)
    assert msgs[1].posted_at is None
    assert msgs[2].posted_at == datetime(2026, 10, 29, 14, 3, 0, 500000, tzinfo=timezone.utc)
    assert msgs[3].posted_at == datetime(2026, 10, 29, 14, 4, tzinfo=timezone.utc)


@pytest.mark.parametrize("ctype", ["directtextchat", "DIRECTGROUPTEXTCHAT", "1", "3"])
def test_discord_dm_type_variants_refused(ctype) -> None:
    body = dict(_DISCORD, channel={"id": "9", "type": ctype})
    with pytest.raises(HTTPException) as exc:
        parse_discord(json.dumps(body))
    assert _code(exc) == (422, "dm_not_supported")


def test_paste_header_time_variants_still_strip() -> None:
    msgs = parse_paste(
        "Tolu Ade  Yesterday 10:02\nhi\nthere\n\n"
        "Sam,  14:05:30 p.m.\nok\nsure\n\n"
        + "x" * 250 + " 10:02\nlong header\nis text\n"
    )
    assert [m.author_raw for m in msgs] == ["Tolu Ade", "Sam"]
    # A header over 200 characters is never parsed as an author.
    assert msgs[1].text.endswith("long header\nis text")


_MIB = 1024 * 1024


@pytest.mark.parametrize(
    "build",
    [
        lambda: "," * (_MIB - 16) + "x\nhello\n",
        lambda: "a" + " " * (_MIB - 16) + "b\nhello\n",
        lambda: ("Tolu" + " ," * 98 + " 1\nhi\n\n") * (_MIB // 210),
        lambda: ("a" + " " * 150 + ":\n") * (_MIB // 153),
        lambda: "[2026-01-01 10:00]" + " " * (_MIB - 40) + "\nx\n",
        lambda: ("x 1:11 " * (_MIB // 7 - 4)) + "\nhello\n",
        lambda: ("a: " * (_MIB // 3 - 3)) + "\n",
    ],
)
def test_paste_worst_cases_are_linear(build) -> None:
    import time

    content = build()
    assert len(content.encode("utf-8")) <= ct.MAX_RAW_PASTE_BYTES
    start = time.perf_counter()
    try:
        parse_paste(content)
    except HTTPException:
        pass
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize("unit", ["<@U1|", "<#C12|", "<@U1"])
def test_slack_unclosed_mentions_are_linear(unit: str) -> None:
    import time

    text = unit * (_MIB // len(unit))
    body = json.dumps({"channel": _CHANNEL, "messages": [{"user": "U1", "text": text, "ts": "1"}]})
    start = time.perf_counter()
    parse_slack(body)
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    "body",
    [
        {"messages": _DISCORD["messages"]},
        {"channel": _DISCORD["channel"], "messages": _DISCORD["messages"]},
        {"guild": _DISCORD["guild"], "messages": _DISCORD["messages"]},
        dict(_DISCORD, channel={"id": "222", "name": "infra"}),
        dict(_DISCORD, channel={"id": "222", "type": "SomethingNew"}),
        dict(_DISCORD, channel={"id": "222", "type": 99}),
        dict(_DISCORD, channel={"id": "222", "type": True}),
        dict(_DISCORD, guild="Acme"),
    ],
)
def test_discord_without_guild_channel_proof_needs_attestation(body) -> None:
    with pytest.raises(HTTPException) as exc:
        parse_discord(json.dumps(body))
    assert _code(exc) == (422, "attestation_required")
    with pytest.raises(HTTPException) as exc:
        parse_chat_thread("discord", json.dumps(body))
    assert _code(exc) == (422, "attestation_required")
    assert parse_chat_thread("discord", json.dumps(body), attestation=True).line_count == 2


@pytest.mark.parametrize(
    "ctype",
    ["GuildTextChat", "guildnews", "GuildPublicThread", "GuildPrivateThread", "GuildNewsThread",
     "GuildForum", 0, 5, 11, 12, "10"],
)
def test_discord_guild_channel_types_need_no_attestation(ctype) -> None:
    body = dict(_DISCORD, channel={"id": "222", "type": ctype, "name": "infra"})
    assert len(parse_discord(json.dumps(body))) == 2


def test_discord_dm_refused_before_attestation() -> None:
    body = {"channel": {"id": "9", "type": "DirectTextChat"}, "messages": _DISCORD["messages"]}
    with pytest.raises(HTTPException) as exc:
        parse_discord(json.dumps(body), attestation=True)
    assert _code(exc) == (422, "dm_not_supported")
