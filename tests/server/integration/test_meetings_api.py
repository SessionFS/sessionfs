"""Meeting transcripts API (GitHub #116, M1a).

Covers upload / list / get / soft-delete for project- and group-scoped
meetings, the participant resolution service and the manual override route,
the org audit trail, and the DLP block / redact behaviour. Extraction and
ticket promotion (M1b) are out of scope here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    MeetingActionItem,
    MeetingParticipant,
    MeetingTranscript,
    OrgAuditEvent,
    OrgMember,
    Organization,
    Project,
    ProjectGroup,
    Session,
    User,
)
from sessionfs.server.routes.meetings import resolve_participants


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _user(db: AsyncSession, name: str, email: str | None = None) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email or f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name,
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _org(db: AsyncSession, *, tier: str = "team", settings: str = "{}") -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=f"Org {uuid.uuid4().hex[:4]}",
        slug=f"org-{uuid.uuid4().hex[:8]}",
        tier=tier,
        seats_limit=20,
        settings=settings,
    )
    db.add(org)
    await db.commit()
    return org


async def _member(db: AsyncSession, org: Organization, user: User, role: str = "member") -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def _project(db: AsyncSession, owner: User, *, org: Organization | None = None) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Shared repo",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="# original",
        owner_id=owner.id,
        org_id=org.id if org is not None else None,
    )
    db.add(project)
    await db.commit()
    return project


async def _group(db: AsyncSession, org: Organization, creator: User) -> ProjectGroup:
    group = ProjectGroup(
        id=f"pg_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        slug=f"frontend-{uuid.uuid4().hex[:6]}",
        name="Frontend",
        created_by_user_id=creator.id,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(group)
    await db.commit()
    return group


async def _persona(
    db: AsyncSession, org: Organization, name: str, *, represented_user_id: str | None = None
) -> AgentPersona:
    persona = AgentPersona(
        id=f"pers_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        name=name,
        role="reviewer",
        content="",
        specializations="[]",
        created_by="test",
        represented_user_id=represented_user_id,
    )
    db.add(persona)
    await db.commit()
    return persona


async def _project_persona(db: AsyncSession, project: Project, name: str) -> AgentPersona:
    persona = AgentPersona(
        id=f"pers_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        name=name,
        role="reviewer",
        content="",
        specializations="[]",
        created_by="test",
    )
    db.add(persona)
    await db.commit()
    return persona


async def _meeting_participant(
    db: AsyncSession, transcript_id: str, name_raw: str
) -> MeetingParticipant:
    participant = MeetingParticipant(
        id=f"mp_{uuid.uuid4().hex[:12]}",
        transcript_id=transcript_id,
        name_raw=name_raw,
        resolution_method="unresolved",
    )
    db.add(participant)
    await db.commit()
    await db.refresh(participant)
    return participant


async def _meeting_action_item(
    db: AsyncSession, transcript_id: str
) -> MeetingActionItem:
    item = MeetingActionItem(
        id=f"mai_{uuid.uuid4().hex[:12]}",
        transcript_id=transcript_id,
        kind="action",
        description="Ship it",
        line_start=1,
        line_end=1,
        status="pending",
        extracted_at=_now(),
    )
    db.add(item)
    await db.commit()
    return item


async def _user_headers(db: AsyncSession, user: User) -> dict[str, str]:
    """Mint a legacy wildcard user key for an arbitrary user and return headers."""
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{user.display_name or 'user'}-key",
            created_at=_now(),
        )
    )
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


async def _service_key(
    db: AsyncSession,
    *,
    owner: User,
    org_id: str,
    scopes: list[str],
    project_ids: list[str] | None = None,
) -> dict[str, str]:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            key_hash=hash_api_key(raw),
            name="meetings-svc",
            created_at=_now(),
            key_kind="service",
            org_id=org_id,
            scopes=json.dumps(scopes),
            service_key_name="meetings-svc",
            created_by_user_id=owner.id,
            project_ids=json.dumps(project_ids) if project_ids is not None else None,
        )
    )
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


async def _captured_session(
    db: AsyncSession, user: User, *, project_id: str
) -> Session:
    now = _now()
    session = Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=user.id,
        title="Captured",
        source_tool="claude-code",
        blob_key=f"sessions/{user.id}/x.tar.gz",
        etag=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        uploaded_at=now,
        project_id=project_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _meeting_row(
    db: AsyncSession,
    *,
    project: Project | None = None,
    group: ProjectGroup | None = None,
    org_id: str | None = None,
    created_at: datetime,
    deleted_at: datetime | None = None,
    content: str = "hello",
) -> MeetingTranscript:
    meeting = MeetingTranscript(
        id=f"meet_{uuid.uuid4().hex[:16]}",
        org_id=org_id,
        project_id=project.id if project is not None else None,
        group_id=group.id if group is not None else None,
        title="t",
        source="manual_paste",
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        line_count=1,
        dlp_findings=0,
        extraction_status="none",
        created_by_user_id=None,
        created_at=created_at,
        updated_at=created_at,
        deleted_at=deleted_at,
    )
    db.add(meeting)
    await db.commit()
    await db.refresh(meeting)
    return meeting


# ---------------------------------------------------------------------------
# Project meetings (personal scope)
# ---------------------------------------------------------------------------


async def test_project_meeting_upload_get_list_delete(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": "Sprint planning", "content": "Alice: plan\nBob: ship", "duration_minutes": 30},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Sprint planning"
    assert body["content"] == "Alice: plan\nBob: ship"
    assert body["project_id"] == project.id
    assert body["org_id"] is None
    assert body["extraction_status"] == "none"
    assert body["line_count"] == 2
    assert body["dlp_findings"] == 0
    assert body["participants"] == []
    assert body["action_items"] == []
    assert body["warnings"] == []
    assert body["content_sha256"]
    meeting_id = body["id"]

    got = await client.get(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["content"] == "Alice: plan\nBob: ship"

    listed = await client.get(f"/api/v1/projects/{project.id}/meetings", headers=auth_headers)
    assert listed.status_code == 200
    assert [m["id"] for m in listed.json()["meetings"]] == [meeting_id]

    # q substring over title
    filtered = await client.get(
        f"/api/v1/projects/{project.id}/meetings", headers=auth_headers, params={"q": "planning"}
    )
    assert [m["id"] for m in filtered.json()["meetings"]] == [meeting_id]

    deleted = await client.delete(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted_at"] is not None

    # Soft-deleted meetings are hidden from list and get.
    assert (await client.get(f"/api/v1/projects/{project.id}/meetings", headers=auth_headers)).json()["meetings"] == []
    assert (await client.get(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)).status_code == 404


async def test_project_meeting_duplicate_warning(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    payload = {"title": "Standup", "content": "same content"}

    first = await client.post(f"/api/v1/projects/{project.id}/meetings", headers=auth_headers, json=payload)
    assert first.status_code == 200
    assert first.json()["warnings"] == []

    second = await client.post(f"/api/v1/projects/{project.id}/meetings", headers=auth_headers, json=payload)
    assert second.status_code == 200
    warnings = second.json()["warnings"]
    assert len(warnings) == 1
    assert warnings[0] == f"duplicate_of: {first.json()['id']}"


async def test_project_meeting_validation(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"

    assert (await client.post(url, headers=auth_headers, json={"title": "  ", "content": "x"})).status_code == 422
    assert (await client.post(url, headers=auth_headers, json={"title": "t", "content": ""})).status_code == 422
    assert (await client.post(url, headers=auth_headers, json={"title": "x" * 201, "content": "x"})).status_code == 422
    assert (await client.post(url, headers=auth_headers, json={"title": "t"})).status_code == 422


async def test_project_meeting_too_large(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"

    big = await client.post(url, headers=auth_headers, json={"title": "big", "content": "a" * 600_000})
    assert big.status_code == 413
    assert big.json()["error"]["details"]["error"] == "meeting_too_large"

    many_lines = await client.post(
        url, headers=auth_headers, json={"title": "lines", "content": "\n".join(["x"] * 10_001)}
    )
    assert many_lines.status_code == 413
    assert many_lines.json()["error"]["details"]["error"] == "meeting_too_large"


# ---------------------------------------------------------------------------
# Participant resolution (§6)
# ---------------------------------------------------------------------------


async def test_resolve_participants(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)

    other = await _user(db_session, "Unique Speaker", email="speaker@example.com")
    await _member(db_session, org, other, role="member")

    persona = await _persona(db_session, org, "Scout", represented_user_id=test_user.id)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": "t", "content": "hello"},
    )
    assert resp.status_code == 200
    transcript = (
        await db_session.execute(
            select(MeetingTranscript).where(MeetingTranscript.id == resp.json()["id"])
        )
    ).scalar_one()

    resolved = await resolve_participants(
        db_session,
        transcript,
        [
            {"name": "Someone", "email": "speaker@example.com"},
            {"name": "Scout"},
            {"name": "Unique Speaker"},
            {"name": "No Such Person"},
        ],
        auth=None,
    )
    assert resolved[0]["resolution_method"] == "exact_email"
    assert resolved[0]["user_id"] == other.id
    assert resolved[1]["resolution_method"] == "persona"
    assert resolved[1]["persona_name"] == persona.name
    assert resolved[1]["user_id"] == test_user.id  # represented member
    assert resolved[2]["resolution_method"] == "display_name"
    assert resolved[2]["user_id"] == other.id
    assert resolved[3]["resolution_method"] == "unresolved"
    assert resolved[3]["user_id"] is None


async def test_resolve_participants_personal_scope_stays_unresolved(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)  # org_id is None
    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": "t", "content": "hello"},
    )
    transcript = (
        await db_session.execute(
            select(MeetingTranscript).where(MeetingTranscript.id == resp.json()["id"])
        )
    ).scalar_one()
    resolved = await resolve_participants(
        db_session, transcript, [{"name": "Someone", "email": "a@b.com"}], auth=None
    )
    assert resolved[0]["resolution_method"] == "unresolved"


# ---------------------------------------------------------------------------
# Group meetings
# ---------------------------------------------------------------------------


async def test_group_meeting_upload_list_get_delete(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session, tier="team")
    await _member(db_session, org, test_user, role="owner")  # org owner => group admin
    group = await _group(db_session, org, test_user)

    resp = await client.post(
        f"/api/v1/groups/{group.id}/meetings",
        headers=auth_headers,
        json={"title": "Retro", "content": "went well\n"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["group_id"] == group.id
    assert body["project_id"] is None
    assert body["org_id"] == org.id
    meeting_id = body["id"]

    got = await client.get(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)
    assert got.status_code == 200

    listed = await client.get(f"/api/v1/groups/{group.id}/meetings", headers=auth_headers)
    assert [m["id"] for m in listed.json()["meetings"]] == [meeting_id]

    assert (await client.delete(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)).status_code == 200


async def test_group_meeting_rejects_non_editor(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session, tier="team")
    await _member(db_session, org, test_user, role="member")  # no grant => no standing
    group = await _group(db_session, org, test_user)

    # A member without a group grant cannot upload (404, not 403, to hide existence).
    resp = await client.post(
        f"/api/v1/groups/{group.id}/meetings",
        headers=auth_headers,
        json={"title": "t", "content": "x"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Participant override
# ---------------------------------------------------------------------------


async def test_override_participant_by_persona_and_user(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)
    other = await _user(db_session, "Colleague", email="colleague@example.com")
    await _member(db_session, org, other, role="member")
    persona = await _persona(db_session, org, "Analyst")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": "t", "content": "hello"},
    )
    meeting_id = resp.json()["id"]
    participant = await _meeting_participant(db_session, meeting_id, "Analyst")

    override = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{participant.id}",
        headers=auth_headers,
        json={"persona_name": "Analyst"},
    )
    assert override.status_code == 200, override.text
    assert override.json()["persona_name"] == persona.name
    assert override.json()["user_id"] is None
    assert override.json()["resolution_method"] == "manual"
    assert override.json()["resolved_by_user_id"] == test_user.id

    # Now re-override to a user id.
    override2 = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{participant.id}",
        headers=auth_headers,
        json={"user_id": other.id},
    )
    assert override2.status_code == 200
    assert override2.json()["user_id"] == other.id
    assert override2.json()["persona_name"] is None

    # Exactly-one rule.
    both = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{participant.id}",
        headers=auth_headers,
        json={"user_id": other.id, "persona_name": "Analyst"},
    )
    assert both.status_code == 422
    neither = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{participant.id}",
        headers=auth_headers,
        json={},
    )
    assert neither.status_code == 422


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


async def test_org_meeting_upload_writes_audit_event(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": "Audited", "content": "text"},
    )
    assert resp.status_code == 200
    meeting_id = resp.json()["id"]

    event = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "meeting_uploaded",
                OrgAuditEvent.target_id == meeting_id,
            )
        )
    ).scalar_one()
    # ids/counts only — the transcript text must never reach the audit row.
    assert "text" not in (event.before or "")
    assert "text" not in (event.after or "")
    after = json.loads(event.after or "{}")
    assert after["meeting_id"] == meeting_id
    # The title is never written to the audit row (ids/counts only).
    assert "title" not in after


# ---------------------------------------------------------------------------
# DLP
# ---------------------------------------------------------------------------


async def test_project_meeting_dlp_block(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    settings = json.dumps({
        "dlp": {
            "enabled": True,
            "mode": "block",
            "categories": ["secrets"],
            "custom_patterns": [
                {"name": "ssn", "regex": r"\d{3}-\d{2}-\d{4}", "category": "pii", "severity": "high"}
            ],
        }
    })
    org = await _org(db_session, settings=settings)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": "t", "content": "SSN 123-45-6789 leaked"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "dlp_blocked"
    assert resp.json()["error"]["details"]["findings"] == {"ssn": 1}


async def test_project_meeting_dlp_redact(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    settings = json.dumps({
        "dlp": {
            "enabled": True,
            "mode": "redact",
            "categories": ["secrets"],
            "custom_patterns": [
                {"name": "ssn", "regex": r"\d{3}-\d{2}-\d{4}", "category": "pii", "severity": "high"}
            ],
        }
    })
    org = await _org(db_session, settings=settings)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": "t", "content": "SSN 123-45-6789 leaked"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "123-45-6789" not in body["content"]
    assert body["dlp_findings"] >= 1


# ---------------------------------------------------------------------------
# Review findings (GitHub #116 M1a) — authorization, normalisation, search,
# paging, delete rules, override uniformity, retention and audit.
# ---------------------------------------------------------------------------


async def test_outsider_and_cross_org_member_get_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A non-member and a member of a different org both get 404 on every
    meeting surface (get/delete/list/upload, project and group) so existence
    never leaks across org boundaries."""
    org = await _org(db_session, tier="team")
    owner = await _user(db_session, "Owner")
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    owner_h = await _user_headers(db_session, owner)

    proj_up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=owner_h, json={"title": "p", "content": "x"},
    )
    assert proj_up.status_code == 200, proj_up.text
    proj_meeting = proj_up.json()["id"]
    grp_up = await client.post(
        f"/api/v1/groups/{group.id}/meetings",
        headers=owner_h, json={"title": "g", "content": "x"},
    )
    assert grp_up.status_code == 200, grp_up.text
    grp_meeting = grp_up.json()["id"]

    outsider = await _user(db_session, "Outsider")
    outsider_h = await _user_headers(db_session, outsider)
    other_org = await _org(db_session, tier="team")
    foreign = await _user(db_session, "Foreign", email="foreign@example.com")
    await _member(db_session, other_org, foreign, role="owner")
    foreign_h = await _user_headers(db_session, foreign)

    for h in (outsider_h, foreign_h):
        assert (await client.get(f"/api/v1/meetings/{proj_meeting}", headers=h)).status_code == 404
        assert (await client.delete(f"/api/v1/meetings/{proj_meeting}", headers=h)).status_code == 404
        assert (await client.get(f"/api/v1/projects/{project.id}/meetings", headers=h)).status_code == 404
        assert (await client.post(f"/api/v1/projects/{project.id}/meetings", headers=h, json={"title": "t", "content": "x"})).status_code == 404
        assert (await client.get(f"/api/v1/meetings/{grp_meeting}", headers=h)).status_code == 404
        assert (await client.delete(f"/api/v1/meetings/{grp_meeting}", headers=h)).status_code == 404
        assert (await client.get(f"/api/v1/groups/{group.id}/meetings", headers=h)).status_code == 404
        assert (await client.post(f"/api/v1/groups/{group.id}/meetings", headers=h, json={"title": "t", "content": "x"})).status_code == 404


