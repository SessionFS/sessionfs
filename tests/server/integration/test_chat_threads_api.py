"""Chat thread ingestion API (GitHub #118, C1).

Upload (project + group) with the meeting access rules, DLP, caps and DM
refusal; the ``?kind=`` filter on list/search/get; chat extraction (prompt
variant, ``chat_extract`` ledger rows on every path, evidence-gated
participants); promote of a chat item; retention covering chat rows.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import sessionfs.server.services.meeting_extraction as extraction_mod
from sessionfs.judge.agent_stream import Done, TextDelta, Usage
from sessionfs.server.db.models import (
    ConversationMessageMeta,
    ConversationSource,
    HelperUsage,
    MeetingParticipant,
    MeetingTranscript,
    OrgAuditEvent,
    Reminder,
    User,
)
from tests.server.integration.test_meeting_actions_api import (
    _fake_resolve,
    _fake_stream,
    _items,
    _participants,
)
from tests.server.integration.test_meetings_api import (
    _group,
    _meeting_row,
    _member,
    _now,
    _org,
    _project,
    _service_key,
    _user,
    _user_headers,
)

_SLACK = [
    {"type": "message", "user": "U1", "text": "We go with Dataproc for the batch jobs", "ts": "1761746520.0"},
    {"type": "message", "user": "U2", "text": "ok. I'll move the LB cert\nby Friday", "ts": "1761746580.0"},
    {"type": "message", "user": "U1", "text": "thanks <@U2>", "ts": "1761746640.0"},
]


def _users(alice_email: str, bob_email: str, lurker_email: str) -> list[dict]:
    return [
        {"id": "U1", "name": "alice", "profile": {"display_name": "Alice", "email": alice_email}},
        {"id": "U2", "name": "bob", "profile": {"display_name": "Bob", "email": bob_email}},
        {"id": "U3", "name": "lurker", "profile": {"display_name": "Lurker", "email": lurker_email}},
    ]


def _slack_body(**extra) -> dict:
    body = {
        "provider": "slack",
        "channel_name": "#infra",
        "content": json.dumps({"channel": {"id": "C0INFRA", "name": "infra"}, "messages": _SLACK}),
    }
    body.update(extra)
    return body


def _err(resp) -> dict:
    """The error envelope's ``code`` plus its ``details`` in one dict."""
    error = resp.json()["error"]
    return {"code": error["code"], **(error.get("details") or {})}


async def _chat_count(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(ConversationSource).where(ConversationSource.kind == "chat")
        )
    ).scalar_one()


# ── Upload ──


