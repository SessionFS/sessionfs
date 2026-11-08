"""External contacts: review fixes (tk_440e42a8a0914d78).

1. Contact emails go through the strictest-union DLP policy (only the
   email-address detector is skipped) on create and update.
2. A contact linked to an active member, or merged, is not an eligible ticket
   assignee, and eligibility re-reads the contact row under a lock inside the
   ticket write transaction.
3. The retention sweep locks and re-checks each candidate, deletes with a
   rowcount-1 guard and emits the same events as a manual delete.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.db.models import (
    ExternalContact,
    OrgAuditEvent,
    OrgMember,
    Ticket,
    TicketEvent,
)
from tests.server.integration.test_contacts_api import _create, _setup
from tests.server.integration.test_meetings_api import (
    _member,
    _now,
    _org,
    _project,
    _user,
)

GHP = "ghp_" + "A" * 36


def _err(resp) -> str:
    return resp.json()["error"]["details"]["error"]


# ── 1. Email DLP ──


async def test_contact_email_dlp_block_mode(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    block = json.dumps({"dlp": {"enabled": True, "mode": "block", "categories": ["secrets", "phi"]}})
    org = await _setup(db_session, test_user, settings=block)
    base = f"/api/v1/orgs/{org.id}/contacts"

    # The token pattern is case-sensitive: the raw value is scanned, not the
    # lowercased address.
    blocked = await client.post(base, headers=auth_headers,
                                json={"display_name": "Leaky", "email": f"{GHP}@vendor.example"})
    assert blocked.status_code == 422, blocked.text
    assert _err(blocked) == "dlp_blocked"
    assert (await db_session.execute(
        select(ExternalContact).where(ExternalContact.org_id == org.id)
    )).scalars().all() == []

    # The email-address detector is the one exemption: an address the PHI
    # "medical email" rule would flag in prose is still storable.
    ok = await _create(client, auth_headers, org.id, email="patientemail.tolu@clinic.example")
    assert ok["email"] == "patientemail.tolu@clinic.example"

    upd = await client.patch(f"{base}/{ok['id']}", headers=auth_headers,
                             json={"email": f"{GHP}@clinic.example"})
    assert upd.status_code == 422 and _err(upd) == "dlp_blocked"
    db_session.expunge_all()
    row = await db_session.get(ExternalContact, ok["id"])
    assert row.email == "patientemail.tolu@clinic.example"


async def test_contact_email_dlp_redact_mode_refuses(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    redact = json.dumps({"dlp": {"enabled": True, "mode": "redact", "categories": ["secrets"]}})
    org = await _setup(db_session, test_user, settings=redact)
    base = f"/api/v1/orgs/{org.id}/contacts"

    # A redacted value is no longer an address: refused, never stored.
    resp = await client.post(base, headers=auth_headers,
                             json={"display_name": "Leaky", "email": f"{GHP}@vendor.example"})
    assert resp.status_code == 422 and _err(resp) == "invalid_email"
    contact = await _create(client, auth_headers, org.id, email="fine@vendor.example")
    resp = await client.patch(f"{base}/{contact['id']}", headers=auth_headers,
                              json={"email": f"{GHP}@vendor.example"})
    assert resp.status_code == 422 and _err(resp) == "invalid_email"
    db_session.expunge_all()
    assert (await db_session.get(ExternalContact, contact["id"])).email == "fine@vendor.example"


# ── 2. Assignee eligibility ──


async def test_linked_or_merged_contact_not_assignable(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    base = f"/api/v1/projects/{project.id}/tickets"
    member = await _user(db_session, "Linked")
    await _member(db_session, org, member)
    contact = await _create(client, auth_headers, org.id)
    await db_session.execute(
        update(ExternalContact).where(ExternalContact.id == contact["id"])
        .values(linked_user_id=member.id, linked_at=_now())
    )
    await db_session.commit()

    made = await client.post(base, headers=auth_headers,
                             json={"title": "x", "assigned_contact_id": contact["id"]})
    assert made.status_code == 422 and _err(made) == "assignee_not_eligible"
    tid = (await client.post(base, headers=auth_headers, json={"title": "y"})).json()["id"]
    upd = await client.put(f"{base}/{tid}", headers=auth_headers,
                           json={"assigned_contact_id": contact["id"]})
    assert upd.status_code == 422 and _err(upd) == "assignee_not_eligible"

    src = await _create(client, auth_headers, org.id, display_name="Dup Person")
    target = await _create(client, auth_headers, org.id, display_name="Real Person")
    merged = await client.post(
        f"/api/v1/orgs/{org.id}/contacts/{target['id']}/merge-from/{src['id']}",
        headers=auth_headers, params={"confirm": "true"})
    assert merged.status_code == 200, merged.text
    upd = await client.put(f"{base}/{tid}", headers=auth_headers,
                           json={"assigned_contact_id": src["id"]})
    assert upd.status_code == 422 and _err(upd) == "assignee_not_eligible"

    # Once the linked member leaves, the contact resolves as itself again
    # (design §4, same as promote drafting).
    await db_session.execute(sa_delete(OrgMember).where(
        OrgMember.org_id == org.id, OrgMember.user_id == member.id))
    await db_session.commit()
    upd = await client.put(f"{base}/{tid}", headers=auth_headers,
                           json={"assigned_contact_id": contact["id"]})
    assert upd.status_code == 200, upd.text
    assert upd.json()["assigned_contact_id"] == contact["id"]


async def test_assignee_contact_revalidated_in_transaction(
    db_session: AsyncSession, db_engine, test_user
) -> None:
    """Eligibility re-reads the row (FOR UPDATE on PostgreSQL) instead of
    trusting a copy already in the session: a link, archive or delete
    committed by another transaction is seen."""
    from sessionfs.server.services.ticket_writes import resolve_assigned_contact_id

    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    member = await _user(db_session, "Late")
    await _member(db_session, org, member)
    rows = [
        ExternalContact(id="ec_" + uuid.uuid4().hex[:12], org_id=org.id, display_name=n,
                        name_norm=n.lower(), created_at=_now(), updated_at=_now())
        for n in ("Linkme", "Archiveme", "Deleteme")
    ]
    db_session.add_all(rows)
    await db_session.commit()
    # Warm the identity map with the "active, unlinked" copies.
    for row in rows:
        assert await resolve_assigned_contact_id(db_session, project=project, value=row.id) == row.id
    await db_session.commit()

    other = async_sessionmaker(db_engine, expire_on_commit=False)
    async with other() as s2:
        await s2.execute(update(ExternalContact).where(ExternalContact.id == rows[0].id)
                         .values(linked_user_id=member.id, linked_at=_now()))
        await s2.execute(update(ExternalContact).where(ExternalContact.id == rows[1].id)
                         .values(status="archived"))
        await s2.execute(sa_delete(ExternalContact).where(ExternalContact.id == rows[2].id))
        await s2.commit()

    for row in rows:
        try:
            await resolve_assigned_contact_id(db_session, project=project, value=row.id)
        except HTTPException as exc:
            assert exc.status_code == 422
            assert exc.detail["error"] == "assignee_not_eligible"
        else:
            raise AssertionError(f"{row.display_name} was still assignable")


# ── 3. Erasure guard + retention sweep ──


async def test_erase_contact_guarded_delete(
    db_session: AsyncSession, db_engine, test_user
) -> None:
    from sessionfs.server.services.contacts import ContactChanged, erase_contact

    org = await _setup(db_session, test_user)
    row = ExternalContact(id="ec_" + uuid.uuid4().hex[:12], org_id=org.id, display_name="G",
                          name_norm="g", status="archived", created_at=_now(), updated_at=_now())
    db_session.add(row)
    await db_session.commit()
    row_id = row.id
    other = async_sessionmaker(db_engine, expire_on_commit=False)
    async with other() as s2:
        await s2.execute(update(ExternalContact).where(ExternalContact.id == row_id)
                         .values(status="active"))
        await s2.commit()
    try:
        await erase_contact(db_session, row, actor_user_id=None)
    except ContactChanged:
        await db_session.rollback()
    else:
        raise AssertionError("stale erasure was not refused")
    db_session.expunge_all()
    assert await db_session.get(ExternalContact, row_id) is not None


async def test_retention_sweep_relocks_rechecks_and_audits(
    client: AsyncClient, db_session: AsyncSession, db_engine, test_user, monkeypatch,
) -> None:
    from sessionfs.server.services import contacts as contacts_svc
    from tests.server.integration.test_skill_marketplace import _platform_admin_headers

    _, admin_headers = await _platform_admin_headers(db_session)
    org = await _org(db_session, settings=json.dumps({"general": {"contact_retention_days": 30}}))
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)
    old = _now() - timedelta(days=40)

    def _row(status: str) -> ExternalContact:
        return ExternalContact(id="ec_" + uuid.uuid4().hex[:12], org_id=org.id,
                               display_name="Secret Name", name_norm="secret name",
                               status=status, created_at=old, updated_at=old)

    doomed, reactivated, gets_ticket = _row("archived"), _row("archived"), _row("active")
    db_session.add_all([doomed, reactivated, gets_ticket])
    await db_session.commit()
    ticket = Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title="t",
                    assigned_contact_id=doomed.id, created_by_user_id=test_user.id)
    # Referenced, so only the "archived + old" rule could take it; the
    # concurrent reactivation below must save it.
    db_session.add(Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title="r",
                          assigned_contact_id=reactivated.id, created_by_user_id=test_user.id))
    db_session.add(ticket)
    await db_session.commit()

    other = async_sessionmaker(db_engine, expire_on_commit=False)
    real_lock = contacts_svc.lock_contact
    late_ticket = f"tk_{uuid.uuid4().hex[:16]}"

    async def racing_lock(db, contact_id):
        # Writes that commit after the sweep read its page but before it
        # takes the row lock.
        async with other() as s2:
            if contact_id == reactivated.id:
                await s2.execute(update(ExternalContact).where(ExternalContact.id == contact_id)
                                 .values(status="active", updated_at=_now()))
            elif contact_id == gets_ticket.id:
                s2.add(Ticket(id=late_ticket, project_id=project.id, title="late",
                              assigned_contact_id=contact_id, created_by_user_id=test_user.id))
            await s2.commit()
        return await real_lock(db, contact_id)

    monkeypatch.setattr(contacts_svc, "lock_contact", racing_lock)
    resp = await client.post("/api/v1/admin/sweeps/contact-retention", headers=admin_headers, json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["contacts_deleted"] == 1

    db_session.expunge_all()
    assert await db_session.get(ExternalContact, doomed.id) is None
    assert await db_session.get(ExternalContact, reactivated.id) is not None
    assert await db_session.get(ExternalContact, gets_ticket.id) is not None
    assert (await db_session.get(Ticket, late_ticket)).assigned_contact_id == gets_ticket.id

    # Same events as a manual delete: an unassign event that never names the
    # contact, and a contact_deleted org audit row.
    assert (await db_session.get(Ticket, ticket.id)).assigned_contact_id is None
    events = (await db_session.execute(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id,
                                  TicketEvent.field == "assigned_contact_id")
    )).scalars().all()
    assert [e.action for e in events] == ["unassigned"]
    stored = json.dumps([e.before_value for e in events]) + json.dumps([e.after_value for e in events])
    assert "contact_removed" in stored and "Secret Name" not in stored
    audits = (await db_session.execute(
        select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id,
                                    OrgAuditEvent.event_type == "contact_deleted")
    )).scalars().all()
    assert [a.target_id for a in audits] == [doomed.id]


# ── Round 2: lock order contact rows → tickets, guarded writes ──


def _ticket(project, test_user, contact_id: str | None, **kw) -> Ticket:
    return Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id,
                  title=kw.pop("title", "t"), assigned_contact_id=contact_id,
                  created_by_user_id=test_user.id, **kw)


def _contact(org, name: str, **kw) -> ExternalContact:
    return ExternalContact(id="ec_" + uuid.uuid4().hex[:12], org_id=org.id, display_name=name,
                           name_norm=name.lower(), created_at=_now(), updated_at=_now(), **kw)


async def test_link_refuses_source_merged_concurrently(
    db_session: AsyncSession, db_engine, test_user, monkeypatch
) -> None:
    """A merge that commits after the candidate list was read (before the
    contact lock, or between the re-check and the UPDATE) is never linked."""
    from sessionfs.server.services import contacts as contacts_svc

    other = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _merge_in_other(source_id: str, target_id: str) -> None:
        async with other() as s2:
            await s2.execute(update(ExternalContact).where(ExternalContact.id == source_id)
                             .values(status="merged", merged_into_id=target_id))
            await s2.commit()

    # (a) merged before the lock: the locked re-read sees it.
    org = await _setup(db_session, test_user)
    email = f"m-{uuid.uuid4().hex[:6]}@vendor.example"
    source, target = _contact(org, "Src", email=email), _contact(org, "Tgt")
    db_session.add_all([source, target])
    await db_session.commit()
    user = await _user(db_session, "Joiner", email=email)
    await _member(db_session, org, user)
    real_lock = contacts_svc.lock_contact

    async def merging_lock(db, contact_id):
        if contact_id == source.id:
            await _merge_in_other(source.id, target.id)
        return await real_lock(db, contact_id)

    monkeypatch.setattr(contacts_svc, "lock_contact", merging_lock)
    assert await contacts_svc.link_contacts_for_user(db_session, user) == []
    await db_session.commit()
    monkeypatch.setattr(contacts_svc, "lock_contact", real_lock)

    # (b) merged after the re-check: the guarded UPDATE refuses it.
    org2 = await _setup(db_session, test_user)
    source2, target2 = _contact(org2, "Src", email=email), _contact(org2, "Tgt")
    db_session.add_all([source2, target2])
    await db_session.commit()
    await _member(db_session, org2, user)
    real_role = contacts_svc.org_role

    async def merging_role(db, org_id, user_id):
        role = await real_role(db, org_id, user_id)
        if org_id == org2.id:
            await _merge_in_other(source2.id, target2.id)
        return role

    monkeypatch.setattr(contacts_svc, "org_role", merging_role)
    assert await contacts_svc.link_contacts_for_user(db_session, user, org_id=org2.id) == []
    await db_session.commit()

    db_session.expunge_all()
    for cid in (source.id, source2.id):
        row = await db_session.get(ExternalContact, cid)
        assert row.status == "merged" and row.linked_user_id is None


async def test_link_and_delete_never_overwrite_concurrent_reassignment(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, db_engine, test_user,
    monkeypatch,
) -> None:
    """Link and erasure lock the contact's tickets and change one only while
    it is still assigned to that contact."""
    from sessionfs.server.services import contacts as contacts_svc

    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    email = f"r-{uuid.uuid4().hex[:6]}@vendor.example"
    linkme, eraseme, elsewhere = (_contact(org, "Linkme", email=email), _contact(org, "Eraseme"),
                                  _contact(org, "Elsewhere"))
    db_session.add_all([linkme, eraseme, elsewhere])
    await db_session.commit()
    raced_link, kept_link = _ticket(project, test_user, linkme.id), _ticket(project, test_user, linkme.id)
    raced_erase, kept_erase = (_ticket(project, test_user, eraseme.id),
                               _ticket(project, test_user, eraseme.id))
    db_session.add_all([raced_link, kept_link, raced_erase, kept_erase])
    await db_session.commit()
    user = await _user(db_session, "Joiner", email=email)
    await _member(db_session, org, user)

    other = async_sessionmaker(db_engine, expire_on_commit=False)
    real = contacts_svc.lock_contact_tickets
    raced = {linkme.id: raced_link.id, eraseme.id: raced_erase.id}

    async def racing_tickets(db, contact_id=None, **kw):
        tickets = await real(db, contact_id, **kw)
        # Another write reassigns one ticket after our read (the window a
        # missing lock would leave open); our copy is now stale.
        if contact_id in raced:
            async with other() as s2:
                await s2.execute(update(Ticket).where(Ticket.id == raced[contact_id])
                                 .values(assigned_contact_id=elsewhere.id))
                await s2.commit()
        return tickets

    monkeypatch.setattr(contacts_svc, "lock_contact_tickets", racing_tickets)
    assert await contacts_svc.link_contacts_for_user(db_session, user) == [linkme.id]
    await db_session.commit()
    resp = await client.delete(f"/api/v1/orgs/{org.id}/contacts/{eraseme.id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tickets_unassigned"] == 1

    db_session.expunge_all()
    for tid in (raced_link.id, raced_erase.id):
        row = await db_session.get(Ticket, tid)
        assert row.assigned_contact_id == elsewhere.id and row.assigned_user_id is None
        assert (await db_session.execute(
            select(TicketEvent).where(TicketEvent.ticket_id == tid))).scalars().all() == []
    moved = await db_session.get(Ticket, kept_link.id)
    assert moved.assigned_user_id == user.id and moved.assigned_contact_id is None
    assert (await db_session.get(Ticket, kept_erase.id)).assigned_contact_id is None


async def test_merge_into_linked_contact_moves_tickets_to_member(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, db_engine, test_user,
    monkeypatch,
) -> None:
    """Merging into a contact linked to an active member moves the source's
    open tickets to the member (never re-creating a linked-contact
    assignment), repoints the rest to the target, and leaves a ticket
    another write reassigned alone."""
    from sessionfs.server.services import contacts as contacts_svc

    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    member = await _user(db_session, "Member")
    await _member(db_session, org, member)
    target = _contact(org, "Target", linked_user_id=member.id, linked_at=_now())
    source, elsewhere = _contact(org, "Source"), _contact(org, "Elsewhere")
    db_session.add_all([target, source, elsewhere])
    await db_session.commit()
    t_open = _ticket(project, test_user, source.id)
    t_done = _ticket(project, test_user, source.id, status="done")
    t_raced = _ticket(project, test_user, source.id)
    db_session.add_all([t_open, t_done, t_raced])
    await db_session.commit()

    other = async_sessionmaker(db_engine, expire_on_commit=False)
    real = contacts_svc.lock_contact_tickets

    async def racing_tickets(db, contact_id=None, **kw):
        tickets = await real(db, contact_id, **kw)
        async with other() as s2:
            await s2.execute(update(Ticket).where(Ticket.id == t_raced.id)
                             .values(assigned_contact_id=elsewhere.id))
            await s2.commit()
        return tickets

    monkeypatch.setattr(contacts_svc, "lock_contact_tickets", racing_tickets)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/contacts/{target.id}/merge-from/{source.id}",
        headers=auth_headers, params={"confirm": "true"})
    assert resp.status_code == 200, resp.text

    db_session.expunge_all()
    moved = await db_session.get(Ticket, t_open.id)
    assert moved.assigned_user_id == member.id and moved.assigned_contact_id is None
    events = (await db_session.execute(
        select(TicketEvent).where(TicketEvent.ticket_id == t_open.id))).scalars().all()
    assert [(e.action, e.field) for e in events] == [("assigned", "assigned_user_id")]
    assert (await db_session.get(Ticket, t_done.id)).assigned_contact_id == target.id
    raced_row = await db_session.get(Ticket, t_raced.id)
    assert raced_row.assigned_contact_id == elsewhere.id and raced_row.assigned_user_id is None
    assert (await db_session.get(ExternalContact, source.id)).status == "merged"


async def test_merge_inherited_link_moves_tickets_to_member(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    """The target inherits the source's link; the same rule applies."""
    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    member = await _user(db_session, "Member")
    await _member(db_session, org, member)
    target = _contact(org, "Target")
    source = _contact(org, "Source", linked_user_id=member.id, linked_at=_now())
    db_session.add_all([target, source])
    await db_session.commit()
    t_open = _ticket(project, test_user, source.id)
    db_session.add(t_open)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/contacts/{target.id}/merge-from/{source.id}",
        headers=auth_headers, params={"confirm": "true"})
    assert resp.status_code == 200, resp.text
    db_session.expunge_all()
    assert (await db_session.get(ExternalContact, target.id)).linked_user_id == member.id
    moved = await db_session.get(Ticket, t_open.id)
    assert moved.assigned_user_id == member.id and moved.assigned_contact_id is None