async def test_captured_session_reader_can_list_get_but_not_upload(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A captured-session reader (read, not edit) can list/get a project's
    meetings but uploading is refused with ``project_edit_forbidden``."""
    owner = await _user(db_session, "Owner")
    project = await _project(db_session, owner)  # personal project owned by someone else
    owner_h = await _user_headers(db_session, owner)
    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=owner_h, json={"title": "t", "content": "hello"},
    )
    assert up.status_code == 200, up.text
    meeting_id = up.json()["id"]

    await _captured_session(db_session, test_user, project_id=project.id)

    assert (await client.get(f"/api/v1/projects/{project.id}/meetings", headers=auth_headers)).status_code == 200
    assert (await client.get(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)).status_code == 200

    denied = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "t", "content": "x"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "project_edit_forbidden"


async def test_service_key_project_upload_group_denial_and_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org = await _org(db_session, tier="team")
    owner = await _user(db_session, "SvcOwner")
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)

    # meetings:write scoped to the project in the key's own org can upload.
    write_h = await _service_key(
        db_session, owner=owner, org_id=org.id,
        scopes=["meetings:write"], project_ids=[project.id],
    )
    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=write_h, json={"title": "svc", "content": "x"},
    )
    assert up.status_code == 200, up.text

    # Group routes are user-key only: the service key is refused there.
    grp = await client.post(
        f"/api/v1/groups/{group.id}/meetings",
        headers=write_h, json={"title": "g", "content": "x"},
    )
    assert grp.status_code == 403
    assert grp.json()["error"]["code"] == "service_key_not_allowed"

    # Without meetings:write the project upload is refused before any access check.
    read_h = await _service_key(
        db_session, owner=owner, org_id=org.id,
        scopes=["meetings:read"], project_ids=[project.id],
    )
    denied = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=read_h, json={"title": "t", "content": "x"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["error"] == "insufficient_scope"


async def test_upload_normalises_crlf_nul_surrogate_and_trailing_space(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"
    raw = "first line  \r\nsecond\x00line\r\nthird  \nsurr \ud800 ok"
    # Serialize with ensure_ascii so the lone surrogate becomes a JSON
    # ``\ud800`` escape (a real client can't UTF-8-encode a raw surrogate).
    body = json.dumps({"title": "norm", "content": raw})
    resp = await client.post(
        url,
        headers={**auth_headers, "content-type": "application/json"},
        content=body,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "first line\nsecondline\nthird\nsurr � ok"
    assert body["line_count"] == 4
    assert "\r" not in body["content"]
    assert "\x00" not in body["content"]
    assert "\ud800" not in body["content"]
    assert "�" in body["content"]


async def test_meeting_search_case_insensitive_and_literal_wildcards(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"
    await client.post(url, headers=auth_headers, json={"title": "Sprint 100% done", "content": "alpha beta"})
    await client.post(url, headers=auth_headers, json={"title": "Planning", "content": "note under_score here"})
    await client.post(url, headers=auth_headers, json={"title": "Other", "content": "nothing special"})

    def _titles(resp) -> list[str]:
        return [m["title"] for m in resp.json()["meetings"]]

    assert _titles(await client.get(url, headers=auth_headers, params={"q": "PLANNING"})) == ["Planning"]
    assert _titles(await client.get(url, headers=auth_headers, params={"q": "ALPHA"})) == ["Sprint 100% done"]
    # % and _ are literal, not LIKE wildcards.
    assert _titles(await client.get(url, headers=auth_headers, params={"q": "%"})) == ["Sprint 100% done"]
    assert _titles(await client.get(url, headers=auth_headers, params={"q": "_"})) == ["Planning"]


async def test_meeting_keyset_paging_newest_first_no_duplicates(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"
    for i in range(5):
        resp = await client.post(url, headers=auth_headers, json={"title": f"m{i}", "content": f"content {i}"})
        assert resp.status_code == 200, resp.text

    full = (await client.get(url, headers=auth_headers)).json()["meetings"]
    assert [m["title"] for m in full] == ["m4", "m3", "m2", "m1", "m0"]

    paged: list[str] = []
    cursor: str | None = None
    while True:
        params: dict = {"limit": 2}
        if cursor:
            params["before"] = cursor
        page = await client.get(url, headers=auth_headers, params=params)
        body = page.json()
        paged.extend(m["id"] for m in body["meetings"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert paged == [m["id"] for m in full]
    assert len(set(paged)) == len(paged)


async def test_delete_by_uploader_and_admin_but_not_plain_editor(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session)
    owner = await _user(db_session, "Owner")
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, test_user, role="admin")
    uploader = await _user(db_session, "Uploader", email="uploader@example.com")
    await _member(db_session, org, uploader, role="member")
    plain = await _user(db_session, "Plain", email="plain@example.com")
    await _member(db_session, org, plain, role="member")
    project = await _project(db_session, owner, org=org)

    uploader_h = await _user_headers(db_session, uploader)
    plain_h = await _user_headers(db_session, plain)
    url = f"/api/v1/projects/{project.id}/meetings"

    m1 = await client.post(url, headers=uploader_h, json={"title": "m1", "content": "a"})
    assert m1.status_code == 200, m1.text
    meeting_id = m1.json()["id"]

    # A plain editor (member, not uploader/admin) cannot delete.
    denied = await client.delete(f"/api/v1/meetings/{meeting_id}", headers=plain_h)
    assert denied.status_code == 403

    # An org admin (not the uploader) can delete.
    ok = await client.delete(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)
    assert ok.status_code == 200

    # Deleted meeting is hidden from get and list.
    assert (await client.get(f"/api/v1/meetings/{meeting_id}", headers=uploader_h)).status_code == 404
    listed = await client.get(url, headers=uploader_h)
    assert all(m["id"] != meeting_id for m in listed.json()["meetings"])

    # The uploader can delete their own meeting.
    m2 = await client.post(url, headers=uploader_h, json={"title": "m2", "content": "b"})
    assert m2.status_code == 200, m2.text
    assert (await client.delete(f"/api/v1/meetings/{m2.json()['id']}", headers=uploader_h)).status_code == 200


async def test_override_ineligible_user_and_unknown_persona_give_same_422(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)

    other_org = await _org(db_session)
    foreign = await _user(db_session, "Foreign", email="foreign@example.com")
    await _member(db_session, other_org, foreign, role="member")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "t", "content": "hello"},
    )
    meeting_id = resp.json()["id"]
    participant = await _meeting_participant(db_session, meeting_id, "Analyst")
    url = f"/api/v1/meetings/{meeting_id}/participants/{participant.id}"

    unknown = await client.put(url, headers=auth_headers, json={"persona_name": "No Such Persona"})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["error"] == "participant_not_eligible"

    ineligible = await client.put(url, headers=auth_headers, json={"user_id": foreign.id})
    assert ineligible.status_code == 422
    assert ineligible.json()["error"]["details"]["error"] == "participant_not_eligible"

    # The participant row is untouched by both failed overrides.
    await db_session.refresh(participant)
    assert participant.user_id is None
    assert participant.persona_name is None
    assert participant.resolution_method == "unresolved"


async def test_resolve_participants_edges(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)

    other_org = await _org(db_session)
    cross = await _user(db_session, "Cross", email="cross@example.com")
    await _member(db_session, other_org, cross, role="member")

    inactive = await _user(db_session, "Inactive", email="inactive@example.com")
    inactive.is_active = False
    await _member(db_session, org, inactive, role="member")

    dup1 = await _user(db_session, "Duplicate", email="dup1@example.com")
    dup2 = await _user(db_session, "Duplicate", email="dup2@example.com")
    await _member(db_session, org, dup1, role="member")
    await _member(db_session, org, dup2, role="member")
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "t", "content": "hello"},
    )
    transcript = (
        await db_session.execute(
            select(MeetingTranscript).where(MeetingTranscript.id == resp.json()["id"])
        )
    ).scalar_one()

    resolved = await resolve_participants(db_session, transcript, [
        {"name": "Whatever", "email": "cross@example.com"},
        {"name": "Whatever", "email": "inactive@example.com"},
        {"name": "Duplicate"},
    ], auth=None)
    assert resolved[0]["resolution_method"] == "unresolved"
    assert resolved[0]["user_id"] is None
    assert resolved[1]["resolution_method"] == "unresolved"
    assert resolved[1]["user_id"] is None
    assert resolved[2]["resolution_method"] == "unresolved"
    assert resolved[2]["user_id"] is None


async def test_meeting_retention_sweep_and_setting_validation(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    from tests.server.integration.test_skill_marketplace import _platform_admin_headers

    admin, admin_headers = await _platform_admin_headers(db_session)

    # Org setting validation: 0 and 3651 rejected, 180 accepted.
    org = await _org(db_session, tier="team")
    await _member(db_session, org, test_user, role="admin")
    settings_url = f"/api/v1/orgs/{org.id}/settings"
    for bad in (0, 3651):
        assert (await client.put(settings_url, headers=auth_headers, json={"meeting_retention_days": bad})).status_code == 400
    good = await client.put(settings_url, headers=auth_headers, json={"meeting_retention_days": 180})
    assert good.status_code == 200, good.text
    assert good.json()["meeting_retention_days"] == 180

    # Tight retention window so the sweep has something to reap.
    org.settings = json.dumps({"general": {"meeting_retention_days": 5}})
    await db_session.commit()

    owner = await _user(db_session, "SweepOwner")
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    now = _now()

    expired = await _meeting_row(db_session, project=project, org_id=org.id, created_at=now - timedelta(days=10))
    fresh = await _meeting_row(db_session, project=project, org_id=org.id, created_at=now - timedelta(days=1))
    old_soft = await _meeting_row(
        db_session, project=project, org_id=org.id,
        created_at=now - timedelta(days=60), deleted_at=now - timedelta(days=31),
    )
    recent_soft = await _meeting_row(
        db_session, project=project, org_id=org.id,
        created_at=now - timedelta(days=1), deleted_at=now - timedelta(days=2),
    )
    await _meeting_participant(db_session, expired.id, "P1")
    await _meeting_action_item(db_session, expired.id)
    await _meeting_participant(db_session, old_soft.id, "P2")
    await _meeting_action_item(db_session, old_soft.id)

    # A non-admin cannot run the sweep.
    assert (await client.post("/api/v1/admin/sweeps/meeting-retention", headers=auth_headers, json={})).status_code == 403

    result = await client.post("/api/v1/admin/sweeps/meeting-retention", headers=admin_headers, json={})
    assert result.status_code == 200, result.text
    assert result.json()["meetings_deleted"] == 2

    async def _count(model, mid: str) -> int:
        return (
            await db_session.execute(
                select(func.count()).select_from(model).where(model.transcript_id == mid)
            )
        ).scalar_one()

    for mid in (expired.id, old_soft.id):
        assert (await db_session.execute(
            select(func.count()).select_from(MeetingTranscript).where(MeetingTranscript.id == mid)
        )).scalar_one() == 0
        assert await _count(MeetingParticipant, mid) == 0
        assert await _count(MeetingActionItem, mid) == 0

    for mid in (fresh.id, recent_soft.id):
        assert (await db_session.execute(
            select(func.count()).select_from(MeetingTranscript).where(MeetingTranscript.id == mid)
        )).scalar_one() == 1


async def test_delete_and_override_audit_ids_only_and_personal_scope_rows(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)

    secret = "TRANSCRIPT-SECRET-99"
    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "Audited", "content": secret},
    )
    meeting_id = resp.json()["id"]
    participant = await _meeting_participant(db_session, meeting_id, "Analyst")
    await _persona(db_session, org, "Analyst")

    # Participant override writes ids only — transcript text never appears.
    ov = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{participant.id}",
        headers=auth_headers, json={"persona_name": "Analyst"},
    )
    assert ov.status_code == 200, ov.text
    ov_event = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "meeting_participant_overridden",
            )
        )
    ).scalar_one()
    assert secret not in (ov_event.before or "")
    assert secret not in (ov_event.after or "")
    assert json.loads(ov_event.after)["meeting_id"] == meeting_id
    assert json.loads(ov_event.after)["participant_id"] == participant.id
    assert "persona_name" not in json.loads(ov_event.after)

    # Delete writes ids/counts only.
    dl = await client.delete(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)
    assert dl.status_code == 200
    dl_event = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "meeting_deleted",
            )
        )
    ).scalar_one()
    assert secret not in (dl_event.before or "")
    assert secret not in (dl_event.after or "")
    assert json.loads(dl_event.before)["meeting_id"] == meeting_id

    # Personal-project upload, override and delete all write an audit row with
    # org_id NULL (and an empty org-name snapshot) — ids/counts only, no text.
    personal = await _project(db_session, test_user)  # org_id is None
    p_up = await client.post(
        f"/api/v1/projects/{personal.id}/meetings",
        headers=auth_headers, json={"title": "Personal", "content": "PRIVATE"},
    )
    assert p_up.status_code == 200
    p_meeting_id = p_up.json()["id"]
    p_up_event = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.event_type == "meeting_uploaded",
                OrgAuditEvent.target_id == p_meeting_id,
            )
        )
    ).scalar_one()
    assert p_up_event.org_id is None
    assert p_up_event.org_name_snapshot == ""
    assert "PRIVATE" not in (p_up_event.before or "")
    assert "PRIVATE" not in (p_up_event.after or "")

    p_participant = await _meeting_participant(db_session, p_meeting_id, "Analyst")
    await _project_persona(db_session, personal, "Analyst")
    p_ov = await client.put(
        f"/api/v1/meetings/{p_meeting_id}/participants/{p_participant.id}",
        headers=auth_headers, json={"persona_name": "Analyst"},
    )
    assert p_ov.status_code == 200, p_ov.text
    p_ov_event = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.event_type == "meeting_participant_overridden",
                OrgAuditEvent.target_id == p_meeting_id,
            )
        )
    ).scalar_one()
    assert p_ov_event.org_id is None
    assert p_ov_event.org_name_snapshot == ""

    p_dl = await client.delete(f"/api/v1/meetings/{p_meeting_id}", headers=auth_headers)
    assert p_dl.status_code == 200
    p_dl_event = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.event_type == "meeting_deleted",
                OrgAuditEvent.target_id == p_meeting_id,
            )
        )
    ).scalar_one()
    assert p_dl_event.org_id is None
    assert p_dl_event.org_name_snapshot == ""
    assert "PRIVATE" not in (p_dl_event.before or "")
    assert "PRIVATE" not in (p_dl_event.after or "")


# ---------------------------------------------------------------------------
# Review round 1 — H1/H2/M1/M2/M3/M4/L2/L3 regressions
# ---------------------------------------------------------------------------


async def test_group_routes_return_uniform_404_for_unknown_other_org_and_tier(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Membership/visibility is checked before the plan feature gate: a
    non-member of the group's org (including a Free-tier org, which could
    never have groups) gets the exact same 404 body as an unknown group."""
    team_org = await _org(db_session, tier="team")
    owner = await _user(db_session, "Owner")
    await _member(db_session, team_org, owner, role="owner")
    group = await _group(db_session, team_org, owner)
    owner_h = await _user_headers(db_session, owner)
    up = await client.post(
        f"/api/v1/groups/{group.id}/meetings",
        headers=owner_h, json={"title": "g", "content": "x"},
    )
    assert up.status_code == 200, up.text
    meeting_id = up.json()["id"]

    free_org = await _org(db_session, tier="free")
    free_user = await _user(db_session, "FreeUser", email="free@example.com")
    await _member(db_session, free_org, free_user, role="owner")
    free_h = await _user_headers(db_session, free_user)

    other_org = await _org(db_session, tier="team")
    other_user = await _user(db_session, "OtherUser", email="other@example.com")
    await _member(db_session, other_org, other_user, role="owner")
    other_h = await _user_headers(db_session, other_user)

    unknown = await _user(db_session, "Unknown")
    unknown_h = await _user_headers(db_session, unknown)

    expected_group = {"code": "404", "message": "Project group not found", "details": {}}
    for h in (free_h, other_h, unknown_h):
        got = await client.get(f"/api/v1/groups/{group.id}/meetings", headers=h)
        assert got.status_code == 404
        assert got.json()["error"] == expected_group
        post = await client.post(
            f"/api/v1/groups/{group.id}/meetings", headers=h, json={"title": "t", "content": "x"}
        )
        assert post.status_code == 404
        assert post.json()["error"] == expected_group

    missing = await client.get("/api/v1/groups/pg_missing/meetings", headers=owner_h)
    assert missing.status_code == 404
    assert missing.json()["error"] == expected_group

    expected_meeting = {"code": "404", "message": "Meeting not found", "details": {}}
    for h in (free_h, other_h, unknown_h):
        got = await client.get(f"/api/v1/meetings/{meeting_id}", headers=h)
        assert got.status_code == 404
        assert got.json()["error"] == expected_meeting


async def test_meeting_title_dlp_block_refuses_and_stores_nothing(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    settings = json.dumps({"dlp": {"enabled": True, "mode": "block", "categories": ["secrets"]}})
    org = await _org(db_session, settings=settings)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)
    ghp = "ghp_" + "A" * 36

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": f"leaked {ghp}", "content": "clean"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "dlp_blocked"
    count = (
        await db_session.execute(
            select(func.count()).select_from(MeetingTranscript).where(
                MeetingTranscript.project_id == project.id
            )
        )
    ).scalar_one()
    assert count == 0


async def test_meeting_title_dlp_redact_stores_redacted_title(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    settings = json.dumps({"dlp": {"enabled": True, "mode": "redact", "categories": ["secrets"]}})
    org = await _org(db_session, settings=settings)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)
    ghp = "ghp_" + "A" * 36

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": f"leaked {ghp}", "content": "clean"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert ghp not in body["title"]
    assert "REDACTED" in body["title"]
    assert body["dlp_findings"] >= 1


async def test_meeting_title_nul_and_surrogate_are_sanitised(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"
    raw_title = "Bad\x00Title\ud800 here\nline2"
    body = json.dumps({"title": raw_title, "content": "x"})
    resp = await client.post(
        url,
        headers={**auth_headers, "content-type": "application/json"},
        content=body,
    )
    assert resp.status_code == 200, resp.text
    title = resp.json()["title"]
    assert "\x00" not in title
    assert "\ud800" not in title
    assert "\n" not in title
    assert "�" in title


async def test_meeting_misses_are_uniform_meeting_not_found(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Every miss on a meeting id — unknown, unreadable project, unreadable
    group, deleted — returns the same "Meeting not found" 404 body."""
    org = await _org(db_session, tier="team")
    owner = await _user(db_session, "Owner")
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    owner_h = await _user_headers(db_session, owner)

    proj_up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=owner_h, json={"title": "p", "content": "x"},
    )
    meeting_id = proj_up.json()["id"]
    grp_up = await client.post(
        f"/api/v1/groups/{group.id}/meetings",
        headers=owner_h, json={"title": "g", "content": "x"},
    )
    grp_meeting_id = grp_up.json()["id"]

    foreign = await _user(db_session, "Foreign", email="foreign2@example.com")
    foreign_h = await _user_headers(db_session, foreign)
    expected = {"code": "404", "message": "Meeting not found", "details": {}}

    # Unknown id.
    assert (await client.get("/api/v1/meetings/meet_missing", headers=owner_h)).json()["error"] == expected
    # Unreadable project meeting (previously "Project not found").
    assert (await client.get(f"/api/v1/meetings/{meeting_id}", headers=foreign_h)).json()["error"] == expected
    # Unreadable group meeting (no grant).
    assert (await client.get(f"/api/v1/meetings/{grp_meeting_id}", headers=foreign_h)).json()["error"] == expected

    # Override on an unreadable meeting is the same 404 (not 403 "no access").
    ov = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/mp_missing",
        headers=foreign_h, json={"persona_name": "X"},
    )
    assert ov.status_code == 404
    assert ov.json()["error"] == expected

    # Deleted meeting reads as "Meeting not found" too.
    assert (await client.delete(f"/api/v1/meetings/{meeting_id}", headers=owner_h)).status_code == 200
    assert (await client.get(f"/api/v1/meetings/{meeting_id}", headers=owner_h)).json()["error"] == expected


async def test_personal_project_redacts_secrets_without_policy(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """With no org DLP policy, secrets are still redacted before storage."""
    project = await _project(db_session, test_user)  # personal, no org
    ghp = "ghp_" + "A" * 36
    akia = "AKIA" + "B" * 16

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers,
        json={"title": "t", "content": f"token {ghp} and key {akia}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert ghp not in body["content"]
    assert akia not in body["content"]
    assert "REDACTED" in body["content"]
    assert body["dlp_findings"] >= 2


async def test_merge_tombstone_blocks_upload_and_reassigns_meetings(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from tests.server.integration.test_admin_overrides import _hdrs, _user as _admin_user

    admin, admin_key = await _admin_user(db_session, "ops", tier="admin")
    alice = await _user(db_session, "Alice")
    bob = await _user(db_session, "Bob")
    source = await _project(db_session, alice)
    target = await _project(db_session, bob)

    alice_h = await _user_headers(db_session, alice)
    up = await client.post(
        f"/api/v1/projects/{source.id}/meetings",
        headers=alice_h, json={"title": "m", "content": "x"},
    )
    assert up.status_code == 200, up.text
    meeting_id = up.json()["id"]

    plan = await client.post(
        f"/api/v1/admin/projects/{target.id}/merge-from/{source.id}",
        json={"reason": "dup", "dry_run": True},
        headers=_hdrs(admin_key),
    )
    assert plan.status_code == 200, plan.text
    assert "meeting_transcripts" in plan.text

    done = await client.post(
        f"/api/v1/admin/projects/{target.id}/merge-from/{source.id}",
        json={"reason": "dup", "dry_run": False},
        headers=_hdrs(admin_key),
    )
    assert done.status_code == 200, done.text

    moved = (
        await db_session.execute(
            select(MeetingTranscript.project_id).where(MeetingTranscript.id == meeting_id)
        )
    ).scalar_one()
    assert moved == target.id

    bob_h = await _user_headers(db_session, bob)
    listed = await client.get(f"/api/v1/projects/{target.id}/meetings", headers=bob_h)
    assert [m["id"] for m in listed.json()["meetings"]] == [meeting_id]

    # Upload to the tombstoned source is now 410 project_merged.
    again = await client.post(
        f"/api/v1/projects/{source.id}/meetings",
        headers=alice_h, json={"title": "t", "content": "x"},
    )
    assert again.status_code == 410
    assert again.json()["error"]["code"] == "project_merged"


async def test_transferred_project_override_uses_live_scope_org(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """After a project moves to a new org, participant override resolves
    personas against the live scope org — not the stale meeting.org_id."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    await _member(db_session, org_a, test_user, role="owner")
    await _persona(db_session, org_a, "Analyst")  # only exists in org A
    project = await _project(db_session, test_user, org=org_a)

    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "t", "content": "hello"},
    )
    meeting_id = up.json()["id"]
    participant = await _meeting_participant(db_session, meeting_id, "Analyst")

    # Simulate a transfer: project moves to org B, leaving meeting.org_id stale.
    project.org_id = org_b.id
    await db_session.commit()

    ov = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{participant.id}",
        headers=auth_headers, json={"persona_name": "Analyst"},
    )
    assert ov.status_code == 422
    assert ov.json()["error"]["details"]["error"] == "participant_not_eligible"

    # Responses report the live scope org, not the stale stored one.
    got = await client.get(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)
    assert got.json()["org_id"] == org_b.id
    listed = await client.get(f"/api/v1/projects/{project.id}/meetings", headers=auth_headers)
    assert [m["org_id"] for m in listed.json()["meetings"]] == [org_b.id]