async def test_project_slack_upload_get_list_kind_filter(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    up = await client.post(
        f"/api/v1/projects/{project.id}/chat-threads",
        headers=auth_headers,
        json=_slack_body(users=json.dumps(_users("a@x.io", "b@x.io", "l@x.io"))),
    )
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["id"].startswith("chat_")
    assert (body["kind"], body["provider"], body["channel_name"]) == ("chat", "slack", "#infra")
    assert body["title"] == "#infra"
    assert body["source"] == "chat_export"
    assert body["line_count"] == 3
    lines = body["content"].split("\n")
    assert lines[0] == "[2025-10-29 14:02] Alice: We go with Dataproc for the batch jobs"
    assert lines[1].endswith("Bob: ok. I'll move the LB cert ⏎ by Friday")
    assert lines[2].endswith("Alice: thanks @Bob")
    assert [(m["line_no"], m["author_raw"]) for m in body["messages"]] == [
        (1, "Alice"), (2, "Bob"), (3, "Alice"),
    ]
    assert body["meeting_at"].startswith("2025-10-29T14:02")
    chat_id = body["id"]
    meta = (
        await db_session.execute(
            select(ConversationMessageMeta).where(ConversationMessageMeta.source_id == chat_id)
        )
    ).scalars().all()
    # The users.json-only lurker was never stored.
    assert {m.email_raw for m in meta} == {"a@x.io", "b@x.io"}

    meeting = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": "standup", "content": "Alice: Dataproc"},
    )
    assert meeting.status_code == 200
    meeting_id = meeting.json()["id"]

    url = f"/api/v1/projects/{project.id}/meetings"
    default = (await client.get(url, headers=auth_headers)).json()["meetings"]
    assert [m["id"] for m in default] == [meeting_id]
    assert default[0]["kind"] == "meeting"
    chats = (await client.get(url, headers=auth_headers, params={"kind": "chat"})).json()["meetings"]
    assert [m["id"] for m in chats] == [chat_id]
    both = (await client.get(url, headers=auth_headers, params={"kind": "all"})).json()["meetings"]
    assert {m["id"] for m in both} == {chat_id, meeting_id}
    search = (
        await client.get(url, headers=auth_headers, params={"kind": "chat", "q": "dataproc"})
    ).json()["meetings"]
    assert [m["id"] for m in search] == [chat_id]
    assert (await client.get(url, headers=auth_headers, params={"kind": "bogus"})).status_code == 422

    # Id routes accept any kind; ?kind= must match or it is the same 404.
    assert (await client.get(f"/api/v1/meetings/{chat_id}", headers=auth_headers)).status_code == 200
    assert (
        await client.get(f"/api/v1/meetings/{chat_id}", headers=auth_headers, params={"kind": "chat"})
    ).status_code == 200
    wrong = await client.get(f"/api/v1/meetings/{chat_id}", headers=auth_headers, params={"kind": "meeting"})
    missing = await client.get("/api/v1/meetings/chat_0000000000000000", headers=auth_headers)
    assert wrong.status_code == missing.status_code == 404
    assert wrong.json()["error"] == missing.json()["error"]
    gone = await client.get(f"/api/v1/meetings/{meeting_id}", headers=auth_headers, params={"kind": "chat"})
    assert gone.status_code == 404

    # Re-upload: duplicate warning, like meetings.
    again = await client.post(
        f"/api/v1/projects/{project.id}/chat-threads",
        headers=auth_headers,
        json=_slack_body(users=json.dumps(_users("a@x.io", "b@x.io", "l@x.io"))),
    )
    assert again.json()["warnings"] == [f"duplicate_of: {chat_id}"]

    # Delete works for chat rows too.
    deleted = await client.delete(f"/api/v1/meetings/{chat_id}", headers=auth_headers)
    assert deleted.status_code == 200, deleted.text
    audit = (
        await db_session.execute(
            select(OrgAuditEvent.event_type).where(OrgAuditEvent.target_id == chat_id)
        )
    ).scalars().all()
    assert "chat_thread_uploaded" in audit