async def test_merge_moves_target_own_open_tickets_to_inherited_member(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    """An unlinked target that inherits a member link never stays an
    assignee: its own open tickets move to the member too, one event each."""
    from sessionfs.server.db.models import TicketEvent

    org = await _setup(db_session, test_user)
    project = await _project(db_session, test_user, org=org)
    member = await _user(db_session, "Member")
    await _member(db_session, org, member)
    target = _contact(org, "Target")
    source = _contact(org, "Source", linked_user_id=member.id, linked_at=_now())
    db_session.add_all([target, source])
    await db_session.commit()
    t_target = _ticket(project, test_user, target.id)
    t_source = _ticket(project, test_user, source.id)
    db_session.add_all([t_target, t_source])
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/contacts/{target.id}/merge-from/{source.id}",
        headers=auth_headers, params={"confirm": "true"})
    assert resp.status_code == 200, resp.text
    db_session.expunge_all()
    for tid in (t_target.id, t_source.id):
        moved = await db_session.get(Ticket, tid)
        assert moved.assigned_user_id == member.id and moved.assigned_contact_id is None
        events = (await db_session.execute(
            select(TicketEvent).where(TicketEvent.ticket_id == tid,
                                      TicketEvent.field == "assigned_user_id")
        )).scalars().all()
        assert len(events) == 1