async def test_transferred_project_retention_uses_live_scope_org(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from tests.server.integration.test_skill_marketplace import _platform_admin_headers

    _, admin_headers = await _platform_admin_headers(db_session)

    org_a = await _org(db_session, settings=json.dumps({"general": {"meeting_retention_days": 1000}}))
    org_b = await _org(db_session, settings=json.dumps({"general": {"meeting_retention_days": 1}}))
    owner = await _user(db_session, "TOwner")
    await _member(db_session, org_a, owner, role="owner")
    project = await _project(db_session, owner, org=org_a)
    meeting = await _meeting_row(
        db_session, project=project, org_id=org_a.id,
        created_at=_now() - timedelta(days=5),
    )

    project.org_id = org_b.id
    await db_session.commit()

    result = await client.post("/api/v1/admin/sweeps/meeting-retention", headers=admin_headers, json={})
    assert result.status_code == 200, result.text
    assert result.json()["meetings_deleted"] == 1
    assert (await db_session.execute(
        select(func.count()).select_from(MeetingTranscript).where(MeetingTranscript.id == meeting.id)
    )).scalar_one() == 0


async def test_validation_error_does_not_echo_submitted_text(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"
    secret = "LEAK-TITLE-7f3a"
    resp = await client.post(
        url, headers=auth_headers,
        json={"title": secret, "content": "x", "source": "bogus"},
    )
    assert resp.status_code == 422
    assert secret not in resp.text
    assert secret not in json.dumps(resp.json())


async def test_delete_audit_counts_real_rows(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)

    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "t", "content": "hello"},
    )
    meeting_id = up.json()["id"]
    await _meeting_participant(db_session, meeting_id, "A")
    await _meeting_participant(db_session, meeting_id, "B")
    await _meeting_action_item(db_session, meeting_id)

    dl = await client.delete(f"/api/v1/meetings/{meeting_id}", headers=auth_headers)
    assert dl.status_code == 200
    dl_event = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "meeting_deleted",
                OrgAuditEvent.target_id == meeting_id,
            )
        )
    ).scalar_one()
    before = json.loads(dl_event.before or "{}")
    assert before["participant_count"] == 2
    assert before["action_item_count"] == 1