async def test_paste_requires_attestation_and_parses(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/chat-threads"
    paste = {"provider": "teams_paste", "channel_name": "Infra team", "content": "Tolu: hi\nSam: ok"}
    refused = await client.post(url, headers=auth_headers, json=paste)
    assert refused.status_code == 422
    assert _err(refused)["code"] == "attestation_required"
    assert await _chat_count(db_session) == 0

    ok = await client.post(url, headers=auth_headers, json={**paste, "attestation": True, "title": "Cutover"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["content"] == "Tolu: hi\nSam: ok"
    assert ok.json()["title"] == "Cutover"
    assert ok.json()["provider"] == "teams_paste"


@pytest.mark.parametrize(
    "body,code",
    [
        ({"provider": "slack", "content": "[]"}, "validation_error"),  # no channel
        ({"provider": "slack", "channel_name": "", "content": "[]"}, "validation_error"),
        ({"provider": "slack", "channel_name": "x" * 81, "content": "[]"}, "validation_error"),
        ({"provider": "irc", "channel_name": "c", "content": "[]"}, "validation_error"),
        ({"provider": "slack", "channel_name": "c", "content": "nope"}, "chat_unparseable"),
        ({"provider": "slack", "channel_name": "c", "attestation": True, "content": "[]"},
         "chat_unparseable"),
        # A bare Slack array has no channel object: attestation required.
        ({"provider": "slack", "channel_name": "c", "content": json.dumps(_SLACK)},
         "attestation_required"),
        ({"provider": "slack", "channel_name": "c", "attestation": False,
          "content": json.dumps({"messages": _SLACK})}, "attestation_required"),
        ({"provider": "slack", "channel_name": "c", "attestation": True,
          "content": json.dumps({"channel": {"id": "C1", "user": "U2"}, "messages": _SLACK})},
         "dm_not_supported"),
        ({"provider": "slack", "channel_name": "c",
          "content": json.dumps({"channel": {"id": "G1", "is_private": True}, "messages": _SLACK})},
         "dm_not_supported"),
        ({"provider": "discord", "channel_name": "c",
          "content": json.dumps({"channel": {"type": "DirectGroupTextChat"}, "messages": []})},
         "dm_not_supported"),
        # Discord without guild + channel proof of a guild channel type.
        ({"provider": "discord", "channel_name": "c",
          "content": json.dumps({"messages": [{"content": "hi", "author": {"name": "a"}}]})},
         "attestation_required"),
        ({"provider": "discord", "channel_name": "c",
          "content": json.dumps({"guild": {"id": "1"}, "channel": {"type": "Mystery"},
                                 "messages": [{"content": "hi", "author": {"name": "a"}}]})},
         "attestation_required"),
        ({"provider": "slack", "channel_name": "c",
          "content": json.dumps({"channel": {"id": "D1", "is_im": True}, "messages": _SLACK})},
         "dm_not_supported"),
        ({"provider": "discord", "channel_name": "c",
          "content": json.dumps({"channel": {"type": "DirectTextChat"}, "messages": []})},
         "dm_not_supported"),
        ({"provider": "paste", "channel_name": "c", "attestation": True, "content": "a: b",
          "users": "[]"}, "users_not_supported"),
    ],
)
async def test_upload_refusals(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, body, code
) -> None:
    project = await _project(db_session, test_user)
    resp = await client.post(f"/api/v1/projects/{project.id}/chat-threads", headers=auth_headers, json=body)
    assert resp.status_code == 422, resp.text
    assert _err(resp)["code"] == code
    assert await _chat_count(db_session) == 0


async def test_attested_bare_slack_array_accepted(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/chat-threads",
        headers=auth_headers,
        json=_slack_body(content=json.dumps(_SLACK), attestation=True),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["line_count"] == 3


async def test_discord_guild_thread_and_attested_bare_export_accepted(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/chat-threads"
    msgs = [{"type": "Default", "content": "hi", "author": {"name": "a"}}]
    thread = {"guild": {"id": "1", "name": "Acme"}, "channel": {"id": "2", "type": "GuildPublicThread"},
              "messages": msgs}
    ok = await client.post(url, headers=auth_headers,
                           json={"provider": "discord", "channel_name": "#c", "content": json.dumps(thread)})
    assert ok.status_code == 200, ok.text
    bare = await client.post(url, headers=auth_headers, json={
        "provider": "discord", "channel_name": "#c", "attestation": True,
        "content": json.dumps({"messages": msgs + [{"type": "Default", "content": "yo", "author": {"name": "b"}}]}),
    })
    assert bare.status_code == 200, bare.text


async def test_channel_name_capped_before_dlp(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sessionfs.server.routes.meetings as meetings_mod

    seen: list[str] = []
    real = meetings_mod._dlp_scan_content

    def _spy(content, policy):
        seen.append(content)
        return real(content, policy)

    monkeypatch.setattr(meetings_mod, "_dlp_scan_content", _spy)
    owner = await _user(db_session, "Owner")
    project = await _project(db_session, owner)
    h = await _user_headers(db_session, owner)
    # 80 visible characters plus padding the route trims: the scan sees the
    # stored 80-character label, never a longer one.
    label = "  " + "c" * 80
    resp = await client.post(
        f"/api/v1/projects/{project.id}/chat-threads", headers=h, json=_slack_body(channel_name=label[:80])
    )
    assert resp.status_code == 200, resp.text
    stored = resp.json()["channel_name"]
    assert stored in seen and len(stored) <= 80


async def test_upload_caps(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/chat-threads"
    # Raw Slack JSON over 2 MiB.
    big = json.dumps([{"type": "message", "user": "U1", "text": "x" * 1000, "ts": "1"}] * 2200)
    resp = await client.post(url, headers=auth_headers, json=_slack_body(content=big, attestation=True))
    assert resp.status_code == 413
    assert _err(resp)["error"] == "chat_too_large"
    # Under the raw cap but over 10,000 messages after normalising.
    many = json.dumps([{"type": "message", "user": "U1", "text": "y", "ts": str(i + 1)} for i in range(10_001)])
    resp = await client.post(url, headers=auth_headers, json=_slack_body(content=many, attestation=True))
    assert resp.status_code == 413
    # Over 512 KiB of normalised text.
    wide = json.dumps([{"type": "message", "user": "U1", "text": "z" * 900, "ts": str(i + 1)} for i in range(700)])
    resp = await client.post(url, headers=auth_headers, json=_slack_body(content=wide, attestation=True))
    assert resp.status_code == 413
    # A body over the streamed cap.
    huge = await client.post(
        url, headers=auth_headers, json=_slack_body(content="q" * (5 * 1024 * 1024 + 10))
    )
    assert huge.status_code == 413
    assert await _chat_count(db_session) == 0


async def test_dlp_block_refuses_and_redact_covers_authors(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    def _settings(mode: str) -> str:
        return json.dumps({
            "dlp": {
                "enabled": True,
                "mode": mode,
                "categories": ["secrets"],
                "custom_patterns": [
                    {"name": "codename", "regex": r"Bluebird\d", "category": "pii", "severity": "high"}
                ],
            }
        })

    org = await _org(db_session, settings=_settings("block"))
    owner = await _user(db_session, "Owner")
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    h = await _user_headers(db_session, owner)
    leaky = [{"type": "message", "user": "U1", "text": "token for Bluebird7", "ts": "1"}]
    resp = await client.post(
        f"/api/v1/projects/{project.id}/chat-threads", headers=h, json=_slack_body(content=json.dumps(leaky), attestation=True)
    )
    assert resp.status_code == 422
    assert _err(resp)["error"] == "dlp_blocked"
    assert await _chat_count(db_session) == 0

    org2 = await _org(db_session, settings=_settings("redact"))
    owner2 = await _user(db_session, "Owner2")
    await _member(db_session, org2, owner2, role="owner")
    project2 = await _project(db_session, owner2, org=org2)
    h2 = await _user_headers(db_session, owner2)
    author_leak = [{"type": "message", "user": "U1", "user_profile": {"real_name": "Bluebird9"},
                    "text": "hello", "ts": "1"}]
    resp = await client.post(
        f"/api/v1/projects/{project2.id}/chat-threads", headers=h2,
        json=_slack_body(content=json.dumps(author_leak), attestation=True),
    )
    assert resp.status_code == 200, resp.text
    assert "Bluebird9" not in resp.json()["content"]
    assert all("Bluebird9" not in m["author_raw"] for m in resp.json()["messages"])
    assert resp.json()["dlp_findings"] >= 2


async def test_upload_auth_parity_with_meetings(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org = await _org(db_session, tier="team")
    owner = await _user(db_session, "Owner")
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    outsider = await _user(db_session, "Outsider")
    out_h = await _user_headers(db_session, outsider)

    chat = await client.post(f"/api/v1/projects/{project.id}/chat-threads", headers=out_h, json=_slack_body())
    meet = await client.post(
        f"/api/v1/projects/{project.id}/meetings", headers=out_h, json={"title": "t", "content": "x"}
    )
    unknown = await client.post("/api/v1/projects/proj_nope/chat-threads", headers=out_h, json=_slack_body())
    assert chat.status_code == meet.status_code == unknown.status_code == 404
    assert chat.json()["error"] == meet.json()["error"] == unknown.json()["error"]

    # Service keys: meetings:write uploads, meetings:read is refused.
    write_h = await _service_key(
        db_session, owner=owner, org_id=org.id, scopes=["meetings:write"], project_ids=[project.id]
    )
    ok = await client.post(f"/api/v1/projects/{project.id}/chat-threads", headers=write_h, json=_slack_body())
    assert ok.status_code == 200, ok.text
    read_h = await _service_key(
        db_session, owner=owner, org_id=org.id, scopes=["meetings:read"], project_ids=[project.id]
    )
    denied = await client.post(f"/api/v1/projects/{project.id}/chat-threads", headers=read_h, json=_slack_body())
    assert denied.status_code == 403
    # Group chat routes are user-key only.
    group = await _group(db_session, org, owner)
    grp = await client.post(f"/api/v1/groups/{group.id}/chat-threads", headers=write_h, json=_slack_body())
    assert grp.status_code == 403


async def test_group_upload_visibility_before_plan_gate(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    team_org = await _org(db_session, tier="team")
    owner = await _user(db_session, "Owner")
    await _member(db_session, team_org, owner, role="owner")
    group = await _group(db_session, team_org, owner)
    owner_h = await _user_headers(db_session, owner)
    up = await client.post(f"/api/v1/groups/{group.id}/chat-threads", headers=owner_h, json=_slack_body())
    assert up.status_code == 200, up.text
    assert up.json()["group_id"] == group.id and up.json()["kind"] == "chat"
    listed = await client.get(
        f"/api/v1/groups/{group.id}/meetings", headers=owner_h, params={"kind": "chat"}
    )
    assert [m["id"] for m in listed.json()["meetings"]] == [up.json()["id"]]
    assert (await client.get(f"/api/v1/groups/{group.id}/meetings", headers=owner_h)).json()["meetings"] == []

    other_org = await _org(db_session, tier="team")
    other = await _user(db_session, "Other")
    await _member(db_session, other_org, other, role="owner")
    other_h = await _user_headers(db_session, other)
    expected = {"code": "404", "message": "Project group not found", "details": {}}
    for gid in (group.id, "pg_missing"):
        resp = await client.post(f"/api/v1/groups/{gid}/chat-threads", headers=other_h, json=_slack_body())
        assert resp.status_code == 404
        assert resp.json()["error"] == expected

    # A free-tier org's group: the owner can see it, so the plan gate answers.
    free_org = await _org(db_session, tier="free")
    free_owner = await _user(db_session, "FreeOwner")
    await _member(db_session, free_org, free_owner, role="owner")
    free_group = await _group(db_session, free_org, free_owner)
    free_h = await _user_headers(db_session, free_owner)
    gated = await client.post(f"/api/v1/groups/{free_group.id}/chat-threads", headers=free_h, json=_slack_body())
    assert gated.status_code == 403
    # A member of the free org who isn't the owner sees the same plan answer
    # only if visible; an outsider still gets the 404.
    gated_out = await client.post(
        f"/api/v1/groups/{free_group.id}/chat-threads", headers=other_h, json=_slack_body()
    )
    assert gated_out.status_code == 404


# ── Extraction ──

_CHAT_EXTRACT = json.dumps({
    "participants": [
        {"name": "Alice"},
        {"name": "Bob"},
        {"name": "Lurker", "email": "lurker@acme.io"},
        {"name": "Mallory", "email": "mallory@acme.io"},
    ],
    "items": [
        {"kind": "decision", "description": "Use Dataproc for batch jobs.", "owner_name": "Alice",
         "line_start": 1, "line_end": 1},
        {"kind": "action", "description": "Move the LB cert.", "owner_name": "Bob",
         "due_date": (_now().date() + timedelta(days=3)).isoformat(), "line_start": 2, "line_end": 2},
        {"kind": "action", "description": "Out of range.", "line_start": 3, "line_end": 4},
    ],
})


async def _org_chat(client: AsyncClient, db: AsyncSession) -> tuple:
    org = await _org(db, tier="team")
    alice = await _user(db, "Alice", email="alice@acme.io")
    bob = await _user(db, "Robert", email="bob@acme.io")
    lurker = await _user(db, "Lurker", email="lurker@acme.io")
    mallory = await _user(db, "Mallory", email="mallory@acme.io")
    for u, role in ((alice, "owner"), (bob, "member"), (lurker, "member"), (mallory, "member")):
        await _member(db, org, u, role=role)
    project = await _project(db, alice, org=org)
    h = await _user_headers(db, alice)
    up = await client.post(
        f"/api/v1/projects/{project.id}/chat-threads",
        headers=h,
        json=_slack_body(users=json.dumps(_users("alice@acme.io", "bob@acme.io", "lurker@acme.io"))),
    )
    assert up.status_code == 200, up.text
    return org, project, h, up.json()["id"], {"alice": alice, "bob": bob, "lurker": lurker}


async def test_chat_extraction_variant_ledger_and_evidence(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, h, chat_id, users = await _org_chat(client, db_session)
    seen: dict = {}

    async def _stream(**kwargs):
        seen.update(kwargs)
        yield Usage(input_tokens=30, output_tokens=9)
        yield TextDelta(_CHAT_EXTRACT)
        yield Done("stop")

    monkeypatch.setattr(extraction_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(extraction_mod, "stream_chat", _stream)
    resp = await client.post(f"/api/v1/meetings/{chat_id}/extract", headers=h, params={"kind": "chat"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["dropped_items"] == 1

    assert "chat thread" in seen["system"]
    prompt = seen["messages"][0]["content"]
    prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt)
    assert "<chat_thread>" in prompt_text and "M1\\t" in json.dumps(prompt_text)

    ledger = (
        await db_session.execute(select(HelperUsage).where(HelperUsage.conversation_id == chat_id))
    ).scalars().all()
    assert [(r.source, r.input_tokens) for r in ledger] == [("chat_extract", 30)]

    parts = {p.name_raw: p for p in await _participants(db_session, chat_id)}
    # Alice: users.json email of a poster -> exact email match.
    assert parts["Alice"].user_id == users["alice"].id
    assert parts["Alice"].resolution_method == "exact_email"
    assert parts["Bob"].user_id == users["bob"].id
    # Lurker never posted or appears in the text: stays unresolved even though
    # users.json (never stored for him) and the model both name his email.
    assert parts["Lurker"].user_id is None
    assert parts["Mallory"].user_id is None

    items = await _items(db_session, chat_id)
    assert sorted((i.kind, i.line_start) for i in items) == [("action", 2), ("decision", 1)]
    events = (
        await db_session.execute(
            select(OrgAuditEvent.event_type).where(OrgAuditEvent.target_id == chat_id)
        )
    ).scalars().all()
    assert "chat_thread_extracted" in events


async def test_unmatched_exporter_email_is_not_kept_on_the_participant(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await _org(db_session, tier="team")
    alice = await _user(db_session, "Alice", email="alice@acme.io")
    await _member(db_session, org, alice, role="owner")
    project = await _project(db_session, alice, org=org)
    h = await _user_headers(db_session, alice)
    # users.json gives Alice an address no member has, and Bob one too.
    up = await client.post(
        f"/api/v1/projects/{project.id}/chat-threads",
        headers=h,
        json=_slack_body(users=json.dumps(_users("alice@elsewhere.io", "bob@elsewhere.io", "l@x.io"))),
    )
    assert up.status_code == 200, up.text
    chat_id = up.json()["id"]
    monkeypatch.setattr(extraction_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(extraction_mod, "stream_chat", _fake_stream(_CHAT_EXTRACT))
    assert (await client.post(f"/api/v1/meetings/{chat_id}/extract", headers=h)).status_code == 200
    parts = {p.name_raw: p for p in await _participants(db_session, chat_id)}
    # Alice still resolves by display name, but the exporter email that
    # matched no member is not stored as identity evidence.
    assert parts["Alice"].resolution_method == "display_name"
    assert parts["Alice"].email_raw is None
    assert parts["Bob"].user_id is None and parts["Bob"].email_raw is None
    decision = next(i for i in await _items(db_session, chat_id) if i.kind == "action")
    preview = await client.post(
        f"/api/v1/meetings/{chat_id}/action-items/{decision.id}/promote/preview", headers=h
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["owner_match_note"] is None


async def test_chat_extraction_failure_and_cancel_record_chat_ledger(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, h, chat_id, _ = await _org_chat(client, db_session)
    monkeypatch.setattr(extraction_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(extraction_mod, "stream_chat", _fake_stream("garbage", input_tokens=5))
    resp = await client.post(f"/api/v1/meetings/{chat_id}/extract", headers=h)
    assert resp.status_code == 200
    assert resp.json()["extraction_status"] == "failed"

    async def _cancelled(**kwargs):
        yield Usage(input_tokens=7, output_tokens=1)
        raise asyncio.CancelledError()

    monkeypatch.setattr(extraction_mod, "stream_chat", _cancelled)
    chat = await db_session.get(MeetingTranscript, chat_id)
    from sessionfs.server.auth.dependencies import AuthContext

    alice = await db_session.get(User, chat.created_by_user_id)
    with pytest.raises(asyncio.CancelledError):
        await extraction_mod.run_meeting_extraction(
            db_session, meeting=chat, auth=AuthContext(user=alice, api_key_id="", key_kind="user")
        )
    rows = (
        await db_session.execute(
            select(HelperUsage.source, HelperUsage.input_tokens)
            .where(HelperUsage.conversation_id == chat_id)
            .order_by(HelperUsage.input_tokens)
        )
    ).all()
    assert [tuple(r) for r in rows] == [("chat_extract", 5), ("chat_extract", 7)]


async def test_meeting_extraction_still_meeting_extract(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await _project(db_session, test_user)
    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings", headers=auth_headers,
        json={"title": "t", "content": "Alice: plan"},
    )
    seen: dict = {}

    async def _stream(**kwargs):
        seen.update(kwargs)
        yield Usage(input_tokens=1, output_tokens=1)
        yield TextDelta('{"participants": [], "items": []}')
        yield Done("stop")

    monkeypatch.setattr(extraction_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(extraction_mod, "stream_chat", _stream)
    resp = await client.post(f"/api/v1/meetings/{up.json()['id']}/extract", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert "meeting transcript" in seen["system"]
    src = (
        await db_session.execute(
            select(HelperUsage.source).where(HelperUsage.conversation_id == up.json()["id"])
        )
    ).scalar_one()
    assert src == "meeting_extract"
    # ?kind=chat on a meeting id is the uniform 404.
    wrong = await client.post(
        f"/api/v1/meetings/{up.json()['id']}/extract", headers=auth_headers, params={"kind": "chat"}
    )
    assert wrong.status_code == 404


async def test_promote_chat_item(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, h, chat_id, _ = await _org_chat(client, db_session)
    monkeypatch.setattr(extraction_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(extraction_mod, "stream_chat", _fake_stream(_CHAT_EXTRACT))
    assert (await client.post(f"/api/v1/meetings/{chat_id}/extract", headers=h)).status_code == 200
    action = next(i for i in await _items(db_session, chat_id) if i.kind == "action")

    preview = await client.post(
        f"/api/v1/meetings/{chat_id}/action-items/{action.id}/promote/preview",
        headers=h, params={"kind": "chat"},
    )
    assert preview.status_code == 200, preview.text
    desc = preview.json()["description"]
    assert "From chat thread *#infra*" in desc
    assert "messages 2-2" in desc
    assert "> [2025-10-29 14:03] Bob: ok. I'll move the LB cert ⏎ by Friday" in desc
    # Bob's member match came only from the export's users.json email.
    assert preview.json()["assigned_user_id"] is not None
    assert preview.json()["owner_match_note"] == "matched by exporter email"

    remind = await client.post(
        f"/api/v1/projects/{project.id}/meetings/{chat_id}/items/{action.id}/reminder",
        headers=h, json={},
    )
    assert remind.status_code == 201, remind.text
    assert remind.json()["source"]["kind"] == "chat_item"

    done = await client.post(
        f"/api/v1/meetings/{chat_id}/action-items/{action.id}/promote",
        headers=h, json={"title": "Move the LB cert", "description": desc},
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "promoted"
    # The chat_item reminder follows the item to its new ticket.
    linked = (
        await db_session.execute(
            select(Reminder.linked_ticket_id).where(Reminder.id == remind.json()["id"])
        )
    ).scalar_one()
    assert linked == done.json()["ticket_id"]
    again = await client.post(
        f"/api/v1/meetings/{chat_id}/action-items/{action.id}/promote",
        headers=h, json={"title": "x", "description": "y"},
    )
    assert again.status_code == 409


# ── Retention ──


async def test_retention_sweep_covers_chat_rows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from tests.server.integration.test_skill_marketplace import _platform_admin_headers

    _, admin_headers = await _platform_admin_headers(db_session)
    org = await _org(db_session, tier="team", settings=json.dumps({"general": {"meeting_retention_days": 5}}))
    owner = await _user(db_session, "Owner")
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    h = await _user_headers(db_session, owner)
    up = await client.post(f"/api/v1/projects/{project.id}/chat-threads", headers=h, json=_slack_body())
    chat_id = up.json()["id"]
    chat = await db_session.get(ConversationSource, chat_id)
    chat.created_at = _now() - timedelta(days=10)
    await db_session.commit()
    fresh = await _meeting_row(db_session, project=project, org_id=org.id, created_at=_now())
    fresh_id = fresh.id

    result = await client.post("/api/v1/admin/sweeps/meeting-retention", headers=admin_headers, json={})
    assert result.status_code == 200, result.text
    assert result.json()["meetings_deleted"] == 1
    db_session.expire_all()
    assert await db_session.get(ConversationSource, chat_id) is None
    assert await db_session.get(ConversationSource, fresh_id) is not None
    left = (
        await db_session.execute(
            select(func.count()).select_from(ConversationMessageMeta)
            .where(ConversationMessageMeta.source_id == chat_id)
        )
    ).scalar_one()
    assert left == 0
    assert (
        await db_session.execute(
            select(func.count()).select_from(MeetingParticipant)
            .where(MeetingParticipant.transcript_id == chat_id)
        )
    ).scalar_one() == 0
