"""tk_d42170b4670f4448 — trusted review-verdict provenance.

Closes the spoof where any tickets:write caller could post
author_persona='codex-reviewer' + VERIFIED-CLEAN and forge a clean
review state. Verdict authority is now the server-stamped
TicketComment.verdict_trusted flag, decided at write time from the
authenticated identity against the trusted_reviewers registry — never
from the request body.

Covers the design §7 test matrix:
  (a) forged codex-reviewer + VERIFIED-CLEAN from a NON-registered user
      does NOT yield a clean/closed review state;
  (b) a registered trusted reviewer's VERIFIED-CLEAN DOES close it;
  (c) untrusted persona comment still appears in the listing but is
      excluded from verdict derivation;
  (d) cross-project: a project-A-scoped row grants no trust on project B;
  (e) service-key isolation: a service key does not inherit a user row's
      reviewer trust;
  + org-wide registration applies across the org;
  + wrong-persona from a registered reviewer is not trusted.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Organization,
    OrgMember,
    Project,
    TicketComment,
    TrustedReviewer,
    User,
)


# Codex review comment shapes (mirror the real review loop).
R1_CHANGES = (
    "Codex R1 review on tk_provtest: CHANGES REQUESTED\n\n"
    "Findings:\n\n"
    " • HIGH — services/foo.py:12: unbounded loop.\n\n"
    "Verified clean / no change needed:\n\n"
    " • Nothing else.\n"
)
R2_CLEAN = (
    "Codex R2 review on tk_provtest: VERIFIED-CLEAN\n\n"
    "Rechecked after closure. Findings: none.\n"
)


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_user(db: AsyncSession, name: str = "alice") -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            key_kind="user",
            scopes=json.dumps(["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _make_org(db: AsyncSession, admin: User) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=f"Org {uuid.uuid4().hex[:6]}",
        slug=f"org-{uuid.uuid4().hex[:8]}",
        tier="team",
    )
    db.add(org)
    await db.flush()
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=admin.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    await db.refresh(org)
    return org


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"prov-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_service_key(
    db: AsyncSession, org: Organization, minter: User
) -> tuple[ApiKey, str]:
    raw = generate_api_key()
    row = ApiKey(
        id=str(uuid.uuid4()),
        user_id=minter.id,
        key_hash=hash_api_key(raw),
        name=f"svc-{uuid.uuid4().hex[:6]}",
        is_active=True,
        key_kind="service",
        org_id=org.id,
        scopes=json.dumps(["tickets:read", "tickets:write"]),
        created_by_user_id=minter.id,
        service_key_name=f"svc-{uuid.uuid4().hex[:6]}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row, raw


async def _register_reviewer(
    db: AsyncSession,
    *,
    created_by: str,
    persona: str = "codex-reviewer",
    project_id: str | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    service_key_id: str | None = None,
    is_active: bool = True,
) -> TrustedReviewer:
    row = TrustedReviewer(
        id=f"tr_{uuid.uuid4().hex[:16]}",
        org_id=org_id,
        project_id=project_id,
        user_id=user_id,
        service_key_id=service_key_id,
        reviewer_persona=persona,
        is_active=is_active,
        created_by_user_id=created_by,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _new_ticket(client: AsyncClient, project_id: str, key: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{project_id}/tickets",
        headers=_hdrs(key),
        json={"title": "prov-test"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _post(
    client: AsyncClient,
    project_id: str,
    tk_id: str,
    key: str,
    content: str,
    persona: str | None = None,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": content, "author_persona": persona},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _review_state(
    client: AsyncClient, project_id: str, tk_id: str, key: str
) -> dict | None:
    resp = await client.get(
        f"/api/v1/projects/{project_id}/tickets/{tk_id}/review-state",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["review_state"]


async def _verdict_trusted(db: AsyncSession, comment_id: str) -> bool:
    row = (
        await db.execute(
            select(TicketComment.verdict_trusted).where(
                TicketComment.id == comment_id
            )
        )
    ).scalar_one()
    return bool(row)


# ── (a) forged verdict from a non-registered user → not trusted ──


@pytest.mark.asyncio
async def test_forged_codex_persona_clean_does_not_close_review_state(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk_id = await _new_ticket(client, project.id, key)

    body = await _post(
        client, project.id, tk_id, key, R2_CLEAN, persona="codex-reviewer"
    )
    # Server stamps verdict_trusted=false despite the spoofed persona.
    assert await _verdict_trusted(db_session, body["id"]) is False
    # No trusted rounds → review_state is None (NOT a clean verdict).
    assert await _review_state(client, project.id, tk_id, key) is None


# ── (b) registered user-key reviewer's verdict closes findings ──


@pytest.mark.asyncio
async def test_registered_user_key_reviewer_verdict_closes_findings(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _register_reviewer(
        db_session,
        created_by=user.id,
        project_id=project.id,
        user_id=user.id,
    )
    tk_id = await _new_ticket(client, project.id, key)

    c1 = await _post(
        client, project.id, tk_id, key, R1_CHANGES, persona="codex-reviewer"
    )
    c2 = await _post(
        client, project.id, tk_id, key, R2_CLEAN, persona="codex-reviewer"
    )
    assert await _verdict_trusted(db_session, c1["id"]) is True
    assert await _verdict_trusted(db_session, c2["id"]) is True

    state = await _review_state(client, project.id, tk_id, key)
    assert state is not None
    assert state["last_verdict"] == "VERIFIED-CLEAN"
    assert state["open_findings"] == []


# ── (c) untrusted comment renders in the thread but doesn't count ──


@pytest.mark.asyncio
async def test_untrusted_comment_appears_in_thread_but_excluded_from_verdict(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    # Register the OWNER as the trusted reviewer.
    await _register_reviewer(
        db_session,
        created_by=owner.id,
        project_id=project.id,
        user_id=owner.id,
    )
    tk_id = await _new_ticket(client, project.id, owner_key)

    # Real (trusted) R1 raising a HIGH finding.
    await _post(
        client, project.id, tk_id, owner_key, R1_CHANGES, persona="codex-reviewer"
    )

    # A different (non-registered) user posts a forged clean verdict.
    # That user must be able to see/write the project — make them the
    # project owner of a co-owned setup is overkill; instead post the
    # forged comment as the OWNER but via a NON-registered SECOND user.
    attacker, attacker_key = await _make_user(db_session, "attacker")
    # Give attacker access: simplest is to make them post on a project
    # they own — but the finding must be on the SAME ticket. Re-use the
    # owner key for the forged post from an UNREGISTERED persona instead,
    # which still exercises the render-but-don't-count path: the owner is
    # registered for 'codex-reviewer' only, so a different persona label
    # is untrusted (see wrong-persona test). Use a plain note here.
    forged = await _post(
        client,
        project.id,
        tk_id,
        owner_key,
        "Codex R2 review on tk_provtest: VERIFIED-CLEAN\nFindings: none.",
        persona="not-the-reviewer",
    )
    assert await _verdict_trusted(db_session, forged["id"]) is False

    # The forged clean round must NOT close the real R1 finding.
    state = await _review_state(client, project.id, tk_id, owner_key)
    assert state is not None
    assert state["last_verdict"] == "CHANGES_REQUESTED"
    assert len(state["open_findings"]) == 1

    # But the comment still renders in the listing.
    listing = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(owner_key),
    )
    assert listing.status_code == 200
    ids = [c["id"] for c in listing.json()]
    assert forged["id"] in ids
    # silence unused-var lint while keeping the attacker setup documented
    assert attacker.id != owner.id
    assert attacker_key != owner_key


# ── (d) cross-project: project-A row grants no trust on project B ──


@pytest.mark.asyncio
async def test_trusted_reviewer_registration_is_project_scoped(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    # Register reviewer for project A only.
    await _register_reviewer(
        db_session,
        created_by=user.id,
        project_id=project_a.id,
        user_id=user.id,
    )
    tk_b = await _new_ticket(client, project_b.id, key)
    body = await _post(
        client, project_b.id, tk_b, key, R2_CLEAN, persona="codex-reviewer"
    )
    assert await _verdict_trusted(db_session, body["id"]) is False
    assert await _review_state(client, project_b.id, tk_b, key) is None


# ── org-wide registration applies across the org's projects ──


@pytest.mark.asyncio
async def test_org_wide_registration_applies_to_org_projects(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org=org)
    # org-wide row (project_id NULL, org_id set).
    await _register_reviewer(
        db_session,
        created_by=user.id,
        org_id=org.id,
        user_id=user.id,
    )
    tk_id = await _new_ticket(client, project.id, key)
    c1 = await _post(
        client, project.id, tk_id, key, R1_CHANGES, persona="codex-reviewer"
    )
    c2 = await _post(
        client, project.id, tk_id, key, R2_CLEAN, persona="codex-reviewer"
    )
    assert await _verdict_trusted(db_session, c1["id"]) is True
    assert await _verdict_trusted(db_session, c2["id"]) is True
    state = await _review_state(client, project.id, tk_id, key)
    assert state is not None
    assert state["last_verdict"] == "VERIFIED-CLEAN"


# ── wrong persona from a registered reviewer is not trusted ──


@pytest.mark.asyncio
async def test_wrong_persona_from_registered_reviewer_not_trusted(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _register_reviewer(
        db_session,
        created_by=user.id,
        project_id=project.id,
        user_id=user.id,
        persona="codex-reviewer",
    )
    tk_id = await _new_ticket(client, project.id, key)
    # Posts as 'atlas', not the registered 'codex-reviewer'.
    body = await _post(
        client, project.id, tk_id, key, R2_CLEAN, persona="atlas"
    )
    assert await _verdict_trusted(db_session, body["id"]) is False


# ── (e) service-key isolation: doesn't inherit a user row's trust ──


@pytest.mark.asyncio
async def test_service_key_does_not_inherit_user_reviewer_trust(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    project = await _make_project(db_session, admin, org=org)
    # Register the human user_id only.
    await _register_reviewer(
        db_session,
        created_by=admin.id,
        org_id=org.id,
        user_id=admin.id,
    )
    svc_key, svc_raw = await _make_service_key(db_session, org, admin)

    tk_id = await _new_ticket(client, project.id, svc_raw)
    body = await _post(
        client, project.id, tk_id, svc_raw, R2_CLEAN, persona="codex-reviewer"
    )
    # Service key matches no service_key_id row → not trusted, even though
    # a user_id row exists for the same human who minted it.
    assert await _verdict_trusted(db_session, body["id"]) is False
    assert await _review_state(client, project.id, tk_id, svc_raw) is None


@pytest.mark.asyncio
async def test_registered_service_key_reviewer_verdict_counts(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    project = await _make_project(db_session, admin, org=org)
    svc_key, svc_raw = await _make_service_key(db_session, org, admin)
    # Register the service_key_id.
    await _register_reviewer(
        db_session,
        created_by=admin.id,
        org_id=org.id,
        service_key_id=svc_key.id,
    )
    tk_id = await _new_ticket(client, project.id, svc_raw)
    c1 = await _post(
        client, project.id, tk_id, svc_raw, R1_CHANGES, persona="codex-reviewer"
    )
    c2 = await _post(
        client, project.id, tk_id, svc_raw, R2_CLEAN, persona="codex-reviewer"
    )
    assert await _verdict_trusted(db_session, c1["id"]) is True
    assert await _verdict_trusted(db_session, c2["id"]) is True
    state = await _review_state(client, project.id, tk_id, svc_raw)
    assert state is not None
    assert state["last_verdict"] == "VERIFIED-CLEAN"