# ---------------------------------------------------------------------------
# Codex round 1 findings — service-key delete authorization, persona
# visibility, service-key 404 uniformity, personal audit rows, cue
# normalisation and retention overflow.
# ---------------------------------------------------------------------------


async def test_service_key_delete_uses_backing_user_authorization(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A meetings:write service key can only delete a project meeting when its
    backing user is the uploader (or a project admin) — same rule as user keys."""
    org = await _org(db_session)
    owner = await _user(db_session, "Owner")
    await _member(db_session, org, owner, role="owner")
    uploader = await _user(db_session, "Uploader", email="svc-uploader@example.com")
    await _member(db_session, org, uploader, role="member")
    editor = await _user(db_session, "Editor", email="svc-editor@example.com")
    await _member(db_session, org, editor, role="member")
    project = await _project(db_session, owner, org=org)

    uploader_h = await _user_headers(db_session, uploader)
    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=uploader_h, json={"title": "t", "content": "x"},
    )
    assert up.status_code == 200, up.text
    meeting_id = up.json()["id"]

    # Backing user is a plain editor (not the uploader) → forbidden.
    editor_key = await _service_key(
        db_session, owner=editor, org_id=org.id,
        scopes=["meetings:write"], project_ids=[project.id],
    )
    denied = await client.delete(f"/api/v1/meetings/{meeting_id}", headers=editor_key)
    assert denied.status_code == 403

    # Backing user is the uploader → allowed.
    uploader_key = await _service_key(
        db_session, owner=uploader, org_id=org.id,
        scopes=["meetings:write"], project_ids=[project.id],
    )
    ok = await client.delete(f"/api/v1/meetings/{meeting_id}", headers=uploader_key)
    assert ok.status_code == 200, ok.text


async def test_override_project_persona_on_personal_project(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A project-scoped persona resolves on a personal project (no org)."""
    project = await _project(db_session, test_user)  # org_id is None
    await _project_persona(db_session, project, "ProjectScout")

    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "t", "content": "hello"},
    )
    meeting_id = up.json()["id"]
    participant = await _meeting_participant(db_session, meeting_id, "ProjectScout")

    ov = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{participant.id}",
        headers=auth_headers, json={"persona_name": "ProjectScout"},
    )
    assert ov.status_code == 200, ov.text
    assert ov.json()["persona_name"] == "ProjectScout"
    assert ov.json()["user_id"] is None
    assert ov.json()["resolution_method"] == "manual"


