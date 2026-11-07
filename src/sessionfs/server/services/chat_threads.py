"""Chat thread parsers and the one-message-per-line normaliser (GitHub #118, C1).

A chat thread is stored as a ``ConversationSource`` with ``kind='chat'``
(docs/design/followons-2026-11.md §3). Every provider implements the same
interface — ``parse(content, users) -> list[ChatMessage]`` — and
``normalise_messages`` turns the messages into the stored text, one message
per line::

    [2026-10-29 14:02] Tolu: text ⏎ second line of the same message

so message N is line N and every line-range validator, citation check and
quote block written for meetings works unchanged. Per-line authors (and an
exporter-supplied email for authors who actually posted) are returned for
``conversation_message_meta``; they are participant evidence, never an
identity on their own.

Direct messages are refused (422 ``dm_not_supported``): Slack ``im`` /
``mpim`` (and any channel object that carries a ``user`` or is a private /
group conversation without a channel name) and Discord DM / group-DM
channels. A paste can't be told apart, so the upload route requires a
channel name and an attestation instead; so does a Slack export with no
channel object (a bare message array) and a Discord export without guild +
channel objects of a recognised guild channel type, 422
``attestation_required``.

Every pattern here is linear: no unbounded quantifier sits next to another
that can match the same characters, and the block-header time is matched
on the last few characters of a short header only.

Everything here is CPU-bound and synchronous; the route runs
``parse_chat_thread`` in a thread pool.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from fastapi import HTTPException

from sessionfs.server.sanitize import sanitize_archive_string

ChatProviderName = Literal["slack", "discord", "teams_paste", "paste"]
CHAT_PROVIDERS: tuple[str, ...] = ("slack", "discord", "teams_paste", "paste")
JSON_PROVIDERS = frozenset({"slack", "discord"})

# Raw export caps: JSON is verbose, so Slack/Discord get 2 MiB of raw text;
# pastes keep the meeting 1 MiB body cap. After normalising, the meeting caps
# (512 KiB / 10,000 lines) apply to the stored text.
MAX_RAW_JSON_BYTES = 2 * 1024 * 1024
MAX_RAW_PASTE_BYTES = 1024 * 1024
MAX_CONTENT_BYTES = 512 * 1024
MAX_MESSAGES = 10_000

# The separator that replaces a newline inside one message.
LINE_BREAK = " ⏎ "

_MAX_AUTHOR_CHARS = 80
_MAX_EMAIL_CHARS = 320


@dataclass(frozen=True)
class ChatMessage:
    author_raw: str
    text: str
    posted_at: datetime | None = None
    email_raw: str | None = None


@dataclass(frozen=True)
class MessageMeta:
    line_no: int
    author_raw: str
    email_raw: str | None
    posted_at: datetime | None


@dataclass(frozen=True)
class NormalisedThread:
    content: str
    line_count: int
    meta: list[MessageMeta]
    first_posted_at: datetime | None


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"error": code, "code": code, "message": message})


def _unparseable(message: str) -> HTTPException:
    return _error(422, "chat_unparseable", message)


def _dm_refused() -> HTTPException:
    return _error(
        422,
        "dm_not_supported",
        "Direct-message and group-DM exports can't be uploaded. Upload a channel instead.",
    )


def _attestation_required() -> HTTPException:
    return _error(
        422,
        "attestation_required",
        "Confirm this export is from a channel, not a direct message.",
    )


def _too_large(message: str = "Chat thread is too large") -> HTTPException:
    return _error(413, "chat_too_large", message)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    cleaned, _ = sanitize_archive_string(text)
    return cleaned


def _clean_author(value: Any) -> str:
    author = " ".join(_clean(value).split())
    # A colon would make the stored ``Author: text`` line ambiguous.
    author = author.replace(":", "：")
    return author[:_MAX_AUTHOR_CHARS]


def _clean_email(value: Any) -> str | None:
    email = _clean(value).strip()
    if not email or "@" not in email or any(c.isspace() for c in email):
        return None
    return email[:_MAX_EMAIL_CHARS]


def _load_json(content: str, what: str) -> Any:
    if len(content.encode("utf-8")) > MAX_RAW_JSON_BYTES:
        raise _too_large()
    try:
        return json.loads(content)
    except (ValueError, RecursionError):
        raise _unparseable(f"{what} export is not valid JSON") from None


# ── Slack ──

# System messages that aren't anyone saying anything.
_SLACK_SKIP_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "group_join",
    "group_leave", "group_topic", "group_purpose", "group_name",
    "group_archive", "group_unarchive", "pinned_item", "unpinned_item",
    "reminder_add", "tombstone",
})
# Labels are bounded so an unclosed ``<@U1|`` run can't rescan the rest of
# the text from every start position.
_SLACK_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]{1,30})(?:\|([^<>]{0,200}))?>")
_SLACK_CHANNEL_MENTION_RE = re.compile(r"<#[CG][A-Z0-9]{2,30}\|([^<>]{0,200})>")
_SLACK_DM_ID_RE = re.compile(r"D[A-Z0-9]{2,30}")


def _slack_is_dm_channel(channel: Any) -> bool:
    if isinstance(channel, str):
        # Slack DM channel ids start with D.
        return bool(_SLACK_DM_ID_RE.fullmatch(channel))
    if not isinstance(channel, dict):
        return False
    if channel.get("is_im") is True or channel.get("is_mpim") is True:
        return True
    # An IM object names the other person in ``user``; a channel never does.
    if isinstance(channel.get("user"), str) and channel["user"]:
        return True
    name = channel.get("name")
    if isinstance(name, str) and name.startswith("mpdm-"):
        return True
    # A private or (legacy) group conversation counts as a channel only when
    # it says so with a name; otherwise it is the DM / group-DM shape.
    if (channel.get("is_private") is True or channel.get("is_group") is True) and not (
        isinstance(name, str) and name.strip()
    ):
        return True
    cid = channel.get("id")
    return isinstance(cid, str) and _slack_is_dm_channel(cid)


def _slack_users_map(users: Any) -> dict[str, tuple[str, str | None]]:
    """``users.json``: a list of user objects -> ``{id: (name, email)}``."""
    if isinstance(users, dict) and isinstance(users.get("members"), list):
        users = users["members"]
    if not isinstance(users, list):
        return {}
    out: dict[str, tuple[str, str | None]] = {}
    for user in users[:50_000]:
        if not isinstance(user, dict) or not isinstance(user.get("id"), str):
            continue
        profile = _dict(user.get("profile"))
        name = ""
        for candidate in (
            profile.get("display_name"),
            profile.get("real_name"),
            user.get("real_name"),
            user.get("name"),
        ):
            name = _clean_author(candidate)
            if name:
                break
        out[user["id"]] = (name or user["id"][:_MAX_AUTHOR_CHARS], _clean_email(profile.get("email")))
    return out


def _slack_ts(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and len(value) > 40:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (0 < seconds < 32503680000):  # before year 3000
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _in_export_order(parsed: list[tuple[datetime | None, ChatMessage]]) -> list[ChatMessage]:
    """Sort by posted time, stably. A message with no usable time keeps its
    place in the export: it sorts with the time of the message before it
    (and after it, by index), never at the end."""
    keyed: list[tuple[float, int, ChatMessage]] = []
    previous = float("-inf")
    for index, (posted_at, msg) in enumerate(parsed):
        if posted_at is not None:
            previous = posted_at.timestamp()
        keyed.append((previous, index, msg))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [m for _, _, m in keyed]


def parse_slack(
    content: str, users: str | None = None, *, attestation: bool = False
) -> list[ChatMessage]:
    """One channel's (or one thread's) message array from a Slack export.

    Accepts a bare message list (a channel day file), or an object with
    ``messages`` (a ``conversations.replies`` / ``history`` response) and
    optionally ``channel`` and ``users``. ``users`` (the export's
    ``users.json``) maps ``U…`` ids to names and emails; without it the raw
    id (or the message's own ``user_profile`` name) is shown.

    Without a channel object the export can't be told apart from a DM, so
    it needs ``attestation=True`` (422 ``attestation_required``), like a
    paste. Exporter emails are kept per user id: when two posters share a
    display name, neither keeps an email (a label must never carry someone
    else's address).
    """
    data = _load_json(content, "Slack")
    user_data: Any = None
    if users:
        user_data = _load_json(users, "Slack users.json")
    has_channel_object = False
    if isinstance(data, dict):
        if data.get("is_im") is True or data.get("is_mpim") is True:
            raise _dm_refused()
        if data.get("channel_type") in ("im", "mpim"):
            raise _dm_refused()
        for key in ("channel", "channel_info"):
            if _slack_is_dm_channel(data.get(key)):
                raise _dm_refused()
            if isinstance(data.get(key), dict):
                has_channel_object = True
        if user_data is None and "users" in data:
            user_data = data.get("users")
        messages = data.get("messages")
    else:
        messages = data
    if not isinstance(messages, list):
        raise _unparseable("Slack export must be a message array or an object with messages")
    user_map = _slack_users_map(user_data)

    def mention(match: re.Match) -> str:
        uid, label = match.group(1), match.group(2)
        if uid in user_map:
            return "@" + user_map[uid][0]
        return "@" + (_clean_author(label) or uid)

    rows: list[tuple[datetime | None, str | None, str, str | None, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("channel_type") in ("im", "mpim") or _slack_is_dm_channel(msg.get("channel")):
            raise _dm_refused()
        if msg.get("type", "message") != "message":
            continue
        if msg.get("subtype") in _SLACK_SKIP_SUBTYPES:
            continue
        text = _clean(msg.get("text"))
        text = _SLACK_USER_MENTION_RE.sub(mention, text)
        text = _SLACK_CHANNEL_MENTION_RE.sub(lambda m: "#" + _clean_author(m.group(1)), text)
        text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        if not text.strip():
            continue
        uid = msg.get("user") if isinstance(msg.get("user"), str) else None
        email: str | None = None
        author = ""
        if uid and uid in user_map:
            author, email = user_map[uid]
        if not author:
            profile = _dict(msg.get("user_profile"))
            bot = _dict(msg.get("bot_profile"))
            for candidate in (
                profile.get("display_name"),
                profile.get("real_name"),
                msg.get("username"),
                bot.get("name"),
                uid,
            ):
                author = _clean_author(candidate)
                if author:
                    break
        rows.append((_slack_ts(msg.get("ts")), uid, author or "unknown", email, text))
    # DM detection runs over every message first; only then is a channel-less
    # export refused for want of an attestation.
    if not has_channel_object and not attestation:
        raise _attestation_required()
    # The exporter's email is keyed by user id. A display name shared by two
    # posters keeps no email at all, so a label never carries another
    # person's address.
    uids_by_label: dict[str, set[str]] = {}
    for _, uid, author, email, _ in rows:
        if email and uid:
            uids_by_label.setdefault(author.casefold(), set()).add(uid)
    parsed: list[tuple[datetime | None, ChatMessage]] = []
    for posted_at, uid, author, email, text in rows:
        if email and len(uids_by_label.get(author.casefold(), ())) != 1:
            email = None
        parsed.append((
            posted_at,
            ChatMessage(author_raw=author, text=text, posted_at=posted_at, email_raw=email),
        ))
    return _in_export_order(parsed)


# ── Discord (DiscordChatExporter JSON) ──

_DISCORD_KEEP_TYPES = frozenset({"Default", "Reply", "ThreadStarterMessage", 0, 19, 21})
# Compared case-insensitively on the string form ("DirectTextChat", "1").
_DISCORD_DM_TYPES = frozenset({"directtextchat", "directgrouptextchat", "dm", "group_dm", "1", "3"})
# ``2026-10-29T14:02:00.1234567`` → the fraction, padded/truncated to the six
# digits Python 3.10's fromisoformat requires.
_ISO_FRACTION_RE = re.compile(r"(T\d{2}:\d{2}:\d{2})\.(\d{1,12})")


# Guild channel kinds (DiscordChatExporter names and Discord's numeric
# types), threads included. Only these are accepted without attestation.
_DISCORD_GUILD_TYPES = frozenset({
    "guildtextchat", "guildvoicechat", "guildcategory", "guildnews",
    "guildnewsthread", "guildpublicthread", "guildprivatethread",
    "guildstagevoice", "guilddirectory", "guildforum", "guildmedia",
    "0", "2", "4", "5", "10", "11", "12", "13", "14", "15", "16",
})


def _discord_type_key(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip().casefold()


def _discord_is_guild_type(value: Any) -> bool:
    return _discord_type_key(value) in _DISCORD_GUILD_TYPES


def _discord_is_dm_type(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    return str(value).strip().casefold() in _DISCORD_DM_TYPES


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    value = value.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    value = _ISO_FRACTION_RE.sub(
        lambda m: f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}", value, count=1
    )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_discord(
    content: str, users: str | None = None, *, attestation: bool = False
) -> list[ChatMessage]:
    """A DiscordChatExporter JSON export of one channel or thread.

    DM / group-DM channels are refused. Without attestation the export must
    prove it is a guild channel: ``guild`` and ``channel`` objects with a
    recognised guild channel type; anything else (missing metadata, an
    unknown type) needs ``attestation=True`` (422 ``attestation_required``).
    """
    del users  # Discord exports carry their own author names.
    data = _load_json(content, "Discord")
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise _unparseable("Discord export must be a DiscordChatExporter JSON object")
    channel = data.get("channel")
    if isinstance(channel, dict) and _discord_is_dm_type(channel.get("type")):
        raise _dm_refused()
    guild = data.get("guild")
    if isinstance(guild, dict) and (
        guild.get("id") in ("0", 0) or guild.get("name") == "Direct Messages"
    ):
        raise _dm_refused()
    is_guild_channel = (
        isinstance(guild, dict)
        and isinstance(channel, dict)
        and _discord_is_guild_type(channel.get("type"))
    )
    if not is_guild_channel and not attestation:
        raise _attestation_required()
    out: list[tuple[datetime | None, ChatMessage]] = []
    for msg in data["messages"]:
        if not isinstance(msg, dict):
            continue
        if msg.get("type", "Default") not in _DISCORD_KEEP_TYPES:
            continue
        text = _clean(msg.get("content"))
        if not text.strip():
            continue
        author_obj = _dict(msg.get("author"))
        author = ""
        for candidate in (author_obj.get("nickname"), author_obj.get("name"), author_obj.get("id")):
            author = _clean_author(candidate)
            if author:
                break
        posted_at = _parse_iso(msg.get("timestamp"))
        out.append((posted_at, ChatMessage(author_raw=author or "unknown", text=text, posted_at=posted_at)))
    return _in_export_order(out)


# ── Paste (any tool, including Teams) ──

_PASTE_STAMP = r"\[(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})(?::\d{2})?\]"
# The name group is bounded and runs straight into the colon (spaces before
# the colon are stripped afterwards), so no two quantifiers compete for the
# same run of whitespace.
_PASTE_LINE_RE = re.compile(
    rf"^(?:{_PASTE_STAMP}[ \t]{{0,8}})?([^\s:][^:]{{0,79}}?):\s+(\S.*)$"
)
# A block header's trailing time ("Tolu Ade 10:02 AM", "Tolu, 14:02",
# "Tolu Ade  Yesterday 10:02"). Searched on the header's last
# _HEADER_TAIL_CHARS characters only, with bounded separators.
_HEADER_TIME_RE = re.compile(
    r"[\s,](?:(?:yesterday|today|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s{1,4})?"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:\s{0,3}[AaPp]\.?[Mm]\.?)?$",
    re.IGNORECASE,
)
_HEADER_TAIL_CHARS = 40
# A longer first line is never an author header (and is never regex-parsed).
_MAX_HEADER_CHARS = 200


def _strip_header_time(header: str) -> str:
    tail_start = max(len(header) - _HEADER_TAIL_CHARS, 0)
    match = _HEADER_TIME_RE.search(header, tail_start)
    if match is not None:
        header = header[: match.start()]
    return header.strip(" ,-")


def _looks_like_name(name: str) -> bool:
    # Up to six words and no URL scheme: "https://x" must stay text.
    return 0 < len(name.split()) <= 6 and "//" not in name and not name.isdigit()


def _paste_line(line: str) -> tuple[str, str, datetime | None] | None:
    match = _PASTE_LINE_RE.match(line)
    if not match:
        return None
    day, clock, name, text = match.groups()
    name = name.strip()
    if not _looks_like_name(name):
        return None
    posted_at = None
    if day and clock:
        try:
            posted_at = datetime.fromisoformat(f"{day} {clock.zfill(5)}").replace(tzinfo=timezone.utc)
        except ValueError:
            posted_at = None
    return name, text, posted_at


def parse_paste(content: str, users: str | None = None) -> list[ChatMessage]:
    """``Name: text`` lines, or blank-line-separated blocks (author first).

    Line mode when most non-blank lines look like ``Name: text`` (a line that
    doesn't continues the previous message); otherwise block mode, where each
    block's first line is the author (a trailing time is dropped) and the rest
    is the message.
    """
    del users
    if len(content.encode("utf-8")) > MAX_RAW_PASTE_BYTES:
        raise _too_large()
    text = _clean(content).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    nonblank = [line for line in lines if line.strip()]
    if not nonblank:
        raise _unparseable("The paste is empty")
    matched = [_paste_line(line.strip()) for line in nonblank]
    messages: list[list[Any]] = []  # [author, [text lines], posted_at]
    if sum(1 for m in matched if m) * 10 >= len(nonblank) * 6:
        for line, parsed in zip(nonblank, matched):
            if parsed is not None:
                messages.append([parsed[0], [parsed[1]], parsed[2]])
            elif messages:
                messages[-1][1].append(line.strip())
            else:
                messages.append(["unknown", [line.strip()], None])
    else:
        block: list[str] = []
        blocks: list[list[str]] = []
        for line in lines:
            if line.strip():
                block.append(line.strip())
            elif block:
                blocks.append(block)
                block = []
        if block:
            blocks.append(block)
        for blk in blocks:
            single = _paste_line(blk[0]) if len(blk) == 1 else None
            if single is not None:
                messages.append([single[0], [single[1]], single[2]])
                continue
            if len(blk) == 1:
                if messages:
                    messages[-1][1].append(blk[0])
                else:
                    messages.append(["unknown", [blk[0]], None])
                continue
            header = (
                _strip_header_time(blk[0]) if len(blk[0]) <= _MAX_HEADER_CHARS else ""
            )
            if not _looks_like_name(header):
                if messages:
                    messages[-1][1].extend(blk)
                else:
                    messages.append(["unknown", blk, None])
                continue
            messages.append([header, blk[1:], None])
    return [
        ChatMessage(author_raw=_clean_author(author) or "unknown", text="\n".join(body), posted_at=posted)
        for author, body, posted in messages
        if any(part.strip() for part in body)
    ]


PARSERS: dict[str, Callable[[str, str | None], list[ChatMessage]]] = {
    "slack": parse_slack,
    "discord": parse_discord,
    "teams_paste": parse_paste,
    "paste": parse_paste,
}


# ── Normaliser ──


def _one_line(text: str) -> str:
    text = _clean(text).replace("\r\n", "\n").replace("\r", "\n")
    parts = [" ".join(part.split()) for part in text.split("\n")]
    return LINE_BREAK.join(part for part in parts if part)


def normalise_messages(messages: list[ChatMessage]) -> NormalisedThread:
    """One message per line: ``[YYYY-MM-DD HH:MM] Author: text``.

    Newlines inside a message become `` ⏎ ``, so message N is line N. Raises
    413 ``chat_too_large`` past 10,000 messages or 512 KiB, 422
    ``chat_unparseable`` when no message has any text.
    """
    lines: list[str] = []
    meta: list[MessageMeta] = []
    first: datetime | None = None
    for msg in messages:
        body = _one_line(msg.text)
        if not body:
            continue
        if len(lines) >= MAX_MESSAGES:
            raise _too_large("Chat thread has too many messages")
        author = _clean_author(msg.author_raw) or "unknown"
        stamp = ""
        if msg.posted_at is not None:
            posted = msg.posted_at.astimezone(timezone.utc)
            stamp = posted.strftime("[%Y-%m-%d %H:%M] ")
            if first is None or posted < first:
                first = posted
        lines.append(f"{stamp}{author}: {body}")
        meta.append(
            MessageMeta(
                line_no=len(lines),
                author_raw=author,
                email_raw=msg.email_raw,
                posted_at=msg.posted_at,
            )
        )
    if not lines:
        raise _unparseable("The thread has no messages with text")
    content = "\n".join(lines)
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise _too_large()
    return NormalisedThread(content=content, line_count=len(lines), meta=meta, first_posted_at=first)


def parse_chat_thread(
    provider: str, content: str, users: str | None = None, *, attestation: bool = False
) -> NormalisedThread:
    """Parse + normalise a raw export. Synchronous; run it in a thread pool.

    ``attestation`` is the uploader's "this is a channel, not a DM"; a Slack
    export without a channel object needs it.
    """
    parser = PARSERS.get(provider)
    if parser is None:
        raise _unparseable("Unknown chat provider")
    if users is not None and provider != "slack":
        raise _error(422, "users_not_supported", "users is only accepted for Slack exports")
    if provider == "slack":
        return normalise_messages(parse_slack(content, users, attestation=attestation))
    if provider == "discord":
        return normalise_messages(parse_discord(content, users, attestation=attestation))
    return normalise_messages(parser(content, users))
