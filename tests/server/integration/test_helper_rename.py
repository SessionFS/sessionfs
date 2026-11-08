"""Renaming a session-helper conversation (PATCH /helper/conversations/{id})."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import sessionfs.server.routes.helper as helper_mod
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, HelperConversation, OrgMember
from tests.server.integration.test_helper_api import (
    _hdrs,
    _make_conversation,
    _make_org_with,
    _make_user,
)


def _url(conversation_id: str) -> str:
    return f"/api/v1/helper/conversations/{conversation_id}"


@pytest.mark.asyncio
async def test_rename_conversation_owner(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session)
    then = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    conv = await _make_conversation(db_session, user, None, then)
    newer = await _make_conversation(db_session, user, None, then + timedelta(days=1))

    resp = await client.patch(
        _url(conv.id),
        headers=_hdrs(key),
        json={"title": "  Release\tnotes\nfor 0.29.11 ‮\x00 "},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Same shape as a list item.
    assert set(body) == {"id", "org_id", "title", "scope", "created_at", "updated_at"}
    assert body["id"] == conv.id
    # Whitespace collapsed; controls and the bidi override dropped.
    assert body["title"] == "Release notes for 0.29.11"

    listed = (await client.get("/api/v1/helper/conversations", headers=_hdrs(key))).json()
    by_id = {c["id"]: c for c in listed}
    assert by_id[conv.id] == body
    # Renaming is not activity: the order and updated_at are unchanged.
    assert [c["id"] for c in listed] == [newer.id, conv.id]
    stored = (
        await db_session.execute(
            select(HelperConversation.updated_at).where(HelperConversation.id == conv.id)
        )
    ).scalar_one()
    assert stored.replace(tzinfo=timezone.utc) == then

    got = await client.get(_url(conv.id), headers=_hdrs(key))
    assert got.json()["title"] == "Release notes for 0.29.11"


@pytest.mark.asyncio
async def test_rename_conversation_other_user_and_missing_404(
    client: AsyncClient, db_session: AsyncSession
):
    alice, _ = await _make_user(db_session, "alice")
    _, bob_key = await _make_user(db_session, "bob")
    conv = await _make_conversation(db_session, alice, None, datetime.now(timezone.utc))

    foreign = await client.patch(_url(conv.id), headers=_hdrs(bob_key), json={"title": "Mine now"})
    missing = await client.patch(
        _url("hc_doesnotexist"), headers=_hdrs(bob_key), json={"title": "Mine now"}
    )
    assert foreign.status_code == 404, foreign.text
    assert missing.status_code == 404, missing.text
    # One body for both, the same as GET and DELETE give.
    assert foreign.json() == missing.json()
    assert (await client.get(_url(conv.id), headers=_hdrs(bob_key))).json() == foreign.json()
    title = (
        await db_session.execute(
            select(HelperConversation.title).where(HelperConversation.id == conv.id)
        )
    ).scalar_one()
    assert title == "Old thread"


@pytest.mark.asyncio
async def test_rename_conversation_after_leaving_org_is_404(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org_with(db_session, admin, member)
    conv = await _make_conversation(db_session, member, org.id, datetime.now(timezone.utc))
    await db_session.execute(
        delete(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == member.id)
    )
    await db_session.commit()

    resp = await client.patch(_url(conv.id), headers=_hdrs(member_key), json={"title": "Still mine?"})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "code"),
    [
        ("", "title_required"),
        ("   \n\t ", "title_required"),
        ("\x00\x07​‮", "title_required"),
        ("x" * 201, "title_too_long"),
    ],
)
async def test_rename_conversation_rejects_empty_and_too_long(
    client: AsyncClient, db_session: AsyncSession, title: str, code: str
):
    user, key = await _make_user(db_session)
    conv = await _make_conversation(db_session, user, None, datetime.now(timezone.utc))

    resp = await client.patch(_url(conv.id), headers=_hdrs(key), json={"title": title})
    assert resp.status_code == 422, resp.text
    assert code in resp.text
    stored = (
        await db_session.execute(
            select(HelperConversation.title).where(HelperConversation.id == conv.id)
        )
    ).scalar_one()
    assert stored == "Old thread"


@pytest.mark.asyncio
async def test_rename_conversation_accepts_exactly_200_chars(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    conv = await _make_conversation(db_session, user, None, datetime.now(timezone.utc))
    # Surrounding whitespace doesn't count toward the limit.
    resp = await client.patch(_url(conv.id), headers=_hdrs(key), json={"title": f"  {'y' * 200}  "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "y" * 200


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"title": None}, {"title": 5}, {"title": "ok", "extra": 1}])
async def test_rename_conversation_rejects_malformed_body(
    client: AsyncClient, db_session: AsyncSession, body: dict
):
    user, key = await _make_user(db_session)
    conv = await _make_conversation(db_session, user, None, datetime.now(timezone.utc))
    resp = await client.patch(_url(conv.id), headers=_hdrs(key), json=body)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_rename_conversation_redacts_secrets(client: AsyncClient, db_session: AsyncSession):
    """A rename is stored like the auto-title: redacted under the caller's
    strictest DLP policy, so it can't store what the helper would redact."""
    user, key = await _make_user(db_session)
    org = await _make_org_with(
        db_session,
        user,
        settings={"dlp": {"enabled": True, "mode": "redact", "categories": ["phi"]}},
    )
    # A personal thread still gets the policy of every org the caller is in.
    conv = await _make_conversation(db_session, user, None, datetime.now(timezone.utc))
    org_conv = await _make_conversation(db_session, user, org.id, datetime.now(timezone.utc))

    resp = await client.patch(
        _url(conv.id),
        headers=_hdrs(key),
        json={"title": "ssn 123-45-6789 and key AKIAIOSFODNN7EXAMPLE"},
    )
    assert resp.status_code == 200, resp.text
    title = resp.json()["title"]
    assert "123-45-6789" not in title and "[REDACTED:ssn]" in title
    assert "AKIAIOSFODNN7EXAMPLE" not in title and "[REDACTED:aws_access_key_id]" in title

    org_resp = await client.patch(
        _url(org_conv.id), headers=_hdrs(key), json={"title": "ssn 123-45-6789"}
    )
    assert org_resp.status_code == 200, org_resp.text
    assert "[REDACTED:ssn]" in org_resp.json()["title"]

    stored = (
        await db_session.execute(
            select(HelperConversation.title).where(HelperConversation.id == conv.id)
        )
    ).scalar_one()
    assert "123-45-6789" not in stored and "AKIAIOSFODNN7EXAMPLE" not in stored