async def test_override_retained_owner_cannot_attach_new_org_persona(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """After a project transfer, a retained owner who is not a member of the
    new org cannot attach the new org's persona (same generic 422)."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    await _member(db_session, org_a, test_user, role="owner")
    await _persona(db_session, org_b, "NewAnalyst")  # only in org B
    project = await _project(db_session, test_user, org=org_a)

    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "t", "content": "hello"},
    )
    meeting_id = up.json()["id"]
    participant = await _meeting_participant(db_session, meeting_id, "NewAnalyst")

    # Transfer to org B; test_user remains a member of org A only.
    project.org_id = org_b.id
    await db_session.commit()

    ov = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{participant.id}",
        headers=auth_headers, json={"persona_name": "NewAnalyst"},
    )
    assert ov.status_code == 422
    assert ov.json()["error"]["details"]["error"] == "participant_not_eligible"


async def test_resolve_participants_respects_persona_visibility(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """resolve_participants applies the same visibility rule as the override:
    a caller who is not a member of the project's live org cannot resolve that
    org's persona, even though the name exists there."""
    from sessionfs.server.auth.dependencies import AuthContext

    org_a = await _org(db_session)
    org_b = await _org(db_session)
    await _member(db_session, org_a, test_user, role="owner")
    await _persona(db_session, org_b, "NewAnalyst")
    project = await _project(db_session, test_user, org=org_a)

    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "t", "content": "hello"},
    )
    transcript = (
        await db_session.execute(
            select(MeetingTranscript).where(MeetingTranscript.id == up.json()["id"])
        )
    ).scalar_one()

    project.org_id = org_b.id
    await db_session.commit()

    auth = AuthContext(user=test_user, api_key_id="", key_kind="user")
    resolved = await resolve_participants(
        db_session, transcript, [{"name": "NewAnalyst"}], auth=auth
    )
    assert resolved[0]["resolution_method"] == "unresolved"
    assert resolved[0]["persona_name"] is None


