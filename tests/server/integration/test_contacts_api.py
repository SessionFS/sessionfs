"""External contacts (GitHub #125, docs/design/external-contacts.md).

Directory CRUD + same-404 + email masking + service keys, DLP on stored
text, resolution order (contact after users/personas, never created by
extraction), participant override, ticket assignment (persona XOR person XOR
contact), merge, erasure, link-on-verified-email moving open tickets, the
"To confirm" queue, export and the retention sweep.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    ExternalContact,
    MeetingParticipant,
    Ticket,
    TicketEvent,
)
from sessionfs.server.routes.meetings import resolve_participants
from sessionfs.server.services.contacts import link_contacts_for_user
from tests.server.integration.test_meetings_api import (
    _meeting_row,
    _member,
    _now,
    _org,
    _persona,
    _project,
    _service_key,
    _user,
    _user_headers,
)


async def _setup(db: AsyncSession, test_user, *, settings: str = "{}"):
    org = await _org(db, settings=settings)
    await _member(db, org, test_user, role="owner")
    return org


async def _create(client, headers, org_id, **body):
    body.setdefault("display_name", "Tolulope Adeyemi")
    resp = await client.post(f"/api/v1/orgs/{org_id}/contacts", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _participant(db, meeting_id: str, name: str, **kw) -> MeetingParticipant:
    p = MeetingParticipant(
        id=f"mp_{uuid.uuid4().hex[:12]}",
        transcript_id=meeting_id,
        name_raw=name,
        resolution_method=kw.pop("resolution_method", "unresolved"),
        **kw,
    )
    db.add(p)
    await db.commit()
    return p


# ── Directory, visibility, masking ──


async def test_crud_same_404_and_email_masking(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    org = await _setup(db_session, test_user)
    other_org = await _org(db_session)
    member = await _user(db_session, "Member")
    await _member(db_session, org, member)
    member_h = await _user_headers(db_session, member)
    outsider = await _user(db_session, "Outsider")
    outsider_h = await _user_headers(db_session, outsider)

    created = await _create(
        client, auth_headers, org.id, email="Tolu@Vendor.example", role="clinical lead",
        tags=["Vendor", "clinical"], aliases=["Tolu"],
    )
    cid = created["id"]
    assert created["email"] == "tolu@vendor.example"
    assert created["tags"] == ["vendor", "clinical"]
    assert created["aliases"] == ["Tolu"]

    # A plain member sees the email masked; owner/creator see it.
    got = (await client.get(f"/api/v1/orgs/{org.id}/contacts/{cid}", headers=member_h)).json()
    assert got["email"] == "t***@vendor.example" and got["email_masked"] is True
    listed = (await client.get(f"/api/v1/orgs/{org.id}/contacts", headers=member_h)).json()
    assert [c["id"] for c in listed["items"]] == [cid]
    assert (await client.get(f"/api/v1/orgs/{org.id}/contacts", headers=member_h,
                             params={"q": "tolu"})).json()["items"][0]["id"] == cid

    # A member who creates a contact sees their own entry's email.
    own = await _create(client, member_h, org.id, display_name="Jamie Vendor",
                        email="jamie@vendor.example")
    assert own["email"] == "jamie@vendor.example"

    # Same 404 for an outsider, another org's path, an unknown id.
    for headers, path in (
        (outsider_h, f"/api/v1/orgs/{org.id}/contacts/{cid}"),
        (auth_headers, f"/api/v1/orgs/{other_org.id}/contacts/{cid}"),
        (auth_headers, f"/api/v1/orgs/{org.id}/contacts/ec_000000000000"),
        (auth_headers, f"/api/v1/orgs/{org.id}/contacts/not-an-id"),
    ):
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 404, (path, resp.text)
    assert (await client.get(f"/api/v1/orgs/{org.id}/contacts", headers=outsider_h)).status_code == 404

    # PATCH: explicit null clears role; archive drops it from the default list.
    patched = await client.patch(f"/api/v1/orgs/{org.id}/contacts/{cid}", headers=member_h,
                                 json={"role": None, "status": "archived"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["role"] is None and patched.json()["status"] == "archived"
    assert cid not in [c["id"] for c in (await client.get(
        f"/api/v1/orgs/{org.id}/contacts", headers=member_h)).json()["items"]]
    assert cid in [c["id"] for c in (await client.get(
        f"/api/v1/orgs/{org.id}/contacts", headers=member_h, params={"status": "archived"})).json()["items"]]

    # Aliases add / remove.
    added = await client.post(f"/api/v1/orgs/{org.id}/contacts/{own['id']}/aliases",
                              headers=member_h, json={"alias": "JV"})
    assert added.json()["aliases"] == ["JV"]
    removed = await client.delete(f"/api/v1/orgs/{org.id}/contacts/{own['id']}/aliases",
                                  headers=member_h, params={"alias": "jv"})
    assert removed.json()["aliases"] == []

    # Members can't merge, delete or export.
    for method, path in (
        ("delete", f"/api/v1/orgs/{org.id}/contacts/{own['id']}"),
        ("get", f"/api/v1/orgs/{org.id}/contacts/{own['id']}/export"),
        ("post", f"/api/v1/orgs/{org.id}/contacts/{own['id']}/merge-from/{cid}"),
    ):
        resp = await getattr(client, method)(path, headers=member_h)
        assert resp.status_code == 403, (path, resp.text)
    csv_resp = await client.get(f"/api/v1/orgs/{org.id}/contacts", headers=member_h,
                                params={"format": "csv"})
    assert csv_resp.status_code == 403


async def test_dedupe_and_dlp(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    settings = json.dumps({"dlp": {"enabled": True, "mode": "redact", "categories": ["secrets"]}})
    org = await _setup(db_session, test_user, settings=settings)
    member = await _user(db_session, "Mem", email=f"mem-{uuid.uuid4().hex[:6]}@acme.example")
    await _member(db_session, org, member)

    first = await _create(client, auth_headers, org.id, email="a@vendor.example")
    dup = await client.post(f"/api/v1/orgs/{org.id}/contacts", headers=auth_headers,
                            json={"display_name": "Other", "email": "A@vendor.example"})
    assert dup.status_code == 409 and dup.json()["error"]["details"]["error"] == "contact_exists"
    is_member = await client.post(f"/api/v1/orgs/{org.id}/contacts", headers=auth_headers,
                                  json={"display_name": "Mem", "email": member.email})
    assert is_member.json()["error"]["details"]["error"] == "email_is_member"
    same_name = await _create(client, auth_headers, org.id)
    assert [d["id"] for d in same_name["possible_duplicates"]] == [first["id"]]

    ghp = "ghp_" + "A" * 36
    redacted = await _create(client, auth_headers, org.id, display_name="Ops", role=f"key {ghp}")
    assert ghp not in redacted["role"] and "REDACTED" in redacted["role"]
    bad = await client.post(f"/api/v1/orgs/{org.id}/contacts", headers=auth_headers,
                            json={"display_name": "X", "email": "not-an-email"})
    assert bad.status_code == 422


async def test_service_keys(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    org = await _setup(db_session, test_user)
    other = await _org(db_session)
    created = await _create(client, auth_headers, org.id, email="s@vendor.example")
    read = await _service_key(db_session, owner=test_user, org_id=org.id, scopes=["contacts:read"])
    write = await _service_key(db_session, owner=test_user, org_id=org.id,
                               scopes=["contacts:read", "contacts:write"])
    foreign = await _service_key(db_session, owner=test_user, org_id=other.id,
                                 scopes=["contacts:read"])

    got = await client.get(f"/api/v1/orgs/{org.id}/contacts/{created['id']}", headers=read)
    assert got.status_code == 200 and got.json()["email"] is None and got.json()["has_email"]
    assert (await client.get(f"/api/v1/orgs/{org.id}/contacts/{created['id']}",
                             headers=foreign)).status_code == 404
    assert (await client.post(f"/api/v1/orgs/{org.id}/contacts", headers=read,
                              json={"display_name": "Nope"})).status_code == 403
    made = await client.post(f"/api/v1/orgs/{org.id}/contacts", headers=write,
                             json={"display_name": "By Key"})
    assert made.status_code == 201
    row = await db_session.get(ExternalContact, made.json()["id"])
    assert row.actor_type == "service_key"
    for method, path in (
        ("delete", f"/api/v1/orgs/{org.id}/contacts/{created['id']}"),
        ("get", f"/api/v1/orgs/{org.id}/contacts/{created['id']}/export"),
        ("get", f"/api/v1/orgs/{org.id}/contacts/suggestions"),
    ):
        resp = await getattr(client, method)(path, headers=write)
        assert resp.status_code == 403, (path, resp.text)


# ── Resolution + participant override ──


async def test_resolution_order_and_override(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    org = await _setup(db_session, test_user)
    other = await _org(db_session)
    project = await _project(db_session, test_user, org=org)
    await _persona(db_session, org, "atlas")
    contact = await _create(client, auth_headers, org.id, email="tolu@vendor.example",
                            aliases=["Tolu"])
    meeting = await _meeting_row(db_session, project=project, org_id=org.id, created_at=_now())

    resolved = await resolve_participants(db_session, meeting, [
        {"name": "Tolu"},
        {"name": "Someone", "email": "TOLU@vendor.example"},
        {"name": "atlas"},
        {"name": "Nobody Known"},
    ], auth=None)
    assert [r["resolution_method"] for r in resolved] == ["contact", "contact", "persona", "unresolved"]
    assert resolved[0]["contact_id"] == contact["id"]

    # Two contacts sharing an alias: ambiguous, stays unresolved.
    await _create(client, auth_headers, org.id, display_name="Tolu Other", aliases=["Tolu"])
    again = await resolve_participants(db_session, meeting, [{"name": "Tolu"}], auth=None)
    assert again[0]["resolution_method"] == "unresolved"

    # Override: exactly one target; a contact of another org is a uniform 422.
    p = await _participant(db_session, meeting.id, "T. A.")
    url = f"/api/v1/meetings/{meeting.id}/participants/{p.id}"
    ok = await client.put(url, headers=auth_headers, json={"contact_id": contact["id"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["contact"]["display_name"] == "Tolulope Adeyemi"
    assert "email" not in ok.json()["contact"]
    foreign = ExternalContact(id="ec_" + uuid.uuid4().hex[:12], org_id=other.id,
                              display_name="F", name_norm="f", created_at=_now(), updated_at=_now())
    db_session.add(foreign)
    await db_session.commit()
    bad = await client.put(url, headers=auth_headers, json={"contact_id": foreign.id})
    assert bad.status_code == 422
    two = await client.put(url, headers=auth_headers,
                           json={"contact_id": contact["id"], "persona_name": "atlas"})
    assert two.status_code == 422

    detail = (await client.get(f"/api/v1/meetings/{meeting.id}", headers=auth_headers)).json()
    part = next(x for x in detail["participants"] if x["id"] == p.id)
    assert part["contact"]["id"] == contact["id"]


# ── Tickets ──


async def test_ticket_assignment_to_contact(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    org = await _setup(db_session, test_user)
    other = await _org(db_session)
    project = await _project(db_session, test_user, org=org)
    contact = await _create(client, auth_headers, org.id)
    base = f"/api/v1/projects/{project.id}/tickets"

    made = await client.post(base, headers=auth_headers,
                             json={"title": "Chase vendor", "assigned_contact_id": contact["id"]})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["assigned_contact_id"] == contact["id"]
    assert body["assigned_contact"]["display_name"] == "Tolulope Adeyemi"
    tid = body["id"]

    conflict = await client.post(base, headers=auth_headers, json={
        "title": "x", "assigned_contact_id": contact["id"], "assigned_to": "atlas"})
    assert conflict.status_code == 422
    assert conflict.json()["error"]["details"]["error"] == "assignee_conflict"
    foreign = ExternalContact(id="ec_" + uuid.uuid4().hex[:12], org_id=other.id,
                              display_name="F", name_norm="f", created_at=_now(), updated_at=_now())
    db_session.add(foreign)
    await db_session.commit()
    for value in (foreign.id, "ec_000000000000"):
        miss = await client.post(base, headers=auth_headers,
                                 json={"title": "x", "assigned_contact_id": value})
        assert miss.json()["error"]["details"]["error"] == "assignee_not_eligible"

    listed = await client.get(base, headers=auth_headers, params={"assigned_contact": contact["id"]})
    assert [t["id"] for t in listed.json()] == [tid]

    # Reassign to a persona clears the contact; back to the contact; unassign.
    upd = await client.put(f"{base}/{tid}", headers=auth_headers, json={"assigned_to": "atlas"})
    assert upd.json()["assigned_contact_id"] is None and upd.json()["assigned_to"] == "atlas"
    upd = await client.put(f"{base}/{tid}", headers=auth_headers,
                           json={"assigned_contact_id": contact["id"]})
    assert upd.json()["assigned_contact_id"] == contact["id"] and upd.json()["assigned_to"] is None
    upd = await client.put(f"{base}/{tid}", headers=auth_headers, json={"assigned_contact_id": None})
    assert upd.status_code == 200 and upd.json()["assigned_contact_id"] is None
    fields = (await db_session.execute(
        select(TicketEvent.field, TicketEvent.action).where(TicketEvent.ticket_id == tid)
    )).all()
    assert ("assigned_contact_id", "assigned") in fields
    assert ("assigned_contact_id", "unassigned") in fields


# ── Merge, delete, export ──


async def test_merge_delete_export(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    target = await _create(client, auth_headers, org.id, display_name="Tolulope A.")
    source = await _create(client, auth_headers, org.id, display_name="Tolu",
                           email="tolu@vendor.example", role="lead")
    meeting = await _meeting_row(db_session, project=project, org_id=org.id, created_at=_now())
    p = await _participant(db_session, meeting.id, "Tolu", contact_id=source["id"],
                           resolution_method="contact")
    ticket = (await client.post(f"/api/v1/projects/{project.id}/tickets", headers=auth_headers,
                                json={"title": "t", "assigned_contact_id": source["id"]})).json()

    url = f"/api/v1/orgs/{org.id}/contacts/{target['id']}/merge-from/{source['id']}"
    preview = await client.post(url, headers=auth_headers)
    assert preview.status_code == 200, preview.text
    assert preview.json() | {"target": None} == {
        "confirmed": False, "target": None, "source_id": source["id"],
        "participants_moved": 1, "tickets_moved": 1, "aliases_added": ["Tolu"],
    }
    assert (await db_session.get(Ticket, ticket["id"])).assigned_contact_id == source["id"]
    done = await client.post(url, headers=auth_headers, params={"confirm": "true"})
    assert done.status_code == 200, done.text
    merged = done.json()["target"]
    assert merged["aliases"] == ["Tolu"]
    assert merged["email"] == "tolu@vendor.example" and merged["role"] == "lead"
    db_session.expunge_all()
    assert (await db_session.get(MeetingParticipant, p.id)).contact_id == target["id"]
    assert (await db_session.get(Ticket, ticket["id"])).assigned_contact_id == target["id"]
    redirected = (await client.get(f"/api/v1/orgs/{org.id}/contacts/{source['id']}",
                                   headers=auth_headers)).json()
    assert redirected["id"] == target["id"] and redirected["redirected_from"] == source["id"]
    assert (await client.post(url, headers=auth_headers, params={"confirm": "true"})).status_code == 404

    export = await client.get(f"/api/v1/orgs/{org.id}/contacts/{target['id']}/export",
                              headers=auth_headers)
    assert export.status_code == 200
    payload = export.json()
    assert payload["contact"]["merged_from"] == [source["id"]]
    assert payload["references"]["tickets"] == [{"id": ticket["id"], "project_id": project.id}]

    csv_resp = await client.get(f"/api/v1/orgs/{org.id}/contacts", headers=auth_headers,
                                params={"format": "csv"})
    assert csv_resp.status_code == 200 and "Tolulope A." in csv_resp.text

    refs = await client.get(f"/api/v1/orgs/{org.id}/contacts/{target['id']}/references",
                            headers=auth_headers)
    assert [c["meeting_id"] for c in refs.json()["conversations"]] == [meeting.id]

    gone = await client.delete(f"/api/v1/orgs/{org.id}/contacts/{target['id']}", headers=auth_headers)
    assert gone.status_code == 200, gone.text
    assert gone.json()["participants_reset"] == 1 and gone.json()["tickets_unassigned"] == 1
    db_session.expunge_all()
    part = await db_session.get(MeetingParticipant, p.id)
    assert part.contact_id is None and part.resolution_method == "unresolved"
    assert (await db_session.get(Ticket, ticket["id"])).assigned_contact_id is None
    assert await db_session.get(ExternalContact, source["id"]) is None
    event = (await db_session.execute(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket["id"],
                                  TicketEvent.action == "unassigned")
    )).scalars().one()
    assert "Tolu" not in (event.before_value or "") and "contact_removed" in event.before_value


async def test_merge_email_conflict(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    org = await _setup(db_session, test_user)
    a = await _create(client, auth_headers, org.id, display_name="A", email="a@v.example")
    b = await _create(client, auth_headers, org.id, display_name="B", email="b@v.example")
    resp = await client.post(f"/api/v1/orgs/{org.id}/contacts/{a['id']}/merge-from/{b['id']}",
                             headers=auth_headers, params={"confirm": "true"})
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "email_conflict"


# ── Linking by verified email ──


async def test_link_on_verified_email_moves_open_tickets(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    from sqlalchemy import update

    from sessionfs.server.db.models import Project, User

    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    # A restricted project the new member can't read keeps its ticket.
    hidden = await _project(db_session, test_user, org=org)
    await db_session.execute(update(Project).where(Project.id == hidden.id).values(org_id=None))
    await db_session.commit()
    email = f"new-{uuid.uuid4().hex[:6]}@vendor.example"
    contact = await _create(client, auth_headers, org.id, email=email)
    t_open = (await client.post(f"/api/v1/projects/{project.id}/tickets", headers=auth_headers,
                                json={"title": "o", "assigned_contact_id": contact["id"]})).json()
    done = Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title="d",
                  status="done", assigned_contact_id=contact["id"], created_by_user_id=test_user.id)
    db_session.add(done)
    await db_session.commit()

    newbie = await _user(db_session, "Newbie", email=email)
    # Not yet a member → nothing links.
    assert await link_contacts_for_user(db_session, newbie) == []
    await _member(db_session, org, newbie)
    # Unverified email → nothing links.
    await db_session.execute(update(User).where(User.id == newbie.id).values(email_verified=False))
    await db_session.commit()
    await db_session.refresh(newbie)
    assert await link_contacts_for_user(db_session, newbie) == []
    await db_session.execute(update(User).where(User.id == newbie.id).values(email_verified=True))
    await db_session.commit()
    await db_session.refresh(newbie)

    assert await link_contacts_for_user(db_session, newbie) == [contact["id"]]
    await db_session.commit()
    db_session.expunge_all()
    moved = await db_session.get(Ticket, t_open["id"])
    assert moved.assigned_user_id == newbie.id and moved.assigned_contact_id is None
    assert (await db_session.get(Ticket, done.id)).assigned_contact_id == contact["id"]
    row = await db_session.get(ExternalContact, contact["id"])
    assert row.linked_user_id == newbie.id
    # Linking is once: a second pass is a no-op.
    assert await link_contacts_for_user(db_session, newbie) == []

    # Resolution: a linked contact resolves to the member.
    meeting = await _meeting_row(db_session, project=project, org_id=org.id, created_at=_now())
    resolved = await resolve_participants(db_session, meeting, [{"name": "Tolulope Adeyemi"}], auth=None)
    assert resolved[0]["user_id"] == newbie.id and resolved[0]["resolution_method"] == "exact_email"


async def test_invite_accept_links_contact(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    from sessionfs.server.db.models import OrgInvite

    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    email = f"inv-{uuid.uuid4().hex[:6]}@vendor.example"
    contact = await _create(client, auth_headers, org.id, email=email)
    ticket = (await client.post(f"/api/v1/projects/{project.id}/tickets", headers=auth_headers,
                                json={"title": "o", "assigned_contact_id": contact["id"]})).json()
    invitee = await _user(db_session, "Invitee", email=email)
    invite = OrgInvite(id=f"inv_{uuid.uuid4().hex[:12]}", org_id=org.id, email=email,
                       role="member", invited_by=test_user.id,
                       expires_at=_now() + timedelta(days=3))
    db_session.add(invite)
    await db_session.commit()
    resp = await client.post(f"/api/v1/org/invite/{invite.id}/accept",
                             headers=await _user_headers(db_session, invitee))
    assert resp.status_code == 200, resp.text
    db_session.expunge_all()
    assert (await db_session.get(Ticket, ticket["id"])).assigned_user_id == invitee.id


# ── Suggestions + retention ──


async def test_suggestions_apply_and_dismiss(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    m1 = await _meeting_row(db_session, project=project, org_id=org.id, created_at=_now())
    m2 = await _meeting_row(db_session, project=project, org_id=org.id, created_at=_now())
    p1 = await _participant(db_session, m1.id, "Tolu")
    p2 = await _participant(db_session, m2.id, "tolu")
    await _participant(db_session, m2.id, "Kemi")
    contact = await _create(client, auth_headers, org.id)

    url = f"/api/v1/orgs/{org.id}/contacts/suggestions"
    groups = {g["name_norm"]: g for g in (await client.get(url, headers=auth_headers)).json()["groups"]}
    assert groups["tolu"]["mention_count"] == 2
    # A hint only ("Tolu" → "Tolulope …"), never an automatic resolution.
    assert [c["hint"] for c in groups["tolu"]["candidates"]] == ["name starts the same"]
    assert {s["meeting_id"] for s in groups["tolu"]["sources"]} == {m1.id, m2.id}

    applied = await client.post(f"{url}/apply", headers=auth_headers,
                                json={"name": "Tolu", "contact_id": contact["id"]})
    assert applied.json() == {"applied": 2, "skipped": 0}
    db_session.expunge_all()
    for pid in (p1.id, p2.id):
        assert (await db_session.get(MeetingParticipant, pid)).contact_id == contact["id"]

    assert (await client.post(f"{url}/dismiss", headers=auth_headers,
                              json={"name": "Kemi"})).status_code == 204
    left = (await client.get(url, headers=auth_headers)).json()["groups"]
    assert left == []


async def test_retention_sweep(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    from tests.server.integration.test_skill_marketplace import _platform_admin_headers

    _, admin_headers = await _platform_admin_headers(db_session)
    org = await _setup(db_session, test_user)
    settings_url = f"/api/v1/orgs/{org.id}/settings"
    assert (await client.put(settings_url, headers=auth_headers,
                             json={"contact_retention_days": 5})).status_code == 400
    ok = await client.put(settings_url, headers=auth_headers, json={"contact_retention_days": 30})
    assert ok.json()["contact_retention_days"] == 30
    unset_org = await _org(db_session)

    old = _now() - timedelta(days=40)

    def _row(org_id, status="active", created=old):
        return ExternalContact(id="ec_" + uuid.uuid4().hex[:12], org_id=org_id, display_name="x",
                               name_norm="x", status=status, created_at=created, updated_at=created)

    archived_old, unref_old, fresh, referenced, other = (
        _row(org.id, "archived"), _row(org.id), _row(org.id, created=_now()), _row(org.id),
        _row(unset_org.id, "archived"),
    )
    db_session.add_all([archived_old, unref_old, fresh, referenced, other])
    await db_session.commit()
    project = await _project(db_session, test_user, org=org)
    db_session.add(Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title="t",
                          assigned_contact_id=referenced.id, created_by_user_id=test_user.id))
    await db_session.commit()

    resp = await client.post("/api/v1/admin/sweeps/contact-retention", headers=admin_headers, json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["contacts_deleted"] == 2
    db_session.expunge_all()
    assert await db_session.get(ExternalContact, archived_old.id) is None
    assert await db_session.get(ExternalContact, unref_old.id) is None
    for keep in (fresh, referenced, other):
        assert await db_session.get(ExternalContact, keep.id) is not None


async def test_service_key_references_need_source_scopes(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    """contacts:read alone never returns meeting or ticket content through
    /references; each section needs its own source read scope."""
    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    contact = await _create(client, auth_headers, org.id, display_name="Tolu Ref")
    meeting = await _meeting_row(db_session, project=project, org_id=org.id, created_at=_now())
    await _participant(db_session, meeting.id, "Tolu Ref", contact_id=contact["id"],
                       resolution_method="contact")
    made = await client.post(f"/api/v1/projects/{project.id}/tickets", headers=auth_headers,
                             json={"title": "t", "assigned_contact_id": contact["id"]})
    assert made.status_code == 201, made.text
    url = f"/api/v1/orgs/{org.id}/contacts/{contact['id']}/references"

    only = await _service_key(db_session, owner=test_user, org_id=org.id, scopes=["contacts:read"])
    body = (await client.get(url, headers=only)).json()
    assert body["conversations"] == [] and body["action_items"] == [] and body["tickets"] == []

    full = await _service_key(db_session, owner=test_user, org_id=org.id,
                              scopes=["contacts:read", "meetings:read", "tickets:read"])
    body = (await client.get(url, headers=full)).json()
    assert [c["meeting_id"] for c in body["conversations"]] == [meeting.id]
    assert [t["id"] for t in body["tickets"]] == [made.json()["id"]]


async def test_email_conflict_is_generic_for_members(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
):
    """A plain member sees contact emails masked, so a duplicate email must not
    tell them which contact (or whether a member) holds it."""
    org = await _setup(db_session, test_user)
    member = await _user(db_session, "Member")
    await _member(db_session, org, member)
    member_h = await _user_headers(db_session, member)
    await _create(client, auth_headers, org.id, email="held@vendor.example")

    for email in ("held@vendor.example", test_user.email):
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/contacts", headers=member_h,
            json={"display_name": "Guess", "email": email},
        )
        assert resp.status_code == 409, resp.text
        details = resp.json()["error"]["details"]
        assert details["error"] == "email_conflict"
        assert "contact_id" not in details