@pytest.mark.asyncio
async def test_rename_conversation_secret_redacted_without_any_org(
    client: AsyncClient, db_session: AsyncSession
):
    """Secrets are always redacted, even for a caller in no org."""
    user, key = await _make_user(db_session)
    conv = await _make_conversation(db_session, user, None, datetime.now(timezone.utc))
    resp = await client.patch(
        _url(conv.id), headers=_hdrs(key), json={"title": "AKIAIOSFODNN7EXAMPLE rotation"}
    )
    assert resp.status_code == 200, resp.text
    assert "AKIAIOSFODNN7EXAMPLE" not in resp.json()["title"]


@pytest.mark.asyncio
async def test_rename_conversation_redaction_failure_refuses(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    conv = await _make_conversation(db_session, user, None, datetime.now(timezone.utc))

    def _boom(text: str, policy: dict | None) -> str:
        raise helper_mod.RedactionError("bad pattern")

    monkeypatch.setattr(helper_mod, "redact", _boom)
    resp = await client.patch(_url(conv.id), headers=_hdrs(key), json={"title": "Anything"})
    assert resp.status_code == 422, resp.text
    assert "title_unscannable" in resp.text
    stored = (
        await db_session.execute(
            select(HelperConversation.title).where(HelperConversation.id == conv.id)
        )
    ).scalar_one()
    assert stored == "Old thread"


@pytest.mark.asyncio
async def test_rename_conversation_service_key_refused(
    client: AsyncClient, db_session: AsyncSession
):
    user, _ = await _make_user(db_session)
    conv = await _make_conversation(db_session, user, None, datetime.now(timezone.utc))
    svc_raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(svc_raw),
            name="ci-runner",
            key_kind="service",
            scopes='["agent_runs:write"]',
            service_key_name="ci-runner",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.patch(_url(conv.id), headers=_hdrs(svc_raw), json={"title": "From a robot"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "service_key_not_allowed"
    stored = (
        await db_session.execute(
            select(HelperConversation.title).where(HelperConversation.id == conv.id)
        )
    ).scalar_one()
    assert stored == "Old thread"