async def test_service_key_meeting_misses_are_uniform_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A service key bound to another org gets a byte-identical 404 body for
    unknown, foreign-project and group meeting ids on GET and DELETE."""
    org = await _org(db_session, tier="team")
    owner = await _user(db_session, "Owner")
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    owner_h = await _user_headers(db_session, owner)

    proj_up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=owner_h, json={"title": "p", "content": "x"},
    )
    proj_meeting_id = proj_up.json()["id"]
    grp_up = await client.post(
        f"/api/v1/groups/{group.id}/meetings",
        headers=owner_h, json={"title": "g", "content": "x"},
    )
    grp_meeting_id = grp_up.json()["id"]

    other_org = await _org(db_session, tier="team")
    svc_owner = await _user(db_session, "SvcOwner", email="svc-owner@example.com")
    await _member(db_session, other_org, svc_owner, role="owner")
    svc = await _service_key(
        db_session, owner=svc_owner, org_id=other_org.id,
        scopes=["meetings:read", "meetings:write"],
    )

    targets = {
        "unknown": "meet_missing",
        "foreign_project": proj_meeting_id,
        "group": grp_meeting_id,
    }
    get_bodies: dict[str, bytes] = {}
    for label, mid in targets.items():
        got = await client.get(f"/api/v1/meetings/{mid}", headers=svc)
        assert got.status_code == 404, (label, got.text)
        get_bodies[label] = got.content
        deleted = await client.delete(f"/api/v1/meetings/{mid}", headers=svc)
        assert deleted.status_code == 404, (label, deleted.text)
        assert deleted.content == got.content, label

    assert get_bodies["unknown"] == get_bodies["foreign_project"] == get_bodies["group"]


async def test_file_upload_normalises_vtt_and_srt_cues(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """file_upload strips VTT/SRT cue timing to `Speaker: text` lines before
    DLP and line numbering; a plain .txt upload is left unchanged."""
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"

    vtt = (
        "WEBVTT\n"
        "\n"
        "NOTE a comment\n"
        "spans lines\n"
        "\n"
        "1\n"
        "00:00:01.000 --> 00:00:03.000\n"
        "<v Alice>Hello there</v>\n"
        "<v Bob>Hi Alice</v>\n"
        "\n"
        "2\n"
        "00:00:03.000 --> 00:00:05.000\n"
        "Same line\n"
        "Same line\n"
    )
    resp = await client.post(
        url, headers=auth_headers, json={"title": "vtt", "content": vtt, "source": "file_upload"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "Alice: Hello there\nBob: Hi Alice\nSame line"

    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:03,000\n"
        "First cue\n"
        "\n"
        "2\n"
        "00:00:03,000 --> 00:00:05,000\n"
        "Second cue\n"
        "Second cue\n"
    )
    resp2 = await client.post(
        url, headers=auth_headers, json={"title": "srt", "content": srt, "source": "file_upload"},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["content"] == "First cue\nSecond cue"

    txt = "Alice: plan\nBob: ship"
    resp3 = await client.post(
        url, headers=auth_headers, json={"title": "txt", "content": txt, "source": "file_upload"},
    )
    assert resp3.status_code == 200, resp3.text
    assert resp3.json()["content"] == "Alice: plan\nBob: ship"


async def test_meeting_retention_sweep_survives_huge_days_setting(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An org with an absurd retention value written out-of-band falls back to
    the default instead of overflowing timedelta and stopping the sweep."""
    from tests.server.integration.test_skill_marketplace import _platform_admin_headers

    _, admin_headers = await _platform_admin_headers(db_session)

    bad_org = await _org(
        db_session,
        settings=json.dumps({"general": {"meeting_retention_days": 10**20}}),
    )
    bad_owner = await _user(db_session, "BadOwner")
    await _member(db_session, bad_org, bad_owner, role="owner")
    bad_project = await _project(db_session, bad_owner, org=bad_org)
    bad_meeting = await _meeting_row(
        db_session, project=bad_project, org_id=bad_org.id,
        created_at=_now() - timedelta(days=10),
    )

    good_org = await _org(
        db_session,
        settings=json.dumps({"general": {"meeting_retention_days": 1}}),
    )
    good_owner = await _user(db_session, "GoodOwner")
    await _member(db_session, good_org, good_owner, role="owner")
    good_project = await _project(db_session, good_owner, org=good_org)
    expired = await _meeting_row(
        db_session, project=good_project, org_id=good_org.id,
        created_at=_now() - timedelta(days=5),
    )

    result = await client.post("/api/v1/admin/sweeps/meeting-retention", headers=admin_headers, json={})
    assert result.status_code == 200, result.text
    assert result.json()["meetings_deleted"] == 1

    # The healthy org's expired meeting is gone; the bad org's meeting stays
    # (its retention fell back to the 180-day default).
    assert (await db_session.execute(
        select(func.count()).select_from(MeetingTranscript).where(MeetingTranscript.id == expired.id)
    )).scalar_one() == 0
    assert (await db_session.execute(
        select(func.count()).select_from(MeetingTranscript).where(MeetingTranscript.id == bad_meeting.id)
    )).scalar_one() == 1


async def test_file_upload_cue_parsing_keeps_ordinary_arrows_and_header_less_vtt(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """Only real cue timestamps count as timing lines: text with an arrow is
    kept, a WEBVTT file missing the blank header line keeps its cues, nested
    cue markup is removed, and a file that reduces to nothing is refused."""
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"

    arrows = "1\nAlice --> Bob\nSecret discussion\n"
    resp = await client.post(
        url, headers=auth_headers, json={"title": "a", "content": arrows, "source": "file_upload"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"].rstrip("\n") == "1\nAlice --> Bob\nSecret discussion"

    no_blank = (
        "WEBVTT\n"
        "00:00:01.000 --> 00:00:02.000 align:start\n"
        "<v Alice><i>Hello</i> <v Bob>there</v></v>\n"
        "\n"
        "00:00:02.000 --> 00:00:03.000\n"
        "<v.loud Bob>Unclosed voice span\n"
    )
    resp = await client.post(
        url, headers=auth_headers, json={"title": "b", "content": no_blank, "source": "file_upload"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "Alice: Hello there\nBob: Unclosed voice span"

    empty = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n\n"
    resp = await client.post(
        url, headers=auth_headers, json={"title": "c", "content": empty, "source": "file_upload"},
    )
    assert resp.status_code == 422


async def test_override_persona_fills_represented_user(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """Picking a persona that represents a member labels the participant with
    both, the same as automatic resolution."""
    org = await _org(db_session)
    await _member(db_session, org, test_user, role="owner")
    tolu = await _user(db_session, "Tolu")
    await _member(db_session, org, tolu)
    await _persona(db_session, org, "tolu", represented_user_id=tolu.id)
    project = await _project(db_session, test_user, org=org)

    up = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=auth_headers, json={"title": "t", "content": "hello"},
    )
    meeting_id = up.json()["id"]
    participant = await _meeting_participant(db_session, meeting_id, "Tolu O")

    ov = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{participant.id}",
        headers=auth_headers, json={"persona_name": "tolu"},
    )
    assert ov.status_code == 200, ov.text
    assert ov.json()["persona_name"] == "tolu"
    assert ov.json()["user_id"] == tolu.id


async def test_file_upload_caption_signatures_and_numeric_cue_text(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A BOM-prefixed WebVTT file is parsed, a near-miss signature is left as
    plain text, and a cue whose whole text is a number survives SRT parsing."""
    project = await _project(db_session, test_user)
    url = f"/api/v1/projects/{project.id}/meetings"

    bom = "﻿WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Alice>Hi</v>\n"
    resp = await client.post(
        url, headers=auth_headers, json={"title": "a", "content": bom, "source": "file_upload"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "Alice: Hi"

    near_miss = "WEBVTTX notes\nAgenda\n"
    resp = await client.post(
        url, headers=auth_headers, json={"title": "b", "content": near_miss, "source": "file_upload"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"].rstrip("\n") == "WEBVTTX notes\nAgenda"

    leading_blank = "\nWEBVTT\nAgenda\n"
    resp = await client.post(
        url, headers=auth_headers,
        json={"title": "b2", "content": leading_blank, "source": "file_upload"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"].strip("\n") == "WEBVTT\nAgenda"

    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n2026\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nBudget approved\n"
    )
    resp = await client.post(
        url, headers=auth_headers, json={"title": "c", "content": srt, "source": "file_upload"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "2026\nBudget approved"
